"""PR #153을 실제 Codex로 두 번 리뷰해 얻은 finding을 테스트 픽스처로 고정한다.

유사도 임계값과 병합 규칙은 이 실제 문장들로 검증한다. 합성 예제로 맞춰 놓은 임계값은
현장에서 쓸모가 없다는 것을 한 번 겪었다(계획 단계에서 추측한 0.55는 실측 0.243 대비
너무 높아 아무것도 접히지 않았을 값이다).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from review_agent.models import Finding

ANGLE_LOGIC = "로직·엣지케이스"
ANGLE_CONC = "동시성·자원"
ANGLE_API = "API·계약 변경"


@dataclass(frozen=True)
class Sample:
    label: str
    file: str
    line_start: int
    line_end: int
    category: str
    severity: str
    claim: str
    suggested_fix: str
    evidence: str = ""
    detected_by: tuple[str, ...] = field(default_factory=tuple)

    def to_finding(self, finding_id: str = "") -> Finding:
        return Finding(
            file=self.file,
            line_start=self.line_start,
            line_end=self.line_end,
            category=self.category,
            severity=self.severity,
            claim=self.claim,
            evidence=self.evidence or f"{self.file}:{self.line_start} 근거 코드",
            suggested_fix=self.suggested_fix,
            detected_by=list(self.detected_by or (ANGLE_LOGIC,)),
            id=finding_id,
        )


ADMIN_SERVICE = "src/main/java/com/mozi/moziserver/adminService/AdminConfirmService.java"
ASYNC_SERVICE = "src/main/java/com/mozi/moziserver/service/AsyncService.java"
CONFIRM_SERVICE = "src/main/java/com/mozi/moziserver/service/ConfirmService.java"
BLOCK_HISTORY = "src/main/java/com/mozi/moziserver/model/entity/ConfirmBlockHistory.java"
SLACK_SERVICE = "src/main/java/com/mozi/moziserver/service/SlackNotiService.java"

# ---- 트랜잭션 경계 누락: 같은 버그를 세 번 다르게 서술한 것 ----

TX_RUN_A = Sample(
    label="run A f-003",
    file=ADMIN_SERVICE,
    line_start=38,
    line_end=44,
    category="concurrency",
    severity="important",
    claim=(
        "인증 BLOCKED 상태 저장과 차단 히스토리 저장이 하나의 트랜잭션이 아니어서, 상태만 "
        "BLOCKED이고 히스토리가 없는 불일치가 발생할 수 있습니다."
    ),
    suggested_fix=(
        "updateConfirmState를 @Transactional로 묶어 상태 전이와 히스토리 삽입을 원자적으로 "
        "처리하세요. Slack 전송은 커밋 이후 이벤트로 분리해 외부 호출 실패가 DB 일관성에 "
        "영향을 주지 않게 하세요."
    ),
    detected_by=(ANGLE_CONC,),
)

TX_RUN_B_DATA = Sample(
    label="run B f-002 (category=data)",
    file=ADMIN_SERVICE,
    line_start=38,
    line_end=44,
    category="data",
    severity="important",
    claim=(
        "인증 상태 변경과 차단 히스토리 저장이 하나의 트랜잭션이 아니어서, 히스토리 저장 "
        "실패 시 인증만 BLOCKED로 남고 감사 이력이 유실됩니다."
    ),
    suggested_fix=(
        "updateConfirmState를 트랜잭션으로 묶어 상태 갱신과 히스토리 저장이 함께 커밋되거나 "
        "함께 롤백되게 하세요."
    ),
    detected_by=(ANGLE_LOGIC,),
)

TX_RUN_B_CONC = Sample(
    label="run B f-003 (category=concurrency)",
    file=ADMIN_SERVICE,
    line_start=38,
    line_end=44,
    category="concurrency",
    severity="important",
    claim=(
        "인증 상태 변경과 차단 이력 저장이 하나의 트랜잭션이 아니므로, 이력 저장이 실패하면 "
        "인증만 BLOCKED로 커밋되어 요구된 이력이 남지 않는 부분 완료 상태가 됩니다."
    ),
    suggested_fix=(
        "상태 변경과 ConfirmBlockHistory 저장을 포함하는 서비스 메서드에 @Transactional을 "
        "적용하세요. 이력 insert가 실패하면 상태 변경도 함께 롤백되도록 하고, Slack 전송은 "
        "커밋 성공 후에 실행하세요."
    ),
    detected_by=(ANGLE_CONC,),
)

# ---- 비동기 콜백이 상태를 덮어씀 ----

ASYNC_RUN_A = Sample(
    label="run A f-001",
    file=ASYNC_SERVICE,
    line_start=105,
    line_end=107,
    category="concurrency",
    severity="important",
    claim=(
        "LLM 응답이 도착하기 전에 사용자가 인증을 삭제하면, 비동기 작업이 DELETED 상태를 "
        "무조건 BLOCKED로 덮어써 삭제 상태가 유실됩니다."
    ),
    suggested_fix=(
        "상태 갱신을 DELETED가 아닌 경우에만 수행하는 조건부 update로 바꾸고, 실제로 상태가 "
        "변경된 경우에만 히스토리와 슬랙 알림을 생성하세요."
    ),
    detected_by=(ANGLE_LOGIC, ANGLE_CONC),
)

ASYNC_RUN_B = Sample(
    label="run B f-001",
    file=ASYNC_SERVICE,
    line_start=103,
    line_end=107,
    category="concurrency",
    severity="important",
    claim=(
        "비동기 LLM 콜백이 그 사이 삭제되거나 관리자에 의해 상태 변경된 인증의 상태를 "
        "무조건 BLOCKED로 덮어씁니다."
    ),
    suggested_fix=(
        "BLOCK 처리 시 현재 상태가 ACTIVE/REPORTED인 경우에만 조건부로 갱신하도록 repository "
        "update 조건을 추가하고, 조건부 갱신이 성공했을 때만 히스토리와 슬랙 알림을 남기세요."
    ),
    detected_by=(ANGLE_LOGIC, ANGLE_CONC, ANGLE_API),
)

# ---- 새 엔티티에 필요한 DDL 누락 ----

DDL_RUN_A = Sample(
    label="run A f-002",
    file=BLOCK_HISTORY,
    line_start=14,
    line_end=15,
    category="data",
    severity="important",
    claim=(
        "새 히스토리 엔티티에 대응하는 스키마 마이그레이션이 없어, 기존 운영/개발 DB에서는 "
        "첫 BLOCK 처리 시 테이블 미존재 오류가 발생합니다."
    ),
    suggested_fix=(
        "배포 DB에 confirm_block_history 테이블과 필요한 인덱스를 생성하는 버전 관리 "
        "마이그레이션을 추가하고 배포 절차에 포함하세요."
    ),
    detected_by=(ANGLE_LOGIC, ANGLE_API),
)

DDL_RUN_B = Sample(
    label="run B f-004",
    file=BLOCK_HISTORY,
    line_start=14,
    line_end=20,
    category="data",
    severity="important",
    claim=(
        "새 히스토리 엔티티에 필요한 운영/개발 DB DDL이 변경에 포함되어 있지 않아, 테이블이 "
        "배포 전에 수동 생성되지 않으면 첫 BLOCK 처리에서 INSERT가 실패합니다."
    ),
    suggested_fix="배포 절차에 confirm_block_history 테이블 생성 DDL을 추가하세요.",
    detected_by=(ANGLE_API,),
)

# ---- 롤백돼도 비동기 작업을 제출 ----

ROLLBACK_RUN_A = Sample(
    label="run A f-004",
    file=CONFIRM_SERVICE,
    line_start=100,
    line_end=100,
    category="concurrency",
    severity="important",
    claim=(
        "인증 생성 트랜잭션이 롤백되어도 LLM 검증 작업을 제출하므로, 존재하지 않는 인증에 "
        "대해 외부 API 자원을 소비하고 비동기 작업이 실패합니다."
    ),
    suggested_fix="트랜잭션 실패를 호출자에게 전파하고, 커밋 성공 후에만 검증을 제출하세요.",
    detected_by=(ANGLE_CONC,),
)

# ---- Slack 본문 JSON 이스케이프 (nit) ----

SLACK_RUN_B = Sample(
    label="run B f-005",
    file=SLACK_SERVICE,
    line_start=16,
    line_end=20,
    category="api_contract",
    severity="nit",
    claim=(
        "Slack 요청 본문을 문자열 보간으로 조립해 챌린지 이름에 큰따옴표·역슬래시·개행이 "
        "있으면 JSON이 깨지고 웹훅 알림이 실패합니다."
    ),
    suggested_fix=(
        "문자열 조립 대신 Map.of(text, message) 또는 별도 요청 DTO를 SlackClient에 전달해 "
        "Jackson/Feign이 JSON 이스케이프를 수행하도록 변경하세요."
    ),
    detected_by=(ANGLE_API,),
)

# ---- 합성 픽스처: 같은 위치·같은 category인데 서로 다른 버그 ----
# 실제 두 실행에는 이런 쌍이 없었다. 1단 병합이 내용을 보지 않으면 조용히 사라지는
# 경우를 재현하기 위해 만든 것이다.

ASYNC_SAME_SPOT_OTHER_BUG = Sample(
    label="합성: 같은 위치·같은 category, 다른 버그",
    file=ASYNC_SERVICE,
    line_start=105,
    line_end=105,
    category="concurrency",
    severity="important",
    claim=(
        "LLM 응답 객체를 널 체크 없이 역참조하므로 외부 호출이 타임아웃으로 빈 응답을 "
        "돌려주면 NullPointerException으로 비동기 스레드가 죽습니다."
    ),
    suggested_fix="응답이 널이거나 본문이 비어 있으면 재시도 큐로 보내고 조기 반환하세요.",
    detected_by=(ANGLE_CONC,),
)


# ---- 유사도 검증용 쌍 ----

SAME_BUG_PAIRS: tuple[tuple[str, Sample, Sample], ...] = (
    ("같은 버그·다른 category (2단 핵심 대상)", TX_RUN_B_DATA, TX_RUN_B_CONC),
    ("같은 버그·실행 간 다른 표현 (트랜잭션)", TX_RUN_A, TX_RUN_B_CONC),
    ("같은 버그·실행 간 다른 표현 (트랜잭션 2)", TX_RUN_A, TX_RUN_B_DATA),
    ("같은 버그·실행 간 다른 표현 (비동기)", ASYNC_RUN_A, ASYNC_RUN_B),
    ("같은 버그·실행 간 다른 표현 (DDL)", DDL_RUN_A, DDL_RUN_B),
)

DIFFERENT_BUG_PAIRS: tuple[tuple[str, Sample, Sample], ...] = (
    ("트랜잭션 vs 비동기", TX_RUN_B_CONC, ASYNC_RUN_B),
    ("트랜잭션 vs DDL", TX_RUN_B_CONC, DDL_RUN_B),
    ("트랜잭션 vs 롤백후제출", TX_RUN_B_CONC, ROLLBACK_RUN_A),
    ("트랜잭션 vs Slack", TX_RUN_B_CONC, SLACK_RUN_B),
    ("비동기 vs DDL", ASYNC_RUN_B, DDL_RUN_B),
    ("비동기 vs 롤백후제출", ASYNC_RUN_B, ROLLBACK_RUN_A),
    ("비동기 vs Slack", ASYNC_RUN_B, SLACK_RUN_B),
    ("DDL vs 롤백후제출", DDL_RUN_B, ROLLBACK_RUN_A),
    ("DDL vs Slack", DDL_RUN_B, SLACK_RUN_B),
    ("롤백후제출 vs Slack", ROLLBACK_RUN_A, SLACK_RUN_B),
    ("같은 위치·같은 category, 다른 버그", ASYNC_RUN_B, ASYNC_SAME_SPOT_OTHER_BUG),
)

# 위 쌍들을 claim만으로 재서 얻은 실측값. 임계값 기본값의 근거다.
MEASURED_SAME_BUG_MIN = 0.243
MEASURED_DIFFERENT_BUG_MAX = 0.122
