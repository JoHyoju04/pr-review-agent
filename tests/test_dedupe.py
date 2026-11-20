"""1단 병합 게이트, 2단 중복 제거, 3단 게시 묶음.

세 가지 불변식을 지키는지 본다.
1. 정체성 = 위치 + 주장 내용 (category는 정체성이 아니다)
2. 검증받지 않은 주장은 게시되지 않는다 (서로 다른 주장은 검증 전에 섞지 않는다)
3. 코멘트 수는 줄이고 정보는 줄이지 않는다
"""

from __future__ import annotations

import unittest

import fixtures_pr153 as fx
import helpers  # noqa: F401
from helpers import make_finding

from review_agent.config import parse_config_text
from review_agent.merger import REASON_DUPLICATE, dedupe_findings, merge_findings
from review_agent.publisher import group_by_spot, render_comment

CONFIG = parse_config_text("")
MERGE_T = CONFIG.merge_similarity
DEDUPE_T = CONFIG.dedupe_similarity


class MergeGateTest(unittest.TestCase):
    """1단: 같은 category + 근접 라인 + 느슨한 내용 유사도."""

    def test_same_bug_different_wording_still_merges(self):
        """세 관점이 같은 버그를 다르게 서술해도 하나로 합쳐 detected_by를 모아야 한다."""
        findings = [
            fx.ASYNC_RUN_A.to_finding(),
            fx.ASYNC_RUN_B.to_finding(),
        ]
        merged = merge_findings(findings, similarity_threshold=MERGE_T)
        self.assertEqual(len(merged), 1)
        self.assertEqual(
            set(merged[0].detected_by),
            {fx.ANGLE_LOGIC, fx.ANGLE_CONC, fx.ANGLE_API},
        )

    def test_same_spot_same_category_different_bug_does_not_merge(self):
        """조현주님이 지적한 경우. 합치면 뒤쪽 버그가 조용히 사라진다."""
        findings = [
            fx.ASYNC_RUN_B.to_finding(),
            fx.ASYNC_SAME_SPOT_OTHER_BUG.to_finding(),
        ]
        merged = merge_findings(findings, similarity_threshold=MERGE_T)

        self.assertEqual(len(merged), 2)
        claims = {f.claim for f in merged}
        self.assertIn(fx.ASYNC_RUN_B.claim, claims)
        self.assertIn(fx.ASYNC_SAME_SPOT_OTHER_BUG.claim, claims)

    def test_without_gate_the_second_bug_is_swallowed(self):
        """게이트가 없던 이전 동작을 재현해 회귀를 막는다."""
        findings = [
            fx.ASYNC_RUN_B.to_finding(),
            fx.ASYNC_SAME_SPOT_OTHER_BUG.to_finding(),
        ]
        merged = merge_findings(findings, similarity_threshold=0.0)
        self.assertEqual(len(merged), 1)
        # 이전 구현은 claim을 버렸다. 지금은 alt_claims에 남는다.
        self.assertIn(fx.ASYNC_SAME_SPOT_OTHER_BUG.claim, merged[0].alt_claims)

    def test_different_category_does_not_merge_in_stage_one(self):
        findings = [fx.TX_RUN_B_DATA.to_finding(), fx.TX_RUN_B_CONC.to_finding()]
        merged = merge_findings(findings, similarity_threshold=MERGE_T)
        self.assertEqual(len(merged), 2)

    def test_absorbed_claim_is_preserved(self):
        merged = merge_findings(
            [fx.ASYNC_RUN_A.to_finding(), fx.ASYNC_RUN_B.to_finding()],
            similarity_threshold=MERGE_T,
        )
        self.assertEqual(merged[0].claim, fx.ASYNC_RUN_A.claim)
        self.assertIn(fx.ASYNC_RUN_B.claim, merged[0].alt_claims)

    def test_evidence_is_unioned(self):
        left = fx.ASYNC_RUN_A.to_finding()
        right = fx.ASYNC_RUN_B.to_finding()
        left.evidence = "AsyncService.java:105 `updateConfirmState(...)`"
        right.evidence = "ConfirmService.java:226 `setState(DELETED)`"
        merged = merge_findings([left, right], similarity_threshold=MERGE_T)
        self.assertIn("AsyncService.java:105", merged[0].evidence)
        self.assertIn("ConfirmService.java:226", merged[0].evidence)


