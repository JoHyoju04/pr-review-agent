"""LLM 실행 백엔드. 탐지와 검증은 모두 LLMRunner 인터페이스로만 호출한다."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Callable, Mapping, Optional, Protocol, Sequence

from .config import (
    DEFAULT_LLM_COMMAND,
    DEFAULT_MODEL,
    DEFAULT_PROFILE,
    DEFAULT_REASONING_EFFORT,
    BackendPreset,
)

logger = logging.getLogger(__name__)

REPO_PLACEHOLDER = "{repo}"
OUT_PLACEHOLDER = "{out}"
MODEL_PLACEHOLDER = "{model}"
EFFORT_PLACEHOLDER = "{effort}"
# 프롬프트를 stdin으로 보내는 두 방법.
#   STDIN_MARKER   : argv에 "-"를 **남긴다**. codex exec는 이 위치 인자를 보고 stdin을 읽는다.
#   STDIN_PLACEHOLDER: argv에서 **제거한다**. claude --print 처럼 별도 표시자가 없고
#                      파이프만으로 stdin을 읽는 CLI를 위한 것이다.
STDIN_MARKER = "-"
STDIN_PLACEHOLDER = "{stdin}"
# 로그인 확인 커맨드가 매달리지 않게 하는 상한.
LOGIN_CHECK_TIMEOUT = 20


class LLMError(RuntimeError):
    pass


class LLMRunner(Protocol):
    """읽기 전용 레포에서 프롬프트 하나를 실행하고 텍스트를 돌려준다."""

    def run(self, prompt: str, repo_path: str, *, timeout: Optional[int] = None) -> str:
        ...

    def preflight(self, **kwargs: object) -> None:
        """첫 호출 전 실행 파일과 인증 확인. 확인할 것이 없으면 아무것도 하지 않는다."""
        ...


class CLIRunner:
    """CLI 백엔드 비대화형 실행. 커맨드가 자리표시자 기반이라 백엔드 중립적이다.

    커맨드는 `llm.backend` 프리셋에서 오거나 `review.yaml`의 `llm.command`로 교체한다.

    - `{repo}`: 대상 레포 경로로 치환된다.
    - `{out}`: 임시 파일 경로로 치환된다. 이 자리표시자가 있으면 CLI가 그 파일에 쓴
      최종 메시지를 응답으로 쓴다(`codex exec -o`). 파일이 비어 있으면 stdout으로
      되돌아간다. stdout에는 진행 로그와 프롬프트 에코가 섞이므로 파일 쪽이 깔끔하다.
    - `{model}` / `{effort}`: 모델과 추론 강도로 치환된다.
    - `-`: 프롬프트를 stdin으로 보낸다. 이 토큰은 argv에 남는다(codex).
    - `{stdin}`: 프롬프트를 stdin으로 보내고 토큰은 argv에서 제거한다(claude).
    - 둘 다 없으면 프롬프트를 마지막 인자로 붙인다.

    읽기 전용은 커맨드에 넘기는 플래그로 보장한다(codex `--sandbox read-only`,
    claude `--permission-prompts none` + `--disallowedTools`). 프롬프트 본문은 로그에
    남기지 않는다.
    """

    def __init__(
        self,
        command: Optional[Sequence[str]] = None,
        timeout: int = 900,
        env: Optional[dict[str, str]] = None,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        extra_args: Optional[Sequence[str]] = None,
    ) -> None:
        self.command = list(command or DEFAULT_LLM_COMMAND)
        self.timeout = timeout
        self.env = env
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.extra_args = list(extra_args or [])

    @property
    def wants_output_file(self) -> bool:
        return any(OUT_PLACEHOLDER in part for part in self.command)

    def _build_argv(
        self, repo_path: str, prompt: str, out_path: Optional[str]
    ) -> tuple[list[str], Optional[str]]:
        argv = []
        for part in self.command:
            rendered = part.replace(REPO_PLACEHOLDER, repo_path)
            rendered = rendered.replace(MODEL_PLACEHOLDER, self.model)
            rendered = rendered.replace(EFFORT_PLACEHOLDER, self.reasoning_effort)
            if out_path is not None:
                rendered = rendered.replace(OUT_PLACEHOLDER, out_path)
            argv.append(rendered)

        stdin_token = next(
            (token for token in (STDIN_PLACEHOLDER, STDIN_MARKER) if token in argv), None
        )

        if self.extra_args:
            # stdin 표시자는 위치 인자이므로 추가 플래그는 그 앞에 넣는다.
            if stdin_token is not None:
                index = argv.index(stdin_token)
                argv = [*argv[:index], *self.extra_args, *argv[index:]]
            else:
                argv = [*argv, *self.extra_args]

        if stdin_token == STDIN_PLACEHOLDER:
            # 표시자 없이 파이프만으로 stdin을 읽는 CLI. 토큰을 argv에서 지운다.
            return [token for token in argv if token != STDIN_PLACEHOLDER], prompt
        if stdin_token == STDIN_MARKER:
            return argv, prompt
        return [*argv, prompt], None

    def preflight(
        self,
        *,
        profile: str = DEFAULT_PROFILE,
        preset: Optional[BackendPreset] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        """첫 LLM 호출 전에 실행 파일과 인증을 한 번 확인한다.

        인증이 안 된 상태로 바로 호출하면 `exit 1`과 섞인 stderr만 남아 원인을 알 수
        없다. 여기서 막고 백엔드별 조치를 알려준다.

        `preset`이 None이면(= `llm.command`를 직접 적은 경우) 어떤 CLI인지 알 수 없으므로
        실행 파일 존재까지만 확인한다.
        """
        env = os.environ if env is None else env
        self._require_executable(self.command[0])

        if preset is None:
            return

        for name in preset.auth_env:
            if str(env.get(name, "")).strip():
                logger.debug("%s 인증: %s 사용", preset.name, name)
                return

        if profile == "ci":
            raise LLMError(
                f"{preset.name} 백엔드 인증값이 없습니다 (프로필 ci). {preset.auth_hint_ci}"
            )

        if not preset.login_check:
            return
        if not self._login_looks_ok(preset):
            raise LLMError(
                f"{preset.name} 백엔드에 로그인되어 있지 않습니다. {preset.auth_hint_local}"
            )

    def _login_looks_ok(self, preset: BackendPreset) -> bool:
        argv = list(preset.login_check)
        if not shutil.which(argv[0]):
            return False
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=LOGIN_CHECK_TIMEOUT,
                env={**os.environ, **(self.env or {})},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("로그인 확인에 실패해 건너뜁니다 (%s): %s", " ".join(argv), exc)
            return True
        output = (proc.stdout or "") + (proc.stderr or "")
        # 마커는 원문과 공백 제거본에서 모두 찾는다. claude는 JSON을, codex는 문장을 낸다.
        compact = "".join(output.split())
        marker = preset.login_ok_marker
        return marker in output or marker in compact

    def _require_executable(self, executable: str) -> None:
        if not shutil.which(executable):
            raise LLMError(
                f"LLM 백엔드 실행 파일을 찾을 수 없습니다: {executable}. "
                "설치되어 있는지, review.yaml의 llm.backend / llm.command가 맞는지 확인하세요."
            )

    def run(self, prompt: str, repo_path: str, *, timeout: Optional[int] = None) -> str:
        out_path: Optional[str] = None
        if self.wants_output_file:
            handle, out_path = tempfile.mkstemp(prefix="review-agent-", suffix=".txt")
            os.close(handle)
        try:
            return self._run(prompt, repo_path, out_path, timeout)
        finally:
            if out_path:
                try:
                    os.unlink(out_path)
                except OSError:
                    pass

    def _run(
        self,
        prompt: str,
        repo_path: str,
        out_path: Optional[str],
        timeout: Optional[int],
    ) -> str:
        argv, stdin_data = self._build_argv(repo_path, prompt, out_path)
        self._require_executable(argv[0])

        logger.debug("LLM 실행: %s (프롬프트 %d자)", " ".join(argv[:3]), len(prompt))
        try:
            proc = subprocess.run(
                argv,
                input=stdin_data,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
                env={**os.environ, **(self.env or {})},
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"LLM 호출이 시간을 초과했습니다 ({timeout or self.timeout}s)") from exc

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()[:800]
            raise LLMError(f"LLM 호출 실패 (exit {proc.returncode}): {stderr}")

        if out_path:
            try:
                with open(out_path, "r", encoding="utf-8", errors="replace") as handle:
                    final_message = handle.read()
            except OSError as exc:
                logger.warning("최종 메시지 파일을 읽지 못해 stdout을 씁니다: %s", exc)
                final_message = ""
            if final_message.strip():
                return final_message
            logger.info("최종 메시지 파일이 비어 있어 stdout으로 되돌립니다")

        return proc.stdout or ""


class FakeLLMRunner:
    """테스트용 가짜 백엔드.

    - handler를 주면 그 함수가 응답을 만든다.
    - rules를 주면 프롬프트에 부분 문자열이 들어 있는 첫 규칙의 응답을 쓴다.
    - 둘 다 맞지 않으면 default를 돌려준다.
    """

    def __init__(
        self,
        rules: Optional[Sequence[tuple[str, str]]] = None,
        handler: Optional[Callable[[str, str], str]] = None,
        default: str = "[]",
    ) -> None:
        self.rules = list(rules or [])
        self.handler = handler
        self.default = default
        self.calls: list[tuple[str, str]] = []
        self.preflight_calls = 0

    def preflight(self, **kwargs: object) -> None:
        """테스트용 백엔드는 확인할 것이 없다."""
        self.preflight_calls += 1

    def run(self, prompt: str, repo_path: str, *, timeout: Optional[int] = None) -> str:
        self.calls.append((prompt, repo_path))
        if self.handler is not None:
            return self.handler(prompt, repo_path)
        for needle, response in self.rules:
            if needle in prompt:
                return response
        return self.default

    @property
    def call_count(self) -> int:
        return len(self.calls)


class CachingRunner:
    """응답 캐시 데코레이터. 새 백엔드가 아니라 LLMRunner를 감싸는 것이다.

    키는 `sha256(scope | prompt)`이고 scope에는 모델·추론 강도·head SHA가 들어간다.
    켜면 같은 입력 재실행 시 LLM 호출이 0회가 되고 출력이 바이트 단위로 같아진다.

    기본으로 켜지 않는 이유: 레포가 바뀌었는데 프롬프트만 같으면 오래된 응답을 재사용해
    새 버그를 가릴 수 있다. 재현이나 A/B 비교가 필요할 때만 `--cache`로 켠다.
    """

    def __init__(
        self,
        inner: LLMRunner,
        cache_dir: str,
        scope: Sequence[str] = (),
    ) -> None:
        self.inner = inner
        self.cache_dir = cache_dir
        self.scope = list(scope)
        self.hits = 0
        self.misses = 0
        os.makedirs(cache_dir, exist_ok=True)

    def preflight(self, **kwargs: object) -> None:
        """캐시는 백엔드가 아니라 데코레이터다. 확인은 감싼 쪽에 맡긴다."""
        inner_preflight = getattr(self.inner, "preflight", None)
        if callable(inner_preflight):
            inner_preflight(**kwargs)

    def _key(self, prompt: str, repo_path: str) -> str:
        digest = hashlib.sha256()
        for part in self.scope:
            digest.update(str(part).encode("utf-8"))
            digest.update(b"\0")
        # 레포 경로는 키에 넣지 않는다. 임시 worktree 경로가 매번 달라지기 때문이다.
        digest.update(prompt.encode("utf-8"))
        return digest.hexdigest()

    def _path(self, key: str) -> str:
        return os.path.join(self.cache_dir, f"{key}.txt")

    def run(self, prompt: str, repo_path: str, *, timeout: Optional[int] = None) -> str:
        key = self._key(prompt, repo_path)
        path = self._path(key)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    cached = handle.read()
                self.hits += 1
                logger.debug("캐시 적중 %s", key[:12])
                return cached
            except OSError as exc:  # pragma: no cover
                logger.warning("캐시를 읽지 못해 다시 호출합니다: %s", exc)

        response = self.inner.run(prompt, repo_path, timeout=timeout)
        self.misses += 1
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(response)
        except OSError as exc:  # pragma: no cover
            logger.warning("캐시를 쓰지 못했습니다: %s", exc)
        return response

    @property
    def stats(self) -> str:
        return f"캐시 적중 {self.hits} / 신규 호출 {self.misses}"


# 이전 이름. 백엔드가 codex 하나뿐이던 때의 import를 깨지 않는다.
CodexCLIRunner = CLIRunner


def build_runner(
    command: Optional[Sequence[str]] = None,
    timeout: int = 900,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    extra_args: Optional[Sequence[str]] = None,
) -> LLMRunner:
    return CLIRunner(
        command=command,
        timeout=timeout,
        model=model,
        reasoning_effort=reasoning_effort,
        extra_args=extra_args,
    )
