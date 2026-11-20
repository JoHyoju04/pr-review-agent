"""탐지·검증 프롬프트 템플릿과 렌더링.

엔진은 프로젝트별 내용을 담지 않는다. 레포 사정은 AGENTS.md / REVIEW.md로만 들어온다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

from .models import SECURITY_CATEGORY, Finding, PostedRef

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


GENERAL_KIND = "general"
SECURITY_KIND = "security"


@dataclass(frozen=True)
class Angle:
    """탐지 관점.

    `id`는 review.yaml이 관점을 고르는 키이고, `name`은 코멘트에 찍히는 표시명이다.
    `kind`가 프롬프트 템플릿을 고른다 (범용 / 보안).
    """

    id: str
    name: str
    description: str
    kind: str = GENERAL_KIND
    # 이 관점에만 붙는 추가 규칙. 주로 오탐 억제 문구.
    extra_rules: str = ""

    @property
    def is_security(self) -> bool:
        return self.kind == SECURITY_KIND


@dataclass(frozen=True)
class Check:
    """검증 확인 항목.

    `refutation`은 이 확인 항목에서 not_real이 되는 조건이다. 체크마다 다르므로
    공통 목록을 쓰지 않고 프롬프트에 체크별로 렌더링한다.
    """

    id: str
    name: str
    description: str
    refutation: str = ""


GENERAL_ANGLES: tuple[Angle, ...] = (
    Angle(
        id="logic",
        name="로직·엣지케이스",
        description=(
            "조건 분기 오류, null/빈 값/경계값 처리, 잘못된 계산, 오류 처리 누락, 상태 전이 오류"
        ),
    ),
    Angle(
        id="concurrency",
        name="동시성·자원",
        description="경쟁 상태, 트랜잭션 경계, 락/데드락, 리소스 누수, 재시도·멱등성 문제",
    ),
    Angle(
        id="api_contract",
        name="API·계약 변경",
        description=(
            "요청/응답 필드·타입 변경, 하위 호환 파괴, 호출부와 구현 불일치, "
            "DB 스키마·마이그레이션 불일치"
        ),
    ),
)

# 보안 관점. review.yaml의 security_angles로만 켜진다 (기본은 꺼짐).
SECURITY_ANGLES: tuple[Angle, ...] = (
    Angle(
        id="authz",
        name="인증·인가",
        description=(
            "인증을 요구하지 않는 엔드포인트, 인가 검사 누락, 토큰 주체와 대상 리소스의 "
            "소유자 불일치(IDOR), 권한 상승 경로, 역할·스코프 검사 우회"
        ),
        kind=SECURITY_KIND,
    ),
    Angle(
        id="injection",
        name="입력 검증·인젝션",
        description=(
            "SQL/NoSQL 인젝션, 커맨드 인젝션, XSS, 경로 조작, 역직렬화, SSRF, 템플릿 인젝션. "
            "외부 입력이 검증 없이 인터프리터·파일시스템·네트워크에 닿는 경로"
        ),
        kind=SECURITY_KIND,
    ),
    Angle(
        id="data_exposure",
        name="민감정보 노출",
        description=(
            "시크릿·키·토큰 하드코딩, 민감정보 로깅·에러 응답 노출, 응답 과다 노출, "
            "취약하거나 잘못 쓴 암호화·해시·난수"
        ),
        kind=SECURITY_KIND,
        extra_rules=(
            "- 시크릿 하드코딩은 이 PR이 새로 추가한 라인에서만, 값이 실제 자격증명 형태일 "
            "때만 보고하세요. 테스트 픽스처, example 설정, 자리표시자(`<your-token>`, "
            "`changeme`, 더미 값)는 보고하지 마세요.\n"
        ),
    ),
    Angle(
        id="config",
        name="권한·설정 변경",
        description=(
            "CORS·권한·세션 쿠키 설정, CI 워크플로 트리거와 시크릿 노출, 컨테이너·인프라 "
            "권한(Dockerfile, k8s, terraform), 새로 추가된 의존성의 위험성"
        ),
        kind=SECURITY_KIND,
    ),
)

# 하위 호환: 기존 호출부와 테스트가 참조한다. 범용 관점만 담는다.
ANGLES: tuple[Angle, ...] = GENERAL_ANGLES

ANGLE_REGISTRY: dict[str, Angle] = {
    angle.id: angle for angle in GENERAL_ANGLES + SECURITY_ANGLES
}
GENERAL_ANGLE_IDS: tuple[str, ...] = tuple(angle.id for angle in GENERAL_ANGLES)
SECURITY_ANGLE_IDS: tuple[str, ...] = tuple(angle.id for angle in SECURITY_ANGLES)

GENERAL_CHECKS: tuple[Check, ...] = (
    Check(
        id="code_evidence",
        name="코드 근거",
        description="인용된 코드가 해당 위치에 실제로 있는가. 주장이 그 코드로 성립하는가",
    ),
    Check(
        id="intended",
        name="의도된 동작 여부",
        description=(
            "PR 설명, 컨벤션, 주변 코드, 테스트에 비추어 의도된 동작이 아닌가. "
            "다른 계층에서 이미 처리되지 않는가"
        ),
    ),
    Check(
        id="impact",
        name="실제 영향 여부",
        description="이 경로에 실제로 도달 가능한가. 도달한다면 사용자나 데이터에 구체적 영향이 있는가",
    ),
)

# 보안 전용 확인 항목.
#
# 범용 체크를 그대로 쓰면 안 되는 이유: "의도된 동작 여부"는 범용 리뷰에서는 훌륭한 오탐
# 필터지만(버그는 정의상 의도하지 않은 동작이다), 취약점은 거의 전부 의도적으로 작성된
# 코드다. 일부러 인증을 뺀 내부 엔드포인트, 바인딩이 안 되는 ORDER BY의 문자열 결합,
# 디버깅용 토큰 로깅이 모두 "의도된 동작"으로 판정되어 떨어진다.
#
# 그래서 "의도 여부"를 빼고 "도달 가능성 + 코드로 확인되는 방어"로 바꾼다. 오탐 필터가
# 없어지는 게 아니라 existing_defense가 그 자리를 맡는다. 이게 빠지면 "모든 문자열 결합은
# 인젝션"이 되어 노이즈가 폭발한다.
#
# 세 항목은 투표가 아니라 독립적인 필요조건이다. 그래서 통과 기준이 전원 합의다
# (config.security_pass_threshold).
SECURITY_CHECKS: tuple[Check, ...] = (
    Check(
        id="reachability",
        name="도달 가능성",
        description=(
            "이 값 또는 경로가 신뢰 경계 밖에서 공격자가 제어하는 형태로 들어오는가. "
            "입구가 라우터 시그니처, 메시지 소비자, 파일 업로드, 웹훅, 환경변수 중 "
            "무엇인지 코드로 짚으세요"
        ),
        refutation=(
            "입구가 내부 호출뿐이고 공격자가 제어하는 입력이 이 코드에 닿지 않음을 "
            "코드로 확인한 경우"
        ),
    ),
    Check(
        id="existing_defense",
        name="기존 방어 존재",
        description=(
            "이 경로를 막는 방어가 코드에 실제로 있는가. 방어로 인정하는 것: ORM 파라미터 "
            "바인딩, 화이트리스트 검증, 템플릿 자동 이스케이핑, 미들웨어·데코레이터 인가 "
            "검사, 프레임워크 기본 방어. 인정하려면 `파일:라인`으로 가리킬 수 있어야 합니다"
        ),
        refutation="이 경로를 막는 방어를 `파일:라인`으로 찾은 경우",
    ),
    Check(
        id="blast_radius",
        name="악용 영향",
        description=(
            "악용에 성공하면 무엇이 노출·변경되거나 어떤 권한이 상승하는가. "
            "영향 범위를 구체적으로 적으세요"
        ),
        refutation=(
            "악용에 성공해도 공격자가 이미 가진 권한이나 이미 볼 수 있는 정보를 "
            "넘지 못하는 경우"
        ),
    ),
)

# 하위 호환: 기존 호출부와 테스트가 참조한다. 범용 체크만 담는다.
CHECKS: tuple[Check, ...] = GENERAL_CHECKS

CHECK_REGISTRY: dict[str, Check] = {
    check.id: check for check in GENERAL_CHECKS + SECURITY_CHECKS
}
GENERAL_CHECK_IDS: tuple[str, ...] = tuple(check.id for check in GENERAL_CHECKS)
SECURITY_CHECK_IDS: tuple[str, ...] = tuple(check.id for check in SECURITY_CHECKS)

INTENSITY_NOTES: dict[str, str] = {
    "quick": "시간을 들이지 말고 눈에 띄는 확실한 문제만 보세요.",
    "standard": "변경된 코드와 그 호출처까지 확인하세요.",
    "deep": "변경된 코드의 호출처, 관련 테스트, 영향 범위까지 넓게 확인하세요.",
}


DETECT_TEMPLATE = """당신은 코드 리뷰어입니다. 이 Pull Request에서 "{{ANGLE_NAME}}" 관점의 실제 버그를 찾으세요.

