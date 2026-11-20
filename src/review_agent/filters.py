"""사전 필터, 투표 집계, 정렬, 상한. 전부 LLM 없이 동작하는 순수 로직."""

from __future__ import annotations

import re
from dataclasses import replace
from functools import lru_cache
from typing import Iterable, Optional, Sequence

from .merger import DEFAULT_LINE_TOLERANCE, ranges_are_close
from .models import (
    CATEGORIES,
    SECURITY_CATEGORY,
    STYLE_CATEGORIES,
    Dropped,
    Finding,
    PostedRef,
    VoteResult,
)

# 사전 필터에서 제거된 이유
REASON_NO_EVIDENCE = "evidence_없음"
REASON_SKIPPED_PATH = "건너뛸_경로"
REASON_STYLE = "스타일_지적"
REASON_ALREADY_POSTED = "이미_게시함"
REASON_VOTE = "투표_미달"
REASON_NIT_CAP = "nit_상한"
REASON_MAX_FINDINGS = "게시_상한"

__all__ = [
    "Dropped",
    "apply_caps",
    "apply_severity_floor",
    "apply_votes",
    "count_by_severity",
    "is_duplicate",
    "path_matches",
    "prefilter",
    "sort_findings",
]


def _normalize_path(path: str) -> str:
    path = str(path or "").strip().strip("`").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


@lru_cache(maxsize=512)
def _compile_pattern(pattern: str) -> re.Pattern[str] | None:
    """glob 비슷한 패턴을 경로 정규식으로 바꾼다.

    규칙:
      - `generated/`  -> 어느 깊이든 generated 디렉터리 아래 전부
      - `src/a/b/`    -> 루트 기준 그 디렉터리 아래 전부
      - `*.lock`      -> 어느 깊이든 .lock 파일
      - `**/__snapshots__/` -> 어느 깊이든 그 디렉터리 아래 전부
      - `/build/`     -> 루트 기준
    """
    pat = str(pattern or "").strip().strip("`").replace("\\", "/")
    if not pat or pat.startswith("#"):
        return None

    anchored = pat.startswith("/")
    pat = pat.lstrip("/")
    is_dir = pat.endswith("/")
    pat = pat.rstrip("/")
    if not pat:
        return None

    if pat.startswith("**/"):
        pat = pat[3:]
        anchored = False

    # 디렉터리 구분자가 남아 있으면 루트 기준으로 본다.
    if "/" in pat:
        anchored = True

    out: list[str] = []
    index = 0
    while index < len(pat):
        char = pat[index]
        if pat.startswith("**", index):
            out.append(".*")
            index += 2
            if pat.startswith("/", index):
                index += 1
                out.append("/?")
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        index += 1

    body = "".join(out)
    prefix = "" if anchored else r"(?:.*/)?"
    suffix = "/.*" if is_dir else ""
    return re.compile(rf"^{prefix}{body}{suffix}$")


def path_matches(path: str, patterns: Iterable[str]) -> bool:
    """경로가 건너뛸 경로 패턴 중 하나에 걸리는가."""
    normalized = _normalize_path(path)
    if not normalized:
        return False
    for pattern in patterns:
        compiled = _compile_pattern(pattern)
        if compiled and compiled.match(normalized):
            return True
    return False


def is_style_category(category: str) -> bool:
    return str(category or "").strip().lower() in STYLE_CATEGORIES


def is_duplicate(
    finding: Finding,
    posted: Sequence[PostedRef],
    tolerance: int = DEFAULT_LINE_TOLERANCE,
) -> bool:
    """이미 게시한 지적과 같은 지문인가.

    정확히 같은 지문이거나, 같은 파일·같은 category에서 라인 범위가 겹치거나
    tolerance 이내로 붙어 있으면 같은 지적으로 본다 (병합 규칙과 같은 기준).
    """
    for ref in posted:
        if ref.fingerprint and ref.fingerprint == finding.fingerprint:
            return True
        if ref.file != finding.file or ref.category != finding.category:
            continue
        if ranges_are_close(
            finding.line_start, finding.line_end, ref.line_start, ref.line_end, tolerance
        ):
            return True
    return False


def prefilter(
    findings: Iterable[Finding],
    skip_paths: Sequence[str] = (),
    posted: Sequence[PostedRef] = (),
    tolerance: int = DEFAULT_LINE_TOLERANCE,
) -> tuple[list[Finding], list[Dropped]]:
    """투표 전 필터.

    제거 대상: evidence 없음, 건너뛸 경로, 스타일/포맷/정리성 지적, 이미 게시한 지문.
    남은 finding의 알 수 없는 category는 other로 정규화한다.
    """
    kept: list[Finding] = []
    dropped: list[Dropped] = []

    for finding in findings:
        if not finding.evidence.strip():
            dropped.append(Dropped(finding, REASON_NO_EVIDENCE))
            continue
        if skip_paths and path_matches(finding.file, skip_paths):
            dropped.append(Dropped(finding, REASON_SKIPPED_PATH, finding.file))
            continue
        if is_style_category(finding.category):
            dropped.append(Dropped(finding, REASON_STYLE, finding.category))
            continue
        if posted and is_duplicate(finding, posted, tolerance):
            dropped.append(Dropped(finding, REASON_ALREADY_POSTED, finding.fingerprint))
            continue

        if finding.category not in CATEGORIES:
            finding = replace(finding, category="other")
        kept.append(finding)

    return kept, dropped


