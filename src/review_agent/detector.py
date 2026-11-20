"""탐지 단계: 관점별 프롬프트 실행, JSON 파싱·스키마 검증."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from . import prompts
from .config import ReviewConfig
from .jsonio import JsonExtractionError, extract_json_array
from .llm import LLMError, LLMRunner
from .models import Finding, SchemaError, parse_finding
from .prompts import Angle

if TYPE_CHECKING:  # pragma: no cover
    from .context import ReviewContext

logger = logging.getLogger(__name__)


@dataclass
class DetectorRun:
    """탐지기 한 번의 실행 결과."""

    angle: str
    findings: list[Finding] = field(default_factory=list)
    schema_errors: list[str] = field(default_factory=list)
    error: str = ""
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class DetectionResult:
    findings: list[Finding] = field(default_factory=list)
    runs: list[DetectorRun] = field(default_factory=list)

    @property
    def failed_angles(self) -> list[str]:
        return [run.angle for run in self.runs if not run.ok]


class Detector:
    """N개의 관점을 병렬로 실행한다."""

    def __init__(self, runner: LLMRunner, config: ReviewConfig) -> None:
        self.runner = runner
        self.config = config

    def detect(self, ctx: "ReviewContext", max_nits: Optional[int] = None) -> DetectionResult:
        angles = self.config.resolved_angles()
        if not angles:
            return DetectionResult()

        nits = self.config.max_nits if max_nits is None else max_nits
        workers = min(len(angles), self.config.max_parallel)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            runs = list(pool.map(lambda angle: self._run_angle(ctx, angle, nits), angles))

        findings: list[Finding] = []
        for run in runs:
            findings.extend(run.findings)
        return DetectionResult(findings=findings, runs=runs)

    def _build_prompt(self, ctx: "ReviewContext", angle: Angle, max_nits: int) -> str:
        return prompts.build_detect_prompt(
            angle=angle,
            diff_range=ctx.diff_range,
            pr_title=ctx.pr.title,
            pr_description=ctx.pr.description,
            agents_md=ctx.docs.agents_md,
            review_md=ctx.docs.review_md,
            agents_name=ctx.docs.agents_source or "AGENTS.md",
            security_md=ctx.docs.security_md,
            max_nits=max_nits,
            language=ctx.config.comment_language,
            intensity=ctx.config.intensity,
            round_number=ctx.round,
            previous_findings=ctx.previous_findings,
        )

    def _run_angle(self, ctx: "ReviewContext", angle: Angle, max_nits: int) -> DetectorRun:
        run = DetectorRun(angle=angle.name)
        prompt = self._build_prompt(ctx, angle, max_nits)
        attempts_allowed = 1 + max(0, self.config.llm.parse_retries)

        for attempt in range(1, attempts_allowed + 1):
            run.attempts = attempt
            try:
                raw = self.runner.run(prompt, ctx.repo_path)
            except LLMError as exc:
                run.error = str(exc)
                logger.warning("탐지 실패 [%s]: %s", angle.name, exc)
                return run

            try:
                payload = extract_json_array(raw)
            except JsonExtractionError as exc:
                if attempt < attempts_allowed:
                    logger.info("탐지 출력 파싱 실패, 재시도 [%s]: %s", angle.name, exc)
                    prompt = prompts.build_repair_prompt(prompt, "JSON 배열")
                    continue
                run.error = f"JSON 파싱 실패: {exc}"
                logger.warning("탐지 출력 파싱 실패 [%s]", angle.name)
                return run

            run.findings, run.schema_errors = _parse_items(payload, angle.name)
            return run

        return run


def _parse_items(payload: list, angle: str) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    errors: list[str] = []
    for index, item in enumerate(payload):
        try:
            findings.append(parse_finding(item, angle=angle))
        except SchemaError as exc:
            errors.append(f"[{angle}] {index}번 항목 제거: {exc}")
            logger.info("스키마 검증 실패로 제거 [%s] %s", angle, exc)
    return findings, errors