## 관점
{{ANGLE_DESC}}

## 작업 방식
1. 레포 루트의 {{AGENTS_NAME}}와 REVIEW.md를 먼저 읽고 따르세요. REVIEW.md의 "건너뛸 경로"는 보지 않습니다.
2. `git diff {{DIFF_RANGE}}`로 변경 내용을 확인하세요.
3. diff만 보지 말고, 변경된 코드의 호출처와 연관 파일을 열어 전체 맥락에서 판단하세요. {{INTENSITY_NOTE}}
4. PR 의도: 제목 "{{PR_TITLE}}" / 설명 "{{PR_DESCRIPTION}}". 변경이 이 의도를 달성하는지, 다른 곳을 깨뜨리지 않는지 확인하세요. 버그 수정 PR이면 근본 원인을 고쳤는지 보세요.
5. 읽기 전용입니다. 파일을 수정하거나 네트워크에 접근하지 마세요.

## 규칙
- 실제로 잘못 동작하거나 잘못될 수 있는 코드만 보고하세요.
- 모든 finding에는 `파일:라인`과 해당 코드 인용이 있어야 합니다. 근거를 대지 못하면 보고하지 마세요.
- 스타일, 포맷, 네이밍 취향, 정리성 제안은 보고하지 마세요.
- 같은 유형의 버그가 여러 곳이면 하나의 finding으로 묶고 모든 위치를 evidence에 적으세요.
- 이 PR이 만든 문제가 아니라 원래 있던 문제면 severity를 `pre_existing`으로 표시하세요.
- 지적할 것이 없으면 빈 배열 `[]`을 출력하세요. 억지로 찾지 마세요.
- nit은 최대 {{MAX_NITS}}개까지만 보고하세요.