def apply_votes(
    votes: Iterable[VoteResult],
    threshold: int,
    *,
    security_threshold: Optional[int] = None,
) -> tuple[list[Finding], list[Dropped]]:
    """real 표가 임계값 이상인 finding만 통과시킨다.

    보안 finding은 `security_threshold`로 판정한다 (보안 체크는 투표가 아니라 독립적인
    필요조건이라 보통 전원 합의다). 주지 않으면 `threshold`를 그대로 쓴다.

    결정적 도구가 낸 finding은 검증을 받지 않으므로 표가 없다. 임계값과 무관하게
    통과시킨다 — 이 분기가 없으면 `0 >= threshold`로 전부 버려진다.
    """
    passed: list[Finding] = []
    dropped: list[Dropped] = []
    for vote in votes:
        finding = vote.finding
        if finding.deterministic:
            passed.append(finding)
            continue
        is_security = finding.category == SECURITY_CATEGORY
        limit = (
            security_threshold
            if is_security and security_threshold is not None
            else threshold
        )
        if vote.passes(limit):
            passed.append(finding)
        else:
            label = "보안 임계값" if is_security else "임계값"
            dropped.append(
                Dropped(
                    finding,
                    REASON_VOTE,
                    f"real {vote.real_count}/{len(vote.verdicts)} ({label} {limit})",
                )
            )
    return passed, dropped


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    """important -> nit -> pre_existing, 같은 등급에서는 detected_by가 많은 순."""
    return sorted(
        findings,
        key=lambda f: (
            f.severity_rank,
            -len(f.detected_by),
            f.file,
            f.line_start,
            f.id,
        ),
    )


def apply_severity_floor(
    findings: Iterable[Finding], enabled: bool = True
) -> list[Finding]:
    """보안 finding의 nit을 important로 올린다. 순수 함수.

    보안 지적이 nit 상한에 밀려 사라지는 걸 막는 것이 목적이다. 병합의 다수결
    (`majority_severity`)은 "더 높은 등급으로 올리지 않는다"가 요구사항이므로 거기서
    하지 않고 별도 단계로 둔다.

    `pre_existing`은 올리지 않는다. 올리면 "이 PR이 만든 문제가 아니다"라는 표시가
    깨지고 코멘트의 참고용 안내문도 사라진다.
    """
    result: list[Finding] = []
    for finding in findings:
        if (
            enabled
            and finding.category == SECURITY_CATEGORY
            and finding.severity == "nit"
        ):
            result.append(replace(finding, severity="important"))
        else:
            result.append(finding)
    return result


def apply_caps(
    findings: Sequence[Finding],
    max_findings: int,
    max_nits: int,
    *,
    reserved_security: int = 0,
) -> tuple[list[Finding], list[Dropped]]:
    """nit 상한을 먼저 적용하고, 그다음 전체 게시 상한을 적용한다.

    입력은 이미 정렬된 순서라고 본다. 결과도 그 순서를 유지한다.

    `reserved_security`는 전체 상한을 적용할 때 보안 finding에 먼저 떼어 주는 슬롯 수다.
    정렬만으로 자르면 important가 상한을 넘는 PR에서 보안 지적이 조용히 잘린다.
    0이면 예약 없이 앞에서부터 자른다 (기존 동작).
    """
    kept: list[Finding] = []
    dropped: list[Dropped] = []

    nit_count = 0
    for finding in findings:
        if finding.severity == "nit":
            if nit_count >= max_nits:
                dropped.append(Dropped(finding, REASON_NIT_CAP, f"nit 상한 {max_nits}"))
                continue
            nit_count += 1
        kept.append(finding)

    if max_findings >= 0 and len(kept) > max_findings:
        reserved_count = min(max(0, reserved_security), max_findings)
        reserved = [
            finding for finding in kept if finding.category == SECURITY_CATEGORY
        ][:reserved_count]

        selected = {id(finding) for finding in reserved}
        for finding in kept:
            if len(selected) >= max_findings:
                break
            selected.add(id(finding))

        for finding in kept:
            if id(finding) not in selected:
                dropped.append(
                    Dropped(finding, REASON_MAX_FINDINGS, f"게시 상한 {max_findings}")
                )
        kept = [finding for finding in kept if id(finding) in selected]

    return kept, dropped


def count_by_severity(findings: Iterable[Finding]) -> dict[str, int]:
    counts = {"important": 0, "nit": 0, "pre_existing": 0}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts
