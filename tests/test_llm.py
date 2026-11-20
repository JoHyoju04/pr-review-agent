from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

import helpers  # noqa: F401

from unittest import mock

from review_agent.config import (
    BACKEND_PRESETS,
    CLAUDE_LLM_COMMAND,
    DEFAULT_LLM_COMMAND,
    BackendPreset,
    ConfigError,
    backend_preset,
    parse_config_text,
)
from review_agent.llm import (
    CachingRunner,
    CLIRunner,
    CodexCLIRunner,
    FakeLLMRunner,
    LLMError,
)


class ArgvBuildTest(unittest.TestCase):
    def test_repo_placeholder_is_substituted(self):
        runner = CodexCLIRunner(command=["tool", "--cd", "{repo}", "-"])
        argv, stdin_data = runner._build_argv("/tmp/repo", "프롬프트", None)
        self.assertEqual(argv, ["tool", "--cd", "/tmp/repo", "-"])
        self.assertEqual(stdin_data, "프롬프트")

    def test_prompt_is_appended_when_no_stdin_marker(self):
        runner = CodexCLIRunner(command=["tool", "--cd", "{repo}"])
        argv, stdin_data = runner._build_argv("/tmp/repo", "프롬프트", None)
        self.assertEqual(argv[-1], "프롬프트")
        self.assertIsNone(stdin_data)

    def test_out_placeholder_is_substituted(self):
        runner = CodexCLIRunner(command=["tool", "-o", "{out}", "-"])
        argv, _ = runner._build_argv("/tmp/repo", "p", "/tmp/out.txt")
        self.assertEqual(argv, ["tool", "-o", "/tmp/out.txt", "-"])

    def test_wants_output_file_detection(self):
        self.assertTrue(CodexCLIRunner(command=["t", "-o", "{out}"]).wants_output_file)
        self.assertFalse(CodexCLIRunner(command=["t", "-"]).wants_output_file)
        self.assertTrue(CodexCLIRunner().wants_output_file)

    def test_default_command_is_read_only(self):
        self.assertIn("--sandbox", DEFAULT_LLM_COMMAND)
        self.assertEqual(
            DEFAULT_LLM_COMMAND[DEFAULT_LLM_COMMAND.index("--sandbox") + 1], "read-only"
        )

    def test_missing_executable_raises(self):
        runner = CodexCLIRunner(command=["definitely-not-a-real-binary-xyz", "-"])
        with self.assertRaises(LLMError):
            runner.run("프롬프트", os.getcwd())


class OutputFileTest(unittest.TestCase):
    """{out} 파일이 있으면 그 내용을, 비어 있으면 stdout을 쓴다."""

    def _runner(self, script: str) -> CodexCLIRunner:
        return CodexCLIRunner(command=[sys.executable, "-c", script, "{out}", "{repo}"])

    def test_output_file_is_preferred_over_stdout(self):
        script = (
            "import sys;"
            "open(sys.argv[1], 'w').write('[{\"from\": \"file\"}]');"
            "print('진행 로그와 프롬프트 에코')"
        )
        result = self._runner(script).run("프롬프트", os.getcwd())
        self.assertIn("file", result)
        self.assertNotIn("진행 로그", result)

    def test_falls_back_to_stdout_when_file_is_empty(self):
        script = "import sys; print('[{\"from\": \"stdout\"}]')"
        result = self._runner(script).run("프롬프트", os.getcwd())
        self.assertIn("stdout", result)

    def test_non_zero_exit_raises(self):
        script = "import sys; sys.stderr.write('백엔드 오류'); sys.exit(3)"
        with self.assertRaises(LLMError) as ctx:
            self._runner(script).run("프롬프트", os.getcwd())
        self.assertIn("백엔드 오류", str(ctx.exception))

    def test_temp_file_is_cleaned_up(self):
        captured: list[str] = []
        script = "import sys; open(sys.argv[1], 'w').write('[]'); print(sys.argv[1])"
        runner = CodexCLIRunner(command=[sys.executable, "-c", script, "{out}", "{repo}"])
        original = runner._run

        def spy(prompt, repo_path, out_path, timeout):
            captured.append(out_path or "")
            return original(prompt, repo_path, out_path, timeout)

        runner._run = spy  # type: ignore[method-assign]
        runner.run("프롬프트", os.getcwd())
        self.assertTrue(captured[0])
        self.assertFalse(os.path.exists(captured[0]))


