"""검증 단계: finding마다 확인 항목이 다른 검증자 M명을 병렬 실행해 투표를 모은다."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Sequence

from . import prompts
from .config import ReviewConfig
from .jsonio import JsonExtractionError, extract_json_object
from .llm import LLMError, LLMRunner
from .models import Finding, Verdict, VoteResult
from .prompts import Check

if TYPE_CHECKING:  # pragma: no cover
    from .context import ReviewContext

logger = logging.getLogger(__name__)

VERDICT_REAL = "real"
VERDICT_NOT_REAL = "not_real"


def normalize_verdict(value: object) -> str:
    """real 외의 값, 빈 값, 알 수 없는 값은 모두 not_real로 본다 (노이즈를 줄이는 쪽으로 닫는다)."""
    token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if token in ("real", "true", "valid", "confirmed"):
        return VERDICT_REAL
    return VERDICT_NOT_REAL


class Verifier:
    """검증자는 탐지기의 추론 과정을 받지 않고 finding 하나와 레포만 받는다."""

    def __init__(self, runner: LLMRunner, config: ReviewConfig) -> None:
        self.runner = runner
        self.config = config

    def _checks_for(self, finding: Finding) -> list[Check]:
        """finding 하나를 검증할 확인 항목.

        보안 finding은 보안 체크를 받는다. 결정적 도구가 낸 finding은 검증하지 않는다.
        """
        if finding.deterministic:
            return []
        return prompts.resolve_checks(
            finding.category, self.config.verifiers, self.config.security_checks
        )

    def verify(
        self, ctx: "ReviewContext", findings: Sequence[Finding]
    ) -> list[VoteResult]:
        jobs = [
            (finding, check) for finding in findings for check in self._checks_for(finding)
        ]
        if not jobs:
            return [VoteResult(finding=f, verdicts=[]) for f in findings]

        workers = min(len(jobs), self.config.max_parallel)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            verdicts = list(
                pool.map(lambda job: (job[0].id, self._run_check(ctx, job[0], job[1])), jobs)
            )

        by_finding: dict[str, list[Verdict]] = {finding.id: [] for finding in findings}
        for finding_id, verdict in verdicts:
            by_finding.setdefault(finding_id, []).append(verdict)

        return [
            VoteResult(finding=finding, verdicts=by_finding.get(finding.id, []))
            for finding in findings
        ]

    def _run_check(self, ctx: "ReviewContext", finding: Finding, check: Check) -> Verdict:
        prompt = prompts.build_verify_prompt(
            check=check,
            finding=finding,
            diff_range=ctx.diff_range,
            agents_md=ctx.docs.agents_md,
            review_md=ctx.docs.review_md,
            agents_name=ctx.docs.agents_source or "AGENTS.md",
            security_md=ctx.docs.security_md,
            language=ctx.config.comment_language,
        )
        attempts_allowed = 1 + max(0, self.config.llm.parse_retries)

        for attempt in range(1, attempts_allowed + 1):
            try:
                raw = self.runner.run(prompt, ctx.repo_path)
            except LLMError as exc:
                logger.warning("검증 실패 [%s/%s]: %s", finding.id, check.name, exc)
                return Verdict(check=check.name, verdict=VERDICT_NOT_REAL, reason=f"검증 실행 실패: {exc}")

            try:
                payload = extract_json_object(raw)
            except JsonExtractionError:
                if attempt < attempts_allowed:
                    prompt = prompts.build_repair_prompt(prompt, "JSON 객체")
                    continue
                logger.warning("검증 출력 파싱 실패 [%s/%s]", finding.id, check.name)
                return Verdict(
                    check=check.name,
                    verdict=VERDICT_NOT_REAL,
                    reason="판정 출력을 JSON으로 읽지 못했습니다",
                )

            return Verdict(
                check=check.name,
                verdict=normalize_verdict(payload.get("verdict")),
                reason=str(payload.get("reason") or "").strip(),
                checked=str(payload.get("checked") or "").strip(),
            )

        return Verdict(check=check.name, verdict=VERDICT_NOT_REAL, reason="판정 없음")
