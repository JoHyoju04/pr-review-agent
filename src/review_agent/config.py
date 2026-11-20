"""review.yaml 로더와 기본값."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional

from . import miniyaml, prompts
from .gating import GateRules

logger = logging.getLogger(__name__)

CONFIG_FILENAMES = ("review.yaml", "review.yml", ".review.yaml")

# Codex CLI 비대화형 실행 기본 커맨드 (codex-cli 0.167 기준).
#   --sandbox read-only          : 모델이 실행하는 셸 명령을 읽기 전용으로 제한
#   --cd {repo}                  : 리뷰 대상 트리를 작업 루트로
#   --color never                : stdout에 ANSI 이스케이프가 섞이지 않게
#   -c model={model}             : 모델을 고정한다. 안 주면 사용자 ~/.codex/config.toml을
#                                  상속해 앱 설정을 바꾸는 순간 리뷰 결과가 조용히 바뀐다
#   -c model_reasoning_effort=…  : 추론 강도도 같은 이유로 고정
#   -o {out}                     : 최종 메시지만 이 파일에 받는다 (stdout에는 진행 로그와
#                                  프롬프트 에코가 섞이므로 파일 쪽이 훨씬 깔끔하다)
#   -                            : 프롬프트를 stdin으로 읽는다
# CLI 버전에 따라 플래그가 다르므로 review.yaml의 llm.command로 교체할 수 있다.
DEFAULT_LLM_COMMAND: list[str] = [
    "codex",
    "exec",
    "--sandbox",
    "read-only",
    "--cd",
    "{repo}",
    "--color",
    "never",
    "-c",
    "model={model}",
    "-c",
    "model_reasoning_effort={effort}",
    "-o",
    "{out}",
    "-",
]

# 고정 기본값. review.yaml의 llm.model / llm.reasoning_effort로 바꾼다.
DEFAULT_MODEL = "gpt-5.6-terra"
DEFAULT_REASONING_EFFORT = "medium"

# 호출 수와 호출당 비용을 함께 조절한다. 리뷰 한 번의 LLM 호출 수는
# detectors + (병합 후 finding 수 x verifiers) 이고, 호출 하나하나가 독립된 에이전트
# 실행(레포를 다시 읽는다)이므로 추론 강도까지 같이 낮춰야 체감 속도가 바뀐다.
INTENSITY_PRESETS: dict[str, dict[str, int]] = {
    "quick": {"detectors": 1, "verifiers": 1, "pass_threshold": 1},
    "standard": {"detectors": 3, "verifiers": 3, "pass_threshold": 2},
    "deep": {"detectors": 3, "verifiers": 5, "pass_threshold": 3},
}

# intensity가 함께 정하는 추론 강도. llm.reasoning_effort를 직접 적으면 그 값이 이긴다.
INTENSITY_EFFORT: dict[str, str] = {
    "quick": "low",
    "standard": "medium",
    "deep": "high",
}


# Claude Code CLI 비대화형 실행 기본 커맨드 (claude 2.1.322 기준).
#   --print                  : 비대화형. 최종 응답만 stdout으로 내보낸다
#   --output-format text     : stdout에 진행 로그가 섞이지 않는다 (codex의 -o {out} 불필요)
#   --model {model}          : 모델 고정. 별칭(opus/sonnet)은 가리키는 모델이 바뀌므로 쓰지 않는다
#   --effort {effort}        : 추론 강도 고정 (low|medium|high|xhigh|max)
#   --permission-prompts none: 승인이 필요한 동작은 묻지 않고 바로 거부한다. CI에서 멈추지 않는다
#   --allowedTools           : 읽기 전용 집합. 프롬프트가 `git diff`를 직접 돌리게 하므로 git 읽기 명령을 허용한다
#   --disallowedTools        : 쓰기·네트워크 도구를 명시적으로 막는다 (읽기 전용 불변식)
#   {stdin}                  : 프롬프트를 stdin으로 보낸다. 이 토큰은 argv에서 제거된다
# 결정성 플래그(--safe-mode)는 프로필별 extra_args로 붙는다. codex의 --ignore-user-config와 같은 역할이다.
CLAUDE_LLM_COMMAND: list[str] = [
    "claude",
    "--print",
    "--output-format",
    "text",
    "--model",
    "{model}",
    "--effort",
    "{effort}",
    "--permission-prompts",
    "none",
    "--allowedTools",
    "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*),"
    "Bash(git blame:*),Bash(git status:*)",
    "--disallowedTools",
    "Write,Edit,NotebookEdit,WebFetch,WebSearch",
    "{stdin}",
]

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"


@dataclass(frozen=True)
class BackendPreset:
    """백엔드 하나의 커맨드 템플릿과 인증 방법.

    프로젝트별 설정이 아니라 **백엔드** 특성이므로 엔진에 둔다. 순수 데이터이므로
    LLM 없이 단위 테스트된다.
    """

    name: str
    command: tuple[str, ...]
    model: str
    # 프로필별 결정성 플래그. llm.extra_args를 직접 적으면 그 값이 이긴다.
    extra_args_local: tuple[str, ...]
    extra_args_ci: tuple[str, ...]
    # 이 중 하나라도 환경변수에 있으면 인증된 것으로 본다 (키/토큰 경로).
    auth_env: tuple[str, ...]
    # 로컬에서 구독 로그인 여부를 확인하는 읽기 전용 커맨드.
    login_check: tuple[str, ...]
    # login_check 출력에서 찾을 문자열. 공백을 제거한 출력에서도 함께 찾는다.
    login_ok_marker: str
    auth_hint_local: str
    auth_hint_ci: str

    def extra_args_for(self, profile: str) -> list[str]:
        return list(self.extra_args_ci if profile == "ci" else self.extra_args_local)


BACKEND_PRESETS: dict[str, BackendPreset] = {
    "codex": BackendPreset(
        name="codex",
        command=tuple(DEFAULT_LLM_COMMAND),
        model=DEFAULT_MODEL,
        # --ignore-user-config: 개인 ~/.codex/config.toml 상속을 끊는다. 인증은 CODEX_HOME을
        # 계속 쓰므로 구독 로그인과 충돌하지 않는다 (codex exec --help 로 확인).
        extra_args_local=("--ignore-user-config",),
        # --ephemeral: 러너에 세션 파일을 남기지 않는다.
        extra_args_ci=("--ignore-user-config", "--ephemeral"),
        auth_env=("OPENAI_API_KEY",),
        login_check=("codex", "login", "status"),
        login_ok_marker="Logged in",
        auth_hint_local="`codex login` 으로 먼저 로그인하세요 (ChatGPT 계정 = 구독 크레딧).",
        auth_hint_ci=(
            "OPENAI_API_KEY 를 설정하고 "
            "`printenv OPENAI_API_KEY | codex login --with-api-key` 를 먼저 실행하세요."
        ),
    ),
    "claude": BackendPreset(
        name="claude",
        command=tuple(CLAUDE_LLM_COMMAND),
        model=DEFAULT_CLAUDE_MODEL,
        # --safe-mode: CLAUDE.md·스킬·플러그인·훅·MCP·개인 설정을 끈다. 레포 사정은
        # 프롬프트의 AGENTS.md/REVIEW.md 블록으로 들어오므로 끊어도 안전하다.
        extra_args_local=("--safe-mode",),
        # --no-session-persistence: --print 전용. 러너에 세션을 남기지 않는다.
        extra_args_ci=("--safe-mode", "--no-session-persistence"),
        auth_env=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
        login_check=("claude", "auth", "status"),
        login_ok_marker='"loggedIn":true',
        auth_hint_local="`claude auth login` 으로 먼저 로그인하세요 (구독 크레딧).",
        auth_hint_ci=(
            "ANTHROPIC_API_KEY 또는 CLAUDE_CODE_OAUTH_TOKEN 중 하나를 환경변수로 넘기세요."
        ),
    ),
}

DEFAULT_BACKEND = "codex"

# 실행 프로필. 백엔드 기본값과 결정성 플래그, 인증 확인 방법을 함께 정한다.
PROFILES = ("local", "ci")
DEFAULT_PROFILE = "local"
# 이 환경변수 중 하나라도 있으면 ci로 본다. GitHub Actions는 둘 다 설정한다.
CI_ENV_MARKERS = ("GITHUB_ACTIONS", "CI")


def detect_profile(env: Mapping[str, str]) -> str:
    """환경변수로 프로필을 추정한다. 순수 함수."""
    for name in CI_ENV_MARKERS:
        value = str(env.get(name, "")).strip().lower()
        if value and value not in ("0", "false", "no", "off"):
            return "ci"
    return DEFAULT_PROFILE


class ConfigError(ValueError):
    pass


@dataclass
class LLMSettings:
    """LLM 백엔드 실행 설정."""

    command: list[str] = field(default_factory=lambda: list(DEFAULT_LLM_COMMAND))
    timeout: int = 900
    # JSON 파싱 실패 시 재시도 횟수 (요구사항: 1회)
    parse_retries: int = 1
    # 결정성을 위해 고정한다. 커맨드의 {model} / {effort} 자리에 들어간다.
    model: str = DEFAULT_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    # 커맨드 뒤에 덧붙일 백엔드별 인자. 예: ["--ignore-user-config"], ["--ephemeral"]
    extra_args: list[str] = field(default_factory=list)
    # 백엔드 프리셋 이름과 실행 프로필. resolve_llm이 채운다.
    backend: str = DEFAULT_BACKEND
    profile: str = DEFAULT_PROFILE
    # review.yaml llm 블록에 직접 적힌 키. 프리셋보다 우선시킬지 판단할 때 쓴다.
    explicit: set[str] = field(default_factory=set)

    def validate(self) -> None:
        if not self.command:
            raise ConfigError("llm.command가 비어 있습니다")
        if self.timeout <= 0:
            raise ConfigError("llm.timeout은 1 이상이어야 합니다")
        if self.parse_retries < 0:
            raise ConfigError("llm.parse_retries는 0 이상이어야 합니다")
        if not str(self.model).strip():
            raise ConfigError("llm.model이 비어 있습니다")
        if self.backend not in BACKEND_PRESETS:
            raise ConfigError(
                f"llm.backend는 {', '.join(BACKEND_PRESETS)} 중 하나여야 합니다: {self.backend!r}"
            )
        if self.profile not in PROFILES:
            raise ConfigError(
                f"llm.profile은 {', '.join(PROFILES)} 중 하나여야 합니다: {self.profile!r}"
            )


@dataclass
class ReviewConfig:
    # 범용 관점 수. 보안 관점은 security_angles로 이 위에 더해진다.
    detectors: int = 3
    # 보안 관점 id 목록. 비어 있으면 보안 리뷰는 돌지 않는다 (opt-in).
    # 덧붙이기만 하므로 이 키로는 범용 관점을 끌 수 없다.
    security_angles: list[str] = field(default_factory=list)
    # 돌릴 관점을 전부 직접 지정하는 탈출구. 적으면 detectors와 security_angles를 무시한다.
    # 범용 관점이 빠질 수 있으므로 의도한 경우에만 쓴다.
    angles: list[str] = field(default_factory=list)
    verifiers: int = 3
    pass_threshold: int = 2
    # 보안 finding을 검증할 확인 항목. 순환하지 않으므로 이 목록의 길이가 곧 검증자 수다.
    security_checks: list[str] = field(
        default_factory=lambda: list(prompts.SECURITY_CHECK_IDS)
    )
    # 보안 finding의 통과 기준. 적지 않으면 전원 합의(= security_checks 길이)다.
    # 세 체크가 독립적인 필요조건이라 투표가 아니라 AND로 본다.
    security_pass_threshold: int = 3
    max_findings: int = 10
    max_nits: int = 3
    # 게시 상한을 적용할 때 보안 finding에 먼저 떼어 주는 슬롯 수.
    # 보안 지적이 없으면 아무 영향이 없다.
    security_reserved_slots: int = 3
    # 보안 finding의 nit을 important로 올려 nit 상한에서 빼낼지.
    security_severity_floor: bool = True
    max_rounds: int = 3
    intensity: str = "standard"
    comment_language: str = "ko"
    # LLM 호출 동시 실행 상한
    max_parallel: int = 8
    # 병합/중복 판정에 쓰는 라인 근접 허용치
    line_tolerance: int = 3
    # 1단 병합(검증 전, 같은 category)의 claim 유사도 게이트. 느슨하게 둔다.
    # 실측 근거는 similarity.py docstring 참고 (같은 버그 최저 0.243 / 다른 버그 최고 0.122).
    merge_similarity: float = 0.16
    # 2단 중복 제거(검증 후, category 무시)의 claim 유사도 게이트. 더 엄격하게 둔다.
    dedupe_similarity: float = 0.20
    # 같은 위치에 남은 서로 다른 지적을 하나의 코멘트로 묶을지
    group_comments: bool = True
    # REVIEW.md의 "건너뛸 경로"와 합쳐서 쓴다. finding 필터이고 실행 게이트가 아니다.
    skip_paths: list[str] = field(default_factory=list)
    # 실행 게이트. 비어 있으면 평가하지 않는다 (기존 동작).
    gate: GateRules = field(default_factory=GateRules)
    summary_empty: str = "수정할 사항 없음"
    llm: LLMSettings = field(default_factory=LLMSettings)
    source: str = "기본값"
    # review.yaml에 직접 적힌 키. REVIEW.md 값보다 우선시킬지 판단할 때 쓴다.
    explicit_keys: set[str] = field(default_factory=set)

    def resolved_angles(self) -> list[prompts.Angle]:
        """이번 실행에서 돌릴 관점. validate()를 통과한 설정에만 쓴다."""
        return prompts.resolve_angles(self.angles, self.security_angles, self.detectors)

    def security_threshold(self) -> int:
        """보안 finding 통과 기준. 적지 않으면 전원 합의.

        `resolve_max_nits`와 같은 방식으로 명시값을 우선한다. security_checks를 줄여도
        기본값이 따라가므로 "체크 2개 + 임계값 3" 같은 설정 오류가 생기지 않는다.
        """
        if "security_pass_threshold" in self.explicit_keys:
            return self.security_pass_threshold
        return max(1, len(self.security_checks))

    def validate(self) -> None:
        if self.detectors < 1:
            raise ConfigError("detectors는 1 이상이어야 합니다")
        for key in ("angles", "security_angles"):
            try:
                resolved = prompts.lookup_angles(getattr(self, key))
            except prompts.UnknownAngleError as exc:
                raise ConfigError(f"{key}: {exc}") from None
            if key == "security_angles":
                general = [angle.id for angle in resolved if not angle.is_security]
                if general:
                    raise ConfigError(
                        "security_angles에는 보안 관점만 적을 수 있습니다: "
                        f"{', '.join(general)} "
                        f"(보안 관점: {', '.join(prompts.SECURITY_ANGLE_IDS)}). "
                        "범용 관점까지 직접 고르려면 angles를 쓰세요."
                    )
        if self.angles and self.security_angles:
            logger.warning(
                "angles를 적었으므로 security_angles는 무시됩니다: %s",
                ", ".join(self.security_angles),
            )
        if self.angles and "detectors" in self.explicit_keys:
            logger.warning("angles를 적었으므로 detectors는 무시됩니다: %s", self.detectors)
        try:
            checks = prompts.lookup_checks(self.security_checks)
        except prompts.UnknownCheckError as exc:
            raise ConfigError(f"security_checks: {exc}") from None
        if not checks:
            raise ConfigError("security_checks가 비어 있습니다")
        if not 1 <= self.security_threshold() <= len(checks):
            raise ConfigError(
                "security_pass_threshold는 1 이상 security_checks 수"
                f"({len(checks)}) 이하여야 합니다: {self.security_threshold()}"
            )
        if self.verifiers < 1:
            raise ConfigError("verifiers는 1 이상이어야 합니다")
        if not 1 <= self.pass_threshold <= self.verifiers:
            raise ConfigError(
                f"pass_threshold는 1 이상 verifiers({self.verifiers}) 이하여야 합니다"
            )
        if self.max_findings < 0:
            raise ConfigError("max_findings는 0 이상이어야 합니다")
        if self.max_nits < 0:
            raise ConfigError("max_nits는 0 이상이어야 합니다")
        if self.security_reserved_slots < 0:
            raise ConfigError("security_reserved_slots는 0 이상이어야 합니다")
        if self.max_rounds < 1:
            raise ConfigError("max_rounds는 1 이상이어야 합니다")
        if self.intensity not in INTENSITY_PRESETS:
            raise ConfigError(
                f"intensity는 {', '.join(INTENSITY_PRESETS)} 중 하나여야 합니다: {self.intensity!r}"
            )
        if self.max_parallel < 1:
            raise ConfigError("max_parallel은 1 이상이어야 합니다")
        if self.line_tolerance < 0:
            raise ConfigError("line_tolerance는 0 이상이어야 합니다")
        for name in ("merge_similarity", "dedupe_similarity"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ConfigError(f"{name}은 0.0 이상 1.0 이하여야 합니다: {value}")
        if self.dedupe_similarity < self.merge_similarity:
            raise ConfigError(
                "dedupe_similarity는 merge_similarity보다 작을 수 없습니다 "
                f"({self.dedupe_similarity} < {self.merge_similarity}). "
                "2단은 1단보다 엄격해야 합니다."
            )
        self.llm.validate()


_INT_KEYS = (
    "detectors",
    "verifiers",
    "pass_threshold",
    "max_findings",
    "max_nits",
    "max_rounds",
    "max_parallel",
    "security_pass_threshold",
    "security_reserved_slots",
    "line_tolerance",
)
_FLOAT_KEYS = ("merge_similarity", "dedupe_similarity")
_BOOL_KEYS = ("group_comments", "security_severity_floor")
_STR_KEYS = ("intensity", "comment_language", "summary_empty")
_LLM_KEYS = (
    "command",
    "timeout",
    "parse_retries",
    "model",
    "reasoning_effort",
    "extra_args",
    "backend",
    "profile",
)
_STR_LIST_KEYS = ("skip_paths", "security_angles", "angles", "security_checks")
_KNOWN_KEYS = (
    set(_INT_KEYS)
    | set(_FLOAT_KEYS)
    | set(_BOOL_KEYS)
    | set(_STR_KEYS)
    | set(_STR_LIST_KEYS)
    | {"llm", "gate"}
)

_GATE_KEYS = ("always_review", "deep_paths", "skip_if_only")


def _as_int(key: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str, float)):
        raise ConfigError(f"{key}는 정수여야 합니다: {value!r}")
    try:
        return int(str(value).strip())
    except ValueError:
        raise ConfigError(f"{key}는 정수여야 합니다: {value!r}") from None


def _as_float(key: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, str, float)):
        raise ConfigError(f"{key}는 숫자여야 합니다: {value!r}")
    try:
        return float(str(value).strip())
    except ValueError:
        raise ConfigError(f"{key}는 숫자여야 합니다: {value!r}") from None


def _as_bool(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in ("true", "yes", "on", "1"):
        return True
    if token in ("false", "no", "off", "0"):
        return False
    raise ConfigError(f"{key}는 true 또는 false여야 합니다: {value!r}")


def _as_str_list(key: str, value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ConfigError(f"{key}는 문자열 리스트여야 합니다: {value!r}")


def config_from_mapping(data: Optional[Mapping[str, Any]], source: str = "기본값") -> ReviewConfig:
    """review.yaml에서 읽은 맵을 ReviewConfig로 변환한다.

    intensity 프리셋을 먼저 적용하고, 파일에 명시된 키가 프리셋을 덮어쓴다.
    """
    config = ReviewConfig(source=source)
    if not data:
        # review.yaml이 없어도 백엔드 프리셋과 프로필은 적용한다.
        config.llm = resolve_llm(config)
        config.validate()
        return config

    for key in data:
        if key not in _KNOWN_KEYS:
            logger.warning("review.yaml에 알 수 없는 키가 있어 무시합니다: %s", key)

    intensity = data.get("intensity")
    if intensity is not None:
        intensity = str(intensity).strip().lower()
        if intensity not in INTENSITY_PRESETS:
            raise ConfigError(
                f"intensity는 {', '.join(INTENSITY_PRESETS)} 중 하나여야 합니다: {intensity!r}"
            )
        config.intensity = intensity
        for key, value in INTENSITY_PRESETS[intensity].items():
            setattr(config, key, value)
        # llm 블록은 아래에서 파싱하므로, 직접 적은 reasoning_effort가 이 값을 덮어쓴다.
        config.llm.reasoning_effort = INTENSITY_EFFORT[intensity]

    for key in _INT_KEYS:
        if key in data and data[key] is not None:
            setattr(config, key, _as_int(key, data[key]))
            config.explicit_keys.add(key)

    for key in _FLOAT_KEYS:
        if key in data and data[key] is not None:
            setattr(config, key, _as_float(key, data[key]))
            config.explicit_keys.add(key)

    for key in _BOOL_KEYS:
        if key in data and data[key] is not None:
            setattr(config, key, _as_bool(key, data[key]))
            config.explicit_keys.add(key)

    for key in ("comment_language", "summary_empty"):
        if key in data and data[key] is not None:
            setattr(config, key, str(data[key]))
            config.explicit_keys.add(key)

    for key in _STR_LIST_KEYS:
        if key in data:
            setattr(config, key, _as_str_list(key, data[key]))
            config.explicit_keys.add(key)

    gate_data = data.get("gate")
    if gate_data is not None:
        if not isinstance(gate_data, Mapping):
            raise ConfigError("gate는 맵이어야 합니다")
        for key in gate_data:
            if key not in _GATE_KEYS:
                logger.warning("review.yaml gate에 알 수 없는 키가 있어 무시합니다: %s", key)
        config.gate = GateRules(
            **{
                key: tuple(_as_str_list(f"gate.{key}", gate_data.get(key)))
                for key in _GATE_KEYS
            }
        )
        config.explicit_keys.add("gate")

    llm_data = data.get("llm")
    if llm_data is not None:
        if not isinstance(llm_data, Mapping):
            raise ConfigError("llm은 맵이어야 합니다")
        for key in llm_data:
            if key not in _LLM_KEYS:
                logger.warning("review.yaml llm에 알 수 없는 키가 있어 무시합니다: %s", key)
        if llm_data.get("command") is not None:
            command = _as_str_list("llm.command", llm_data["command"])
            if not command:
                raise ConfigError("llm.command가 비어 있습니다")
            config.llm.command = command
            config.llm.explicit.add("command")
        if llm_data.get("timeout") is not None:
            config.llm.timeout = _as_int("llm.timeout", llm_data["timeout"])
        if llm_data.get("parse_retries") is not None:
            config.llm.parse_retries = _as_int("llm.parse_retries", llm_data["parse_retries"])
        if llm_data.get("model") is not None:
            config.llm.model = str(llm_data["model"]).strip()
            config.llm.explicit.add("model")
        if llm_data.get("reasoning_effort") is not None:
            config.llm.reasoning_effort = str(llm_data["reasoning_effort"]).strip()
            config.llm.explicit.add("reasoning_effort")
        if llm_data.get("extra_args") is not None:
            config.llm.extra_args = _as_str_list("llm.extra_args", llm_data["extra_args"])
            config.llm.explicit.add("extra_args")
        if llm_data.get("backend") is not None:
            backend = str(llm_data["backend"]).strip().lower()
            if backend not in BACKEND_PRESETS:
                raise ConfigError(
                    f"llm.backend는 {', '.join(BACKEND_PRESETS)} 중 하나여야 합니다: {backend!r}"
                )
            config.llm.backend = backend
            config.llm.explicit.add("backend")
        if llm_data.get("profile") is not None:
            profile = str(llm_data["profile"]).strip().lower()
            if profile not in PROFILES:
                raise ConfigError(
                    f"llm.profile은 {', '.join(PROFILES)} 중 하나여야 합니다: {profile!r}"
                )
            config.llm.profile = profile
            config.llm.explicit.add("profile")

    # 백엔드 프리셋과 프로필을 여기서 확정한다. CLI 오버라이드가 있으면 cli.py가 다시 부른다.
    config.llm = resolve_llm(config)
    config.validate()
    return config


def apply_intensity(config: ReviewConfig, intensity: str) -> ReviewConfig:
    """intensity 프리셋을 다시 적용한 새 설정을 돌려준다.

    게이트가 강도를 올릴 때 쓴다. review.yaml에 직접 적은 키는 건드리지 않는다
    (`explicit_keys`가 그 판단 기준이고, 설정 파일 로딩과 같은 규칙이다).

    리스트·집합 필드는 원본과 공유한다. 읽기만 하므로 괜찮다.
    """
    intensity = str(intensity or "").strip().lower()
    if intensity not in INTENSITY_PRESETS:
        raise ConfigError(
            f"intensity는 {', '.join(INTENSITY_PRESETS)} 중 하나여야 합니다: {intensity!r}"
        )
    if intensity == config.intensity:
        return config

    updates: dict[str, Any] = {"intensity": intensity}
    for key, value in INTENSITY_PRESETS[intensity].items():
        if key not in config.explicit_keys:
            updates[key] = value

    # verifiers를 직접 적은 레포에서 프리셋의 pass_threshold가 그 수를 넘을 수 있다.
    # 리뷰 중간에 ConfigError로 죽는 대신 가능한 값으로 낮춘다.
    verifiers = updates.get("verifiers", config.verifiers)
    threshold = updates.get("pass_threshold", config.pass_threshold)
    if threshold > verifiers:
        updates["pass_threshold"] = verifiers

    llm = config.llm
    if "reasoning_effort" not in llm.explicit:
        llm = replace(llm, reasoning_effort=INTENSITY_EFFORT[intensity])

    resolved = replace(config, llm=llm, **updates)
    resolved.validate()
    return resolved


def resolve_llm(
    config: ReviewConfig,
    *,
    backend: Optional[str] = None,
    profile: Optional[str] = None,
    model: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> LLMSettings:
    """백엔드 프리셋과 프로필을 적용한 LLMSettings를 새로 만들어 돌려준다.

    순수 함수다. 입력을 바꾸지 않고 `env`를 주입받으므로 테스트에서 실제 환경변수를
    건드릴 필요가 없다. 호출을 여러 번 해도 결과가 같다(`explicit`이 보존되므로).

    먼저 적힌 쪽이 이긴다.

    | 항목       | 순서                                                               |
    |------------|--------------------------------------------------------------------|
    | 프로필     | `profile` 인자 → `llm.profile` → `detect_profile(env)` → local     |
    | 백엔드     | `backend` 인자 → `llm.backend` → codex                             |
    | 커맨드     | `llm.command`(프리셋 전체 무시) → 백엔드 프리셋                    |
    | 모델       | `model` 인자 → `llm.model` → 백엔드 프리셋 기본값                  |
    | extra_args | `llm.extra_args` → 백엔드·프로필 프리셋                            |

    `reasoning_effort`는 `intensity`가 정하므로 여기서 건드리지 않는다.
    """
    settings = config.llm
    env = os.environ if env is None else env

    if profile:
        resolved_profile = str(profile).strip().lower()
    elif "profile" in settings.explicit:
        resolved_profile = settings.profile
    else:
        resolved_profile = detect_profile(env)
    if resolved_profile not in PROFILES:
        raise ConfigError(
            f"profile은 {', '.join(PROFILES)} 중 하나여야 합니다: {resolved_profile!r}"
        )

    if backend:
        resolved_backend = str(backend).strip().lower()
    elif "backend" in settings.explicit:
        resolved_backend = settings.backend
    else:
        resolved_backend = DEFAULT_BACKEND
    if resolved_backend not in BACKEND_PRESETS:
        raise ConfigError(
            f"backend는 {', '.join(BACKEND_PRESETS)} 중 하나여야 합니다: {resolved_backend!r}"
        )

    preset = BACKEND_PRESETS[resolved_backend]

    if "command" in settings.explicit:
        command = list(settings.command)
    else:
        command = list(preset.command)

    if model:
        resolved_model = str(model).strip()
    elif "model" in settings.explicit:
        resolved_model = settings.model
    else:
        resolved_model = preset.model

    if "extra_args" in settings.explicit:
        extra_args = list(settings.extra_args)
    else:
        extra_args = preset.extra_args_for(resolved_profile)

    resolved = replace(
        settings,
        command=command,
        model=resolved_model,
        extra_args=extra_args,
        backend=resolved_backend,
        profile=resolved_profile,
        explicit=set(settings.explicit),
    )
    resolved.validate()
    return resolved


def backend_preset(settings: LLMSettings) -> Optional[BackendPreset]:
    """해석된 설정에 대응하는 프리셋. `llm.command`를 직접 적었으면 None.

    None이면 엔진이 어떤 CLI인지 알 수 없으므로 인증 확인을 건너뛴다.
    """
    if "command" in settings.explicit:
        return None
    return BACKEND_PRESETS.get(settings.backend)


def parse_config_text(text: str, source: str = "review.yaml") -> ReviewConfig:
    data: Any
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
    except ImportError:
        data = miniyaml.loads(text)
    except Exception as exc:  # pragma: no cover - PyYAML 파싱 오류
        raise ConfigError(f"{source} 파싱 실패: {exc}") from exc

    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"{source}의 최상위는 맵이어야 합니다")
    return config_from_mapping(data, source=source)


def find_config_file(repo_path: str) -> Optional[str]:
    for name in CONFIG_FILENAMES:
        candidate = os.path.join(repo_path, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def load_config(repo_path: str, config_path: Optional[str] = None) -> ReviewConfig:
    """대상 레포 루트의 review.yaml을 읽는다. 없으면 기본값."""
    path = config_path or find_config_file(repo_path)
    if not path:
        return config_from_mapping(None)
    if not os.path.isfile(path):
        raise ConfigError(f"설정 파일을 찾을 수 없습니다: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        return parse_config_text(handle.read(), source=path)
