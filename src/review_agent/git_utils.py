"""git 호출 유틸. 전부 읽기 전용 명령만 쓴다."""

from __future__ import annotations

import re
import subprocess
from typing import Optional, Sequence

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class GitError(RuntimeError):
    pass


def run_git(repo_path: str, args: Sequence[str], check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} 실패 (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def is_git_repo(repo_path: str) -> bool:
    try:
        return run_git(repo_path, ["rev-parse", "--is-inside-work-tree"]).strip() == "true"
    except (GitError, FileNotFoundError):
        return False


def ref_exists(repo_path: str, ref: str) -> bool:
    if not ref:
        return False
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def resolve_sha(repo_path: str, ref: str) -> str:
    return run_git(repo_path, ["rev-parse", f"{ref}^{{commit}}"]).strip()


def first_existing_ref(repo_path: str, candidates: Sequence[str]) -> Optional[str]:
    for candidate in candidates:
        if candidate and ref_exists(repo_path, candidate):
            return candidate
    return None


def merge_base(repo_path: str, base: str, head: str) -> str:
    return run_git(repo_path, ["merge-base", base, head]).strip()


def is_ancestor(repo_path: str, candidate: str, ref: str) -> bool:
    """candidate가 ref의 조상인가.

    PR의 head가 base의 조상이면 그 PR은 이미 base에 머지되어 있고, 세 점 diff
    (`base...head`)가 빈 결과를 낸다. 그 상태를 진단하는 데 쓴다.
    """
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", candidate, ref],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def diff(repo_path: str, base: str, head: str, three_dot: bool = True) -> str:
    """base와 head 사이의 통합 diff.

    three_dot=True면 `base...head` (머지 베이스 기준, PR 전체 리뷰용),
    False면 `base..head` (마지막 리뷰 커밋 이후만 보는 재리뷰용).
    """
    separator = "..." if three_dot else ".."
    return run_git(repo_path, ["diff", "--unified=3", f"{base}{separator}{head}"])


def changed_files(repo_path: str, base: str, head: str, three_dot: bool = True) -> list[str]:
    separator = "..." if three_dot else ".."
    out = run_git(repo_path, ["diff", "--name-only", f"{base}{separator}{head}"])
    return [line.strip() for line in out.splitlines() if line.strip()]


def repo_slug_from_remote(repo_path: str) -> str:
    """origin 리모트에서 owner/name을 뽑는다. 못 찾으면 빈 문자열."""
    try:
        url = run_git(repo_path, ["remote", "get-url", "origin"]).strip()
    except (GitError, FileNotFoundError):
        return ""
    if not url:
        return ""
    url = url.removesuffix(".git")
    match = re.search(r"[:/]([^/:]+)/([^/]+)$", url)
    if not match:
        return ""
    return f"{match.group(1)}/{match.group(2)}"


def diff_line_map(diff_text: str) -> dict[str, set[int]]:
    """diff에 등장하는 새 파일(오른쪽) 라인 번호를 파일별로 모은다.

    GitHub 인라인 코멘트는 diff에 포함된 라인에만 달 수 있으므로 게시 단계에서 쓴다.
    추가된 줄과 문맥 줄을 모두 포함한다.
    """
    result: dict[str, set[int]] = {}
    current: Optional[str] = None
    new_line = 0

    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path == "/dev/null":
                current = None
            else:
                current = path[2:] if path.startswith(("a/", "b/")) else path
                result.setdefault(current, set())
            continue
        if line.startswith("@@"):
            match = HUNK_RE.match(line)
            if match:
                new_line = int(match.group(1))
            continue
        if current is None:
            continue
        if line.startswith("+"):
            result[current].add(new_line)
            new_line += 1
        elif line.startswith("-"):
            continue
        elif line.startswith("\\"):
            continue
        elif line.startswith(" "):
            result[current].add(new_line)
            new_line += 1

    return result
