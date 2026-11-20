from __future__ import annotations

import os
import tempfile
import unittest

import helpers  # noqa: F401  (sys.path 설정)

from review_agent import miniyaml
from review_agent.config import (
    BACKEND_PRESETS,
    DEFAULT_BACKEND,
    DEFAULT_LLM_COMMAND,
    ConfigError,
    backend_preset,
    detect_profile,
    load_config,
    parse_config_text,
    resolve_llm,
)


class MiniYamlTest(unittest.TestCase):
    def test_scalars_and_comments(self):
        data = miniyaml.loads(
            """
            # 주석
            detectors: 3
            intensity: standard   # 꼬리 주석
            comment_language: "ko"
            enabled: true
            ratio: 0.5
            nothing: null
            """
        )
        self.assertEqual(data["detectors"], 3)
        self.assertEqual(data["intensity"], "standard")
        self.assertEqual(data["comment_language"], "ko")
        self.assertIs(data["enabled"], True)
        self.assertEqual(data["ratio"], 0.5)
        self.assertIsNone(data["nothing"])

    def test_block_list_indented(self):
        data = miniyaml.loads("skip_paths:\n  - build/\n  - '*.lock'\n")
        self.assertEqual(data["skip_paths"], ["build/", "*.lock"])

    def test_block_list_at_same_indent(self):
        data = miniyaml.loads("skip_paths:\n- build/\n- vendor/\nmax_nits: 2\n")
        self.assertEqual(data["skip_paths"], ["build/", "vendor/"])
        self.assertEqual(data["max_nits"], 2)

    def test_inline_list(self):
        data = miniyaml.loads('llm:\n  command: ["codex", "exec", "-"]\n')
        self.assertEqual(data["llm"]["command"], ["codex", "exec", "-"])

    def test_nested_map(self):
        data = miniyaml.loads("llm:\n  timeout: 60\n  parse_retries: 1\n")
        self.assertEqual(data["llm"], {"timeout": 60, "parse_retries": 1})

    def test_empty_document(self):
        self.assertEqual(miniyaml.loads(""), {})

    def test_tab_indent_rejected(self):
        with self.assertRaises(miniyaml.MiniYamlError):
            miniyaml.loads("a:\n\t- b\n")


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        config = parse_config_text("")
        self.assertEqual(config.detectors, 3)
        self.assertEqual(config.verifiers, 3)
        self.assertEqual(config.pass_threshold, 2)
        self.assertEqual(config.max_findings, 10)
        self.assertEqual(config.max_nits, 3)
        self.assertEqual(config.max_rounds, 3)
        self.assertEqual(config.intensity, "standard")
        self.assertEqual(config.comment_language, "ko")

    def test_quick_intensity_preset(self):
        config = parse_config_text("intensity: quick\n")
        self.assertEqual((config.detectors, config.verifiers, config.pass_threshold), (1, 1, 1))

    def test_deep_intensity_preset(self):
        config = parse_config_text("intensity: deep\n")
        self.assertEqual((config.detectors, config.verifiers, config.pass_threshold), (3, 5, 3))

    def test_explicit_key_overrides_preset(self):
        config = parse_config_text("intensity: quick\nverifiers: 3\npass_threshold: 2\n")
        self.assertEqual(config.verifiers, 3)
        self.assertEqual(config.pass_threshold, 2)
        self.assertIn("verifiers", config.explicit_keys)

    def test_threshold_above_verifiers_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config_text("verifiers: 2\npass_threshold: 3\n")

    def test_unknown_intensity_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config_text("intensity: paranoid\n")

    def test_non_integer_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config_text("detectors: many\n")

    def test_llm_command_override(self):
        config = parse_config_text('llm:\n  command: ["my-llm", "--repo", "{repo}"]\n  timeout: 30\n')
        self.assertEqual(config.llm.command, ["my-llm", "--repo", "{repo}"])
        self.assertEqual(config.llm.timeout, 30)

    def test_unknown_key_is_ignored(self):
        config = parse_config_text("detectors: 1\nverifiers: 1\npass_threshold: 1\nnonsense: 5\n")
        self.assertEqual(config.detectors, 1)

    def test_missing_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(tmp)
            self.assertEqual(config.detectors, 3)
            self.assertEqual(config.source, "기본값")

    def test_loads_review_yaml_from_repo_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "review.yaml"), "w", encoding="utf-8") as handle:
                handle.write("max_findings: 5\nmax_nits: 1\n")
            config = load_config(tmp)
            self.assertEqual(config.max_findings, 5)
            self.assertEqual(config.max_nits, 1)


if __name__ == "__main__":
    unittest.main()