## 출력
JSON 배열만 출력하세요. 설명, 코드 펜스, 서문 금지. claim과 suggested_fix는 {{LANGUAGE_NAME}}로 작성하세요.
항목 필드: file, line_start, line_end, category, severity, claim, evidence, suggested_fix
- category: logic | concurrency | api_contract | security | data | other
- severity: important | nit | pre_existing
- file: 레포 루트 기준 상대 경로
"""


SECURITY_DETECT_TEMPLATE = """당신은 보안 리뷰어입니다. 이 Pull Request에서 "{{ANGLE_NAME}}" 관점의 취약점을 찾으세요.

## 관점
{{ANGLE_DESC}}

## 판단 기준
"지금 잘못 동작하는가"가 아니라 **공격자가 이 변경을 어떻게 악용할 수 있는가**로 판단하세요.
취약점은 대부분 정상 동작하는, 의도적으로 작성된 코드입니다. 개발자가 의도한 구현이라는
사실은 보고하지 않을 이유가 되지 않습니다.

## 작업 방식
1. 레포 루트의 {{AGENTS_NAME}}, REVIEW.md, SECURITY-REVIEW.md를 먼저 읽고 따르세요.
   REVIEW.md의 "건너뛸 경로"는 보지 않습니다.
2. `git diff {{DIFF_RANGE}}`로 변경 내용을 확인하세요.
3. 외부 입력이 들어오는 입구(라우터 시그니처, 메시지 소비자, 파일 업로드, 환경변수, 웹훅)를
   먼저 찾고, 그 값이 이 변경까지 어떤 경로로 흐르는지 확인하세요. {{INTENSITY_NOTE}}
4. PR 의도: 제목 "{{PR_TITLE}}" / 설명 "{{PR_DESCRIPTION}}".
5. 읽기 전용입니다. 파일을 수정하거나 네트워크에 접근하지 마세요. 취약점을 실제로 악용해
   보려 하지 마세요. 코드를 읽어서 판단하세요.