class DedupeTest(unittest.TestCase):
    """2단: 위치 + 엄격한 내용 유사도. category는 보지 않는다."""

    def test_folds_the_real_duplicate_from_pr153(self):
        """실제로 코멘트 두 개로 나갔던 그 쌍."""
        findings = [
            fx.TX_RUN_B_DATA.to_finding("f-002"),
            fx.TX_RUN_B_CONC.to_finding("f-003"),
        ]
        kept, dropped = dedupe_findings(findings, threshold=DEDUPE_T)

        self.assertEqual(len(kept), 1)
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0].reason, REASON_DUPLICATE)
        # 접힌 쪽의 주장도 남는다
        self.assertTrue(kept[0].alt_claims)

    def test_keeps_genuinely_different_bugs_at_the_same_spot(self):
        findings = [
            fx.ASYNC_RUN_B.to_finding("f-001"),
            fx.ASYNC_SAME_SPOT_OTHER_BUG.to_finding("f-002"),
        ]
        kept, dropped = dedupe_findings(findings, threshold=DEDUPE_T)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])

    def test_different_files_never_fold(self):
        findings = [
            fx.TX_RUN_B_CONC.to_finding("f-001"),
            fx.DDL_RUN_B.to_finding("f-002"),
        ]
        kept, _ = dedupe_findings(findings, threshold=DEDUPE_T)
        self.assertEqual(len(kept), 2)

    def test_survivor_prefers_more_detectors(self):
        few = fx.TX_RUN_B_DATA.to_finding("f-002")
        many = fx.TX_RUN_B_CONC.to_finding("f-003")
        many.detected_by = [fx.ANGLE_LOGIC, fx.ANGLE_CONC, fx.ANGLE_API]

        kept, _ = dedupe_findings([few, many], threshold=DEDUPE_T)
        self.assertEqual(kept[0].id, "f-003")

    def test_survivor_prefers_more_real_votes_when_detectors_tie(self):
        left = fx.TX_RUN_B_DATA.to_finding("f-002")
        right = fx.TX_RUN_B_CONC.to_finding("f-003")
        left.detected_by = [fx.ANGLE_LOGIC]
        right.detected_by = [fx.ANGLE_CONC]

        kept, _ = dedupe_findings(
            [left, right], threshold=DEDUPE_T, real_votes={"f-002": 2, "f-003": 3}
        )
        self.assertEqual(kept[0].id, "f-003")

    def test_result_is_independent_of_input_order(self):
        a = fx.TX_RUN_B_DATA.to_finding("f-002")
        b = fx.TX_RUN_B_CONC.to_finding("f-003")
        forward, _ = dedupe_findings([a, b], threshold=DEDUPE_T)
        backward, _ = dedupe_findings(
            [fx.TX_RUN_B_CONC.to_finding("f-003"), fx.TX_RUN_B_DATA.to_finding("f-002")],
            threshold=DEDUPE_T,
        )
        self.assertEqual([f.id for f in forward], [f.id for f in backward])

    def test_threshold_zero_disables_dedupe(self):
        findings = [
            fx.TX_RUN_B_DATA.to_finding("f-002"),
            fx.TX_RUN_B_CONC.to_finding("f-003"),
        ]
        kept, dropped = dedupe_findings(findings, threshold=0.0)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])

    def test_single_finding_passes_through(self):
        kept, dropped = dedupe_findings([fx.DDL_RUN_B.to_finding("f-001")], threshold=DEDUPE_T)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])


