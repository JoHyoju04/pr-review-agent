from __future__ import annotations

import unittest

from helpers import make_finding

from review_agent.merger import merge_findings, ranges_are_close
from review_agent.models import majority_severity


class RangeProximityTest(unittest.TestCase):
    def test_overlapping_ranges_are_close(self):
        self.assertTrue(ranges_are_close(10, 20, 15, 25))

    def test_gap_within_tolerance(self):
        self.assertTrue(ranges_are_close(10, 12, 15, 16, tolerance=3))

    def test_gap_beyond_tolerance(self):
        self.assertFalse(ranges_are_close(10, 12, 16, 18, tolerance=3))

    def test_order_does_not_matter(self):
        self.assertTrue(ranges_are_close(15, 16, 10, 12, tolerance=3))


class MergeFindingsTest(unittest.TestCase):
    def test_same_line_same_category_merges_and_records_angles(self):
        findings = [
            make_finding(line_start=42, line_end=48, detected_by=["로직·엣지케이스"]),
            make_finding(line_start=44, line_end=50, detected_by=["API·계약 변경"]),
        ]
        merged = merge_findings(findings)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].line_start, 42)
        self.assertEqual(merged[0].line_end, 50)
        self.assertEqual(merged[0].detected_by, ["로직·엣지케이스", "API·계약 변경"])
        self.assertEqual(merged[0].id, "f-001")

    def test_nearby_lines_within_three_merge(self):
        findings = [
            make_finding(line_start=10, line_end=10),
            make_finding(line_start=13, line_end=13),
        ]
        self.assertEqual(len(merge_findings(findings)), 1)

    def test_lines_four_apart_do_not_merge(self):
        findings = [
            make_finding(line_start=10, line_end=10),
            make_finding(line_start=14, line_end=14),
        ]
        self.assertEqual(len(merge_findings(findings)), 2)

    def test_different_category_does_not_merge(self):
        findings = [
            make_finding(line_start=10, category="logic"),
            make_finding(line_start=10, category="concurrency"),
        ]
        self.assertEqual(len(merge_findings(findings)), 2)

    def test_different_file_does_not_merge(self):
        findings = [
            make_finding(file="a.py", line_start=10),
            make_finding(file="b.py", line_start=10),
        ]
        self.assertEqual(len(merge_findings(findings)), 2)

    def test_evidence_is_unioned(self):
        findings = [
            make_finding(line_start=10, evidence="a.py:10 `foo()`"),
            make_finding(line_start=11, evidence="a.py:11 `bar()`"),
        ]
        merged = merge_findings(findings)
        self.assertIn("`foo()`", merged[0].evidence)
        self.assertIn("`bar()`", merged[0].evidence)

    def test_merge_does_not_mutate_input(self):
        original = make_finding(line_start=10, detected_by=["로직·엣지케이스"])
        merge_findings([original, make_finding(line_start=11, detected_by=["동시성·자원"])])
        self.assertEqual(original.detected_by, ["로직·엣지케이스"])
        self.assertEqual(original.line_end, 10)


class MajoritySeverityTest(unittest.TestCase):
    def test_majority_wins_not_the_higher_grade(self):
        findings = [
            make_finding(line_start=10, severity="nit"),
            make_finding(line_start=10, severity="nit"),
            make_finding(line_start=10, severity="important"),
        ]
        merged = merge_findings(findings)
        self.assertEqual(merged[0].severity, "nit")

    def test_tie_keeps_first_seen(self):
        self.assertEqual(majority_severity(["nit", "important"]), "nit")
        self.assertEqual(majority_severity(["important", "nit"]), "important")

    def test_empty_votes_default_to_important(self):
        self.assertEqual(majority_severity([]), "important")


if __name__ == "__main__":
    unittest.main()
