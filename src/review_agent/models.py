"""finding/검증 결과 데이터 모델과 스키마 검증.

이 모듈은 LLM 없이 동작하는 순수 로직만 담는다.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Optional, Sequence

SEVERITIES: tuple[str, ...] = ("important", "nit", "pre_existing")
SEVERITY_ORDER: dict[str, int] = {s: i for i, s in enumerate(SEVERITIES)}

CATEGORIES: tuple[str, ...] = (
    "logic",
    "concurrency",
    "api_contract",
    "security",
    "data",
    "other",
)

# 보안 전용 경로(검증 체크, 통과 기준, 게시 예약 슬롯)를 가르는 category.
SECURITY_CATEGORY = "security"

# category가 이 중 하나로 정규화되면 사전 필터에서 제거한다 (스타일/포맷/정리성).
STYLE_CATEGORIES: frozenset[str] = frozenset(
    {
        "style",
        "styling",
        "format",
        "formatting",
        "whitespace",
        "indentation",
        "naming",
        "lint",
        "linting",
        "import",
        "imports",
        "cleanup",
        "tidiness",
        "readability",
        "typo",
        "comment",
        "doc_style",
        "스타일",
        "포맷",
        "포맷팅",
        "정리성",
        "네이밍",
    }
)

_SEVERITY_ALIASES: dict[str, str] = {
    "important": "important",
    "major": "important",
    "critical": "important",
    "blocker": "important",
    "bug": "important",
    "must": "important",
    "must_fix": "important",
    "high": "important",
    "중요": "important",
    "nit": "nit",
    "nitpick": "nit",
    "minor": "nit",
    "trivial": "nit",
    "low": "nit",
    "사소": "nit",
    "pre_existing": "pre_existing",
    "preexisting": "pre_existing",
    "pre-existing": "pre_existing",
    "existing": "pre_existing",
    "기존": "pre_existing",
}

_CATEGORY_ALIASES: dict[str, str] = {
    "logic": "logic",
    "logical": "logic",
    "edge_case": "logic",
    "edgecase": "logic",
    "correctness": "logic",
    "로직": "logic",
    "concurrency": "concurrency",
    "concurrent": "concurrency",
    "thread": "concurrency",
    "threading": "concurrency",
    "race": "concurrency",
    "transaction": "concurrency",
    "resource": "concurrency",
    "동시성": "concurrency",
    "api_contract": "api_contract",
    "api": "api_contract",
    "contract": "api_contract",
    "apicontract": "api_contract",
    "compatibility": "api_contract",
    "schema": "api_contract",
    "migration": "api_contract",
    "계약": "api_contract",
    "security": "security",
    "auth": "security",
    "authz": "security",
    "보안": "security",
    "data": "data",
    "database": "data",
    "db": "data",
    "데이터": "data",
    "other": "other",
    "기타": "other",
}


class SchemaError(ValueError):
    """LLM이 돌려준 finding이 스키마를 만족하지 않을 때."""


def normalize_severity(value: Any) -> str:
    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    # 알 수 없는 값은 important로 둔다: 진짜 버그를 놓치는 비용이 더 크다.
    return _SEVERITY_ALIASES.get(key, "important")


def normalize_category(value: Any) -> str:
    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not key:
        return "other"
    return _CATEGORY_ALIASES.get(key, key)


def _coerce_line(value: Any, fieldname: str) -> int:
    if isinstance(value, bool):
        raise SchemaError(f"{fieldname}이 정수가 아닙니다: {value!r}")
    try:
        line = int(str(value).strip())
    except (TypeError, ValueError):
        raise SchemaError(f"{fieldname}이 정수가 아닙니다: {value!r}") from None
    if line < 1:
        raise SchemaError(f"{fieldname}이 1보다 작습니다: {line}")
    return line


@dataclass
class Finding:
    """하나의 지적."""

    file: str
    line_start: int
    line_end: int
    category: str
    severity: str
    claim: str
    evidence: str
    suggested_fix: str = ""
    detected_by: list[str] = field(default_factory=list)
    # 병합 시 다수결을 내기 위해 각 탐지기가 매긴 severity를 모아 둔다.
    severity_votes: list[str] = field(default_factory=list)
    # 병합·중복 제거 과정에서 흡수된 다른 표현의 claim. 절대 버리지 않는다.
    # 같은 결함을 다르게 서술한 문장이 여기 쌓이고, 코멘트에 참고로 덧붙는다.
    alt_claims: list[str] = field(default_factory=list)
    # 결정적 도구(gitleaks, Semgrep 등)가 낸 finding인가. 참이면 LLM 검증을 건너뛰고
    # 투표 없이 통과한다. 결정적 결과에 LLM 투표를 붙이면 노이즈만 는다.
    #
    # 엔진 내부에서만 세운다. `parse_finding`은 LLM 출력의 이 필드를 절대 읽지 않는다
    # (읽으면 LLM이 `deterministic: true`로 검증을 우회할 수 있다).
    deterministic: bool = False
    id: str = ""

    def __post_init__(self) -> None:
        if not self.severity_votes:
            self.severity_votes = [self.severity]

    @property
    def fingerprint(self) -> str:
        """파일 + 라인 범위 + category. 라운드 간 중복 게시를 막는 데 쓴다."""
        return f"{self.file}:{self.line_start}-{self.line_end}:{self.category}"

    @property
    def severity_rank(self) -> int:
        return SEVERITY_ORDER.get(self.severity, len(SEVERITIES))

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "file": self.file,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "category": self.category,
            "severity": self.severity,
            "claim": self.claim,
            "evidence": self.evidence,
            "suggested_fix": self.suggested_fix,
            "detected_by": list(self.detected_by),
        }
        if self.alt_claims:
            payload["alt_claims"] = list(self.alt_claims)
        if self.deterministic:
            payload["deterministic"] = True
        return payload

    def with_category(self, category: str) -> "Finding":
        return replace(self, category=category)

    def add_alt_claim(self, claim: str) -> None:
        """흡수된 claim을 보존한다. 대표 claim과 같거나 이미 있으면 넣지 않는다."""
        text = str(claim or "").strip()
        if not text or text == self.claim.strip() or text in self.alt_claims:
            return
        self.alt_claims.append(text)

    def copy(self) -> "Finding":
        return Finding(
            file=self.file,
            line_start=self.line_start,
            line_end=self.line_end,
            category=self.category,
            severity=self.severity,
            claim=self.claim,
            evidence=self.evidence,
            suggested_fix=self.suggested_fix,
            detected_by=list(self.detected_by),
            severity_votes=list(self.severity_votes),
            alt_claims=list(self.alt_claims),
            deterministic=self.deterministic,
            id=self.id,
        )


def parse_finding(obj: Any, angle: Optional[str] = None) -> Finding:
    """LLM이 돌려준 dict 하나를 Finding으로 검증·정규화한다.

    스키마를 만족하지 않으면 SchemaError. evidence가 비어 있으면 이 단계에서 제거된다.
    """
    if not isinstance(obj, Mapping):
        raise SchemaError("finding이 JSON 객체가 아닙니다")

    file = str(obj.get("file") or "").strip()
    if not file:
        raise SchemaError("file 필드가 비어 있습니다")

    claim = str(obj.get("claim") or "").strip()
    if not claim:
        raise SchemaError("claim 필드가 비어 있습니다")

    evidence = str(obj.get("evidence") or "").strip()
    if not evidence:
        raise SchemaError("evidence 필드가 비어 있습니다 (파일:라인과 코드 인용 필수)")

    line_start = _coerce_line(obj.get("line_start"), "line_start")
    raw_end = obj.get("line_end")
    line_end = line_start if raw_end in (None, "") else _coerce_line(raw_end, "line_end")
    if line_end < line_start:
        line_start, line_end = line_end, line_start

    severity = normalize_severity(obj.get("severity"))
    category = normalize_category(obj.get("category"))

    # `deterministic`은 일부러 읽지 않는다. LLM이 출력에 넣어도 무시되어야 한다 —
    # 읽으면 검증을 건너뛰게 만들 수 있다.
    return Finding(
        file=file.lstrip("./"),
        line_start=line_start,
        line_end=line_end,
        category=category,
        severity=severity,
        claim=claim,
        evidence=evidence,
        suggested_fix=str(obj.get("suggested_fix") or "").strip(),
        detected_by=[angle] if angle else [],
        severity_votes=[severity],
    )


def majority_severity(votes: Sequence[str]) -> str:
    """가장 많이 나온 severity. 동수면 먼저 나온 값(탐지 순서)을 쓴다.

    더 높은 등급으로 올리지 않는 것이 요구사항이다.
    """
    if not votes:
        return "important"
    counter: Counter[str] = Counter()
    for vote in votes:
        counter[vote] += 1  # Counter는 삽입 순서를 유지하므로 동수 시 먼저 본 값이 앞선다
    return counter.most_common(1)[0][0]


@dataclass
class Verdict:
    """검증자 한 명의 판정."""

    check: str
    verdict: str
    reason: str = ""
    checked: str = ""

    @property
    def is_real(self) -> bool:
        return self.verdict == "real"

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "verdict": self.verdict,
            "reason": self.reason,
            "checked": self.checked,
        }


@dataclass
class VoteResult:
    """finding 하나에 대한 검증자 M명의 투표 결과."""

    finding: Finding
    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def real_count(self) -> int:
        return sum(1 for v in self.verdicts if v.is_real)

    def passes(self, threshold: int) -> bool:
        return self.real_count >= threshold

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding": self.finding.to_dict(),
            "verdicts": [v.to_dict() for v in self.verdicts],
            "real_count": self.real_count,
        }


@dataclass
class Dropped:
    """걸러낸 후보와 그 이유. 어느 단계에서 왜 빠졌는지 보고에 쓴다."""

    finding: "Finding"
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class PostedRef:
    """이전 라운드에 이미 게시한 지적의 지문."""

    file: str
    line_start: int
    line_end: int
    category: str
    fingerprint: str = ""
    claim: str = ""
    finding_id: str = ""
    round: int = 0


@dataclass
class PRMeta:
    number: int
    title: str = ""
    description: str = ""
    base: str = ""
    head: str = ""
    repo_slug: str = ""


def findings_to_payload(findings: Iterable[Finding]) -> list[dict[str, Any]]:
    return [f.to_dict() for f in findings]