class GroupCommentTest(unittest.TestCase):
    """3단: 같은 위치에 남은 서로 다른 지적을 한 코멘트에 항목으로 싣는다."""

    def test_same_spot_different_bugs_become_one_comment(self):
        findings = [
            fx.ASYNC_RUN_B.to_finding("f-001"),
            fx.ASYNC_SAME_SPOT_OTHER_BUG.to_finding("f-002"),
        ]
        groups = group_by_spot(findings)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)

    def test_grouped_comment_keeps_both_claims_and_fixes(self):
        findings = [
            fx.ASYNC_RUN_B.to_finding("f-001"),
            fx.ASYNC_SAME_SPOT_OTHER_BUG.to_finding("f-002"),
        ]
        body = render_comment(group_by_spot(findings)[0], round_number=1)

        self.assertIn(fx.ASYNC_RUN_B.claim, body)
        self.assertIn(fx.ASYNC_SAME_SPOT_OTHER_BUG.claim, body)
        self.assertIn(fx.ASYNC_RUN_B.suggested_fix, body)
        self.assertIn(fx.ASYNC_SAME_SPOT_OTHER_BUG.suggested_fix, body)
        self.assertIn("지적 2건", body)

    def test_marker_lists_every_id_for_the_feedback_cli(self):
        findings = [
            fx.ASYNC_RUN_B.to_finding("f-001"),
            fx.ASYNC_SAME_SPOT_OTHER_BUG.to_finding("f-002"),
        ]
        body = render_comment(group_by_spot(findings)[0], round_number=2)
        self.assertIn("ids=f-001,f-002", body)
        self.assertIn("round=2", body)

    def test_distant_findings_stay_separate(self):
        findings = [
            fx.ASYNC_RUN_B.to_finding("f-001"),
            fx.DDL_RUN_B.to_finding("f-002"),
        ]
        self.assertEqual(len(group_by_spot(findings)), 2)

    def test_grouping_can_be_disabled(self):
        findings = [
            fx.ASYNC_RUN_B.to_finding("f-001"),
            fx.ASYNC_SAME_SPOT_OTHER_BUG.to_finding("f-002"),
        ]
        self.assertEqual(len(group_by_spot(findings, enabled=False)), 2)

    def test_single_finding_comment_has_no_group_header(self):
        body = render_comment([fx.DDL_RUN_B.to_finding("f-001")])
        self.assertNotIn("지적 1건이 있습니다", body)
        self.assertIn(fx.DDL_RUN_B.claim, body)

    def test_alt_claims_are_shown_in_the_comment(self):
        merged = merge_findings(
            [fx.ASYNC_RUN_A.to_finding(), fx.ASYNC_RUN_B.to_finding()],
            similarity_threshold=MERGE_T,
        )
        body = render_comment(merged)
        self.assertIn("다르게 서술한", body)
        self.assertIn(fx.ASYNC_RUN_B.claim, body)


if __name__ == "__main__":
    unittest.main()


class CrossReviewerMergeTest(unittest.TestCase):
    """보안 리뷰를 별도 워크플로로 쪼개지 않는 근거.

    범용 관점도 security category를 낼 수 있다. 같은 파이프라인 안에 있으면 1단 병합이
    보안 관점 finding과 하나로 합친다. 워크플로를 분리하면 이 병합이 일어날 자리가
    없어서 같은 지적이 두 번 게시된다.
    """

    def general(self):
        return make_finding(
            file="src/orders.py",
            line_start=42,
            category="security",
            claim="sort_by 파라미터가 검증 없이 SQL 문자열에 결합되어 인젝션이 가능하다",
            evidence="src/orders.py:42 `f\"ORDER BY {sort_by}\"`",
            detected_by=["로직·엣지케이스"],
        )

    def security(self):
        return make_finding(
            file="src/orders.py",
            line_start=42,
            category="security",
            claim="sort_by가 검증 없이 SQL 문자열에 결합된다. SQL 인젝션이 가능하다",
            evidence="src/orders.py:42 `f\"ORDER BY {sort_by}\"`",
            detected_by=["입력 검증·인젝션"],
        )

    def test_same_issue_from_both_reviewers_merges_into_one(self):
        merged = merge_findings(
            [self.general(), self.security()], similarity_threshold=MERGE_T
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(
            set(merged[0].detected_by), {"로직·엣지케이스", "입력 검증·인젝션"}
        )
        self.assertEqual(merged[0].category, "security")

    def test_the_absorbed_wording_is_kept(self):
        merged = merge_findings(
            [self.general(), self.security()], similarity_threshold=MERGE_T
        )
        self.assertTrue(merged[0].alt_claims)

    def test_a_different_bug_at_the_same_spot_is_not_swallowed(self):
        other = make_finding(
            file="src/orders.py",
            line_start=42,
            category="security",
            claim="응답에 결제 카드번호가 마스킹 없이 포함된다",
            evidence="src/orders.py:42 `return order.dict()`",
            detected_by=["민감정보 노출"],
        )
        merged = merge_findings([self.security(), other], similarity_threshold=MERGE_T)
        self.assertEqual(len(merged), 2)
