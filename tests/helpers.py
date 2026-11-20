"""테스트 공용 헬퍼: 임시 git 레포와 finding 팩토리."""

from __future__ import annotations

import logging
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

# 오류 경로를 일부러 밟는 테스트가 많아 엔진 로그는 테스트 중에 끈다.
logging.getLogger("review_agent").setLevel(logging.CRITICAL)

from review_agent.models import Finding, PostedRef, Verdict, VoteResult  # noqa: E402


def make_finding(
    file: str = "src/app.py",
    line_start: int = 10,
    line_end: int | None = None,
    category: str = "logic",
    severity: str = "important",
    claim: str = "널 체크가 없어 NPE가 발생한다",
    evidence: str = "src/app.py:10 `user.getName()`",
    suggested_fix: str = "널 체크를 추가한다",
    detected_by: list[str] | None = None,
    finding_id: str = "",
) -> Finding:
    return Finding(
        file=file,
        line_start=line_start,
        line_end=line_start if line_end is None else line_end,
        category=category,
        severity=severity,
        claim=claim,
        evidence=evidence,
        suggested_fix=suggested_fix,
        detected_by=list(detected_by or ["로직·엣지케이스"]),
        id=finding_id,
    )


def make_posted(
    file: str = "src/app.py",
    line_start: int = 10,
    line_end: int | None = None,
    category: str = "logic",
    claim: str = "이전 라운드에 게시한 지적",
) -> PostedRef:
    end = line_start if line_end is None else line_end
    return PostedRef(
        file=file,
        line_start=line_start,
        line_end=end,
        category=category,
        fingerprint=f"{file}:{line_start}-{end}:{category}",
        claim=claim,
    )


def make_votes(finding: Finding, verdicts: list[str]) -> VoteResult:
    return VoteResult(
        finding=finding,
        verdicts=[Verdict(check=f"검증{i}", verdict=v) for i, v in enumerate(verdicts)],
    )


def run(cmd: list[str], cwd: str) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} 실패: {proc.stderr}")
    return proc.stdout.strip()


def init_repo(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    run(["git", "init", "-q", "-b", "main"], path)
    run(["git", "config", "user.email", "test@example.com"], path)
    run(["git", "config", "user.name", "Review Agent Test"], path)
    run(["git", "config", "commit.gpgsign", "false"], path)


def write_file(repo: str, relpath: str, content: str) -> None:
    full = os.path.join(repo, relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(content)


def commit_all(repo: str, message: str) -> str:
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "-q", "-m", message], repo)
    return run(["git", "rev-parse", "HEAD"], repo)
