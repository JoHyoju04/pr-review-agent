from __future__ import annotations

import unittest

from helpers import make_finding, make_posted, make_votes

from review_agent.filters import (
    REASON_ALREADY_POSTED,
    REASON_MAX_FINDINGS,
    REASON_NIT_CAP,
    REASON_NO_EVIDENCE,
    REASON_SKIPPED_PATH,
    REASON_STYLE,
    REASON_VOTE,
    apply_caps,
    apply_severity_floor,
    apply_votes,
    count_by_severity,
    is_duplicate,
    path_matches,
    prefilter,
    sort_findings,
)


class PathMatchTest(unittest.TestCase):
    def test_bare_directory_matches_at_any_depth(self):
        patterns = ["generated/"]
        self.assertTrue(path_matches("generated/a.py", patterns))
        self.assertTrue(path_matches("src/main/generated/Q.java", patterns))
        self.assertFalse(path_matches("src/main/generator.java", patterns))

    def test_rooted_directory_only_matches_from_root(self):
        patterns = ["src/main/generated/"]
        self.assertTrue(path_matches("src/main/generated/Q.java", patterns))
        self.assertFalse(path_matches("other/src/main/generated/Q.java", patterns))

    def test_extension_glob(self):
        self.assertTrue(path_matches("gradle/wrapper/gradle-wrapper.lock", ["*.lock"]))
        self.assertTrue(path_matches("package.lock", ["*.lock"]))
        self.assertFalse(path_matches("a.lockfile", ["*.lock"]))

    def test_double_star_prefix(self):
        patterns = ["**/__snapshots__/"]
        self.assertTrue(path_matches("web/src/__snapshots__/a.snap", patterns))

    def test_backticks_and_leading_dot_slash_are_tolerated(self):
        self.assertTrue(path_matches("./build/out.jar", ["`build/`"]))

    def test_empty_pattern_list(self):
        self.assertFalse(path_matches("a.py", []))


class PrefilterTest(unittest.TestCase):
    def test_removes_finding_without_evidence(self):
        kept, dropped = prefilter([make_finding(evidence="   ")])
        self.assertEqual(kept, [])
        self.assertEqual(dropped[0].reason, REASON_NO_EVIDENCE)

    def test_removes_skipped_path(self):
        kept, dropped = prefilter(
            [make_finding(file="src/main/generated/QUser.java")],
            skip_paths=["generated/"],
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped[0].reason, REASON_SKIPPED_PATH)

    def test_removes_style_category(self):
        kept, dropped = prefilter([make_finding(category="style")])
        self.assertEqual(kept, [])
        self.assertEqual(dropped[0].reason, REASON_STYLE)

    def test_removes_already_posted_fingerprint(self):
        posted = [make_posted(file="src/app.py", line_start=10, category="logic")]
        kept, dropped = prefilter([make_finding(line_start=10)], posted=posted)
        self.assertEqual(kept, [])
        self.assertEqual(dropped[0].reason, REASON_ALREADY_POSTED)

    def test_keeps_new_finding_in_other_location(self):
        posted = [make_posted(line_start=10)]
        kept, _ = prefilter([make_finding(line_start=90)], posted=posted)
        self.assertEqual(len(kept), 1)

    def test_unknown_category_becomes_other(self):
        kept, _ = prefilter([make_finding(category="performance")])
        self.assertEqual(kept[0].category, "other")

    def test_known_categories_pass_through(self):
        kept, dropped = prefilter([make_finding(category="security")])
        self.assertEqual(kept[0].category, "security")
        self.assertEqual(dropped, [])


class DuplicateTest(unittest.TestCase):
    def test_exact_fingerprint_is_duplicate(self):
        self.assertTrue(is_duplicate(make_finding(line_start=10), [make_posted(line_start=10)]))

    def test_nearby_same_category_is_duplicate(self):
        self.assertTrue(is_duplicate(make_finding(line_start=12), [make_posted(line_start=10)]))

    def test_far_away_is_not_duplicate(self):
        self.assertFalse(is_duplicate(make_finding(line_start=30), [make_posted(line_start=10)]))

    def test_other_category_is_not_duplicate(self):
        self.assertFalse(
            is_duplicate(
                make_finding(line_start=10, category="concurrency"),
                [make_posted(line_start=10, category="logic")],
            )
        )


class VoteThresholdTest(unittest.TestCase):
    def test_two_of_three_passes_default_threshold(self):
        votes = [make_votes(make_finding(finding_id="f-001"), ["real", "real", "not_real"])]
        passed, dropped = apply_votes(votes, threshold=2)
        self.assertEqual(len(passed), 1)
        self.assertEqual(dropped, [])

    def test_one_of_three_fails_default_threshold(self):
        votes = [make_votes(make_finding(finding_id="f-001"), ["real", "not_real", "not_real"])]
        passed, dropped = apply_votes(votes, threshold=2)
        self.assertEqual(passed, [])
        self.assertEqual(dropped[0].reason, REASON_VOTE)

    def test_threshold_three_requires_unanimous(self):
        votes = [make_votes(make_finding(), ["real", "real", "not_real"])]
        passed, _ = apply_votes(votes, threshold=3)
        self.assertEqual(passed, [])

    def test_no_verdicts_never_passes(self):
        votes = [make_votes(make_finding(), [])]
        passed, _ = apply_votes(votes, threshold=1)
        self.assertEqual(passed, [])


