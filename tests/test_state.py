from __future__ import annotations

import unittest

from helpers import make_finding

from review_agent.models import Verdict
from review_agent.state import PRState, StateStore

REPO = "owner/repo"


class StateStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = StateStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_unknown_pr_starts_at_round_zero(self):
        state = self.store.get_state(REPO, 7)
        self.assertEqual(state.rounds, 0)
        self.assertEqual(state.next_round, 1)
        self.assertEqual(state.last_reviewed_sha, "")
        self.assertFalse(state.stopped)

    def test_save_and_reload(self):
        self.store.save_state(
            PRState(REPO, 7, rounds=2, last_reviewed_sha="abc123", stopped=False, rounds_to_pass=None)
        )
        state = self.store.get_state(REPO, 7)
        self.assertEqual(state.rounds, 2)
        self.assertEqual(state.last_reviewed_sha, "abc123")
        self.assertEqual(state.next_round, 3)

    def test_save_is_idempotent_per_pr(self):
        for rounds in (1, 2, 3):
            self.store.save_state(PRState(REPO, 7, rounds=rounds, last_reviewed_sha="x"))
        self.assertEqual(self.store.get_state(REPO, 7).rounds, 3)

    def test_prs_are_isolated(self):
        self.store.save_state(PRState(REPO, 1, rounds=3))
        self.assertEqual(self.store.get_state(REPO, 2).rounds, 0)
        self.assertEqual(self.store.get_state("other/repo", 1).rounds, 0)

    def test_posted_refs_round_trip(self):
        finding = make_finding(line_start=42, line_end=48, finding_id="f-001")
        self.store.record_posted(
            REPO,
            7,
            1,
            [finding],
            verdicts_by_finding={
                "f-001": [
                    Verdict("코드 근거", "real", "확인됨", "src/app.py:42"),
                    Verdict("의도된 동작 여부", "not_real", "의도된 동작"),
                ]
            },
        )
        refs = self.store.posted_refs(REPO, 7)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].fingerprint, "src/app.py:42-48:logic")
        self.assertEqual(refs[0].claim, finding.claim)

        rows = self.store.list_posted(REPO, 7)
        self.assertIn("real", rows[0]["votes_json"])
        self.assertIn("로직", rows[0]["detected_by"])

    def test_feedback_recorded_for_latest_round(self):
        finding = make_finding(finding_id="f-001")
        self.store.record_posted(REPO, 7, 1, [finding])
        self.store.record_posted(REPO, 7, 2, [finding])

        self.assertTrue(self.store.record_feedback(REPO, 7, "f-001", "fixed", note="고쳤음"))
        rows = self.store.list_posted(REPO, 7)
        reacted = [row for row in rows if row["reaction"]]
        self.assertEqual(len(reacted), 1)
        self.assertEqual(reacted[0]["round"], 2)
        self.assertEqual(self.store.feedback_summary(REPO, 7), {"fixed": 1})

    def test_feedback_for_specific_round(self):
        finding = make_finding(finding_id="f-001")
        self.store.record_posted(REPO, 7, 1, [finding])
        self.store.record_posted(REPO, 7, 2, [finding])
        self.assertTrue(
            self.store.record_feedback(REPO, 7, "f-001", "wrong", round_number=1)
        )
        reacted = [row for row in self.store.list_posted(REPO, 7) if row["reaction"]]
        self.assertEqual(reacted[0]["round"], 1)

    def test_feedback_for_unknown_finding_returns_false(self):
        self.assertFalse(self.store.record_feedback(REPO, 7, "f-999", "ignored"))

    def test_invalid_reaction_rejected(self):
        self.store.record_posted(REPO, 7, 1, [make_finding(finding_id="f-001")])
        with self.assertRaises(ValueError):
            self.store.record_feedback(REPO, 7, "f-001", "lgtm")

    def test_rounds_to_pass_is_stored(self):
        self.store.save_state(PRState(REPO, 7, rounds=2, rounds_to_pass=2))
        self.assertEqual(self.store.get_state(REPO, 7).rounds_to_pass, 2)


if __name__ == "__main__":
    unittest.main()
