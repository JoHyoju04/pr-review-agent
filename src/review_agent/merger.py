"""후보 finding 병합과 중복 제거. LLM 없이 동작하는 순수 로직.

## 왜 2단으로 나누는가

병합은 "같은 결함인가"를 판단하는 일이다. 세 가지 신호가 있다.

| 신호 | 믿을 수 있는가 |
|---|---|
| 파일·라인 위치 | 믿을 수 있다. 다만 한 위치에 서로 다른 버그가 둘 있을 수 있다 |
| `category` | **믿을 수 없다.** 탐지기가 추측한 라벨이라 같은 버그에 `data`/`concurrency`가 갈린다 |
| `claim` 내용 | 믿을 수 있다. 다만 표현이 달라 완전 일치를 기대할 수 없다 |

그래서 정체성은 **위치 + claim 내용**으로 판단하고, category는 보조로만 쓴다.

- **1단 (검증 전)**: 위치 + 같은 category + 느슨한 내용 유사도. 여러 관점이 같은 버그를
  찾았을 때 `detected_by`를 모으고 검증 비용을 줄인다. 내용이 확연히 다르면 합치지 않는다.
- **2단 (검증 후)**: 위치 + 엄격한 내용 유사도. category는 보지 않는다. 라벨이 갈려
  1단을 통과하지 못한 중복을 여기서 접는다.

**2단을 검증 후에 하는 이유**: 서로 다른 주장을 검증 전에 섞으면 검증자가 두 주장을 한꺼번에
판정해 투표가 흐려지고, 흡수된 주장은 아무 검증도 받지 않은 채 코멘트에 실린다. 각 claim이
자기 근거로 독립 검증을 통과한 다음에 합쳐야 한다.

어느 단계에서도 **claim을 버리지 않는다.** 흡수된 claim은 `alt_claims`에 남고, 합쳐지지 않은
서로 다른 지적은 게시 단계에서 하나의 코멘트로 묶인다(`publisher.group_by_spot`).
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence

from .models import Dropped, Finding, majority_severity
from .similarity import claim_signature, similarity

DEFAULT_LINE_TOLERANCE = 3
REASON_DUPLICATE = "중복_지적"


def ranges_are_close(
    a_start: int, a_end: int, b_start: int, b_end: int, tolerance: int = DEFAULT_LINE_TOLERANCE
) -> bool:
    """두 라인 범위가 겹치거나 tolerance 줄 이내로 붙어 있는가."""
    if a_start > b_start:
        a_start, a_end, b_start, b_end = b_start, b_end, a_start, a_end
    if b_start <= a_end:  # 겹침
        return True
    return b_start - a_end <= tolerance


def same_location(
    a: Finding, b: Finding, tolerance: int = DEFAULT_LINE_TOLERANCE
) -> bool:
    """같은 파일에서 라인 범위가 가까운가. category와 내용은 보지 않는다."""
    if a.file != b.file:
        return False
    return ranges_are_close(a.line_start, a.line_end, b.line_start, b.line_end, tolerance)


def claim_similarity(a: Finding, b: Finding) -> float:
    return similarity(claim_signature(a), claim_signature(b))


def is_same_spot(
    a: Finding,
    b: Finding,
    tolerance: int = DEFAULT_LINE_TOLERANCE,
    similarity_threshold: float = 0.0,
) -> bool:
    """1단 병합 조건: 같은 파일 + 같은 category + 근접 라인 + 내용 유사도.

    `similarity_threshold`가 0이면 내용을 보지 않는다(이전 동작).
    """
    if a.category != b.category:
        return False
    if not same_location(a, b, tolerance):
        return False
    if similarity_threshold <= 0:
        return True
    return claim_similarity(a, b) >= similarity_threshold


def _absorb(group: Finding, other: Finding) -> None:
    """other를 group에 흡수한다. claim을 포함해 아무 정보도 버리지 않는다."""
    group.line_start = min(group.line_start, other.line_start)
    group.line_end = max(group.line_end, other.line_end)

    for angle in other.detected_by:
        if angle not in group.detected_by:
            group.detected_by.append(angle)

    group.severity_votes.extend(other.severity_votes)
    group.severity = majority_severity(group.severity_votes)

    # evidence는 같은 유형의 모든 위치를 남기기 위해 합친다.
    existing = [chunk.strip() for chunk in group.evidence.split("\n") if chunk.strip()]
    for chunk in other.evidence.split("\n"):
        chunk = chunk.strip()
        if chunk and chunk not in existing:
            existing.append(chunk)
            group.evidence = group.evidence.rstrip() + "\n" + chunk

    # 대표 claim은 먼저 나온 것을 유지하고, 흡수된 표현은 보존한다.
    group.add_alt_claim(other.claim)
    for claim in other.alt_claims:
        group.add_alt_claim(claim)

    if not group.suggested_fix and other.suggested_fix:
        group.suggested_fix = other.suggested_fix


def merge_findings(
    findings: Iterable[Finding],
    tolerance: int = DEFAULT_LINE_TOLERANCE,
    assign_ids: bool = True,
    similarity_threshold: float = 0.0,
) -> list[Finding]:
    """1단 병합. 같은 category의 같은 결함을 하나로 합친다.

    - 조건: 같은 파일 + 같은 category + 라인 근접 + claim 유사도 >= similarity_threshold
    - 내용이 확연히 다르면 **합치지 않는다.** 같은 위치에 서로 다른 버그가 둘 있는 경우가
      여기서 갈라지고, 각각 검증을 받는다.
    - `detected_by`에 어떤 관점이 찾았는지 누적한다.
    - severity는 다수결(동수면 먼저 나온 값)로 정한다. 더 높은 등급으로 올리지 않는다.
    """
    groups: list[Finding] = []
    for candidate in findings:
        target = next(
            (g for g in groups if is_same_spot(g, candidate, tolerance, similarity_threshold)),
            None,
        )
        if target is None:
            groups.append(candidate.copy())  # 입력을 변형하지 않는다
        else:
            _absorb(target, candidate)

    if assign_ids:
        assign_finding_ids(groups)
    return groups


def _dedupe_sort_key(finding: Finding) -> tuple:
    """그룹 묶기 전 입력을 정렬해 결과가 입력 순서에 좌우되지 않게 한다."""
    return (
        finding.file,
        finding.line_start,
        finding.line_end,
        finding.category,
        finding.id,
    )


def _survivor_key(finding: Finding, real_votes: Mapping[str, int]) -> tuple:
    """생존자 선택 기준. 앞쪽이 작을수록 먼저 선택된다."""
    return (
        -len(finding.detected_by),            # 여러 관점이 찾은 것
        -real_votes.get(finding.id, 0),       # real 표가 많은 것
        finding.severity_rank,                # important 먼저
        finding.file,
        finding.line_start,
        finding.id,
    )


def dedupe_findings(
    findings: Sequence[Finding],
    tolerance: int = DEFAULT_LINE_TOLERANCE,
    threshold: float = 0.20,
    real_votes: Optional[Mapping[str, int]] = None,
) -> tuple[list[Finding], list[Dropped]]:
    """2단 중복 제거. 검증을 통과한 finding들에서 같은 지적을 하나로 접는다.

    조건은 **같은 파일 + 근접 라인 + claim 유사도 >= threshold**이며 category는 보지 않는다.
    탐지기가 같은 버그에 다른 category를 매겨 1단을 통과하지 못한 중복이 여기서 접힌다.

    threshold가 0 이하면 아무것도 접지 않는다(비활성).
    """
    if threshold <= 0 or len(findings) < 2:
        return list(findings), []

    votes = dict(real_votes or {})
    ordered = sorted(findings, key=_dedupe_sort_key)

    # 대표와 비교해 그룹을 만든다. 정렬된 입력을 쓰므로 결과가 결정적이다.
    groups: list[list[Finding]] = []
    for finding in ordered:
        target = next(
            (
                group
                for group in groups
                if same_location(group[0], finding, tolerance)
                and claim_similarity(group[0], finding) >= threshold
            ),
            None,
        )
        if target is None:
            groups.append([finding])
        else:
            target.append(finding)

    kept: list[Finding] = []
    dropped: list[Dropped] = []
    for group in groups:
        if len(group) == 1:
            kept.append(group[0])
            continue

        group.sort(key=lambda f: _survivor_key(f, votes))
        survivor = group[0].copy()
        for loser in group[1:]:
            score = claim_similarity(survivor, loser)
            _absorb(survivor, loser)
            dropped.append(
                Dropped(
                    loser,
                    REASON_DUPLICATE,
                    f"{survivor.id}와 같은 지적 (유사도 {score:.2f}, 임계값 {threshold:.2f})",
                )
            )
        kept.append(survivor)

    # 원래 정렬은 바깥(filters.sort_findings)에서 다시 하므로 여기서는 위치순으로 둔다.
    kept.sort(key=_dedupe_sort_key)
    return kept, dropped


def assign_finding_ids(findings: Sequence[Finding], prefix: str = "f") -> None:
    for index, finding in enumerate(findings, start=1):
        finding.id = f"{prefix}-{index:03d}"