class SortTest(unittest.TestCase):
    def test_severity_order(self):
        findings = [
            make_finding(severity="pre_existing", line_start=10, finding_id="f-003"),
            make_finding(severity="nit", line_start=20, finding_id="f-002"),
            make_finding(severity="important", line_start=30, finding_id="f-001"),
        ]
        ordered = sort_findings(findings)
        self.assertEqual(
            [f.severity for f in ordered], ["important", "nit", "pre_existing"]
        )

    def test_more_detectors_first_within_same_severity(self):
        few = make_finding(line_start=10, detected_by=["로직·엣지케이스"], finding_id="f-001")
        many = make_finding(
            line_start=50,
            detected_by=["로직·엣지케이스", "동시성·자원", "API·계약 변경"],
            finding_id="f-002",
        )
        ordered = sort_findings([few, many])
        self.assertEqual(ordered[0].id, "f-002")


class CapTest(unittest.TestCase):
    def test_nit_cap_keeps_only_three(self):
        findings = [
            make_finding(severity="nit", line_start=10 * i, finding_id=f"f-{i:03d}")
            for i in range(1, 6)
        ]
        kept, dropped = apply_caps(findings, max_findings=10, max_nits=3)
        self.assertEqual(len(kept), 3)
        self.assertEqual(len(dropped), 2)
        self.assertTrue(all(item.reason == REASON_NIT_CAP for item in dropped))

    def test_nit_cap_does_not_touch_important(self):
        findings = [
            make_finding(severity="important", line_start=10 * i, finding_id=f"i-{i}")
            for i in range(1, 6)
        ]
        kept, dropped = apply_caps(findings, max_findings=10, max_nits=3)
        self.assertEqual(len(kept), 5)
        self.assertEqual(dropped, [])

    def test_overall_cap(self):
        findings = [
            make_finding(severity="important", line_start=10 * i, finding_id=f"i-{i}")
            for i in range(1, 15)
        ]
        kept, dropped = apply_caps(findings, max_findings=10, max_nits=3)
        self.assertEqual(len(kept), 10)
        self.assertTrue(all(item.reason == REASON_MAX_FINDINGS for item in dropped))

    def test_nit_cap_applies_before_overall_cap(self):
        findings = sort_findings(
            [make_finding(severity="important", line_start=10 * i) for i in range(1, 4)]
            + [make_finding(severity="nit", line_start=100 * i) for i in range(1, 6)]
        )
        kept, _ = apply_caps(findings, max_findings=10, max_nits=3)
        counts = count_by_severity(kept)
        self.assertEqual(counts["important"], 3)
        self.assertEqual(counts["nit"], 3)


if __name__ == "__main__":
    unittest.main()


class SecurityVoteThresholdTest(unittest.TestCase):
    """보안 finding은 범용과 다른 임계값으로 판정한다."""

    def security(self, verdicts: list[str]):
        return make_votes(make_finding(category="security", finding_id="s-001"), verdicts)

    def test_security_needs_unanimous_while_general_needs_two(self):
        votes = [
            self.security(["real", "real", "not_real"]),
            make_votes(make_finding(finding_id="f-001"), ["real", "real", "not_real"]),
        ]
        passed, dropped = apply_votes(votes, threshold=2, security_threshold=3)
        self.assertEqual([f.id for f in passed], ["f-001"])
        self.assertEqual([d.finding.id for d in dropped], ["s-001"])

    def test_security_passes_when_all_three_agree(self):
        votes = [self.security(["real", "real", "real"])]
        passed, dropped = apply_votes(votes, threshold=2, security_threshold=3)
        self.assertEqual(len(passed), 1)
        self.assertEqual(dropped, [])

    def test_dropped_detail_names_the_security_threshold(self):
        _, dropped = apply_votes(
            [self.security(["real", "not_real", "not_real"])],
            threshold=2,
            security_threshold=3,
        )
        self.assertIn("보안 임계값 3", dropped[0].detail)

    def test_without_security_threshold_the_general_one_is_used(self):
        votes = [self.security(["real", "real", "not_real"])]
        passed, _ = apply_votes(votes, threshold=2)
        self.assertEqual(len(passed), 1)


