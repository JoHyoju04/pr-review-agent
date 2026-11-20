from __future__ import annotations

import io
import unittest

from helpers import make_finding

from review_agent.orchestrator import (
    ReviewScope,
    empty_scope_reason,
    plan_publish,
    plan_round,
)
from review_agent.publisher import (
    ESCALATION_TEXT,
    DryRunPublisher,
    PublishRequest,
    render_comment,
    render_counts_line,
    render_summary,
)
from review_agent.state import PRState


def state(rounds: int = 0, sha: str = "", stopped: bool = False) -> PRState:
    return PRState(
        repo_key="owner/repo",
        pr_number=1,
        rounds=rounds,
        last_reviewed_sha=sha,
        stopped=stopped,
    )


class PlanRoundTest(unittest.TestCase):
    def test_first_round_reviews_full_diff(self):
        plan = plan_round(state(), max_rounds=3)
        self.assertEqual(plan.round_number, 1)
        self.assertFalse(plan.diff_only)
        self.assertFalse(plan.escalate_only)

    def test_second_round_is_diff_only(self):
        plan = plan_round(state(rounds=1, sha="abc123"), max_rounds=3)
        self.assertEqual(plan.round_number, 2)
        self.assertTrue(plan.diff_only)

    def test_second_round_without_sha_falls_back_to_full_diff(self):
        plan = plan_round(state(rounds=1, sha=""), max_rounds=3)
        self.assertFalse(plan.diff_only)

    def test_fourth_round_escalates(self):
        plan = plan_round(state(rounds=3, sha="abc123"), max_rounds=3)
        self.assertEqual(plan.round_number, 4)
        self.assertTrue(plan.escalate_only)

    def test_stopped_pr_does_nothing(self):
        plan = plan_round(state(rounds=4, sha="abc", stopped=True), max_rounds=3)
        self.assertTrue(plan.stopped)

    def test_custom_max_rounds(self):
        self.assertTrue(plan_round(state(rounds=1, sha="a"), max_rounds=1).escalate_only)


class PlanPublishTest(unittest.TestCase):
    def test_normal_round_publishes_everything(self):
        findings = [make_finding(severity="important"), make_finding(line_start=50, severity="nit")]
        plan = plan_publish(plan_round(state(), 3), findings)
        self.assertEqual(len(plan.findings), 2)
        self.assertFalse(plan.escalation)

    def test_escalation_keeps_only_important(self):
        findings = [
            make_finding(severity="important", line_start=10),
            make_finding(severity="nit", line_start=50),
            make_finding(severity="pre_existing", line_start=90),
        ]
        plan = plan_publish(plan_round(state(rounds=3, sha="abc"), 3), findings)
        self.assertEqual(len(plan.findings), 1)
        self.assertEqual(plan.findings[0].severity, "important")
        self.assertTrue(plan.escalation)

    def test_escalation_with_no_important_publishes_nothing(self):
        findings = [make_finding(severity="nit")]
        plan = plan_publish(plan_round(state(rounds=3, sha="abc"), 3), findings)
        self.assertEqual(plan.findings, ())
        self.assertFalse(plan.escalation)


class SummaryTest(unittest.TestCase):
    def test_counts_line_starts_with_counts(self):
        findings = [
            make_finding(severity="important"),
            make_finding(severity="nit", line_start=50),
            make_finding(severity="nit", line_start=90),
        ]
        self.assertEqual(render_counts_line(findings), "Important 1, Nit 2")

    def test_counts_line_omits_zero_buckets(self):
        self.assertEqual(render_counts_line([make_finding(severity="nit")]), "Nit 1")

    def test_empty_uses_configured_text(self):
        self.assertEqual(render_counts_line([], "수정할 사항 없음"), "수정할 사항 없음")

    def test_escalation_summary_is_one_comment(self):
        body = render_summary(
            [make_finding(severity="important")], round_number=4, escalation=True
        )
        self.assertTrue(body.startswith("Important 1"))
        self.assertIn(ESCALATION_TEXT, body)
        self.assertEqual(body.count("pr-review-agent summary"), 1)

    def test_overflow_findings_listed_in_summary(self):
        finding = make_finding(severity="important")
        body = render_summary([finding], overflow=[finding])
        self.assertIn("인라인으로 달지 못했습니다", body)
        self.assertIn(finding.claim, body)