class DeterminismFlagTest(unittest.TestCase):
    """모델과 추론 강도를 커맨드에 박아 사용자 개인 설정 상속을 끊는다."""

    def test_default_command_pins_model_and_effort(self):
        runner = CodexCLIRunner(model="my-model", reasoning_effort="high")
        argv, _ = runner._build_argv("/tmp/repo", "p", "/tmp/out.txt")
        self.assertIn("model=my-model", argv)
        self.assertIn("model_reasoning_effort=high", argv)

    def test_model_placeholder_substituted_in_custom_command(self):
        runner = CodexCLIRunner(
            command=["tool", "--model", "{model}", "--effort", "{effort}", "-"],
            model="m1",
            reasoning_effort="low",
        )
        argv, _ = runner._build_argv("/tmp/repo", "p", None)
        self.assertEqual(argv, ["tool", "--model", "m1", "--effort", "low", "-"])

    def test_extra_args_go_before_the_stdin_marker(self):
        runner = CodexCLIRunner(
            command=["tool", "--cd", "{repo}", "-"],
            extra_args=["--ignore-user-config", "--ephemeral"],
        )
        argv, stdin_data = runner._build_argv("/tmp/repo", "p", None)
        self.assertEqual(
            argv,
            ["tool", "--cd", "/tmp/repo", "--ignore-user-config", "--ephemeral", "-"],
        )
        self.assertEqual(stdin_data, "p")

    def test_extra_args_appended_when_no_stdin_marker(self):
        runner = CodexCLIRunner(command=["tool", "--cd", "{repo}"], extra_args=["--flag"])
        argv, _ = runner._build_argv("/tmp/repo", "prompt", None)
        self.assertEqual(argv[-2:], ["--flag", "prompt"])

    def test_config_carries_model_settings(self):
        config = parse_config_text(
            "llm:\n  model: pinned-model\n  reasoning_effort: high\n"
            '  extra_args: ["--ignore-user-config"]\n'
        )
        self.assertEqual(config.llm.model, "pinned-model")
        self.assertEqual(config.llm.reasoning_effort, "high")
        self.assertEqual(config.llm.extra_args, ["--ignore-user-config"])

    def test_empty_model_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config_text('llm:\n  model: ""\n')


