"""파이프라인 연결과 라운드/상한 정책.

라운드 판단과 게시 대상 결정은 순수 함수로 두어 LLM 없이 단위 테스트할 수 있게 한다.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional, Sequence

from . import git_utils
from .config import ReviewConfig, apply_intensity, load_config
from .context import ContextLoader, ReviewContext, load_pr_meta, resolve_max_nits
from .detector import DetectionResult, Detector
from .gating import GateDecision, resolve_gate
from .filters import (
    apply_caps,
    apply_severity_floor,
    apply_votes,
    count_by_severity,
    prefilter,
    sort_findings,
)
from .llm import CachingRunner, LLMRunner
from .merger import dedupe_findings, merge_findings
from .models import SECURITY_CATEGORY, Dropped, Finding, Verdict, VoteResult
from .publisher import (
    DryRunPublisher,
    GitHubPublisher,
    PublishOutcome,
    Publisher,
    PublishRequest,
    group_by_spot,
    render_counts_line,
)
from .state import PRState, StateStore
from .verifier import Verifier
from .worktree import ReviewTree, review_tree

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoundPlan:
    """이번 실행이 몇 번째 라운드이고 어떤 범위를 보는지."""

    round_number: int
    diff_only: bool = False
    escalate_only: bool = False
    stopped: bool = False
    reason: str = ""


def plan_round(state: PRState, max_rounds: int) -> RoundPlan:
    """라운드 상태로 이번 실행의 계획을 정한다 (순수 함수).

    - 라운드 1: 전체 diff
    - 라운드 2 이상: 마지막 리뷰 SHA 이후의 diff만
    - max_rounds를 넘으면 요약 코멘트 하나만 게시하고 중단
    - 이미 중단된 PR은 아무것도 하지 않음
    """
    if state.stopped:
        return RoundPlan(
            round_number=state.rounds,
            stopped=True,
            reason=f"라운드 상한({max_rounds})을 넘겨 이미 중단된 PR입니다",
        )

    round_number = state.next_round
    diff_only = round_number > 1 and bool(state.last_reviewed_sha)

    if round_number > max_rounds:
        return RoundPlan(
            round_number=round_number,
            diff_only=diff_only,
            escalate_only=True,
            reason=f"라운드 {max_rounds}회를 모두 소진했습니다",
        )

    if diff_only:
        return RoundPlan(
            round_number=round_number,
            diff_only=True,
            reason="마지막 리뷰 커밋 이후의 diff만 봅니다",
        )

    return RoundPlan(round_number=round_number, reason="PR 전체 diff를 봅니다")


@dataclass(frozen=True)
class ReviewScope:
    """이번 실행이 볼 변경 범위.

    `empty_reason`이 비어 있지 않으면 볼 변경이 없다는 뜻이고, LLM을 부르지 않는다.
    """

    changed: tuple[str, ...] = ()
    # 변경 목록을 읽지 못했다. 이때는 게이트를 적용하지 않고 리뷰를 진행한다 —
    # 읽기 실패 때문에 리뷰가 조용히 사라지는 쪽이 더 나쁘다.
    read_failed: bool = False
    empty_reason: str = ""

    @property
    def is_empty(self) -> bool:
        return bool(self.empty_reason)


def empty_scope_reason(diff_only: bool, already_merged: bool) -> str:
    """리뷰할 변경이 없을 때의 사유 문장 (순수 함수).

    `already_merged`는 head가 base의 조상이라는 뜻이다. 이때 `base...head` 세 점 diff는
    머지 베이스가 head 자신이므로 빈 결과를 낸다. 2점 diff로 폴백하면 안 된다 —
    `base..head`는 base가 그 뒤로 추가한 내용을 전부 되돌리는 역방향 diff가 된다.
    범위를 사람이 직접 주는 것이 유일하게 맞는 해법이다.
    """
    if diff_only:
        return "마지막으로 리뷰한 커밋 이후 새 변경이 없습니다"
    if already_merged:
        return (
            "head가 base의 조상입니다 — 이 PR은 이미 base에 머지되어 "
            "`base...head` diff가 비어 있습니다. 머지된 PR을 리뷰하려면 "
            "`--base <head>^ --head <head>` 처럼 범위를 직접 주세요"
        )
    return "base와 head 사이에 변경이 없습니다"


@dataclass(frozen=True)
class PublishPlan:
    findings: tuple[Finding, ...]
    escalation: bool = False


def plan_publish(plan: RoundPlan, findings: Sequence[Finding]) -> PublishPlan:
    """라운드 상한을 넘겼으면 important만 모아 요약 하나로 게시한다 (순수 함수)."""
    if plan.escalate_only:
        important = tuple(f for f in findings if f.severity == "important")
        if important:
            return PublishPlan(findings=important, escalation=True)
        return PublishPlan(findings=())
    return PublishPlan(findings=tuple(findings))


@dataclass
class ReviewOutcome:
    repo_key: str
    pr_number: int
    round_number: int
    diff_only: bool = False
    escalated: bool = False
    stopped: bool = False
    # 실행 게이트에서 걸러졌다. 라운드를 소모하지 않는다 (stopped와 다르다).
    skipped: bool = False
    reason: str = ""
    posted: list[Finding] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    votes: list[VoteResult] = field(default_factory=list)
    detection: Optional[DetectionResult] = None
    publish: PublishOutcome = field(default_factory=PublishOutcome)
    rounds_to_pass: Optional[int] = None
    head_sha: str = ""
    state_recorded: bool = False
    model: str = ""
    tree_reason: str = ""
    cache_stats: str = ""
    comment_count: int = 0
    # 단계별 통과 수. 후보가 어디서 걸렸는지 보여 준다.
    stage_counts: dict[str, int] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        return count_by_severity(self.posted)

    @property
    def summary_line(self) -> str:
        return render_counts_line(self.posted)


def resolve_repo_key(repo_path: str, repo_slug: str = "") -> str:
    return (
        repo_slug
        or git_utils.repo_slug_from_remote(repo_path)
        or os.path.basename(os.path.abspath(repo_path))
    )


class Orchestrator:
    def __init__(
        self,
        runner: LLMRunner,
        store: StateStore,
        loader: Optional[ContextLoader] = None,
        gh_path: str = "gh",
    ) -> None:
        self.runner = runner
        self.store = store
        self.loader = loader or ContextLoader(gh_path=gh_path)
        self.gh_path = gh_path

    def run(
        self,
        repo_path: str,
        pr_number: int,
        *,
        dry_run: bool = True,
        config_path: Optional[str] = None,
        repo_slug: str = "",
        pr_title: Optional[str] = None,
        pr_body: Optional[str] = None,
        base: Optional[str] = None,
        head: Optional[str] = None,
        publisher: Optional[Publisher] = None,
        record_state: Optional[bool] = None,
        post_when_empty: bool = False,
        use_worktree: bool = True,
        cache_dir: Optional[str] = None,
    ) -> ReviewOutcome:
        repo_path = os.path.abspath(os.path.expanduser(repo_path))
        config = load_config(repo_path, config_path)
        repo_key = resolve_repo_key(repo_path, repo_slug)

        if record_state is None:
            record_state = not dry_run

        state = self.store.get_state(repo_key, pr_number)
        plan = plan_round(state, config.max_rounds)

        if plan.stopped:
            logger.info("중단된 PR이라 리뷰하지 않습니다: %s#%s", repo_key, pr_number)
            return ReviewOutcome(
                repo_key=repo_key,
                pr_number=pr_number,
                round_number=plan.round_number,
                stopped=True,
                reason=plan.reason,
                rounds_to_pass=state.rounds_to_pass,
            )

        # PR 메타데이터를 먼저 확정해야 worktree를 만들 head를 알 수 있다.
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
        previous = self.store.posted_refs(repo_key, pr_number)

        # 변경 파일 목록을 한 번만 읽어 빈 범위 진단과 실행 게이트에 함께 쓴다.
        # 둘 다 worktree를 만들기 전에 끝내서 그 비용까지 아낀다.
        diff_base = meta.base
        if plan.diff_only and state.last_reviewed_sha:
            diff_base = state.last_reviewed_sha
        scope = self._scope(repo_path, diff_base, meta.head, plan)

        if scope.is_empty:
            # LLM을 부르지 않는다. 빈 diff로 진행하면 탐지기가 diff 대신 현재 파일을
            # 읽어, 이 PR이 바꾸지 않은 것을 이 PR의 문제처럼 보고한다.
            logger.warning(
                "리뷰할 변경이 없어 건너뜁니다: %s#%s — %s",
                repo_key,
                pr_number,
                scope.empty_reason,
            )
            return ReviewOutcome(
                repo_key=repo_key,
                pr_number=pr_number,
                round_number=plan.round_number,
                diff_only=plan.diff_only,
                skipped=True,
                reason=scope.empty_reason,
                rounds_to_pass=state.rounds_to_pass,
            )

        gate = self._gate(scope, config)
        if gate.skip:
            logger.info("게이트에서 건너뜁니다: %s#%s — %s", repo_key, pr_number, gate.reason)
            return ReviewOutcome(
                repo_key=repo_key,
                pr_number=pr_number,
                round_number=plan.round_number,
                diff_only=plan.diff_only,
                skipped=True,
                reason=gate.reason,
                rounds_to_pass=state.rounds_to_pass,
            )
        if gate.intensity != config.intensity:
            logger.info("게이트가 강도를 바꿉니다: %s — %s", gate.intensity, gate.reason)
            config = apply_intensity(config, gate.intensity)

        with review_tree(repo_path, meta.head, enabled=use_worktree) as tree:
            ctx = self.loader.load(
                repo_path,
                pr_number,
                config_path=config_path,
                repo_slug=meta.repo_slug,
                pr_title=meta.title,
                pr_body=meta.description,
                base=meta.base,
                head=meta.head,
                round_number=plan.round_number,
                diff_only=plan.diff_only,
                last_reviewed_sha=state.last_reviewed_sha,
                previous_findings=previous,
                config=config,
                tree_path=tree.path if tree.temporary else None,
                tree_reason=tree.reason,
            )
            outcome = self._review(
                ctx, plan, state, publisher, dry_run, post_when_empty, tree, cache_dir
            )

        if record_state:
            self._record(ctx, plan, state, outcome)
        return outcome

    def _scope(
        self, repo_path: str, diff_base: str, head: str, plan: RoundPlan
    ) -> ReviewScope:
        """변경 파일 목록을 읽고, 비어 있으면 왜 비었는지 진단한다.

        비교 범위는 ContextLoader와 같게 맞춘다 (라운드 2 이상은 마지막 리뷰 SHA 이후만).
        """
        try:
            changed = git_utils.changed_files(
                repo_path, diff_base, head, three_dot=not plan.diff_only
            )
        except git_utils.GitError as exc:
            logger.warning("변경 파일 목록을 읽지 못했습니다: %s", exc)
            return ReviewScope(read_failed=True)

        if changed:
            return ReviewScope(changed=tuple(changed))

        already_merged = git_utils.is_ancestor(repo_path, head, diff_base)
        return ReviewScope(
            empty_reason=empty_scope_reason(plan.diff_only, already_merged)
        )

    def _gate(self, scope: ReviewScope, config: ReviewConfig) -> GateDecision:
        """실행 게이트를 평가한다. 변경 목록을 못 읽었으면 적용하지 않는다."""
        if config.gate.is_empty:
            return GateDecision(intensity=config.intensity, reason="게이트 규칙 없음")
        if scope.read_failed:
            return GateDecision(
                intensity=config.intensity, reason="변경 파일 목록 읽기 실패"
            )
        return resolve_gate(list(scope.changed), config.gate, config.intensity)

    # ---- 파이프라인 ----

    def _wrap_runner(
        self, config: ReviewConfig, cache_dir: Optional[str], head_sha: str
    ) -> LLMRunner:
        if not cache_dir:
            return self.runner
        return CachingRunner(
            self.runner,
            cache_dir,
            # 백엔드를 캐시 키에 넣는다. 없으면 --backend를 바꿔도 이전 백엔드 응답이 적중한다.
            scope=(
                config.llm.backend,
                config.llm.model,
                config.llm.reasoning_effort,
                head_sha,
            ),
        )

    def _review(
        self,
        ctx: ReviewContext,
        plan: RoundPlan,
        state: PRState,
        publisher: Optional[Publisher],
        dry_run: bool,
        post_when_empty: bool,
        tree: ReviewTree,
        cache_dir: Optional[str],
    ) -> ReviewOutcome:
        config = ctx.config
        max_nits = resolve_max_nits(config, ctx.guidance)

        try:
            head_sha = git_utils.resolve_sha(ctx.git_path, ctx.head)
        except git_utils.GitError:
            head_sha = tree.head_sha or ctx.head

        runner = self._wrap_runner(config, cache_dir, head_sha)

        # 1단: 탐지 -> 같은 category + 근접 라인 + 느슨한 내용 유사도로 병합
        detection = Detector(runner, config).detect(ctx, max_nits=max_nits)
        merged = merge_findings(
            detection.findings,
            config.line_tolerance,
            similarity_threshold=config.merge_similarity,
        )

        kept, dropped = prefilter(
            merged,
            skip_paths=ctx.skip_paths,
            posted=ctx.previous_findings,
            tolerance=config.line_tolerance,
        )

        # 검증: finding 하나당 독립 투표. 서로 다른 주장을 섞지 않는다.
        votes = Verifier(runner, config).verify(ctx, kept)
        passed, vote_dropped = apply_votes(
            votes,
            config.pass_threshold,
            security_threshold=config.security_threshold(),
        )
        dropped.extend(vote_dropped)

        # 2단: 투표를 통과한 것들에서 category가 갈린 중복을 접는다
        real_votes = {vote.finding.id: vote.real_count for vote in votes}
        deduped, dupe_dropped = dedupe_findings(
            passed,
            tolerance=config.line_tolerance,
            threshold=config.dedupe_similarity,
            real_votes=real_votes,
        )
        dropped.extend(dupe_dropped)

        # 보안 지적은 nit 상한에 밀리지 않게 등급을 올린 뒤 정렬한다.
        floored = apply_severity_floor(deduped, config.security_severity_floor)
        ordered = sort_findings(floored)
        capped, cap_dropped = apply_caps(
            ordered,
            config.max_findings,
            max_nits,
            reserved_security=config.security_reserved_slots,
        )
        dropped.extend(cap_dropped)

        publish_plan = plan_publish(plan, capped)

        stage_counts = {
            "탐지 후보": len(detection.findings),
            "1단 병합": len(merged),
            "사전 필터": len(kept),
            "투표 통과": len(passed),
            "2단 중복 제거": len(deduped),
            "정렬·상한": len(capped),
            "게시": len(publish_plan.findings),
            "게시(보안)": sum(
                1 for f in publish_plan.findings if f.category == SECURITY_CATEGORY
            ),
        }

        outcome = ReviewOutcome(
            repo_key=ctx.repo_key,
            pr_number=ctx.pr.number,
            round_number=plan.round_number,
            diff_only=plan.diff_only,
            escalated=publish_plan.escalation,
            reason=plan.reason,
            posted=list(publish_plan.findings),
            dropped=dropped,
            votes=votes,
            detection=detection,
            head_sha=head_sha,
            model=config.llm.model,
            tree_reason=ctx.tree_reason or tree.reason,
            stage_counts=stage_counts,
        )

        if isinstance(runner, CachingRunner):
            outcome.cache_stats = runner.stats

        target = publisher or self._default_publisher(ctx, dry_run)
        request = PublishRequest(
            findings=list(publish_plan.findings),
            round_number=plan.round_number,
            head_sha=head_sha,
            inline_lines=self._inline_lines(ctx, dry_run),
            escalation=publish_plan.escalation,
            empty_text=config.summary_empty,
            post_when_empty=post_when_empty,
            group_comments=config.group_comments,
            line_tolerance=config.line_tolerance,
        )
        outcome.publish = target.publish(request)
        outcome.comment_count = outcome.publish.comment_count or len(
            group_by_spot(
                list(publish_plan.findings), config.line_tolerance, config.group_comments
            )
        )

        important = sum(1 for f in publish_plan.findings if f.severity == "important")
        if important == 0 and state.rounds_to_pass is None:
            outcome.rounds_to_pass = plan.round_number
        else:
            outcome.rounds_to_pass = state.rounds_to_pass

        return outcome

    def _default_publisher(self, ctx: ReviewContext, dry_run: bool) -> Publisher:
        if dry_run:
            return DryRunPublisher()
        return GitHubPublisher(
            repo_slug=ctx.pr.repo_slug,
            pr_number=ctx.pr.number,
            gh_path=self.gh_path,
            cwd=ctx.git_path,
        )

    def _inline_lines(self, ctx: ReviewContext, dry_run: bool) -> dict[str, set[int]]:
        """인라인 코멘트를 달 수 있는 라인은 PR 전체 diff 기준으로 계산한다."""
        if dry_run:
            return {}
        try:
            full_diff = git_utils.diff(ctx.git_path, ctx.pr.base, ctx.pr.head, three_dot=True)
        except git_utils.GitError as exc:
            logger.warning("전체 diff를 계산하지 못해 인라인 코멘트를 건너뜁니다: %s", exc)
            return {}
        return git_utils.diff_line_map(full_diff)

    # ---- 상태 기록 ----

    def _record(
        self,
        ctx: ReviewContext,
        plan: RoundPlan,
        state: PRState,
        outcome: ReviewOutcome,
    ) -> None:
        # 게시한 것이 없어도 라운드 수와 마지막 리뷰 SHA는 갱신한다.
        verdicts: dict[str, Sequence[Verdict]] = {
            vote.finding.id: vote.verdicts for vote in outcome.votes
        }
        if outcome.posted:
            self.store.record_posted(
                ctx.repo_key,
                ctx.pr.number,
                plan.round_number,
                outcome.posted,
                verdicts_by_finding=verdicts,
                model=outcome.model,
            )

        self.store.save_state(
            PRState(
                repo_key=ctx.repo_key,
                pr_number=ctx.pr.number,
                rounds=plan.round_number,
                last_reviewed_sha=outcome.head_sha or state.last_reviewed_sha,
                stopped=outcome.escalated,
                rounds_to_pass=outcome.rounds_to_pass,
            )
        )
        outcome.state_recorded = True
