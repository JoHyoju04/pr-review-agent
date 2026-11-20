from __future__ import annotations

import unittest

import helpers  # noqa: F401
import fixtures_pr153 as fx

from review_agent.config import parse_config_text
from review_agent.similarity import (
    bigrams,
    claim_signature,
    findings_are_similar,
    normalize,
    similarity,
)


class NormalizeTest(unittest.TestCase):
    def test_removes_whitespace_and_punctuation(self):
        self.assertEqual(normalize("트랜잭션이 아니어서, 유실됩니다."), "트랜잭션이아니어서유실됩니다")

    def test_lowercases_ascii(self):
        self.assertEqual(normalize("@Transactional"), "transactional")

    def test_empty_input(self):
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize(None), "")

    def test_spacing_differences_vanish(self):
        self.assertEqual(normalize("한 트랜잭션"), normalize("한트랜잭션"))


class BigramTest(unittest.TestCase):
    def test_bigram_count(self):
        self.assertEqual(bigrams("abcd"), frozenset({"ab", "bc", "cd"}))

    def test_single_character_has_no_bigram(self):
        self.assertEqual(bigrams("a"), frozenset({"a"}))

    def test_empty_is_empty(self):
        self.assertEqual(bigrams(""), frozenset())


class SimilarityTest(unittest.TestCase):
    def test_identical_text_is_one(self):
        self.assertEqual(similarity("트랜잭션 경계", "트랜잭션 경계"), 1.0)

    def test_empty_never_matches(self):
        self.assertEqual(similarity("", ""), 0.0)
        self.assertEqual(similarity("무언가", ""), 0.0)

    def test_symmetry(self):
        a, b = fx.TX_RUN_A.claim, fx.TX_RUN_B_CONC.claim
        self.assertAlmostEqual(similarity(a, b), similarity(b, a))

    def test_insensitive_to_korean_particles(self):
        """조사만 다른 문장은 같은 지적으로 판정될 만큼 비슷해야 한다.

        단어 토큰 Jaccard라면 "트랜잭션이"와 "트랜잭션은"이 서로 다른 토큰이 되어 훨씬
        낮게 나온다. 문자 bigram을 쓰는 이유가 이것이다.
        """
        particles = similarity(
            "트랜잭션이 하나가 아니어서 이력이 유실된다",
            "트랜잭션은 하나가 아니므로 이력은 유실된다",
        )
        unrelated = similarity(
            "트랜잭션이 하나가 아니어서 이력이 유실된다",
            "Slack 본문을 문자열 보간으로 조립해 JSON이 깨진다",
        )
        self.assertGreater(particles, parse_config_text("").dedupe_similarity)
        self.assertGreater(particles, unrelated * 3)

    def test_unrelated_text_is_low(self):
        self.assertLess(similarity("트랜잭션 경계가 잘못됐다", "Slack JSON 이스케이프가 빠졌다"), 0.1)


class ClaimSignatureTest(unittest.TestCase):
    def test_uses_claim_only(self):
        finding = fx.TX_RUN_B_CONC.to_finding()
        signature = claim_signature(finding)
        self.assertEqual(signature, finding.claim)
        # suggested_fix는 상투적 문구가 많아 분리력을 떨어뜨리므로 제외한다.
        self.assertNotIn("적용하세요", signature)


class MeasuredThresholdTest(unittest.TestCase):
    """실제 PR #153 리뷰 결과로 기본 임계값이 유효한지 지킨다.

    계획 단계에서 추측한 0.55를 그대로 썼다면 아무것도 접히지 않았다. 그래서 추측이 아니라
    이 픽스처로 재고, 기본값이 바뀌면 이 테스트가 먼저 깨진다.
    """

    def setUp(self) -> None:
        self.config = parse_config_text("")

    def test_same_bug_pairs_clear_dedupe_threshold(self):
        for label, left, right in fx.SAME_BUG_PAIRS:
            with self.subTest(label):
                score = similarity(left.claim, right.claim)
                self.assertGreaterEqual(
                    score,
                    self.config.dedupe_similarity,
                    f"{label}: {score:.3f} < {self.config.dedupe_similarity}",
                )

    def test_different_bug_pairs_stay_below_merge_threshold(self):
        for label, left, right in fx.DIFFERENT_BUG_PAIRS:
            with self.subTest(label):
                score = similarity(left.claim, right.claim)
                self.assertLess(
                    score,
                    self.config.merge_similarity,
                    f"{label}: {score:.3f} >= {self.config.merge_similarity}",
                )

    def test_measured_band_is_stable(self):
        same = [similarity(a.claim, b.claim) for _, a, b in fx.SAME_BUG_PAIRS]
        diff = [similarity(a.claim, b.claim) for _, a, b in fx.DIFFERENT_BUG_PAIRS]
        self.assertAlmostEqual(min(same), fx.MEASURED_SAME_BUG_MIN, places=2)
        self.assertAlmostEqual(max(diff), fx.MEASURED_DIFFERENT_BUG_MAX, places=2)
        self.assertGreater(min(same), max(diff), "같은 버그와 다른 버그가 겹치면 임계값을 못 고른다")

    def test_same_spot_different_bug_is_clearly_separated(self):
        """같은 위치·같은 category인데 다른 버그 — 합쳐지면 하나가 사라진다."""
        score = similarity(fx.ASYNC_RUN_B.claim, fx.ASYNC_SAME_SPOT_OTHER_BUG.claim)
        self.assertLess(score, self.config.merge_similarity)


class FindingsAreSimilarTest(unittest.TestCase):
    def test_threshold_zero_always_matches(self):
        self.assertTrue(
            findings_are_similar(
                fx.TX_RUN_B_CONC.to_finding(), fx.SLACK_RUN_B.to_finding(), 0.0
            )
        )

    def test_respects_threshold(self):
        left = fx.TX_RUN_B_DATA.to_finding()
        right = fx.TX_RUN_B_CONC.to_finding()
        self.assertTrue(findings_are_similar(left, right, 0.20))
        self.assertFalse(findings_are_similar(left, right, 0.90))


if __name__ == "__main__":
    unittest.main()