class IntensityEffortTest(unittest.TestCase):
    """intensity는 호출 수와 호출당 추론 강도를 함께 정한다."""

    def test_quick_lowers_reasoning_effort(self):
        config = parse_config_text("intensity: quick\n")
        self.assertEqual(config.llm.reasoning_effort, "low")

    def test_standard_is_medium(self):
        self.assertEqual(parse_config_text("intensity: standard\n").llm.reasoning_effort, "medium")

    def test_deep_is_high(self):
        self.assertEqual(parse_config_text("intensity: deep\n").llm.reasoning_effort, "high")

    def test_default_without_intensity_is_medium(self):
        self.assertEqual(parse_config_text("").llm.reasoning_effort, "medium")

    def test_explicit_effort_beats_the_preset(self):
        config = parse_config_text("intensity: quick\nllm:\n  reasoning_effort: high\n")
        self.assertEqual(config.llm.reasoning_effort, "high")
        self.assertEqual(config.detectors, 1)


class ProfileDetectionTest(unittest.TestCase):
    """프로필 자동 감지. 순수 함수라 실제 환경변수를 건드리지 않는다."""

    def test_empty_env_is_local(self):
        self.assertEqual(detect_profile({}), "local")

    def test_github_actions_is_ci(self):
        self.assertEqual(detect_profile({"GITHUB_ACTIONS": "true"}), "ci")

    def test_bare_ci_marker_is_ci(self):
        self.assertEqual(detect_profile({"CI": "1"}), "ci")

    def test_falsy_marker_is_local(self):
        """`CI=false`를 ci로 읽으면 로컬에서 엉뚱한 인증 경로를 탄다."""
        for value in ("false", "0", "no", "off", ""):
            self.assertEqual(detect_profile({"CI": value}), "local", value)


class BackendResolutionTest(unittest.TestCase):
    """해석 순서: 먼저 적힌 쪽이 이긴다."""

    def test_default_is_codex_local(self):
        config = parse_config_text("")
        self.assertEqual(config.llm.backend, DEFAULT_BACKEND)
        self.assertEqual(config.llm.profile, "local")
        self.assertEqual(config.llm.command, DEFAULT_LLM_COMMAND)

    def test_backend_in_yaml_swaps_command_and_model(self):
        config = parse_config_text("llm:\n  backend: claude\n")
        self.assertEqual(config.llm.backend, "claude")
        self.assertEqual(config.llm.command[0], "claude")
        self.assertEqual(config.llm.model, BACKEND_PRESETS["claude"].model)

    def test_cli_backend_beats_yaml(self):
        config = parse_config_text("llm:\n  backend: claude\n")
        resolved = resolve_llm(config, backend="codex")
        self.assertEqual(resolved.backend, "codex")
        self.assertEqual(resolved.command[0], "codex")

    def test_cli_model_beats_yaml_and_preset(self):
        config = parse_config_text("llm:\n  backend: claude\n  model: from-yaml\n")
        self.assertEqual(config.llm.model, "from-yaml")
        self.assertEqual(resolve_llm(config, model="from-cli").model, "from-cli")

    def test_yaml_model_beats_preset_default(self):
        config = parse_config_text("llm:\n  backend: claude\n  model: pinned\n")
        self.assertEqual(config.llm.model, "pinned")

    def test_cli_profile_beats_yaml(self):
        config = parse_config_text("llm:\n  profile: local\n")
        self.assertEqual(resolve_llm(config, profile="ci").profile, "ci")

    def test_yaml_profile_beats_auto_detection(self):
        config = parse_config_text("llm:\n  profile: local\n")
        resolved = resolve_llm(config, env={"GITHUB_ACTIONS": "true"})
        self.assertEqual(resolved.profile, "local")

    def test_auto_detection_is_used_when_nothing_is_set(self):
        config = parse_config_text("")
        self.assertEqual(resolve_llm(config, env={"CI": "true"}).profile, "ci")
        self.assertEqual(resolve_llm(config, env={}).profile, "local")

    def test_profile_changes_extra_args(self):
        config = parse_config_text("")
        self.assertEqual(
            resolve_llm(config, profile="local").extra_args,
            list(BACKEND_PRESETS["codex"].extra_args_local),
        )
        self.assertEqual(
            resolve_llm(config, profile="ci").extra_args,
            list(BACKEND_PRESETS["codex"].extra_args_ci),
        )

    def test_explicit_extra_args_beat_the_preset(self):
        config = parse_config_text('llm:\n  extra_args: ["--only-this"]\n')
        self.assertEqual(resolve_llm(config, profile="ci").extra_args, ["--only-this"])

    def test_explicit_empty_extra_args_are_respected(self):
        """`extra_args: []`는 "프리셋 값을 쓰지 않겠다"는 뜻이다."""
        config = parse_config_text("llm:\n  extra_args: []\n")
        self.assertEqual(resolve_llm(config, profile="ci").extra_args, [])

    def test_explicit_command_ignores_the_preset(self):
        config = parse_config_text('llm:\n  command: ["my-llm", "{repo}"]\n')
        resolved = resolve_llm(config, backend="claude")
        self.assertEqual(resolved.command, ["my-llm", "{repo}"])
        self.assertIsNone(backend_preset(resolved), "프리셋을 모르면 인증 확인을 건너뛴다")

    def test_preset_is_known_without_explicit_command(self):
        config = parse_config_text("llm:\n  backend: claude\n")
        preset = backend_preset(config.llm)
        self.assertIsNotNone(preset)
        self.assertEqual(preset.name, "claude")

    def test_resolution_is_idempotent(self):
        """cli.py가 로딩 후 한 번 더 부른다. 두 번 불러도 결과가 같아야 한다."""
        config = parse_config_text("llm:\n  backend: claude\n")
        once = resolve_llm(config, profile="ci")
        config.llm = once
        twice = resolve_llm(config, profile="ci")
        self.assertEqual(once.command, twice.command)
        self.assertEqual(once.extra_args, twice.extra_args)
        self.assertEqual((once.backend, once.model), (twice.backend, twice.model))

    def test_resolve_does_not_mutate_the_input(self):
        config = parse_config_text("llm:\n  backend: claude\n")
        before = (config.llm.backend, config.llm.model, list(config.llm.extra_args))
        resolve_llm(config, backend="codex", profile="ci", model="x")
        after = (config.llm.backend, config.llm.model, list(config.llm.extra_args))
        self.assertEqual(before, after)

    def test_intensity_still_owns_reasoning_effort(self):
        config = parse_config_text("intensity: deep\nllm:\n  backend: claude\n")
        self.assertEqual(config.llm.reasoning_effort, "high")
        self.assertEqual(resolve_llm(config, profile="ci").reasoning_effort, "high")

    def test_unknown_backend_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config_text("llm:\n  backend: gemini\n")
        with self.assertRaises(ConfigError):
            resolve_llm(parse_config_text(""), backend="gemini")

    def test_unknown_profile_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config_text("llm:\n  profile: staging\n")
        with self.assertRaises(ConfigError):
            resolve_llm(parse_config_text(""), profile="staging")


