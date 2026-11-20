from __future__ import annotations

import unittest

import helpers  # noqa: F401  (sys.path 설정)

from review_agent.config import ConfigError, apply_intensity, parse_config_text
from review_agent.gating import GateRules, resolve_gate

RULES = GateRules(
    always_review=(".github/workflows/", "Dockerfile", "*.tf", "**/requirements*.txt"),
    deep_paths=("src/**/auth/", "src/**/payment/"),
    skip_if_only=("docs/", "*.md", "**/*_test.py"),
)


class GateRulesTest(unittest.TestCase):
    def test_empty_rules_never_gate(self):
        decision = resolve_gate(["docs/a.md"], GateRules(), "standard")
        self.assertFalse(decision.skip)
        self.assertEqual(decision.intensity, "standard")

    def test_docs_only_change_is_skipped(self):
        decision = resolve_gate(["docs/guide.md", "README.md"], RULES, "standard")
        self.assertTrue(decision.skip)

    def test_allowlist_beats_the_denylist(self):
        # 문서 하나 + 워크플로 하나. 건너뛰면 가장 위험한 변경을 놓친다.
        decision = resolve_gate(["README.md", ".github/workflows/ci.yml"], RULES, "standard")
        self.assertFalse(decision.skip)
        self.assertIn("always_review", decision.reason)

    def test_dependency_list_is_never_skipped(self):
        decision = resolve_gate(["requirements.txt"], RULES, "standard")
        self.assertFalse(decision.skip)

    def test_deep_paths_raise_the_intensity(self):
        decision = resolve_gate(["src/main/auth/Token.kt"], RULES, "standard")
        self.assertEqual(decision.intensity, "deep")
        self.assertFalse(decision.skip)

    def test_deep_paths_also_prevent_skipping(self):
        decision = resolve_gate(["src/main/auth/Token_test.py"], RULES, "standard")
        self.assertFalse(decision.skip)
        self.assertEqual(decision.intensity, "deep")

    def test_allowlist_and_deep_can_both_apply(self):
        decision = resolve_gate(
            [".github/workflows/ci.yml", "src/main/auth/Token.kt"], RULES, "standard"
        )
        self.assertFalse(decision.skip)
        self.assertEqual(decision.intensity, "deep")

    def test_ordinary_change_uses_the_default_intensity(self):
        decision = resolve_gate(["src/main/App.kt"], RULES, "standard")
        self.assertFalse(decision.skip)
        self.assertEqual(decision.intensity, "standard")

    def test_partial_denylist_match_does_not_skip(self):
        decision = resolve_gate(["docs/a.md", "src/main/App.kt"], RULES, "standard")
        self.assertFalse(decision.skip)

    def test_empty_file_list_does_not_skip(self):
        # 목록을 못 읽은 것과 변경이 없는 것을 구분할 수 없다. 리뷰가 조용히
        # 사라지는 쪽이 더 나쁘므로 건너뛰지 않는다.
        decision = resolve_gate([], RULES, "standard")
        self.assertFalse(decision.skip)

    def test_blank_entries_are_ignored(self):
        decision = resolve_gate(["docs/a.md", "", "  "], RULES, "standard")
        self.assertTrue(decision.skip)


class GateConfigTest(unittest.TestCase):
    def test_gate_block_is_parsed(self):
        config = parse_config_text(
            "gate:\n"
            "  always_review: [Dockerfile]\n"
            "  deep_paths: [\"src/**/auth/\"]\n"
            "  skip_if_only: [docs/]\n"
        )
        self.assertEqual(config.gate.always_review, ("Dockerfile",))
        self.assertEqual(config.gate.deep_paths, ("src/**/auth/",))
        self.assertEqual(config.gate.skip_if_only, ("docs/",))

    def test_missing_gate_is_empty(self):
        self.assertTrue(parse_config_text("").gate.is_empty)

    def test_partial_gate_block_is_allowed(self):
        config = parse_config_text("gate:\n  skip_if_only: [docs/]\n")
        self.assertEqual(config.gate.always_review, ())
        self.assertFalse(config.gate.is_empty)

    def test_non_map_gate_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config_text("gate: [docs/]\n")


class ApplyIntensityTest(unittest.TestCase):
    def test_raises_the_preset_values(self):
        config = apply_intensity(parse_config_text("intensity: standard"), "deep")
        self.assertEqual(config.intensity, "deep")
        self.assertEqual(config.verifiers, 5)
        self.assertEqual(config.pass_threshold, 3)
        self.assertEqual(config.llm.reasoning_effort, "high")

    def test_explicit_keys_are_not_overwritten(self):
        config = apply_intensity(parse_config_text("verifiers: 3\nmax_findings: 4"), "deep")
        self.assertEqual(config.verifiers, 3)
        self.assertEqual(config.max_findings, 4)

    def test_threshold_is_clamped_to_an_explicit_verifier_count(self):
        # deep 프리셋의 pass_threshold 3이 verifiers 1을 넘으면 설정이 무효가 된다.
        config = apply_intensity(parse_config_text("verifiers: 1\npass_threshold: 1"), "deep")
        self.assertEqual(config.pass_threshold, 1)

    def test_explicit_reasoning_effort_is_kept(self):
        config = apply_intensity(
            parse_config_text("llm:\n  reasoning_effort: low\n"), "deep"
        )
        self.assertEqual(config.llm.reasoning_effort, "low")

    def test_same_intensity_returns_the_same_object(self):
        config = parse_config_text("intensity: standard")
        self.assertIs(apply_intensity(config, "standard"), config)

    def test_security_settings_survive(self):
        config = apply_intensity(parse_config_text("security_angles: [authz]"), "deep")
        self.assertEqual(
            [a.id for a in config.resolved_angles()],
            ["logic", "concurrency", "api_contract", "authz"],
        )
        self.assertEqual(config.security_threshold(), 3)

    def test_quick_gate_keeps_the_security_angle(self):
        # quick 프리셋은 detectors를 1로 낮춘다. 보안 관점은 그래도 남아야 한다.
        config = apply_intensity(parse_config_text("security_angles: [authz]"), "quick")
        self.assertEqual([a.id for a in config.resolved_angles()], ["logic", "authz"])

    def test_unknown_intensity_is_rejected(self):
        with self.assertRaises(ConfigError):
            apply_intensity(parse_config_text(""), "paranoid")


if __name__ == "__main__":
    unittest.main()
