from __future__ import annotations

import os
import tempfile
import unittest

import helpers  # noqa: F401

from review_agent.config import parse_config_text
from review_agent.context import load_repo_docs, parse_review_md, resolve_max_nits
from review_agent import prompts
from review_agent.prompts import build_detect_prompt, select_angles, select_checks

REVIEW_MD = """# REVIEW.md

## 우선순위
1. 실제 버그

## 건너뛸 경로
- `generated/`, `vendor/`
- `*.lock`

## 코멘트 규칙
- 모든 지적에 `파일:라인`을 붙인다.
- Nit은 PR당 최대 2개.

## 요약 형식
- 첫 줄은 개수로 시작한다.
"""


class ParseReviewMdTest(unittest.TestCase):
    def test_skip_paths_from_section(self):
        guidance = parse_review_md(REVIEW_MD)
        self.assertEqual(guidance.skip_paths, ["generated/", "vendor/", "*.lock"])

    def test_nit_cap_from_comment_rules(self):
        self.assertEqual(parse_review_md(REVIEW_MD).max_nits, 2)

    def test_bullets_outside_skip_section_are_ignored(self):
        guidance = parse_review_md("## 우선순위\n- `src/` 중요\n")
        self.assertEqual(guidance.skip_paths, [])

    def test_nit_mention_without_limit_word_is_ignored(self):
        self.assertIsNone(parse_review_md("- nit은 신중하게 적는다\n").max_nits)

    def test_empty_document(self):
        guidance = parse_review_md("")
        self.assertEqual(guidance.skip_paths, [])
        self.assertIsNone(guidance.max_nits)


class MaxNitsPrecedenceTest(unittest.TestCase):
    def test_review_md_beats_default(self):
        config = parse_config_text("")
        guidance = parse_review_md(REVIEW_MD)
        self.assertEqual(resolve_max_nits(config, guidance), 2)

    def test_explicit_yaml_beats_review_md(self):
        config = parse_config_text("max_nits: 1\n")
        guidance = parse_review_md(REVIEW_MD)
        self.assertEqual(resolve_max_nits(config, guidance), 1)

    def test_default_when_nothing_specified(self):
        self.assertEqual(resolve_max_nits(parse_config_text(""), parse_review_md("")), 3)


class RepoDocsTest(unittest.TestCase):
    def test_reads_agents_and_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "AGENTS.md"), "w", encoding="utf-8") as handle:
                handle.write("# 프로젝트\nSpring Boot 백엔드")
            with open(os.path.join(tmp, "REVIEW.md"), "w", encoding="utf-8") as handle:
                handle.write(REVIEW_MD)
            docs = load_repo_docs(tmp)
            self.assertEqual(docs.agents_source, "AGENTS.md")
            self.assertEqual(docs.review_source, "REVIEW.md")
            self.assertTrue(docs.has_agents)
            self.assertTrue(docs.has_review)

    def test_falls_back_to_claude_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "CLAUDE.md"), "w", encoding="utf-8") as handle:
                handle.write("# 프로젝트")
            docs = load_repo_docs(tmp)
            self.assertEqual(docs.agents_source, "CLAUDE.md")

    def test_prefers_agents_over_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("AGENTS.md", "CLAUDE.md"):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as handle:
                    handle.write(name)
            self.assertEqual(load_repo_docs(tmp).agents_source, "AGENTS.md")

    def test_no_docs_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = load_repo_docs(tmp)
            self.assertFalse(docs.has_agents)
            self.assertFalse(docs.has_review)