class SecurityAnglesConfigTest(unittest.TestCase):
    """보안 리뷰는 opt-in이고, security_angles로는 범용 관점을 끌 수 없다."""

    def ids(self, text: str) -> list[str]:
        return [angle.id for angle in parse_config_text(text).resolved_angles()]

    def test_default_config_runs_no_security_angle(self):
        config = parse_config_text("")
        self.assertEqual(config.security_angles, [])
        self.assertEqual(config.angles, [])
        self.assertTrue(all(not a.is_security for a in config.resolved_angles()))

    def test_security_angles_are_appended(self):
        ids = self.ids("security_angles: [authz, injection]")
        self.assertEqual(ids[:3], ["logic", "concurrency", "api_contract"])
        self.assertEqual(ids[3:], ["authz", "injection"])

    def test_detectors_keeps_meaning_general_angle_count(self):
        # detectors는 범용 관점 수다. 보안 관점을 켜도 이 값이 바뀌지 않아야
        # 설정을 두 번 읽어도 결과가 같다.
        config = parse_config_text("security_angles: [authz]")
        self.assertEqual(config.detectors, 3)
        self.assertEqual(len(config.resolved_angles()), 4)
        self.assertEqual(len(config.resolved_angles()), 4)

    def test_quick_intensity_keeps_security_angles(self):
        self.assertEqual(self.ids("intensity: quick\nsecurity_angles: [authz]"), ["logic", "authz"])

    def test_angles_can_turn_general_review_off(self):
        self.assertEqual(self.ids("angles: [authz]"), ["authz"])

    def test_general_angle_in_security_angles_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config_text("security_angles: [logic]")

    def test_unknown_angle_id_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config_text("security_angles: [sqli]")
        with self.assertRaises(ConfigError):
            parse_config_text("angles: [bogus]")

    def test_scalar_value_is_accepted_as_one_item(self):
        self.assertEqual(self.ids("security_angles: authz")[3:], ["authz"])


class SecurityChecksConfigTest(unittest.TestCase):
    """보안 통과 기준은 적지 않으면 전원 합의다."""

    def test_default_is_unanimous(self):
        config = parse_config_text("")
        self.assertEqual(len(config.security_checks), 3)
        self.assertEqual(config.security_threshold(), 3)

    def test_explicit_threshold_wins(self):
        self.assertEqual(parse_config_text("security_pass_threshold: 2").security_threshold(), 2)

    def test_default_threshold_follows_a_narrowed_check_list(self):
        # 체크를 줄였는데 기본 임계값이 3으로 남으면 설정 오류가 된다.
        config = parse_config_text("security_checks: [reachability, existing_defense]")
        self.assertEqual(config.security_threshold(), 2)

    def test_intensity_does_not_touch_the_security_threshold(self):
        for intensity in ("quick", "standard", "deep"):
            config = parse_config_text(f"intensity: {intensity}")
            self.assertEqual(config.security_threshold(), 3, intensity)

    def test_threshold_above_check_count_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config_text("security_checks: [reachability]\nsecurity_pass_threshold: 2")

    def test_empty_check_list_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config_text("security_checks: []")

    def test_unknown_check_id_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config_text("security_checks: [vibes]")