class DryRunPublisherTest(unittest.TestCase):
    def test_prints_findings_in_order(self):
        stream = io.StringIO()
        publisher = DryRunPublisher(stream)
        findings = [
            make_finding(severity="important", claim="중요한 문제", finding_id="f-001"),
            make_finding(severity="nit", line_start=50, claim="사소한 문제", finding_id="f-002"),
        ]
        outcome = publisher.publish(PublishRequest(findings=findings, round_number=1))
        text = stream.getvalue()

        self.assertIn("Important 1, Nit 1", text)
        self.assertLess(text.index("중요한 문제"), text.index("사소한 문제"))
        self.assertEqual(outcome.inline_posted, ["f-001", "f-002"])

    def test_empty_result_prints_summary_only(self):
        stream = io.StringIO()
        outcome = DryRunPublisher(stream).publish(PublishRequest(findings=[]))
        self.assertIn("수정할 사항 없음", stream.getvalue())
        self.assertTrue(outcome.skipped)


if __name__ == "__main__":
    unittest.main()


class SecuritySummaryTest(unittest.TestCase):
    def security(self, **kwargs):
        kwargs.setdefault("category", "security")
        kwargs.setdefault("claim", "sort_by가 검증 없이 SQL에 결합된다")
        return make_finding(**kwargs)

    def test_counts_line_notes_the_security_total(self):
        line = render_counts_line([self.security(), make_finding()])
        self.assertTrue(line.startswith("Important 2"))
        self.assertIn("보안 1건 포함", line)

    def test_counts_line_is_unchanged_without_security_findings(self):
        self.assertNotIn("보안", render_counts_line([make_finding()]))

    def test_empty_text_is_untouched(self):
        self.assertEqual(render_counts_line([], empty_text="수정할 사항 없음"), "수정할 사항 없음")

    def test_summary_gets_a_security_section(self):
        body = render_summary([self.security(line_start=42), make_finding()])
        self.assertIn("### 보안", body)
        self.assertIn("sort_by가 검증 없이 SQL에 결합된다", body)

    def test_summary_has_no_security_section_without_security_findings(self):
        self.assertNotIn("### 보안", render_summary([make_finding()]))

    def test_escalation_summary_does_not_repeat_the_section(self):
        # 에스컬레이션 요약은 이미 목록을 싣는다.
        body = render_summary([self.security()], escalation=True)
        self.assertNotIn("### 보안", body)

    def test_inline_comment_labels_security_items(self):
        body = render_comment([self.security()])
        self.assertIn("[보안]", body)
        self.assertNotIn("[보안]", render_comment([make_finding()]))

    def test_grouped_comment_labels_only_the_security_item(self):
        body = render_comment([self.security(line_start=10), make_finding(line_start=10)])
        self.assertEqual(body.count("[보안]"), 1)


class EmptyScopeReasonTest(unittest.TestCase):
    """빈 diff의 사유 판정. 순수 함수."""

    def test_merged_pr_tells_you_to_pass_the_range(self):
        reason = empty_scope_reason(diff_only=False, already_merged=True)
        self.assertIn("이미 base에 머지", reason)
        self.assertIn("--base", reason)
        self.assertIn("--head", reason)

    def test_plain_empty_diff(self):
        reason = empty_scope_reason(diff_only=False, already_merged=False)
        self.assertIn("변경이 없습니다", reason)
        self.assertNotIn("--base", reason)

    def test_rereview_with_no_new_commits_wins_over_ancestor(self):
        # 라운드 2 이상에서 base는 마지막 리뷰 SHA다. 새 푸시가 없으면 head가 그
        # SHA 자신이므로 is_ancestor가 참이 되는데, "이미 머지됨"은 틀린 설명이다.
        reason = empty_scope_reason(diff_only=True, already_merged=True)
        self.assertIn("마지막으로 리뷰한 커밋 이후", reason)
        self.assertNotIn("머지", reason)

    def test_every_case_gives_a_nonempty_reason(self):
        for diff_only in (True, False):
            for merged in (True, False):
                self.assertTrue(empty_scope_reason(diff_only, merged).strip())


class ReviewScopeTest(unittest.TestCase):
    def test_changed_files_mean_not_empty(self):
        self.assertFalse(ReviewScope(changed=("a.py",)).is_empty)

    def test_reason_means_empty(self):
        self.assertTrue(ReviewScope(empty_reason="변경 없음").is_empty)

    def test_read_failure_is_not_treated_as_empty(self):
        # 읽기 실패로 리뷰를 건너뛰면 리뷰가 조용히 사라진다. 진행해야 한다.
        self.assertFalse(ReviewScope(read_failed=True).is_empty)