class CachingRunnerTest(unittest.TestCase):
    """같은 입력 재실행 시 LLM 호출 0회, 출력 동일."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_second_call_is_served_from_cache(self):
        inner = FakeLLMRunner(default='[{"from": "inner"}]')
        runner = CachingRunner(inner, self.tmp, scope=("model-a", "medium", "abc123"))

        first = runner.run("프롬프트", "/tmp/repo")
        second = runner.run("프롬프트", "/tmp/repo")

        self.assertEqual(first, second)
        self.assertEqual(inner.call_count, 1)
        self.assertEqual((runner.hits, runner.misses), (1, 1))

    def test_different_prompt_is_a_new_call(self):
        inner = FakeLLMRunner(default="[]")
        runner = CachingRunner(inner, self.tmp)
        runner.run("프롬프트 A", "/tmp/repo")
        runner.run("프롬프트 B", "/tmp/repo")
        self.assertEqual(inner.call_count, 2)

    def test_scope_change_invalidates(self):
        inner = FakeLLMRunner(default="[]")
        CachingRunner(inner, self.tmp, scope=("model-a",)).run("p", "/tmp/repo")
        CachingRunner(inner, self.tmp, scope=("model-b",)).run("p", "/tmp/repo")
        self.assertEqual(inner.call_count, 2)

    def test_repo_path_is_not_part_of_the_key(self):
        """임시 worktree 경로는 매번 달라지므로 키에 들어가면 캐시가 늘 빗나간다."""
        inner = FakeLLMRunner(default="[]")
        runner = CachingRunner(inner, self.tmp)
        runner.run("p", "/tmp/tree-1")
        runner.run("p", "/tmp/tree-2")
        self.assertEqual(inner.call_count, 1)

    def test_cache_survives_a_new_runner_instance(self):
        inner = FakeLLMRunner(default="[]")
        CachingRunner(inner, self.tmp, scope=("m",)).run("p", "/tmp/repo")
        CachingRunner(inner, self.tmp, scope=("m",)).run("p", "/tmp/repo")
        self.assertEqual(inner.call_count, 1)


class ConfigCommandTest(unittest.TestCase):
    def test_review_yaml_can_replace_the_backend(self):
        config = parse_config_text(
            'llm:\n  command: ["claude", "-p", "--add-dir", "{repo}"]\n  timeout: 120\n'
        )
        self.assertEqual(config.llm.command, ["claude", "-p", "--add-dir", "{repo}"])
        self.assertEqual(config.llm.timeout, 120)


class FakeRunnerTest(unittest.TestCase):
    def test_rules_match_in_order(self):
        runner = FakeLLMRunner(rules=[("검증자", "verify"), ("리뷰어", "detect")])
        self.assertEqual(runner.run("당신은 검증자입니다", "/tmp"), "verify")
        self.assertEqual(runner.run("당신은 리뷰어입니다", "/tmp"), "detect")
        self.assertEqual(runner.run("그 외", "/tmp"), "[]")
        self.assertEqual(runner.call_count, 3)


class BackendPresetCommandTest(unittest.TestCase):
    """프리셋 커맨드는 읽기 전용이어야 한다. 이 불변식이 깨지면 리뷰가 레포를 고칠 수 있다."""

    def test_codex_preset_is_sandboxed_read_only(self):
        command = list(BACKEND_PRESETS["codex"].command)
        self.assertIn("--sandbox", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")

    def test_claude_preset_denies_prompts_and_write_tools(self):
        command = list(BACKEND_PRESETS["claude"].command)
        self.assertIn("--permission-prompts", command)
        self.assertEqual(command[command.index("--permission-prompts") + 1], "none")
        denied = command[command.index("--disallowedTools") + 1]
        for tool in ("Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch"):
            self.assertIn(tool, denied, tool)

    def test_claude_preset_allows_only_read_tools(self):
        command = list(BACKEND_PRESETS["claude"].command)
        allowed = command[command.index("--allowedTools") + 1]
        for tool in ("Write", "Edit", "NotebookEdit"):
            self.assertNotIn(tool, allowed, tool)
        # 프롬프트가 `git diff`를 직접 돌리게 하므로 git 읽기는 허용해야 한다.
        self.assertIn("Bash(git diff:*)", allowed)

    def test_claude_preset_is_non_interactive(self):
        self.assertIn("--print", CLAUDE_LLM_COMMAND)

    def test_claude_preset_does_not_want_an_output_file(self):
        """claude --print --output-format text 는 stdout에 최종 응답만 낸다."""
        runner = CLIRunner(command=list(BACKEND_PRESETS["claude"].command))
        self.assertFalse(runner.wants_output_file)

    def test_every_preset_pins_a_model(self):
        for name, preset in BACKEND_PRESETS.items():
            self.assertTrue(preset.model.strip(), name)
            self.assertIn("{model}", " ".join(preset.command), name)

    def test_ci_extra_args_extend_local_ones(self):
        """ci는 local의 결정성 플래그를 유지한 채 세션 저장만 더 끊는다."""
        for name, preset in BACKEND_PRESETS.items():
            with self.subTest(backend=name):
                self.assertEqual(
                    list(preset.extra_args_ci)[: len(preset.extra_args_local)],
                    list(preset.extra_args_local),
                )
                self.assertEqual(preset.extra_args_for("local"), list(preset.extra_args_local))
                self.assertEqual(preset.extra_args_for("ci"), list(preset.extra_args_ci))


class StdinPlaceholderTest(unittest.TestCase):
    """`-`는 argv에 남고 `{stdin}`은 지워진다. 합치면 claude가 `-`를 프롬프트로 읽는다."""

    def test_placeholder_is_removed_and_prompt_goes_to_stdin(self):
        runner = CLIRunner(command=["claude", "--print", "{stdin}"])
        argv, stdin_data = runner._build_argv("/tmp/repo", "프롬프트", None)
        self.assertEqual(argv, ["claude", "--print"])
        self.assertEqual(stdin_data, "프롬프트")

    def test_dash_marker_stays_in_argv(self):
        runner = CLIRunner(command=["codex", "exec", "-"])
        argv, stdin_data = runner._build_argv("/tmp/repo", "프롬프트", None)
        self.assertEqual(argv, ["codex", "exec", "-"])
        self.assertEqual(stdin_data, "프롬프트")

    def test_extra_args_go_before_the_placeholder(self):
        runner = CLIRunner(command=["claude", "--print", "{stdin}"], extra_args=["--safe-mode"])
        argv, _ = runner._build_argv("/tmp/repo", "p", None)
        self.assertEqual(argv, ["claude", "--print", "--safe-mode"])

    def test_claude_preset_substitutes_model_and_effort(self):
        preset = BACKEND_PRESETS["claude"]
        runner = CLIRunner(
            command=list(preset.command),
            model="claude-test",
            reasoning_effort="high",
            extra_args=preset.extra_args_for("ci"),
        )
        argv, stdin_data = runner._build_argv("/tmp/repo", "프롬프트", None)
        self.assertEqual(stdin_data, "프롬프트")
        self.assertNotIn("{stdin}", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-test")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertIn("--no-session-persistence", argv)


def _fake_preset(**overrides) -> BackendPreset:
    """실제 CLI를 건드리지 않는 프리셋. 로그인 확인은 python 스크립트로 흉내낸다."""
    defaults = dict(
        name="fake",
        command=(sys.executable, "-c", "pass", "{stdin}"),
        model="fake-model",
        extra_args_local=(),
        extra_args_ci=(),
        auth_env=("FAKE_API_KEY",),
        login_check=("definitely-not-a-real-binary-xyz", "status"),
        login_ok_marker="Logged in",
        auth_hint_local="로컬 조치 문구",
        auth_hint_ci="CI 조치 문구",
    )
    defaults.update(overrides)
    return BackendPreset(**defaults)


class PreflightTest(unittest.TestCase):
    """인증을 첫 호출 전에 막는다. 그냥 부르면 원인을 알 수 없는 exit 1만 남는다."""

    def _runner(self, preset: BackendPreset) -> CLIRunner:
        return CLIRunner(command=list(preset.command))

    def test_ci_without_auth_env_fails_with_the_hint(self):
        preset = _fake_preset()
        with self.assertRaises(LLMError) as ctx:
            self._runner(preset).preflight(profile="ci", preset=preset, env={})
        self.assertIn("CI 조치 문구", str(ctx.exception))

    def test_auth_env_passes_in_any_profile(self):
        preset = _fake_preset()
        for profile in ("ci", "local"):
            with self.subTest(profile=profile):
                self._runner(preset).preflight(
                    profile=profile, preset=preset, env={"FAKE_API_KEY": "x"}
                )

    def test_blank_auth_env_does_not_count(self):
        preset = _fake_preset()
        with self.assertRaises(LLMError):
            self._runner(preset).preflight(profile="ci", preset=preset, env={"FAKE_API_KEY": "  "})

    def test_local_without_login_fails_with_the_hint(self):
        preset = _fake_preset()
        with self.assertRaises(LLMError) as ctx:
            self._runner(preset).preflight(profile="local", preset=preset, env={})
        self.assertIn("로컬 조치 문구", str(ctx.exception))

    def test_local_login_marker_is_matched_ignoring_whitespace(self):
        """claude auth status는 들여쓴 JSON을 낸다. 공백 제거본에서도 찾아야 한다."""
        script = 'print(\'{\\n  "loggedIn": true\\n}\')'
        preset = _fake_preset(
            login_check=(sys.executable, "-c", script),
            login_ok_marker='"loggedIn":true',
        )
        self._runner(preset).preflight(profile="local", preset=preset, env={})

    def test_local_login_marker_is_matched_in_raw_output(self):
        preset = _fake_preset(
            login_check=(sys.executable, "-c", "print('Logged in using ChatGPT')"),
            login_ok_marker="Logged in",
        )
        self._runner(preset).preflight(profile="local", preset=preset, env={})

    def test_login_check_failure_does_not_block_the_review(self):
        """확인 수단이 고장 났다고 리뷰를 막지는 않는다."""
        preset = _fake_preset(login_check=(sys.executable, "-c", "pass"))
        with mock.patch("review_agent.llm.subprocess.run", side_effect=OSError("boom")):
            self._runner(preset).preflight(profile="local", preset=preset, env={})

    def test_no_preset_checks_only_the_executable(self):
        """llm.command를 직접 적으면 어떤 CLI인지 알 수 없어 인증 확인을 건너뛴다."""
        CLIRunner(command=[sys.executable, "-c", "pass"]).preflight(profile="ci", preset=None)

    def test_missing_executable_is_reported_before_any_call(self):
        runner = CLIRunner(command=["definitely-not-a-real-binary-xyz", "-"])
        with self.assertRaises(LLMError) as ctx:
            runner.preflight(profile="local", preset=None)
        self.assertIn("definitely-not-a-real-binary-xyz", str(ctx.exception))

    def test_real_presets_are_wired_to_the_resolved_config(self):
        config = parse_config_text("llm:\n  backend: claude\n")
        preset = backend_preset(config.llm)
        runner = CLIRunner(command=config.llm.command)
        with self.assertRaises(LLMError) as ctx:
            runner.preflight(profile="ci", preset=preset, env={})
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", str(ctx.exception))

    def test_fake_runner_preflight_is_a_noop(self):
        runner = FakeLLMRunner()
        runner.preflight(profile="ci", preset=None)
        self.assertEqual(runner.preflight_calls, 1)

    def test_caching_runner_delegates_preflight(self):
        inner = FakeLLMRunner()
        with tempfile.TemporaryDirectory() as tmp:
            CachingRunner(inner, tmp).preflight(profile="local", preset=None)
        self.assertEqual(inner.preflight_calls, 1)


class LegacyNameTest(unittest.TestCase):
    def test_codex_runner_alias_still_points_at_the_generic_runner(self):
        self.assertIs(CodexCLIRunner, CLIRunner)


if __name__ == "__main__":
    unittest.main()
