"""게시 단계: 요약/코멘트 렌더링, --dry-run 출력, GitHub PR 게시.

같은 코드 위치에 서로 다른 지적이 남아 있으면(각각 독립 검증을 통과한 것들이다)
**하나의 인라인 코멘트에 항목으로 나란히** 싣는다. 코멘트 수는 줄이고 정보는 줄이지 않는다.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, Sequence, TextIO

from .merger import DEFAULT_LINE_TOLERANCE, same_location
from .models import SECURITY_CATEGORY, Finding

logger = logging.getLogger(__name__)

SEVERITY_LABELS: dict[str, str] = {
    "important": "Important",
    "nit": "Nit",
    "pre_existing": "Pre-existing",
}

MARKER_PREFIX = "pr-review-agent"

# 한 코멘트에 보안 지적과 로직 지적이 섞일 수 있다(group_by_spot이 같은 위치를 묶는다).
# 읽는 사람이 어느 리뷰어의 지적인지 바로 알 수 있게 접두를 붙인다.
SECURITY_PREFIX = "[보안] "

ESCALATION_TEXT = (
    "라운드 상한에 도달했는데도 important 지적이 남아 있습니다. "
    "개별 코멘트 대신 접근 방식을 다시 검토해 보시기를 권합니다."
)


# ---- 묶기 ----


def group_by_spot(
    findings: Sequence[Finding],
    tolerance: int = DEFAULT_LINE_TOLERANCE,
    enabled: bool = True,
) -> list[list[Finding]]:
    """같은 파일·근접 라인의 finding을 한 코멘트 단위로 묶는다.

    입력 순서(심각도 정렬 결과)를 유지한다. 각 묶음의 첫 finding이 기준이다.
    `enabled=False`면 finding마다 따로 묶어 이전 동작과 같아진다.
    """
    groups: list[list[Finding]] = []
    for finding in findings:
        if not enabled:
            groups.append([finding])
            continue
        target = next(
            (group for group in groups if same_location(group[0], finding, tolerance)),
            None,
        )
        if target is None:
            groups.append([finding])
        else:
            target.append(finding)
    return groups


# ---- 렌더링 ----


def security_findings(findings: Sequence[Finding]) -> list[Finding]:
    return [f for f in findings if f.category == SECURITY_CATEGORY]


def render_counts_line(findings: Sequence[Finding], empty_text: str = "수정할 사항 없음") -> str:
    """요약 첫 줄. 개수로 시작하고, 지적이 없으면 빈 문구만 쓴다.

    보안 건수는 새 severity 축을 만들지 않고 괄호로 덧붙인다 (등급과 섞으면 혼란스럽다).
    """
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    parts = [
        f"{SEVERITY_LABELS[severity]} {counts[severity]}"
        for severity in ("important", "nit", "pre_existing")
        if counts.get(severity)
    ]
    if not parts:
        return empty_text
    line = ", ".join(parts)
    security = len(security_findings(findings))
    if security:
        line += f" (보안 {security}건 포함)"
    return line


def marker(findings: Sequence[Finding], round_number: int) -> str:
    ids = ",".join(f.id for f in findings if f.id)
    return f"<!-- {MARKER_PREFIX} ids={ids} round={round_number} -->"


def _render_item(finding: Finding, heading: Optional[str] = None) -> list[str]:
    label = SEVERITY_LABELS.get(finding.severity, finding.severity)
    angles = ", ".join(finding.detected_by) if finding.detected_by else "-"
    prefix = SECURITY_PREFIX if finding.category == SECURITY_CATEGORY else ""

    lines: list[str] = []
    if heading:
        lines.append(f"### {heading}")
    lines.append(f"{prefix}**{label}** · `{finding.category}` · 탐지 관점: {angles}")
    lines.append("")
    if finding.severity == "pre_existing":
        lines.append("> 이 PR이 만든 문제는 아닙니다. 참고용입니다.")
        lines.append("")
    lines.append(finding.claim)
    lines.append("")
    lines.append("근거:")
    lines.append("```")
    lines.append(finding.evidence.strip())
    lines.append("```")
    if finding.suggested_fix:
        lines.append("")
        lines.append(f"제안: {finding.suggested_fix}")
    if finding.alt_claims:
        lines.append("")
        lines.append("<details><summary>같은 결함을 다르게 서술한 내용</summary>")
        lines.append("")
        for claim in finding.alt_claims:
            lines.append(f"- {claim}")
        lines.append("")
        lines.append("</details>")
    return lines


def render_comment(findings: Sequence[Finding], round_number: int = 1) -> str:
    """인라인 코멘트 본문. 묶음이면 항목을 나란히 싣는다."""
    if not findings:
        return ""

    if len(findings) == 1:
        lines = _render_item(findings[0])
    else:
        lines = [f"이 위치에 지적 {len(findings)}건이 있습니다.", ""]
        for index, finding in enumerate(findings, start=1):
            location = _format_location(finding)
            lines.extend(_render_item(finding, heading=f"{index}. `{location}`"))
            if index < len(findings):
                lines.append("")

    lines.append("")
    lines.append(marker(findings, round_number))
    return "\n".join(lines)


def _format_location(finding: Finding) -> str:
    if finding.line_start == finding.line_end:
        return f"{finding.file}:{finding.line_start}"
    return f"{finding.file}:{finding.line_start}-{finding.line_end}"


def render_finding_line(finding: Finding) -> str:
    label = SEVERITY_LABELS.get(finding.severity, finding.severity)
    return f"- **{label}** `{_format_location(finding)}` [{finding.category}] {finding.claim}"


def render_summary(
    findings: Sequence[Finding],
    *,
    round_number: int = 1,
    empty_text: str = "수정할 사항 없음",
    escalation: bool = False,
    overflow: Sequence[Finding] = (),
) -> str:
    """요약 코멘트 본문. 첫 줄은 항상 개수(또는 빈 문구)로 시작한다."""
    lines = [render_counts_line(findings, empty_text)]

    if escalation:
        lines.append("")
        lines.append(ESCALATION_TEXT)
        lines.append("")
        for finding in findings:
            lines.append(render_finding_line(finding))
    elif overflow:
        lines.append("")
        lines.append("아래 지적은 해당 라인이 diff에 없어 인라인으로 달지 못했습니다.")
        lines.append("")
        for finding in overflow:
            lines.append(render_finding_line(finding))

    # 보안 지적은 요약에서 한 번 더 모아 보여 준다. 인라인 코멘트에 섞여 있으면
    # 몇 건이 어디 있는지 한눈에 안 보인다. 에스컬레이션 요약은 이미 목록을 싣는다.
    if not escalation:
        security = security_findings(findings)
        if security:
            lines.append("")
            lines.append("### 보안")
            lines.append("")
            for finding in security:
                lines.append(render_finding_line(finding))

    lines.append("")
    lines.append(f"<!-- {MARKER_PREFIX} summary round={round_number} -->")
    return "\n".join(lines)


# ---- 게시 요청/결과 ----


@dataclass
class PublishRequest:
    findings: list[Finding]
    round_number: int = 1
    head_sha: str = ""
    # 인라인 코멘트를 달 수 있는 (파일 -> 라인 집합). PR 전체 diff 기준이어야 한다.
    inline_lines: Mapping[str, set[int]] = field(default_factory=dict)
    escalation: bool = False
    empty_text: str = "수정할 사항 없음"
    post_when_empty: bool = False
    group_comments: bool = True
    line_tolerance: int = DEFAULT_LINE_TOLERANCE

    def groups(self) -> list[list[Finding]]:
        return group_by_spot(self.findings, self.line_tolerance, self.group_comments)


@dataclass
class PublishOutcome:
    inline_posted: list[str] = field(default_factory=list)
    overflow: list[str] = field(default_factory=list)
    summary_posted: bool = False
    skipped: bool = False
    comment_count: int = 0
    errors: list[str] = field(default_factory=list)


class Publisher(Protocol):
    def publish(self, request: PublishRequest) -> PublishOutcome:
        ...


def _split_inline(
    groups: Sequence[Sequence[Finding]], inline_lines: Mapping[str, set[int]]
) -> tuple[list[list[Finding]], list[list[Finding]]]:
    """인라인으로 달 수 있는 묶음과 요약으로 넘길 묶음을 나눈다."""
    if not inline_lines:
        return [], [list(group) for group in groups]

    inline: list[list[Finding]] = []
    overflow: list[list[Finding]] = []
    for group in groups:
        available = inline_lines.get(group[0].file) or set()
        if _anchor_line(group, available) is not None:
            inline.append(list(group))
        else:
            overflow.append(list(group))
    return inline, overflow


def _anchor_line(group: Sequence[Finding], available: set[int]) -> Optional[int]:
    """묶음에서 diff에 포함된 가장 앞선 라인. 없으면 None."""
    candidates = [
        line
        for finding in group
        for line in range(finding.line_start, finding.line_end + 1)
        if line in available
    ]
    return min(candidates) if candidates else None


def _flatten(groups: Sequence[Sequence[Finding]]) -> list[Finding]:
    return [finding for group in groups for finding in group]


class DryRunPublisher:
    """게시하지 않고 stdout으로만 출력한다."""

    def __init__(self, stream: Optional[TextIO] = None) -> None:
        self.stream = stream or sys.stdout

    def publish(self, request: PublishRequest) -> PublishOutcome:
        outcome = PublishOutcome()
        write = self.stream.write

        write(f"\n=== 리뷰 결과 (라운드 {request.round_number}, dry-run) ===\n")
        write(render_counts_line(request.findings, request.empty_text) + "\n")

        if request.escalation:
            write("\n" + ESCALATION_TEXT + "\n")

        if not request.findings:
            write("\n게시할 지적이 없습니다.\n")
            outcome.skipped = not request.post_when_empty
            return outcome

        groups = request.groups()
        outcome.comment_count = len(groups)
        for group in groups:
            write("\n" + "-" * 72 + "\n")
            if len(group) > 1:
                write(f"[한 코멘트에 묶임: 지적 {len(group)}건]\n")
            for finding in group:
                write(
                    f"[{SEVERITY_LABELS.get(finding.severity, finding.severity)}] "
                    f"{finding.id} {_format_location(finding)} ({finding.category})\n"
                )
                write(f"탐지 관점: {', '.join(finding.detected_by) or '-'}\n")
                write(f"주장: {finding.claim}\n")
                write(f"근거: {finding.evidence.strip()}\n")
                if finding.suggested_fix:
                    write(f"제안: {finding.suggested_fix}\n")
                for claim in finding.alt_claims:
                    write(f"  (다른 서술) {claim}\n")
                outcome.inline_posted.append(finding.id)

        write("\n" + "-" * 72 + "\n")
        write(f"코멘트 {len(groups)}개 / 지적 {len(request.findings)}건\n")
        outcome.summary_posted = True
        return outcome


class GitHubPublisher:
    """gh CLI로 PR에 인라인 코멘트와 요약을 게시한다.

    토큰은 gh가 관리하므로 이 코드와 로그에는 비밀값이 남지 않는다.
    승인(approve)은 하지 않고 event=COMMENT로만 게시한다.
    """

    def __init__(
        self,
        repo_slug: str,
        pr_number: int,
        gh_path: str = "gh",
        cwd: Optional[str] = None,
    ) -> None:
        if not repo_slug:
            raise ValueError("repo_slug가 필요합니다 (owner/name)")
        self.repo_slug = repo_slug
        self.pr_number = pr_number
        self.gh_path = gh_path
        self.cwd = cwd

    def publish(self, request: PublishRequest) -> PublishOutcome:
        outcome = PublishOutcome()

        if not request.findings and not request.post_when_empty:
            outcome.skipped = True
            return outcome

        if request.escalation:
            body = render_summary(
                request.findings,
                round_number=request.round_number,
                empty_text=request.empty_text,
                escalation=True,
            )
            outcome.comment_count = 1
            return self._post_review(body, [], request, outcome)

        inline_groups, overflow_groups = _split_inline(request.groups(), request.inline_lines)
        comments = []
        for group in inline_groups:
            available = request.inline_lines.get(group[0].file) or set()
            line = _anchor_line(group, available)
            comments.append(
                {
                    "path": group[0].file,
                    "line": line,
                    "side": "RIGHT",
                    "body": render_comment(group, request.round_number),
                }
            )

        overflow = _flatten(overflow_groups)
        body = render_summary(
            request.findings,
            round_number=request.round_number,
            empty_text=request.empty_text,
            overflow=overflow,
        )
        outcome.overflow = [f.id for f in overflow]
        outcome.inline_posted = [f.id for f in _flatten(inline_groups)]
        outcome.comment_count = len(comments) + 1
        return self._post_review(body, comments, request, outcome)

    def _post_review(
        self,
        body: str,
        comments: list[dict],
        request: PublishRequest,
        outcome: PublishOutcome,
    ) -> PublishOutcome:
        payload: dict[str, object] = {"body": body, "event": "COMMENT"}
        if request.head_sha:
            payload["commit_id"] = request.head_sha
        if comments:
            payload["comments"] = comments

        ok, error = self._gh_api(payload)
        if ok:
            outcome.summary_posted = True
            return outcome

        if comments:
            # 인라인 라인이 diff에 없다는 등의 이유로 거절되면 요약만 다시 올린다.
            logger.warning("인라인 코멘트 게시 실패, 요약만 게시합니다: %s", error)
            fallback_body = render_summary(
                request.findings,
                round_number=request.round_number,
                empty_text=request.empty_text,
                overflow=request.findings,
            )
            retry_payload: dict[str, object] = {"body": fallback_body, "event": "COMMENT"}
            if request.head_sha:
                retry_payload["commit_id"] = request.head_sha
            ok, retry_error = self._gh_api(retry_payload)
            outcome.errors.append(error)
            if ok:
                outcome.summary_posted = True
                outcome.inline_posted = []
                outcome.overflow = [f.id for f in request.findings]
                outcome.comment_count = 1
                return outcome
            error = retry_error

        outcome.errors.append(error)
        outcome.inline_posted = []
        return outcome

    def _gh_api(self, payload: dict[str, object]) -> tuple[bool, str]:
        if not shutil.which(self.gh_path):
            return False, f"gh CLI를 찾을 수 없습니다: {self.gh_path}"

        args = [
            self.gh_path,
            "api",
            "--method",
            "POST",
            f"/repos/{self.repo_slug}/pulls/{self.pr_number}/reviews",
            "--input",
            "-",
        ]
        proc = subprocess.run(
            args,
            input=json.dumps(payload, ensure_ascii=False),
            cwd=self.cwd,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return False, (proc.stderr or "").strip()[:800]
        return True, ""
