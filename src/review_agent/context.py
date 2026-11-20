"""대상 레포의 컨텍스트 로더: diff, PR 메타데이터, AGENTS.md, REVIEW.md, review.yaml."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional, Sequence

from . import git_utils
from .config import ReviewConfig, load_config
from .models import PRMeta, PostedRef

logger = logging.getLogger(__name__)

AGENTS_FILENAMES = ("AGENTS.md", "CLAUDE.md")
REVIEW_FILENAME = "REVIEW.md"
# 보안 리뷰에만 주입하는 사내 규칙 문서. 쓰는 인증 방식, 금지 API, 데이터 분류 기준처럼
# 엔진이 알 수 없는 레포 사정이 여기로 들어온다.
SECURITY_REVIEW_FILENAME = "SECURITY-REVIEW.md"

_SKIP_HEADINGS = ("건너뛸 경로", "건너뛸경로", "skip path", "skip paths", "ignore path", "제외 경로")
_NIT_LIMIT_WORDS = ("최대", "상한", "이내", "까지", "max", "limit", "at most", "no more")


@dataclass
class RepoDocs:
    """레포 루트에서 읽은 지침 문서."""

    agents_md: str = ""
    agents_source: str = ""
    review_md: str = ""
    review_source: str = ""
    security_md: str = ""
    security_source: str = ""

    @property
    def has_agents(self) -> bool:
        return bool(self.agents_md.strip())

    @property
    def has_review(self) -> bool:
        return bool(self.review_md.strip())

    @property
    def has_security(self) -> bool:
        return bool(self.security_md.strip())


@dataclass
class ReviewGuidance:
    """REVIEW.md에서 코드가 직접 쓰는 항목."""

    skip_paths: list[str] = field(default_factory=list)
    max_nits: Optional[int] = None


def parse_review_md(text: str) -> ReviewGuidance:
    """REVIEW.md에서 건너뛸 경로와 nit 상한을 뽑는다.

    나머지(우선순위, 실패의 정의, 코멘트 규칙, 요약 형식)는 프롬프트에 원문으로 주입한다.
    """
    guidance = ReviewGuidance()
    if not text:
        return guidance

    in_skip_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()

        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            in_skip_section = any(key in heading for key in _SKIP_HEADINGS)
            continue

        if in_skip_section and line.startswith(("-", "*", "+")):
            item = line[1:].strip()
            for part in item.split(","):
                cleaned = part.strip().strip("`").strip()
                if cleaned:
                    guidance.skip_paths.append(cleaned)
            continue

        if guidance.max_nits is None and "nit" in line.lower():
            lowered = line.lower()
            if any(word in lowered for word in _NIT_LIMIT_WORDS):
                match = re.search(r"(\d+)", line)
                if match:
                    guidance.max_nits = int(match.group(1))

    return guidance


def load_repo_docs(repo_path: str) -> RepoDocs:
    """AGENTS.md(없으면 CLAUDE.md), REVIEW.md, SECURITY-REVIEW.md를 읽는다.

    없으면 빈 문자열. SECURITY-REVIEW.md는 보안 프롬프트에만 주입된다 (범용 프롬프트에
    넣으면 범용 리뷰어가 보안 지적을 하게 되고, 그러면 관점을 나눈 의미가 없어진다).
    """
    docs = RepoDocs()
    for name in AGENTS_FILENAMES:
        path = os.path.join(repo_path, name)
        if os.path.isfile(path):
            docs.agents_md = _read_text(path)
            docs.agents_source = name
            break

    review_path = os.path.join(repo_path, REVIEW_FILENAME)
    if os.path.isfile(review_path):
        docs.review_md = _read_text(review_path)
        docs.review_source = REVIEW_FILENAME

    security_path = os.path.join(repo_path, SECURITY_REVIEW_FILENAME)
    if os.path.isfile(security_path):
        docs.security_md = _read_text(security_path)
        docs.security_source = SECURITY_REVIEW_FILENAME

    return docs


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def resolve_max_nits(config: ReviewConfig, guidance: ReviewGuidance) -> int:
    """nit 상한 우선순위: review.yaml 명시값 > REVIEW.md > 기본값."""
    if "max_nits" in config.explicit_keys:
        return config.max_nits
    if guidance.max_nits is not None:
        return guidance.max_nits
    return config.max_nits


@dataclass
class ReviewContext:
    """한 번의 리뷰 실행에 필요한 모든 입력.

    `repo_path`와 `docs_path`는 다를 수 있다.
    - `repo_path`: LLM이 파일을 읽는 경로. PR head 커밋의 임시 worktree일 수 있다.
    - `docs_path`: 원본 레포. AGENTS.md / REVIEW.md / review.yaml과 git 명령은 여기서 쓴다.
      리뷰 규칙은 "지금의 규칙"이어야 하고, 커밋되지 않은 REVIEW.md도 읽혀야 한다.
    """

    repo_path: str
    repo_key: str
    pr: PRMeta
    config: ReviewConfig
    docs: RepoDocs
    guidance: ReviewGuidance
    diff_text: str
    base: str
    head: str
    round: int = 1
    diff_only: bool = False
    last_reviewed_sha: str = ""
    previous_findings: list[PostedRef] = field(default_factory=list)
    docs_path: str = ""
    tree_reason: str = ""

    def __post_init__(self) -> None:
        if not self.docs_path:
            self.docs_path = self.repo_path

    @property
    def git_path(self) -> str:
        """git 명령을 실행할 경로. 항상 원본 레포다."""
        return self.docs_path

    @property
    def uses_separate_tree(self) -> bool:
        return os.path.realpath(self.repo_path) != os.path.realpath(self.docs_path)

    @property
    def skip_paths(self) -> list[str]:
        merged = list(self.guidance.skip_paths)
        for pattern in self.config.skip_paths:
            if pattern not in merged:
                merged.append(pattern)
        return merged

    @property
    def diff_range(self) -> str:
        separator = ".." if self.diff_only else "..."
        return f"{self.base}{separator}{self.head}"


class PRMetaError(RuntimeError):
    pass


def load_pr_meta(
    repo_path: str,
    pr_number: int,
    *,
    repo_slug: str = "",
    title: Optional[str] = None,
    body: Optional[str] = None,
    base: Optional[str] = None,
    head: Optional[str] = None,
    gh_path: str = "gh",
) -> PRMeta:
    """PR 메타데이터를 gh CLI로 읽는다.

    title/body/base/head를 직접 주면 그 값이 우선하며, 네 값이 모두 주어지면 gh를 호출하지 않는다.
    """
    slug = repo_slug or git_utils.repo_slug_from_remote(repo_path)
    meta = PRMeta(
        number=pr_number,
        title=title or "",
        description=body or "",
        base=base or "",
        head=head or "",
        repo_slug=slug,
    )

    needs_gh = not (title is not None and body is not None and base and head)
    if not needs_gh:
        return meta

    data = _fetch_pr_json(repo_path, pr_number, slug, gh_path)
    if data is None:
        missing = [
            name
            for name, value in (("base", meta.base), ("head", meta.head))
            if not value
        ]
        if missing:
            raise PRMetaError(
                "PR 메타데이터를 읽을 수 없습니다. gh CLI로 인증하거나 "
                f"--base/--head(그리고 --pr-title/--pr-body)를 직접 넘기세요. (누락: {', '.join(missing)})"
            )
        return meta

    if title is None:
        meta.title = str(data.get("title") or "")
    if body is None:
        meta.description = str(data.get("body") or "")
    if not meta.repo_slug:
        repo_info = data.get("headRepository") or {}
        owner = (data.get("headRepositoryOwner") or {}).get("login")
        name = repo_info.get("name")
        if owner and name:
            meta.repo_slug = f"{owner}/{name}"

    if not meta.base:
        base_ref = str(data.get("baseRefName") or "")
        meta.base = (
            git_utils.first_existing_ref(
                repo_path,
                [f"origin/{base_ref}", base_ref, str(data.get("baseRefOid") or "")],
            )
            or str(data.get("baseRefOid") or "")
        )
    if not meta.head:
        head_ref = str(data.get("headRefName") or "")
        meta.head = (
            git_utils.first_existing_ref(
                repo_path,
                [str(data.get("headRefOid") or ""), f"origin/{head_ref}", head_ref],
            )
            or str(data.get("headRefOid") or "")
        )

    if not meta.base or not meta.head:
        raise PRMetaError(
            "PR의 base/head 커밋을 레포에서 찾지 못했습니다. git fetch 후 다시 시도하거나 "
            "--base/--head를 직접 넘기세요."
        )
    return meta


def _fetch_pr_json(
    repo_path: str, pr_number: int, slug: str, gh_path: str
) -> Optional[dict]:
    if not shutil.which(gh_path):
        logger.warning("gh CLI를 찾을 수 없어 PR 메타데이터를 읽지 못했습니다")
        return None

    fields = (
        "number,title,body,baseRefName,baseRefOid,headRefName,headRefOid,"
        "headRepository,headRepositoryOwner"
    )
    args = [gh_path, "pr", "view", str(pr_number), "--json", fields]
    if slug:
        args += ["--repo", slug]

    proc = subprocess.run(args, cwd=repo_path, capture_output=True, text=True)
    if proc.returncode != 0:
        logger.warning("gh pr view 실패: %s", proc.stderr.strip()[:500])
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning("gh pr view 출력을 JSON으로 읽지 못했습니다")
        return None


class ContextLoader:
    """리뷰 컨텍스트를 구성한다."""

    def __init__(self, gh_path: str = "gh") -> None:
        self.gh_path = gh_path

    def load(
        self,
        repo_path: str,
        pr_number: int,
        *,
        config_path: Optional[str] = None,
        repo_slug: str = "",
        pr_title: Optional[str] = None,
        pr_body: Optional[str] = None,
        base: Optional[str] = None,
        head: Optional[str] = None,
        round_number: int = 1,
        diff_only: bool = False,
        last_reviewed_sha: str = "",
        previous_findings: Sequence[PostedRef] = (),
        config: Optional[ReviewConfig] = None,
        tree_path: Optional[str] = None,
        tree_reason: str = "",
    ) -> ReviewContext:
        repo_path = os.path.abspath(os.path.expanduser(repo_path))
        if not git_utils.is_git_repo(repo_path):
            raise PRMetaError(f"git 레포가 아닙니다: {repo_path}")

        if config is None:
            config = load_config(repo_path, config_path)
        docs = load_repo_docs(repo_path)
        guidance = parse_review_md(docs.review_md)

        meta = load_pr_meta(
            repo_path,
            pr_number,
            repo_slug=repo_slug,
            title=pr_title,
            body=pr_body,
            base=base,
            head=head,
            gh_path=self.gh_path,
        )

        diff_base = meta.base
        if diff_only and last_reviewed_sha:
            diff_base = last_reviewed_sha

        diff_text = git_utils.diff(repo_path, diff_base, meta.head, three_dot=not diff_only)

        return ReviewContext(
            # LLM은 tree_path(= head 커밋 worktree)에서 파일을 읽고,
            # 문서와 git은 원본 repo_path를 쓴다.
            repo_path=os.path.abspath(tree_path) if tree_path else repo_path,
            docs_path=repo_path,
            tree_reason=tree_reason,
            repo_key=meta.repo_slug or os.path.basename(repo_path),
            pr=meta,
            config=config,
            docs=docs,
            guidance=guidance,
            diff_text=diff_text,
            base=diff_base,
            head=meta.head,
            round=round_number,
            diff_only=diff_only,
            last_reviewed_sha=last_reviewed_sha,
            previous_findings=list(previous_findings),
        )