## 규칙
- 공격자가 악용할 수 있는 경로만 보고하세요. 보안과 무관한 지적(로직 버그, 성능, 가독성,
  스타일)은 이 리뷰의 범위가 아닙니다. 다른 리뷰어가 봅니다.
- category는 항상 `security`입니다.
- 모든 finding의 evidence에는 세 가지가 있어야 합니다.
  (1) `파일:라인`과 해당 코드 인용
  (2) 외부 입력이 들어오는 입구와 그 값이 이 코드까지 흐르는 경로
  (3) 구체적인 공격 입력 또는 호출 예시
  이 셋을 대지 못하면 보고하지 마세요.
- 코드에서 확인되지 않는 방어를 가정하지 마세요. 반대로, 코드에서 확인되는 방어가 이 경로를
  막는다면 보고하지 마세요. 방어로 인정하는 것: ORM 파라미터 바인딩, 화이트리스트 검증,
  템플릿 자동 이스케이핑, 미들웨어·데코레이터 인가 검사, 프레임워크 기본 방어.
- 클라이언트 측 제한(프론트엔드 드롭다운, 모바일 앱 검증)은 방어가 아닙니다.
- 코드로 확인되지 않는 네트워크·인프라 가정("내부망이라 안전하다")은 방어가 아닙니다.
- 같은 유형의 취약점이 여러 곳이면 하나의 finding으로 묶고 모든 위치를 evidence에 적으세요.
- 이 PR이 만든 취약점이 아니라 원래 있던 것이면 severity를 `pre_existing`으로 표시하세요.
- 지적할 것이 없으면 빈 배열 `[]`을 출력하세요. 억지로 찾지 마세요.
{{ANGLE_RULES}}
## 출력
JSON 배열만 출력하세요. 설명, 코드 펜스, 서문 금지. claim과 suggested_fix는 {{LANGUAGE_NAME}}로 작성하세요.
항목 필드: file, line_start, line_end, category, severity, claim, evidence, suggested_fix
- category: security (고정)
- severity: important 또는 pre_existing. 보안 지적에 nit은 쓰지 않습니다
- file: 레포 루트 기준 상대 경로
"""


VERIFY_TEMPLATE = """당신은 코드 리뷰 지적을 검증하는 검증자입니다. 아래 지적이 실제 문제인지 판정하세요.
당신의 역할은 지적을 지지하는 것이 아니라 반증을 찾아보는 것입니다.

## 검증 대상
{{FINDING_JSON}}

## 당신의 확인 항목: {{CHECK_NAME}}
{{CHECK_DESC}}

## 작업 방식
1. 레포 루트의 {{AGENTS_NAME}}와 REVIEW.md를 읽으세요.
2. `git diff {{DIFF_RANGE}}`와 해당 파일, 호출처, 관련 테스트를 직접 열어 확인하세요.
3. 지적에 적힌 근거 코드가 그 위치에 실제로 있는지부터 확인하세요.
4. 읽기 전용입니다. 파일을 수정하지 마세요.

## 판정 기준
- real: 확인 항목 기준으로 지적이 코드로 뒷받침되고 반증을 찾지 못했다.
- not_real: 근거 코드가 없거나 다르다, 의도된 동작이다, 이미 다른 곳에서 처리된다, 도달할 수 없다, 영향이 없다 중 하나라도 해당한다.
- 증거가 불충분해 판단할 수 없으면 not_real.

## 출력
JSON 객체 하나만 출력하세요. reason은 {{LANGUAGE_NAME}} 한 줄입니다.
{"verdict": "real | not_real", "reason": "...", "checked": "파일:라인 목록"}
"""


SECURITY_VERIFY_TEMPLATE = """당신은 보안 지적을 검증하는 검증자입니다. 아래 지적이 실제로 악용 가능한지 판정하세요.
당신의 역할은 지적을 지지하는 것이 아니라 반증을 찾아보는 것입니다.

## 검증 대상
{{FINDING_JSON}}

## 당신의 확인 항목: {{CHECK_NAME}}
{{CHECK_DESC}}

## 판정 원칙
- **개발자가 의도한 구현인지는 판정 근거가 아닙니다.** 취약점은 대부분 의도적으로 작성된,
  지금 정상 동작하는 코드입니다. "의도된 동작이다", "주변 코드도 같은 방식이다",
  "테스트가 통과한다", "PR 설명에 적혀 있다"는 real/not_real 어느 쪽의 근거도 아닙니다.
