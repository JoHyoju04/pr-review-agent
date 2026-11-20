"""변경 파일 목록으로 이번 PR을 볼지, 얼마나 깊게 볼지 정한다.

전부 LLM 없이 동작하는 순수 함수다. 경로 패턴 매칭은 `filters.path_matches`를 그대로 쓴다.

`skip_paths`와 다른 것: `skip_paths`는 **finding 필터**다 (이미 돈 리뷰의 결과를 버린다).
여기는 **실행 게이트**다. LLM을 아예 부르지 않으므로 비용이 줄어든다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .filters import path_matches


@dataclass(frozen=True)
class GateRules:
    """review.yaml의 `gate` 블록.

    `always_review`가 denylist보다 먼저 평가되는 것이 이 설계의 핵심이다. CI 워크플로,
    Dockerfile, 의존성 목록은 "문서/설정"처럼 보이지만 보안상 최우선 대상이다.
    """

    # 하나라도 걸리면 건너뛰지 않는다 (allowlist).
    always_review: tuple[str, ...] = ()
    # 하나라도 걸리면 intensity를 deep으로 올린다.
    deep_paths: tuple[str, ...] = ()
    # 변경 파일 전부가 여기 걸리면 리뷰를 건너뛴다 (denylist).
    skip_if_only: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.always_review or self.deep_paths or self.skip_if_only)


@dataclass(frozen=True)
class GateDecision:
    """이번 실행을 돌릴지와 어떤 강도로 돌릴지."""

    intensity: str
    skip: bool = False
    reason: str = ""
    # 판단 근거가 된 파일들. 로그와 --dry-run 보고에 쓴다.
    matched: tuple[str, ...] = field(default_factory=tuple)


def _hits(changed_files: Sequence[str], patterns: Sequence[str]) -> list[str]:
    if not patterns:
        return []
    return [path for path in changed_files if path_matches(path, patterns)]


def resolve_gate(
    changed_files: Sequence[str],
    rules: GateRules,
    default_intensity: str = "standard",
    deep_intensity: str = "deep",
) -> GateDecision:
    """게이트를 평가한다. 순수 함수.

    평가 순서:
      1. `always_review`에 걸리는 파일이 하나라도 있으면 건너뛰지 않는다
      2. `deep_paths`에 걸리면 강도를 올린다
      3. 변경 파일 **전부**가 `skip_if_only`면 건너뛴다
      4. 아니면 기본 강도로 본다
    """
    if rules.is_empty:
        return GateDecision(intensity=default_intensity, reason="게이트 규칙 없음")

    paths = [str(path).strip() for path in changed_files if str(path).strip()]
    if not paths:
        # 변경 파일을 못 읽었을 때 조용히 건너뛰면 리뷰가 사라진 것처럼 보인다.
        return GateDecision(
            intensity=default_intensity, reason="변경 파일 목록이 비어 게이트를 적용하지 않음"
        )

    forced = _hits(paths, rules.always_review)
    deep_hits = _hits(paths, rules.deep_paths)
    intensity = deep_intensity if deep_hits else default_intensity

    if forced:
        reason = f"always_review에 걸리는 변경이 있어 반드시 리뷰합니다: {forced[0]}"
        if deep_hits:
            reason += f" / deep_paths로 강도를 올립니다: {deep_hits[0]}"
        return GateDecision(
            intensity=intensity, reason=reason, matched=tuple(forced + deep_hits)
        )

    if deep_hits:
        return GateDecision(
            intensity=intensity,
            reason=f"deep_paths에 걸려 강도를 올립니다: {deep_hits[0]}",
            matched=tuple(deep_hits),
        )

    skippable = _hits(paths, rules.skip_if_only)
    if len(skippable) == len(paths):
        return GateDecision(
            intensity=intensity,
            skip=True,
            reason=f"변경이 skip_if_only 경로뿐이라 리뷰를 건너뜁니다 ({len(paths)}개 파일)",
            matched=tuple(paths),
        )

    return GateDecision(intensity=intensity, reason="기본 강도로 리뷰합니다")