class PromptTest(unittest.TestCase):
    def test_context_is_injected(self):
        prompt = build_detect_prompt(
            angle=select_angles(1)[0],
            diff_range="base...head",
            pr_title="제목",
            pr_description="설명",
            agents_md="# 프로젝트\n스택: Spring Boot",
            review_md=REVIEW_MD,
            max_nits=2,
        )
        self.assertIn("스택: Spring Boot", prompt)
        self.assertIn("건너뛸 경로", prompt)
        self.assertIn("git diff base...head", prompt)
        self.assertIn("nit은 최대 2개", prompt)

    def test_missing_docs_use_placeholder(self):
        prompt = build_detect_prompt(
            angle=select_angles(1)[0],
            diff_range="base...head",
            pr_title="제목",
            pr_description="",
            agents_md="",
            review_md="",
        )
        self.assertIn("AGENTS.md 없음", prompt)
        self.assertIn("REVIEW.md 없음", prompt)

    def test_rereview_instruction_added_from_round_two(self):
        from helpers import make_posted

        prompt = build_detect_prompt(
            angle=select_angles(1)[0],
            diff_range="abc123..head",
            pr_title="제목",
            pr_description="",
            agents_md="",
            review_md="",
            round_number=2,
            previous_findings=[make_posted(claim="이전 지적")],
        )
        self.assertIn("재리뷰 지침 (라운드 2)", prompt)
        self.assertIn("이전 지적", prompt)
        self.assertIn("git diff abc123..head", prompt)

    def test_round_one_has_no_rereview_block(self):
        prompt = build_detect_prompt(
            angle=select_angles(1)[0],
            diff_range="base...head",
            pr_title="제목",
            pr_description="",
            agents_md="",
            review_md="",
        )
        self.assertNotIn("재리뷰 지침", prompt)

    def test_angle_and_check_selection_cycles(self):
        self.assertEqual(len(select_angles(3)), 3)
        self.assertEqual(len({a.name for a in select_angles(3)}), 3)
        self.assertEqual(len(select_angles(5)), 5)
        self.assertEqual(len(select_checks(1)), 1)
        self.assertEqual(select_angles(0), [])


class ResolveAnglesTest(unittest.TestCase):
    """관점 선택은 순수 함수다. 보안 관점은 덧붙이고, 범용을 끌 수 있는 건 angles뿐이다."""

    def ids(self, **kwargs) -> list[str]:
        return [angle.id for angle in prompts.resolve_angles(**kwargs)]

    def test_default_is_unchanged_general_angles(self):
        self.assertEqual(self.ids(), list(prompts.GENERAL_ANGLE_IDS))

    def test_security_angles_are_appended_not_replacing(self):
        ids = self.ids(security_angles=["authz", "injection"])
        self.assertEqual(ids, list(prompts.GENERAL_ANGLE_IDS) + ["authz", "injection"])

    def test_angles_replaces_everything_including_general(self):
        self.assertEqual(self.ids(angles=["authz", "injection"]), ["authz", "injection"])

    def test_angles_wins_over_security_angles_and_detectors(self):
        self.assertEqual(
            self.ids(angles=["authz"], security_angles=["injection"], detectors=3),
            ["authz"],
        )

    def test_intensity_preset_cannot_drop_security_angles(self):
        # quick 프리셋은 detectors를 1로 낮춘다. 보안 관점은 그래도 살아 있어야 한다.
        self.assertEqual(self.ids(security_angles=["authz"], detectors=1), ["logic", "authz"])

    def test_duplicate_ids_collapse(self):
        self.assertEqual(self.ids(angles=["authz", "authz"]), ["authz"])

    def test_unknown_id_raises(self):
        with self.assertRaises(prompts.UnknownAngleError):
            self.ids(security_angles=["sqli"])

    def test_blank_entries_are_ignored(self):
        self.assertEqual(self.ids(angles=["authz", "", "  "]), ["authz"])


class ResolveChecksTest(unittest.TestCase):
    def test_security_category_gets_security_checks(self):
        self.assertEqual(
            [c.id for c in prompts.resolve_checks("security")],
            list(prompts.SECURITY_CHECK_IDS),
        )

    def test_other_categories_keep_the_general_checks(self):
        self.assertEqual(
            [c.id for c in prompts.resolve_checks("logic", 3)],
            list(prompts.GENERAL_CHECK_IDS),
        )

    def test_general_checks_still_cycle_but_security_does_not(self):
        self.assertEqual(len(prompts.resolve_checks("logic", 5)), 5)
        self.assertEqual(len(prompts.resolve_checks("security", 5)), 3)

    def test_security_checks_can_be_narrowed(self):
        self.assertEqual(
            [c.id for c in prompts.resolve_checks("security", security_checks=["reachability"])],
            ["reachability"],
        )

    def test_unknown_check_id_raises(self):
        with self.assertRaises(prompts.UnknownCheckError):
            prompts.resolve_checks("security", security_checks=["vibes"])

    def test_every_security_check_states_its_refutation(self):
        # not_real 조건은 체크마다 다르다. 비어 있으면 프롬프트에 빈 줄이 들어간다.
        for check in prompts.SECURITY_CHECKS:
            self.assertTrue(check.refutation.strip(), check.id)