- 클라이언트 측 제한(프론트엔드 드롭다운, 모바일 앱 검증)은 방어가 아닙니다. 공격자는
  클라이언트를 거치지 않습니다.
- 코드에서 확인되지 않는 네트워크·인프라 가정("내부망이라 안전하다", "WAF가 막는다",
  "이 파일은 내부 전용이다")은 방어가 아닙니다. 주석과 파일명은 네트워크 사실이 아닙니다.
- 방어로 인정하려면 그 방어를 `파일:라인`으로 가리킬 수 있어야 합니다.

## 작업 방식
1. 레포 루트의 {{AGENTS_NAME}}, REVIEW.md, SECURITY-REVIEW.md를 읽으세요.
2. `git diff {{DIFF_RANGE}}`와 해당 파일, 호출처, 관련 테스트를 직접 열어 확인하세요.
3. 지적에 적힌 근거 코드가 그 위치에 실제로 있는지부터 확인하세요.
4. 읽기 전용입니다. 파일을 수정하지 마세요. 취약점을 실제로 악용해 보려 하지 마세요.

## 판정 기준
- real: 이 확인 항목 기준으로 악용 가능성이 코드로 뒷받침되고 반증을 찾지 못했다.
- not_real: {{CHECK_REFUTATION}}
- 증거가 불충분해 판단할 수 없으면 not_real.

## 출력
JSON 객체 하나만 출력하세요. reason은 {{LANGUAGE_NAME}} 한 줄입니다.
{"verdict": "real | not_real", "reason": "...", "checked": "파일:라인 목록"}
"""


REREVIEW_TEMPLATE = """
## 재리뷰 지침 (라운드 {{ROUND}})

이 PR은 이미 리뷰를 받았습니다. 아래 규칙을 우선 적용하세요.

- 변경 범위: 마지막으로 리뷰한 커밋 이후의 변경만 봅니다. `git diff {{DIFF_RANGE}}`
- 반복 금지: 아래 목록에 있는 지적과 같은 내용은 다시 보고하지 않습니다.
{{PREVIOUS_FINDINGS}}
- 이번 커밋이 새로 만든 문제와, 이전 지적을 고치다가 생긴 회귀를 우선 찾으세요.
- 같은 유형의 버그를 이전에 지적한 적이 있다면, 그 유형의 남은 인스턴스를 모두 찾아 하나의 finding으로 묶으세요.
- 새로 보고할 것이 없으면 `[]`을 출력하세요.
"""


CONTEXT_TEMPLATE = """
## 레포 컨텍스트
아래는 대상 레포가 제공한 지침 원문입니다. 엔진 지시와 충돌하면 레포 지침을 따르세요.

### {{AGENTS_NAME}}
{{AGENTS_BODY}}

### REVIEW.md
{{REVIEW_BODY}}
"""


# 보안 프롬프트에만 덧붙는다. 범용 프롬프트에 넣으면 범용 리뷰어가 보안 지적을 하게 된다.
SECURITY_CONTEXT_TEMPLATE = """
### SECURITY-REVIEW.md
아래는 이 레포의 보안 규칙 원문입니다. 일반적인 보안 통념과 충돌하면 이 문서를 따르세요.

{{SECURITY_BODY}}
"""


REPAIR_SUFFIX = """

