"""CLI 인자 파싱. 실제 리뷰는 돌리지 않는다.

여기에 테스트가 없어서 `review run ... -v`가 거부되는 버그를 실제 실행에서야 발견했다.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import unittest

import helpers  # noqa: F401

from review_agent.cli import (
    _cmd_show,
    _normalize_argv,
    _resolve_cache_dir,
    build_parser,
)


def parse(argv: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(_normalize_argv(argv))


def expect_exit(testcase, argv: list[str]) -> None:
    """argparse가 usage를 stderr로 쏟지 않게 눌러 둔다."""
    with contextlib.redirect_stderr(io.StringIO()):
        with testcase.assertRaises(SystemExit):
            build_parser().parse_args(argv)


class SubcommandTest(unittest.TestCase):
    def test_run_is_the_default_subcommand(self):
        args = parse(["--repo", ".", "--pr", "7"])
        self.assertEqual(args.command, "run")
        self.assertEqual(args.pr, 7)

    def test_explicit_subcommand_still_works(self):
        self.assertEqual(parse(["run", "--repo", ".", "--pr", "7"]).command, "run")
        self.assertEqual(
            parse(["show", "--repo", ".", "--pr", "7"]).command, "show"
        )

    def test_normalize_leaves_help_alone(self):
        self.assertEqual(_normalize_argv(["--help"]), ["--help"])
        self.assertEqual(_normalize_argv([]), [])

    def test_normalize_does_not_prefix_a_bare_word(self):
        self.assertEqual(_normalize_argv(["feedback", "--pr", "1"])[0], "feedback")


class VerbosityTest(unittest.TestCase):
    """`-v`가 서브커맨드 앞뒤 어디에 와도 받아야 한다."""

    def test_verbose_before_subcommand(self):
        self.assertEqual(parse(["-v", "run", "--repo", ".", "--pr", "1"]).verbose, 1)

    def test_verbose_after_subcommand(self):
        self.assertEqual(parse(["run", "--repo", ".", "--pr", "1", "-v"]).verbose, 1)

    def test_verbose_with_implicit_run(self):
        self.assertEqual(parse(["--repo", ".", "--pr", "1", "-v"]).verbose, 1)

    def test_double_verbose(self):
        self.assertEqual(parse(["run", "--repo", ".", "--pr", "1", "-vv"]).verbose, 2)

    def test_default_is_quiet(self):
        self.assertEqual(parse(["run", "--repo", ".", "--pr", "1"]).verbose, 0)


class PostModeTest(unittest.TestCase):
    def test_dry_run_is_the_default(self):
        args = parse(["--repo", ".", "--pr", "1"])
        self.assertTrue(args.dry_run)
        self.assertFalse(args.post)

    def test_post_is_explicit(self):
        self.assertTrue(parse(["--repo", ".", "--pr", "1", "--post"]).post)

    def test_dry_run_and_post_are_mutually_exclusive(self):
        expect_exit(self, ["run", "--repo", ".", "--pr", "1", "--post", "--dry-run"])


class WorktreeFlagTest(unittest.TestCase):
    def test_worktree_is_on_by_default(self):
        self.assertTrue(parse(["--repo", ".", "--pr", "1"]).use_worktree)

    def test_no_worktree_turns_it_off(self):
        self.assertFalse(parse(["--repo", ".", "--pr", "1", "--no-worktree"]).use_worktree)


class CacheFlagTest(unittest.TestCase):
    def test_cache_is_off_unless_asked(self):
        args = parse(["--repo", ".", "--pr", "1"])
        self.assertIsNone(args.cache)
        self.assertIsNone(_resolve_cache_dir(args, "/tmp/state.db"))

    def test_bare_cache_flag_uses_the_state_directory(self):
        args = parse(["--repo", ".", "--pr", "1", "--cache"])
        self.assertEqual(args.cache, "")
        self.assertEqual(
            _resolve_cache_dir(args, "/tmp/review/state.db"),
            os.path.join("/tmp/review", "cache"),
        )

    def test_cache_with_explicit_directory(self):
        args = parse(["--repo", ".", "--pr", "1", "--cache", "/tmp/mycache"])
        self.assertEqual(_resolve_cache_dir(args, "/tmp/state.db"), "/tmp/mycache")

    def test_bare_cache_flag_does_not_swallow_the_next_option(self):
        """--cache 뒤에 다른 플래그가 와도 그것을 값으로 먹지 않아야 한다."""
        args = parse(["--repo", ".", "--pr", "1", "--cache", "--no-worktree"])
        self.assertEqual(args.cache, "")
        self.assertFalse(args.use_worktree)


class ModelOverrideTest(unittest.TestCase):
    def test_model_override_is_optional(self):
        self.assertIsNone(parse(["--repo", ".", "--pr", "1"]).model)

    def test_model_override_is_read(self):
        self.assertEqual(
            parse(["--repo", ".", "--pr", "1", "--model", "other"]).model, "other"
        )


class FeedbackArgsTest(unittest.TestCase):
    def test_reaction_must_be_known(self):
        args = parse(
            ["feedback", "--repo", ".", "--pr", "1", "--finding", "f-001",
             "--reaction", "fixed"]
        )
        self.assertEqual(args.reaction, "fixed")

    def test_unknown_reaction_is_rejected(self):
        expect_exit(
            self,
            ["feedback", "--repo", ".", "--pr", "1", "--finding", "f-001",
             "--reaction", "lgtm"],
        )

    def test_round_can_narrow_the_lookup(self):
        args = parse(
            ["feedback", "--repo", ".", "--pr", "1", "--finding", "f-001",
             "--reaction", "wrong", "--round", "2"]
        )
        self.assertEqual(args.round, 2)


class ShowArgsTest(unittest.TestCase):
    def test_pr_is_optional_for_show(self):
        self.assertIsNone(parse(["show", "--repo", "."]).pr)


if __name__ == "__main__":
    unittest.main()


class BackendProfileFlagTest(unittest.TestCase):
    """로컬은 구독 크레딧, CI는 키/토큰. 둘을 플래그로 고를 수 있어야 한다."""

    def test_backend_and_profile_are_optional(self):
        args = parse(["--repo", ".", "--pr", "1"])
        self.assertIsNone(args.backend)
        self.assertIsNone(args.profile)

    def test_backend_is_read(self):
        self.assertEqual(parse(["--repo", ".", "--pr", "1", "--backend", "claude"]).backend, "claude")
        self.assertEqual(parse(["--repo", ".", "--pr", "1", "--backend", "codex"]).backend, "codex")

    def test_profile_is_read(self):
        self.assertEqual(parse(["--repo", ".", "--pr", "1", "--profile", "ci"]).profile, "ci")
        self.assertEqual(parse(["--repo", ".", "--pr", "1", "--profile", "local"]).profile, "local")

    def test_unknown_backend_is_rejected_by_the_parser(self):
        expect_exit(self, ["run", "--repo", ".", "--pr", "1", "--backend", "gemini"])

    def test_unknown_profile_is_rejected_by_the_parser(self):
        expect_exit(self, ["run", "--repo", ".", "--pr", "1", "--profile", "staging"])

    def test_backend_and_model_can_be_combined(self):
        args = parse(["--repo", ".", "--pr", "1", "--backend", "claude", "--model", "other"])
        self.assertEqual((args.backend, args.model), ("claude", "other"))

    def test_show_accepts_backend_and_profile(self):
        args = parse(["show", "--repo", ".", "--backend", "claude", "--profile", "ci"])
        self.assertEqual((args.command, args.backend, args.profile), ("show", "claude", "ci"))


class ShowOutputTest(unittest.TestCase):
    """`review show`는 설정이 실제로 어떻게 해석됐는지 보여 주는 유일한 수단이다."""

    def show(self, review_yaml: str = "", **files: str) -> str:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            if review_yaml:
                with open(os.path.join(tmp, "review.yaml"), "w", encoding="utf-8") as fh:
                    fh.write(review_yaml)
            for name, body in files.items():
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                    fh.write(body)
            args = parse(["show", "--repo", tmp])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(_cmd_show(args), 0)
            return out.getvalue()

    def test_default_lists_only_general_angles(self):
        body = self.show()
        self.assertIn("logic(general)", body)
        self.assertNotIn("(security)", body)
        self.assertNotIn("보안 검증:", body)

    def test_security_angles_are_listed_with_their_kind(self):
        body = self.show("security_angles: [authz, injection]\n")
        self.assertIn("탐지 5 /", body)
        self.assertIn("authz(security)", body)
        self.assertIn("보안 검증: reachability, existing_defense, blast_radius", body)
        self.assertIn("통과 기준 3", body)

    def test_angles_escape_hatch_shows_the_missing_general_angles(self):
        # 이 출력이 범용 리뷰가 꺼진 설정을 알아챌 수 있는 유일한 지점이다.
        body = self.show("angles: [authz]\n")
        self.assertIn("관점: authz(security)", body)
        self.assertNotIn("logic(general)", body)
        self.assertIn("탐지 1 /", body)

    def test_security_review_doc_presence_is_reported(self):
        self.assertIn("SECURITY-REVIEW.md: 없음", self.show())
        self.assertIn(
            "SECURITY-REVIEW.md: 있음",
            self.show("", **{"SECURITY-REVIEW.md": "# 보안 규칙\n"}),
        )

    def test_gate_is_only_printed_when_configured(self):
        self.assertNotIn("게이트:", self.show())
        body = self.show("gate:\n  skip_if_only: [docs/]\n")
        self.assertIn("게이트:", body)
        self.assertIn("건너뜀 docs/", body)


class DroppedLineTest(unittest.TestCase):
    """걸러낸 후보 상세. 투표 미달의 표 지수가 보여야 임계값을 조정할 수 있다."""

    def line(self, **kwargs) -> str:
        from helpers import make_finding
        from review_agent.cli import _dropped_line
        from review_agent.models import Dropped

        detail = kwargs.pop("detail", "")
        reason = kwargs.pop("reason", "투표_미달")
        return _dropped_line(Dropped(make_finding(**kwargs), reason, detail))

    def test_shows_the_vote_split(self):
        out = self.line(detail="real 2/3 (보안 임계값 3)", category="security")
        self.assertIn("real 2/3", out)
        self.assertIn("보안 임계값 3", out)

    def test_shows_location_and_category(self):
        out = self.line(file="src/a.py", line_start=10, line_end=12, category="security")
        self.assertIn("src/a.py:10-12", out)
        self.assertIn("(security)", out)

    def test_single_line_location_has_no_range(self):
        self.assertIn("src/a.py:10 ", self.line(file="src/a.py", line_start=10))

    def test_long_claim_is_truncated_to_one_line(self):
        out = self.line(claim="가" * 200)
        self.assertLess(len(out), 140)
        self.assertIn("...", out)

    def test_newlines_in_claim_are_collapsed(self):
        out = self.line(claim="첫 줄\n둘째 줄")
        self.assertNotIn("\n", out)
        self.assertIn("첫 줄 둘째 줄", out)

    def test_works_without_a_detail(self):
        self.assertIn("src/app.py", self.line(detail=""))
