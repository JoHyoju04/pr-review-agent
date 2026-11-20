"""리뷰 대상 트리 준비.

**원칙: 리뷰 대상은 PR head 커밋이고, 사람의 워킹트리 상태는 결과에 영향을 주지 않는다.**

`git diff base...head`는 히스토리에서 나오지만, 탐지 프롬프트는 "호출처와 연관 파일을 열어
보라"고 지시하므로 LLM은 **체크아웃된 파일**을 읽는다. 둘이 다른 시점이면 인용 라인 번호가
diff와 어긋나고(인라인 코멘트가 엉뚱한 줄에 붙는다), 워킹트리가 바뀔 때마다 결과가 달라진다.

그래서 head가 현재 체크아웃과 다르면 head 커밋의 임시 worktree를 만들어 거기서 LLM을 돌린다.
단 `AGENTS.md` / `REVIEW.md` / `review.yaml`은 **원본 레포에서** 읽는다. 그것은 "리뷰 대상
코드"가 아니라 "지금 적용할 리뷰 규칙"이고, 커밋되지 않은 REVIEW.md도 읽혀야 한다.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from . import git_utils

logger = logging.getLogger(__name__)

TMPDIR_ENV = "REVIEW_AGENT_TMPDIR"
TREE_PREFIX = "review-agent-tree-"
PID_FILENAME = "owner.pid"


@dataclass
class ReviewTree:
    """LLM이 파일을 읽을 경로."""

    path: str
    temporary: bool = False
    head_sha: str = ""
    reason: str = ""


def _temp_base() -> Optional[str]:
    base = os.environ.get(TMPDIR_ENV)
    if base and os.path.isdir(base):
        return base
    return None


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # 다른 사용자 소유지만 살아 있다
        return True
    except OSError:  # pragma: no cover
        return True
    return True


def _owner_alive(holder: str) -> bool:
    """holder에 남은 PID 파일로 이전 실행이 아직 살아 있는지 본다."""
    pid_path = os.path.join(holder, PID_FILENAME)
    try:
        with open(pid_path, encoding="utf-8") as handle:
            pid = int(handle.read().strip())
    except (OSError, ValueError):
        return False
    return _pid_is_alive(pid)


def list_tree_paths(repo_path: str) -> list[str]:
    """이 레포에 등록된 worktree 경로 목록."""
    try:
        out = git_utils.run_git(repo_path, ["worktree", "list", "--porcelain"])
    except (git_utils.GitError, FileNotFoundError):
        return []
    return [
        line[len("worktree ") :].strip()
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


def prune_stale_trees(repo_path: str) -> list[str]:
    """강제 종료 때 남은 리뷰 트리를 정리한다.

    프로세스가 SIGKILL로 죽으면 `finally`가 돌지 않아 worktree가 등록된 채 남는다.
    디렉터리가 아직 있으면 `git worktree prune`으로도 사라지지 않는다.

    소유 프로세스가 아직 살아 있으면(동시 실행) 건드리지 않는다.
    """
    removed: list[str] = []
    for path in list_tree_paths(repo_path):
        if TREE_PREFIX not in path:
            continue
        if os.path.realpath(path) == os.path.realpath(repo_path):
            continue
        holder = os.path.dirname(path)
        if _owner_alive(holder):
            logger.debug("다른 실행이 쓰고 있는 트리라 건너뜁니다: %s", path)
            continue
        try:
            git_utils.run_git(repo_path, ["worktree", "remove", "--force", path])
        except (git_utils.GitError, FileNotFoundError) as exc:
            logger.warning("남은 리뷰 트리를 지우지 못했습니다: %s (%s)", path, exc)
            continue
        shutil.rmtree(holder, ignore_errors=True)
        removed.append(path)
        logger.info("이전 실행이 남긴 리뷰 트리를 정리했습니다: %s", path)

    if removed:
        try:
            git_utils.run_git(repo_path, ["worktree", "prune"], check=False)
        except FileNotFoundError:  # pragma: no cover
            pass
    return removed


@contextmanager
def review_tree(
    repo_path: str,
    head: str,
    *,
    enabled: bool = True,
    base_dir: Optional[str] = None,
) -> Iterator[ReviewTree]:
    """head 커밋 상태의 트리를 준비한다.

    - `enabled=False`면 원본 레포를 그대로 쓴다(커밋하지 않은 변경을 리뷰할 때).
    - head가 현재 체크아웃과 같으면 worktree를 만들지 않는다(불필요한 복사 회피).
    - worktree를 만들 수 없으면 경고하고 원본 레포로 되돌린다. 리뷰 자체를 실패시키지 않는다.
    """
    if not enabled:
        yield ReviewTree(repo_path, False, reason="--no-worktree: 현재 워킹트리를 그대로 리뷰")
        return

    try:
        head_sha = git_utils.resolve_sha(repo_path, head)
    except (git_utils.GitError, FileNotFoundError) as exc:
        logger.warning("head 커밋을 확인하지 못해 현재 워킹트리를 씁니다: %s", exc)
        yield ReviewTree(repo_path, False, reason=f"head 확인 실패: {exc}")
        return

    try:
        current = git_utils.resolve_sha(repo_path, "HEAD")
    except (git_utils.GitError, FileNotFoundError):
        current = ""

    if current and current == head_sha:
        yield ReviewTree(repo_path, False, head_sha, "현재 체크아웃이 이미 head")
        return

    # 강제 종료로 남은 이전 트리를 먼저 치운다.
    prune_stale_trees(repo_path)

    holder = tempfile.mkdtemp(prefix=TREE_PREFIX, dir=base_dir or _temp_base())
    with open(os.path.join(holder, PID_FILENAME), "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    tree_path = os.path.join(holder, "head")
    try:
        git_utils.run_git(repo_path, ["worktree", "add", "--detach", tree_path, head_sha])
    except (git_utils.GitError, FileNotFoundError) as exc:
        shutil.rmtree(holder, ignore_errors=True)
        logger.warning(
            "worktree를 만들 수 없어 현재 워킹트리를 씁니다. 인용 라인이 PR과 어긋날 수 "
            "있습니다: %s",
            exc,
        )
        yield ReviewTree(repo_path, False, head_sha, f"worktree 실패: {exc}")
        return

    logger.info("head %s 의 임시 worktree에서 리뷰합니다: %s", head_sha[:12], tree_path)
    try:
        yield ReviewTree(tree_path, True, head_sha, "head 커밋의 임시 worktree")
    finally:
        _cleanup(repo_path, tree_path, holder)


def _cleanup(repo_path: str, tree_path: str, holder: str) -> None:
    try:
        git_utils.run_git(repo_path, ["worktree", "remove", "--force", tree_path])
    except (git_utils.GitError, FileNotFoundError) as exc:
        logger.warning("worktree 정리 실패, 디렉터리를 직접 지웁니다: %s", exc)
    shutil.rmtree(holder, ignore_errors=True)
    try:
        git_utils.run_git(repo_path, ["worktree", "prune"], check=False)
    except FileNotFoundError:  # pragma: no cover
        pass