## 재출력 요청
이전 출력이 JSON 파싱에 실패했습니다. 설명이나 코드 펜스 없이 {{SHAPE}}만 다시 출력하세요.
"""

LANGUAGE_NAMES: dict[str, str] = {
    "ko": "한국어",
    "en": "영어",
    "ja": "일본어",
}


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(str(code or "").lower(), str(code or "한국어"))


def render(template: str, variables: Mapping[str, str]) -> str:
    """`{{VAR}}` 치환. JSON 중괄호와 충돌하지 않도록 str.format을 쓰지 않는다."""

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise KeyError(f"프롬프트 변수 누락: {key}")
        return str(variables[key])

    return _PLACEHOLDER_RE.sub(substitute, template)


def select_angles(count: int) -> list[Angle]:
    """N개의 범용 탐지 관점을 고른다. N이 관점 수보다 크면 순환한다."""
    if count <= 0:
        return []
    return [ANGLES[index % len(ANGLES)] for index in range(count)]


class UnknownAngleError(ValueError):
    """review.yaml이 등록되지 않은 관점 id를 가리킬 때."""


def lookup_angles(ids: Sequence[str]) -> list[Angle]:
    """id 목록을 관점으로 바꾼다. 순수 함수. 알 수 없는 id는 UnknownAngleError."""
    angles: list[Angle] = []
    for raw in ids:
        key = str(raw or "").strip()
        if not key:
            continue
        angle = ANGLE_REGISTRY.get(key)
        if angle is None:
            raise UnknownAngleError(
                f"알 수 없는 관점 id: {key!r} "
                f"(가능한 값: {', '.join(ANGLE_REGISTRY)})"
            )
        if angle not in angles:
            angles.append(angle)
    return angles


def resolve_angles(
    angles: Sequence[str] = (),
    security_angles: Sequence[str] = (),
    detectors: int = len(GENERAL_ANGLES),
) -> list[Angle]:
    """이번 실행에서 돌릴 관점을 정한다. 순수 함수.

    두 가지 경로가 있다.

    - `angles`를 적으면 그 목록이 전부다 (전체 치환 탈출구). 범용 관점이 빠질 수도 있으므로
      의도한 경우에만 쓴다.
    - 적지 않으면 범용 관점 `detectors`개 + `security_angles`를 덧붙인다. 보안 관점을
      켜면서 범용 리뷰를 실수로 끄는 일이 생기지 않게 하는 쪽이 기본 경로다.
    """
    if angles:
        return lookup_angles(angles)
    return select_angles(detectors) + lookup_angles(security_angles)


def select_checks(count: int) -> list[Check]:
    """M개의 범용 확인 항목을 고른다. M이 항목 수보다 크면 순환한다."""
    if count <= 0:
        return []
    return [CHECKS[index % len(CHECKS)] for index in range(count)]


class UnknownCheckError(ValueError):
    """review.yaml이 등록되지 않은 확인 항목 id를 가리킬 때."""


def lookup_checks(ids: Sequence[str]) -> list[Check]:
    """id 목록을 확인 항목으로 바꾼다. 순수 함수. 순환하지 않는다."""
    checks: list[Check] = []
    for raw in ids:
        key = str(raw or "").strip()
        if not key:
            continue
        check = CHECK_REGISTRY.get(key)
        if check is None:
            raise UnknownCheckError(
                f"알 수 없는 확인 항목 id: {key!r} "
                f"(가능한 값: {', '.join(CHECK_REGISTRY)})"
            )
        if check not in checks:
            checks.append(check)
    return checks


def resolve_checks(
    category: str,
    verifiers: int = len(GENERAL_CHECKS),
    security_checks: Sequence[str] = SECURITY_CHECK_IDS,
) -> list[Check]:
    """finding 하나를 검증할 확인 항목을 정한다. 순수 함수.

    보안 finding은 보안 체크를 받고 **순환하지 않는다**. 세 항목이 독립적인 필요조건이라
    같은 항목을 두 번 뽑으면 그 항목에 가중치가 실려 의미가 깨진다.

    그 외 finding은 기존 동작을 그대로 쓴다 (`verifiers`개, 부족하면 순환).
    """
    if str(category or "").strip().lower() == SECURITY_CATEGORY:
        return lookup_checks(security_checks)
    return select_checks(verifiers)


def _doc_or_placeholder(body: str, name: str) -> str:
    body = (body or "").strip()
    if body:
        return body
    return f"({name} 없음 — 기본 지침으로 진행하세요.)"


def render_context_block(
    agents_md: str,
    review_md: str,
    agents_name: str = "AGENTS.md",
    security_md: Optional[str] = None,
) -> str:
    """레포 지침 블록.

    `security_md`가 None이면 보안 문서 블록을 넣지 않는다 (범용 프롬프트). 문자열이면
    넣는다 — 빈 문자열이어도 "없음" 자리표시자로 넣어, 보안 리뷰어가 그 문서를 찾아야
    한다는 사실 자체는 알게 한다.
    """
    block = render(
        CONTEXT_TEMPLATE,
        {
            "AGENTS_NAME": agents_name or "AGENTS.md",
            "AGENTS_BODY": _doc_or_placeholder(agents_md, agents_name or "AGENTS.md"),
            "REVIEW_BODY": _doc_or_placeholder(review_md, "REVIEW.md"),
        },
    )
    if security_md is None:
        return block
    return block + render(
        SECURITY_CONTEXT_TEMPLATE,
        {"SECURITY_BODY": _doc_or_placeholder(security_md, "SECURITY-REVIEW.md")},
    )


def format_previous_findings(previous: Sequence[PostedRef]) -> str:
    if not previous:
        return "  (이전 라운드에 게시한 지적 없음)"
    lines = []
    for ref in previous:
        claim = (ref.claim or "").strip().replace("\n", " ")
        if len(claim) > 160:
            claim = claim[:157] + "..."
        lines.append(
            f"  - {ref.file}:{ref.line_start}-{ref.line_end} [{ref.category}] {claim}"
        )
    return "\n".join(lines)


def build_detect_prompt(
    *,
    angle: Angle,
    diff_range: str,
    pr_title: str,
    pr_description: str,
    agents_md: str,
    review_md: str,
    agents_name: str = "AGENTS.md",
    security_md: str = "",
    max_nits: int = 3,
    language: str = "ko",
    intensity: str = "standard",
    round_number: int = 1,
    previous_findings: Sequence[PostedRef] = (),
) -> str:
    """탐지 프롬프트. 라운드 2 이상이면 재리뷰 지침을 덧붙인다."""
    description = (pr_description or "").strip() or "(설명 없음)"
    if len(description) > 4000:
        description = description[:4000] + "\n...(생략)"

    template = SECURITY_DETECT_TEMPLATE if angle.is_security else DETECT_TEMPLATE
    prompt = render(
        template,
        {
            "ANGLE_NAME": angle.name,
            "ANGLE_DESC": angle.description,
            "ANGLE_RULES": angle.extra_rules,
            "DIFF_RANGE": diff_range,
            "PR_TITLE": (pr_title or "").strip() or "(제목 없음)",
            "PR_DESCRIPTION": description,
            "AGENTS_NAME": agents_name or "AGENTS.md",
            "MAX_NITS": str(max_nits),
            "LANGUAGE_NAME": language_name(language),
            "INTENSITY_NOTE": INTENSITY_NOTES.get(intensity, ""),
        },
    )

    if round_number > 1:
        prompt += render(
            REREVIEW_TEMPLATE,
            {
                "ROUND": str(round_number),
                "DIFF_RANGE": diff_range,
                "PREVIOUS_FINDINGS": format_previous_findings(previous_findings),
            },
        )

    prompt += render_context_block(
        agents_md,
        review_md,
        agents_name,
        security_md=security_md if angle.is_security else None,
    )
    return prompt


def build_verify_prompt(
    *,
    check: Check,
    finding: Finding,
    diff_range: str,
    agents_md: str,
    review_md: str,
    agents_name: str = "AGENTS.md",
    security_md: str = "",
    language: str = "ko",
) -> str:
    """검증 프롬프트. 탐지 에이전트의 추론 과정은 넘기지 않고 finding만 넘긴다."""
    payload = {
        "file": finding.file,
        "line_start": finding.line_start,
        "line_end": finding.line_end,
        "category": finding.category,
        "severity": finding.severity,
        "claim": finding.claim,
        "evidence": finding.evidence,
    }
    is_security = finding.category == SECURITY_CATEGORY
    prompt = render(
        SECURITY_VERIFY_TEMPLATE if is_security else VERIFY_TEMPLATE,
        {
            "FINDING_JSON": json.dumps(payload, ensure_ascii=False, indent=2),
            "CHECK_NAME": check.name,
            "CHECK_DESC": check.description,
            "CHECK_REFUTATION": check.refutation,
            "DIFF_RANGE": diff_range,
            "AGENTS_NAME": agents_name or "AGENTS.md",
            "LANGUAGE_NAME": language_name(language),
        },
    )
    prompt += render_context_block(
        agents_md,
        review_md,
        agents_name,
        security_md=security_md if is_security else None,
    )
    return prompt


def build_repair_prompt(original: str, shape: str) -> str:
    return original + render(REPAIR_SUFFIX, {"SHAPE": shape})


def angle_names(angles: Iterable[Angle]) -> list[str]:
    return [angle.name for angle in angles]


def find_angle(name: str) -> Optional[Angle]:
    """표시명으로 관점을 찾는다. 범용·보안 관점을 모두 본다."""
    return next(
        (angle for angle in ANGLE_REGISTRY.values() if angle.name == name), None
    )
