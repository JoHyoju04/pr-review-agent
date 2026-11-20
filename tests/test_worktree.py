"""리뷰 대상 트리가 항상 PR head 기준인지 본다."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from helpers import commit_all, init_repo, run, write_file

from review_agent import git_utils
from review_agent.worktree import (
    PID_FILENAME,
    TREE_PREFIX,
    list_tree_paths,
    prune_stale_trees,
    review_tree,
)


class WorktreeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        init_repo(self.repo)

        write_file(self.repo, "src/app.py", "VERSION = 'head'\n")
        self.head = commit_all(self.repo, "head commit")

        write_file(self.repo, "src/app.py", "VERSION = 'later'\n")
        self.later = commit_all(self.repo, "later commit")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self, root: str, relpath: str) -> str:
        with open(os.path.join(root, relpath), encoding="utf-8") as handle:
            return handle.read()

    def test_disabled_uses_the_working_tree_as_is(self):
        with review_tree(self.repo, self.head, enabled=False) as tree:
            self.assertEqual(tree.path, self.repo)
            self.assertFalse(tree.temporary)
            self.assertIn("no-worktree", tree.reason)

    def test_head_equal_to_checkout_needs_no_worktree(self):
        with review_tree(self.repo, self.later) as tree:
            self.assertEqual(tree.path, self.repo)
            self.assertFalse(tree.temporary)

    def test_older_head_gets_its_own_tree(self):
        """핵심: 워킹트리는 later인데 리뷰 대상은 head여야 한다."""
        self.assertEqual(self.read(self.repo, "src/app.py"), "VERSION = 'later'\n")

        with review_tree(self.repo, self.head) as tree:
            self.assertTrue(tree.temporary)
            self.assertNotEqual(os.path.abspath(tree.path), os.path.abspath(self.repo))
            self.assertEqual(self.read(tree.path, "src/app.py"), "VERSION = 'head'\n")
            self.assertEqual(tree.head_sha, self.head)

        # 원본 워킹트리는 건드리지 않는다
        self.assertEqual(self.read(self.repo, "src/app.py"), "VERSION = 'later'\n")

    def test_working_tree_edits_do_not_leak_into_the_review(self):
        """워킹트리를 더럽혀도 리뷰 대상은 PR head 그대로여야 한다."""
        write_file(self.repo, "src/app.py", "VERSION = 'dirty uncommitted'\n")
        with review_tree(self.repo, self.head) as tree:
            self.assertEqual(self.read(tree.path, "src/app.py"), "VERSION = 'head'\n")

    def test_untracked_files_are_absent_from_the_tree(self):
        """REVIEW.md가 untracked인 경우가 있어서 문서는 원본에서 읽어야 한다."""
        write_file(self.repo, "REVIEW.md", "# REVIEW.md\n")
        with review_tree(self.repo, self.head) as tree:
            self.assertTrue(os.path.exists(os.path.join(self.repo, "REVIEW.md")))
            self.assertFalse(os.path.exists(os.path.join(tree.path, "REVIEW.md")))

    def test_tree_is_cleaned_up(self):
        with review_tree(self.repo, self.head) as tree:
            path = tree.path
            self.assertTrue(os.path.isdir(path))

        self.assertFalse(os.path.exists(path))
        listed = run(["git", "worktree", "list"], self.repo)
        self.assertNotIn(path, listed)
        self.assertEqual(len(listed.strip().splitlines()), 1)

    def test_cleanup_runs_even_on_exception(self):
        path = ""
        with self.assertRaises(RuntimeError):
            with review_tree(self.repo, self.head) as tree:
                path = tree.path
                raise RuntimeError("리뷰 중 오류")
        self.assertFalse(os.path.exists(path))

    def test_unknown_head_falls_back_without_raising(self):
        with review_tree(self.repo, "0" * 40) as tree:
            self.assertEqual(tree.path, self.repo)
            self.assertFalse(tree.temporary)
            self.assertTrue(tree.reason)

    def test_git_history_is_reachable_from_the_tree(self):
        """worktree에서도 diff를 계산할 수 있어야 한다."""
        with review_tree(self.repo, self.head) as tree:
            out = git_utils.run_git(tree.path, ["rev-parse", "HEAD"]).strip()
            self.assertEqual(out, self.head)


if __name__ == "__main__":
    unittest.main()


class PruneStaleTreesTest(unittest.TestCase):
    """강제 종료로 남은 리뷰 트리를 다음 실행이 치우는지."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        init_repo(self.repo)
        write_file(self.repo, "src/app.py", "VERSION = 'head'\n")
        self.head = commit_all(self.repo, "head")
        write_file(self.repo, "src/app.py", "VERSION = 'later'\n")
        commit_all(self.repo, "later")

    def tearDown(self) -> None:
        for path in list_tree_paths(self.repo):
            if TREE_PREFIX in path and os.path.exists(path):
                run(["git", "worktree", "remove", "--force", path], self.repo)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _orphan(self, pid: str) -> str:
        """정리되지 않은 트리를 손으로 만든다 (강제 종료 상황 재현)."""
        holder = tempfile.mkdtemp(prefix=TREE_PREFIX, dir=self.tmp)
        with open(os.path.join(holder, PID_FILENAME), "w", encoding="utf-8") as handle:
            handle.write(pid)
        tree = os.path.join(holder, "head")
        run(["git", "worktree", "add", "--detach", tree, self.head], self.repo)
        # git은 realpath를 돌려준다 (macOS /var -> /private/var)
        return os.path.realpath(tree)

    def test_dead_owner_tree_is_removed(self):
        # 존재할 수 없는 PID로 죽은 소유자를 흉내 낸다
        orphan = self._orphan("999999")
        self.assertIn(orphan, list_tree_paths(self.repo))

        removed = prune_stale_trees(self.repo)

        self.assertIn(orphan, removed)
        self.assertFalse(os.path.exists(orphan))
        self.assertNotIn(orphan, list_tree_paths(self.repo))

    def test_live_owner_tree_is_left_alone(self):
        """동시 실행 중인 다른 리뷰의 트리를 지워서는 안 된다."""
        orphan = self._orphan(str(os.getpid()))
        removed = prune_stale_trees(self.repo)
        self.assertEqual(removed, [])
        self.assertTrue(os.path.exists(orphan))

    def test_main_worktree_is_never_touched(self):
        prune_stale_trees(self.repo)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "src/app.py")))
        self.assertIn(os.path.realpath(self.repo), " ".join(list_tree_paths(self.repo)))

    def test_new_run_prunes_before_creating(self):
        orphan = self._orphan("999999")
        with review_tree(self.repo, self.head) as tree:
            self.assertTrue(tree.temporary)
            self.assertFalse(os.path.exists(orphan))

    def test_pid_file_is_written_for_the_new_tree(self):
        with review_tree(self.repo, self.head) as tree:
            pid_path = os.path.join(os.path.dirname(tree.path), PID_FILENAME)
            with open(pid_path, encoding="utf-8") as handle:
                self.assertEqual(handle.read().strip(), str(os.getpid()))