class DeterministicFindingTest(unittest.TestCase):
    """결정적 도구가 낼 finding을 위한 확장 지점. 어댑터는 아직 없다."""

    def test_deterministic_finding_passes_without_verdicts(self):
        finding = make_finding(category="security", finding_id="g-001")
        finding.deterministic = True
        passed, dropped = apply_votes(
            [make_votes(finding, [])], threshold=2, security_threshold=3
        )
        self.assertEqual([f.id for f in passed], ["g-001"])
        self.assertEqual(dropped, [])

    def test_non_deterministic_finding_without_verdicts_is_dropped(self):
        passed, dropped = apply_votes([make_votes(make_finding(), [])], threshold=2)
        self.assertEqual(passed, [])
        self.assertEqual(len(dropped), 1)


class SecuritySeverityFloorTest(unittest.TestCase):
    def test_security_nit_becomes_important(self):
        floored = apply_severity_floor([make_finding(category="security", severity="nit")])
        self.assertEqual(floored[0].severity, "important")

    def test_security_pre_existing_is_left_alone(self):
        # 올리면 "이 PR이 만든 문제가 아니다" 표시가 깨진다.
        floored = apply_severity_floor(
            [make_finding(category="security", severity="pre_existing")]
        )
        self.assertEqual(floored[0].severity, "pre_existing")

    def test_non_security_nit_is_left_alone(self):
        floored = apply_severity_floor([make_finding(category="logic", severity="nit")])
        self.assertEqual(floored[0].severity, "nit")

    def test_disabled_changes_nothing(self):
        floored = apply_severity_floor(
            [make_finding(category="security", severity="nit")], enabled=False
        )
        self.assertEqual(floored[0].severity, "nit")

    def test_input_is_not_mutated(self):
        original = make_finding(category="security", severity="nit")
        apply_severity_floor([original])
        self.assertEqual(original.severity, "nit")

    def test_security_nit_escapes_the_nit_cap(self):
        # floor -> sort -> cap 순서가 파이프라인과 같다.
        findings = [
            make_finding(severity="nit", line_start=10 * i, finding_id=f"n-{i}")
            for i in range(1, 5)
        ] + [make_finding(category="security", severity="nit", line_start=500, finding_id="s-1")]
        kept, _ = apply_caps(
            sort_findings(apply_severity_floor(findings)), max_findings=10, max_nits=3
        )
        self.assertIn("s-1", [f.id for f in kept])
        self.assertEqual(count_by_severity(kept)["nit"], 3)


class SecurityReservedSlotsTest(unittest.TestCase):
    def crowd(self, count: int = 12) -> list:
        return [
            make_finding(severity="important", line_start=10 * i, finding_id=f"i-{i}")
            for i in range(1, count + 1)
        ]

    def test_security_findings_survive_a_crowded_cap(self):
        findings = self.crowd() + [
            make_finding(category="security", severity="important", line_start=900, finding_id="s-1"),
            make_finding(category="security", severity="important", line_start=910, finding_id="s-2"),
        ]
        kept, dropped = apply_caps(
            findings, max_findings=10, max_nits=3, reserved_security=3
        )
        self.assertEqual(len(kept), 10)
        self.assertIn("s-1", [f.id for f in kept])
        self.assertIn("s-2", [f.id for f in kept])
        self.assertTrue(all(d.reason == REASON_MAX_FINDINGS for d in dropped))

    def test_reserved_zero_keeps_the_old_behaviour(self):
        findings = self.crowd() + [
            make_finding(category="security", severity="important", line_start=900, finding_id="s-1")
        ]
        kept, _ = apply_caps(findings, max_findings=10, max_nits=3, reserved_security=0)
        self.assertEqual([f.id for f in kept], [f.id for f in findings[:10]])

    def test_reserved_slots_are_a_floor_not_a_quota(self):
        # 보안 지적이 예약보다 적으면 남은 슬롯은 그대로 다른 지적이 쓴다.
        findings = self.crowd() + [
            make_finding(category="security", severity="important", line_start=900, finding_id="s-1")
        ]
        kept, _ = apply_caps(findings, max_findings=10, max_nits=3, reserved_security=3)
        self.assertEqual(len(kept), 10)
        self.assertEqual(len([f for f in kept if f.category == "security"]), 1)

    def test_original_order_is_preserved(self):
        findings = self.crowd(4) + [
            make_finding(category="security", severity="important", line_start=900, finding_id="s-1")
        ]
        kept, _ = apply_caps(findings, max_findings=3, max_nits=3, reserved_security=1)
        self.assertEqual([f.id for f in kept], ["i-1", "i-2", "s-1"])

    def test_reserved_cannot_exceed_the_overall_cap(self):
        findings = [
            make_finding(category="security", severity="important", line_start=10 * i, finding_id=f"s-{i}")
            for i in range(1, 6)
        ]
        kept, _ = apply_caps(findings, max_findings=2, max_nits=3, reserved_security=5)
        self.assertEqual(len(kept), 2)

    def test_no_security_findings_means_no_change(self):
        findings = self.crowd()
        kept, _ = apply_caps(findings, max_findings=10, max_nits=3, reserved_security=3)
        self.assertEqual([f.id for f in kept], [f.id for f in findings[:10]])
