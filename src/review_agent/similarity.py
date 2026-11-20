"""주장 내용 유사도. LLM 없이 동작하는 순수 함수.

병합과 중복 제거는 "같은 결함인가"를 판단해야 한다. 위치만으로는 부족하고(같은 줄에 서로
다른 버그가 둘 있을 수 있다), category만으로도 부족하다(탐지기가 같은 버그에 다른 라벨을
매긴다). 그래서 위치와 함께 claim 내용을 본다.

**문자 bigram Jaccard를 쓰는 이유**: 한국어는 조사가 붙어 단어 단위 Jaccard가 낮게 나온다
("트랜잭션이" vs "트랜잭션은"은 단어로는 불일치). 문자 bigram은 조사 변화와 띄어쓰기 차이에
둔감하고 언어에 의존하지 않는다.

**임계값은 추측이 아니라 실측으로 정했다.** PR #153을 두 번 리뷰해 얻은 실제 claim 15쌍
(같은 버그 5쌍 / 다른 버그 10쌍, `tests/fixtures_pr153.py`)으로 재면 이렇다.

| 비교 대상 | 같은 버그 최저 | 다른 버그 최고 | 간격 |
|---|---|---|---|
| claim만 | 0.243 | 0.122 | +0.120 |
| claim + suggested_fix | 0.229 | 0.152 | +0.077 |

그래서 `suggested_fix`는 비교에서 **제외한다**. "~하세요", "변경하세요" 같은 상투적 표현이
많아 서로 다른 버그끼리도 비슷해 보이게 만들고, 실제로 간격을 절반 가까이 좁힌다.

**주의**: 이 수치는 한국어 claim으로 측정했다. 문자 bigram의 기저 유사도는 언어마다 다르므로
(영어는 bigram 공간이 좁아 기저값이 높다) `comment_language`를 바꾸면 임계값도 다시 재야 한다.
그래서 두 임계값 모두 review.yaml에서 조정할 수 있게 둔다.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .models import Finding

# 유사도 판정에서 제거할 문자: 구두점, 따옴표, 괄호, 코드 펜스 기호 등
_STRIP_RE = re.compile(r"[^\w가-힣]+", re.UNICODE)

BIGRAM_SIZE = 2


def normalize(text: str) -> str:
    """유사도 비교용 정규화.

    유니코드 정규화(NFKC) -> 소문자 -> 단어 문자와 한글만 남김 -> 공백 전부 제거.
    띄어쓰기를 지우는 이유는 한국어 띄어쓰기가 사람·모델마다 달라서다.
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", str(text)).lower()
    return _STRIP_RE.sub("", normalized)


@lru_cache(maxsize=2048)
def bigrams(text: str) -> frozenset[str]:
    """정규화된 문자열의 문자 bigram 집합."""
    cleaned = normalize(text)
    if len(cleaned) < BIGRAM_SIZE:
        return frozenset({cleaned} if cleaned else ())
    return frozenset(
        cleaned[index : index + BIGRAM_SIZE] for index in range(len(cleaned) - BIGRAM_SIZE + 1)
    )


def similarity(left: str, right: str) -> float:
    """문자 bigram Jaccard 유사도. 0.0 ~ 1.0.

    양쪽이 모두 비어 있으면 0.0을 돌려준다. 내용이 없는 두 지적을 "같다"고 보면
    근거 없이 병합되기 때문이다.
    """
    left_grams = bigrams(left)
    right_grams = bigrams(right)
    if not left_grams or not right_grams:
        return 0.0
    intersection = len(left_grams & right_grams)
    if not intersection:
        return 0.0
    return intersection / len(left_grams | right_grams)


def claim_signature(finding: "Finding") -> str:
    """유사도 비교에 쓰는 텍스트: claim만.

    - `evidence`를 넣지 않는 이유: 같은 코드를 인용하면 서로 다른 버그도 비슷해 보인다.
    - `suggested_fix`를 넣지 않는 이유: 상투적인 지시 문구가 많아 분리력을 떨어뜨린다
      (실측으로 간격이 +0.120 -> +0.077로 좁아졌다. 모듈 docstring 참고).
    """
    return str(finding.claim or "").strip()


def findings_are_similar(left: "Finding", right: "Finding", threshold: float) -> bool:
    """두 finding의 주장이 같은 결함을 가리키는가.

    threshold가 0 이하면 내용을 보지 않고 항상 같다고 본다(내용 게이트 비활성).
    """
    if threshold <= 0:
        return True
    return similarity(claim_signature(left), claim_signature(right)) >= threshold