class SecurityDetectPromptTest(unittest.TestCase):
    def build(self, angle_id: str = "authz", **kwargs) -> str:
        defaults = dict(
            angle=prompts.ANGLE_REGISTRY[angle_id],
            diff_range="base...head",
            pr_title="제목",
            pr_description="설명",
            agents_md="",
            review_md="",
        )
        defaults.update(kwargs)
        return build_detect_prompt(**defaults)

    def test_judges_by_exploitability_not_malfunction(self):
        prompt = self.build()
        self.assertIn("공격자가 이 변경을 어떻게 악용할 수 있는가", prompt)
        # 범용 템플릿의 "잘못 동작" 기준이 섞여 들어오면 보안 지적이 스스로 걸러진다.
        self.assertNotIn("실제로 잘못 동작하거나 잘못될 수 있는 코드만", prompt)

    def test_intent_is_not_a_reason_to_stay_silent(self):
        self.assertIn("의도한 구현이라는", self.build())

    def test_category_is_pinned_to_security(self):
        self.assertIn("category: security (고정)", self.build())

    def test_non_security_findings_are_out_of_scope(self):
        self.assertIn("보안과 무관한 지적", self.build())

    def test_evidence_requires_entry_point_and_attack_example(self):
        prompt = self.build()
        self.assertIn("외부 입력이 들어오는 입구", prompt)
        self.assertIn("공격 입력 또는 호출 예시", prompt)

    def test_client_side_limits_are_not_defenses(self):
        prompt = self.build()
        self.assertIn("클라이언트 측 제한", prompt)
        self.assertIn("내부망이라 안전하다", prompt)

    def test_angle_specific_rules_are_rendered(self):
        self.assertIn("자리표시자", self.build("data_exposure"))
        self.assertNotIn("자리표시자", self.build("authz"))

    def test_repo_docs_are_still_injected(self):
        prompt = self.build(agents_md="# 프로젝트\n스택: Spring Boot", review_md=REVIEW_MD)
        self.assertIn("스택: Spring Boot", prompt)
        self.assertIn("건너뛸 경로", prompt)

    def test_security_doc_goes_only_to_security_angles(self):
        rules = "금지 API: Runtime.exec(String)"
        self.assertIn(rules, self.build("authz", security_md=rules))
        self.assertNotIn(rules, self.build("logic", security_md=rules))

    def test_security_angle_without_the_doc_gets_a_placeholder(self):
        prompt = self.build("authz")
        self.assertIn("SECURITY-REVIEW.md 없음", prompt)
        self.assertNotIn("SECURITY-REVIEW.md 없음", self.build("logic"))

    def test_rereview_block_still_applies(self):
        from helpers import make_posted

        prompt = self.build(round_number=2, previous_findings=[make_posted(claim="이전 지적")])
        self.assertIn("재리뷰 지침 (라운드 2)", prompt)
        self.assertIn("이전 지적", prompt)

    def test_general_angles_keep_the_general_template(self):
        prompt = self.build("logic")
        self.assertIn("실제로 잘못 동작하거나 잘못될 수 있는 코드만", prompt)
        self.assertNotIn("공격자가 이 변경을 어떻게 악용할 수 있는가", prompt)


if __name__ == "__main__":
    unittest.main()


class SecurityDocSlotTest(unittest.TestCase):
    def test_load_repo_docs_reads_security_review(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "SECURITY-REVIEW.md"), "w", encoding="utf-8") as fh:
                fh.write("# 보안 규칙\n금지 API: MD5")
            docs = load_repo_docs(tmp)
            self.assertTrue(docs.has_security)
            self.assertEqual(docs.security_source, "SECURITY-REVIEW.md")
            self.assertIn("금지 API: MD5", docs.security_md)

    def test_missing_security_review_is_empty(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            docs = load_repo_docs(tmp)
            self.assertFalse(docs.has_security)
            self.assertEqual(docs.security_source, "")

    def test_context_block_omits_the_security_section_by_default(self):
        block = prompts.render_context_block("a", "r")
        self.assertNotIn("SECURITY-REVIEW.md", block)
