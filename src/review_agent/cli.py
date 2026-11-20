"""CLI 진입점.

    review run --repo <path> --pr <n> [--dry-run | --post]
    review feedback --repo <path> --pr <n> --finding f-001 --reaction fixed
    review show --repo <path> --pr <n>

게시는 바깥으로 나가는 동작이므로 기본값이 --dry-run이다. 실제 게시는 --post를 붙인다.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional, Sequence

from .config import (
    BACKEND_PRESETS,
    PROFILES,
    ConfigError,
    backend_preset,
    load_config,
    resolve_llm,
)
from .context import ContextLoader, PRMetaError, load_repo_docs, parse_review_md, resolve_max_nits
from .git_utils import GitError
from .llm import CLIRunner, LLMError
from .models import Dropped
from .orchestrator import Orchestrator, resolve_repo_key
from .publisher import SEVERITY_LABELS
from .state import REACTIONS, StateStore, default_state_path

SUBCOMMANDS = ("run", "feedback", "show")


def _verbosity_parent() -> argparse.ArgumentParser:
    """`review -v run ...`과 `review run ... -v` 둘 다 받기 위한 공용 부모 파서.

    default를 SUPPRESS로 두면 서브커맨드 쪽에서 플래그를 안 썼을 때 최상위 값이 유지된다.
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=argparse.SUPPRESS,
        help="로그를 자세히 출력",
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review",
        description="Pull Request 리뷰 에이전트 (병렬 탐지 + 검증자 투표)",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0, help="로그를 자세히 출력")
    subparsers = parser.add_subparsers(dest="command")
    verbosity = _verbosity_parent()

    run = subparsers.add_parser("run", help="PR 하나를 리뷰한다", parents=[verbosity])
    run.add_argument("--repo", required=True, help="대상 레포 경로")
    run.add_argument("--pr", required=True, type=int, help="PR 번호")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="게시하지 않고 stdout에만 출력 (기본값)",
    )
    mode.add_argument("--post", action="store_true", help="GitHub PR에 실제로 게시")
    run.add_argument("--config", help="review.yaml 경로 (기본: 레포 루트에서 찾음)")
    run.add_argument("--state", help=f"상태 DB 경로 (기본: {default_state_path()})")
    run.add_argument("--repo-slug", default="", help="owner/name (리모트에서 못 읽을 때)")
    run.add_argument("--base", help="base 커밋/ref 직접 지정")
    run.add_argument("--head", help="head 커밋/ref 직접 지정")
    run.add_argument("--pr-title", help="PR 제목 직접 지정 (gh 없이 실행할 때)")
    run.add_argument("--pr-body", help="PR 설명 직접 지정 (gh 없이 실행할 때)")
    run.add_argument(
        "--record-state",
        dest="record_state",
        action="store_true",
        default=None,
        help="dry-run에서도 라운드 상태를 기록",
    )
    run.add_argument(
        "--no-record-state",
        dest="record_state",
        action="store_false",
        help="게시해도 라운드 상태를 기록하지 않음",
    )
    run.add_argument(
        "--post-when-empty",
        action="store_true",
        help="지적이 없을 때도 요약 코멘트를 남김",
    )
    run.add_argument(
        "--no-worktree",
        dest="use_worktree",
        action="store_false",
        default=True,
        help="PR head worktree를 만들지 않고 현재 워킹트리를 그대로 리뷰 "
        "(커밋하지 않은 변경을 볼 때만)",
    )
    run.add_argument(
        "--cache",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help="응답 캐시를 켠다. 같은 입력 재실행 시 LLM 호출 0회, 출력 동일. "
        "경로를 생략하면 상태 DB 옆의 cache/ 를 쓴다",
    )
    run.add_argument("--model", help="이번 실행만 모델을 덮어씀 (기본은 review.yaml)")
    run.add_argument(
        "--backend",
        choices=list(BACKEND_PRESETS),
        help="이번 실행만 LLM 백엔드를 덮어씀 (기본: review.yaml의 llm.backend, 없으면 codex)",
    )
    run.add_argument(
        "--profile",
        choices=list(PROFILES),
        help="실행 프로필. 결정성 플래그와 인증 확인 방법이 달라진다 "
        "(기본: CI/GITHUB_ACTIONS 환경변수로 자동 감지)",
    )
    run.add_argument("--gh", default="gh", help="gh 실행 파일 경로")

    feedback = subparsers.add_parser(
        "feedback", help="작성자 반응을 기록한다", parents=[verbosity]
    )
    feedback.add_argument("--repo", required=True)
    feedback.add_argument("--pr", required=True, type=int)
    feedback.add_argument("--finding", required=True, help="finding id (예: f-001)")
    feedback.add_argument("--reaction", required=True, choices=list(REACTIONS))
    feedback.add_argument("--note", default="", help="한 줄 메모")
    feedback.add_argument("--round", type=int, help="특정 라운드의 finding으로 한정")
    feedback.add_argument("--state", help="상태 DB 경로")
    feedback.add_argument("--repo-slug", default="")

    show = subparsers.add_parser(
        "show", help="라운드 상태와 게시 이력을 본다", parents=[verbosity]
    )
    show.add_argument("--repo", required=True)
    show.add_argument("--pr", type=int, help="생략하면 설정 요약만 출력")
    show.add_argument("--state", help="상태 DB 경로")
    show.add_argument("--repo-slug", default="")
    show.add_argument("--config", help="review.yaml 경로")
    show.add_argument(
        "--backend", choices=list(BACKEND_PRESETS), help="이 백엔드로 해석한 설정을 본다"
    )
    show.add_argument(
        "--profile", choices=list(PROFILES), help="이 프로필로 해석한 설정을 본다"
    )

    return parser


def _normalize_argv(argv: Sequence[str]) -> list[str]:
    """`review --repo ... --pr ...` 처럼 서브커맨드를 생략하면 run으로 본다."""
    args = list(argv)
    if not args:
        return args
    first = args[0]
    if first in SUBCOMMANDS or first in ("-h", "--help", "-v", "--verbose"):
        return args
    if first.startswith("-"):
        return ["run", *args]
    return args


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize_argv(argv if argv is not None else sys.argv[1:]))
    _setup_logging(getattr(args, "verbose", 0))

    command = args.command or "run"
    try:
        if command == "run":
            return _cmd_run(args)
        if command == "feedback":
            return _cmd_feedback(args)
        if command == "show":
            return _cmd_show(args)
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2
    except (PRMetaError, GitError, LLMError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 1


def _resolve_cache_dir(args: argparse.Namespace, state_path: str) -> Optional[str]:
    """--cache 처리. 생략하면 None(캐시 끔), 경로 없이 주면 상태 DB 옆의 cache/."""
    if args.cache is None:
        return None
    if args.cache:
        return os.path.abspath(os.path.expanduser(args.cache))
    parent = os.path.dirname(os.path.abspath(state_path)) or os.getcwd()
    return os.path.join(parent, "cache")


def _dropped_line(item: Dropped, claim_width: int = 70) -> str:
    """걸러낸 후보 한 줄. 위치, 사유 상세, 줄인 claim."""
    finding = item.finding
    location = f"{finding.file}:{finding.line_start}"
    if finding.line_end != finding.line_start:
        location += f"-{finding.line_end}"

    claim = " ".join(finding.claim.split())
    if len(claim) > claim_width:
        claim = claim[: claim_width - 3] + "..."

    parts = [location, f"({finding.category})"]
    if item.detail:
        parts.append(item.detail)
    parts.append(claim)
    return " ".join(parts)


def _cmd_run(args: argparse.Namespace) -> int:
    dry_run = not args.post
    config = load_config(args.repo, args.config)
    config.llm = resolve_llm(
        config, backend=args.backend, profile=args.profile, model=args.model
    )

    runner = CLIRunner(
        command=config.llm.command,
        timeout=config.llm.timeout,
        model=config.llm.model,
        reasoning_effort=config.llm.reasoning_effort,
        extra_args=config.llm.extra_args,
    )
    # LLM을 부르기 전에 막는다. 인증이 안 된 상태로 호출하면 원인을 알 수 없는 exit 1만 남는다.
    runner.preflight(profile=config.llm.profile, preset=backend_preset(config.llm))

    state_path = args.state or default_state_path()
    cache_dir = _resolve_cache_dir(args, state_path)

    with StateStore(state_path) as store:
        orchestrator = Orchestrator(
            runner, store, loader=ContextLoader(gh_path=args.gh), gh_path=args.gh
        )
        outcome = orchestrator.run(
            args.repo,
            args.pr,
            dry_run=dry_run,
            config_path=args.config,
            repo_slug=args.repo_slug,
            pr_title=args.pr_title,
            pr_body=args.pr_body,
            base=args.base,
            head=args.head,
            record_state=args.record_state,
            post_when_empty=args.post_when_empty,
            use_worktree=args.use_worktree,
            cache_dir=cache_dir,
        )

    if outcome.stopped:
        print(f"중단된 PR입니다: {outcome.reason}")
        return 0

    if outcome.skipped:
        # 라운드를 쓰지 않았으므로 다음 푸시에서 다시 평가된다.
        print(f"리뷰를 건너뛰었습니다: {outcome.reason}")
        return 0

    print(f"\n라운드 {outcome.round_number}" + (" (diff-only 재리뷰)" if outcome.diff_only else ""))
    print(f"범위: {outcome.reason}")
    print(
        f"백엔드: {config.llm.backend} / 프로필: {config.llm.profile}"
        f" / 모델: {outcome.model} / head {outcome.head_sha[:12] or '-'}"
    )
    if outcome.tree_reason:
        print(f"리뷰 트리: {outcome.tree_reason}")
    if outcome.cache_stats:
        print(f"캐시: {outcome.cache_stats}")
    if outcome.detection:
        failed = outcome.detection.failed_angles
        if failed:
            print(f"탐지 실패 관점: {', '.join(failed)}")
    print(f"요약: {outcome.summary_line} (코멘트 {outcome.comment_count}개)")
    if outcome.escalated:
        print("라운드 상한 초과: 요약 코멘트 하나만 게시하고 중단합니다.")
    if outcome.stage_counts:
        funnel = " -> ".join(
            f"{name} {count}" for name, count in outcome.stage_counts.items()
        )
        print(f"단계별: {funnel}")
    if outcome.dropped:
        reasons: dict[str, int] = {}
        for item in outcome.dropped:
            reasons[item.reason] = reasons.get(item.reason, 0) + 1
        detail = ", ".join(f"{reason} {count}" for reason, count in sorted(reasons.items()))
        print(f"걸러낸 후보: {detail}")
        # -v 이상에서는 후보별 상세를 찍는다. 투표 미달의 표 지수(real N/M)가 여기 있고,
        # 임계값을 조정할지 판단하려면 그 수치가 필요하다. 상태 DB는 게시한 지적의
        # 표만 저장하므로 탈락분은 이 출력이 유일한 근거다.
        if args.verbose >= 1:
            for item in outcome.dropped:
                print(f"  - [{item.reason}] {_dropped_line(item)}")
    if outcome.publish.errors:
        for error in outcome.publish.errors:
            print(f"게시 오류: {error}", file=sys.stderr)
        return 1
    if outcome.publish.overflow:
        print(f"인라인 불가(요약으로 이동): {', '.join(outcome.publish.overflow)}")
    return 0


def _cmd_feedback(args: argparse.Namespace) -> int:
    repo_key = resolve_repo_key(args.repo, args.repo_slug)
    with StateStore(args.state or default_state_path()) as store:
        ok = store.record_feedback(
            repo_key,
            args.pr,
            args.finding,
            args.reaction,
            note=args.note,
            round_number=args.round,
        )
    if not ok:
        print(
            f"게시 이력에서 finding을 찾지 못했습니다: {repo_key}#{args.pr} {args.finding}",
            file=sys.stderr,
        )
        return 1
    print(f"기록했습니다: {args.finding} -> {args.reaction}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    config = load_config(args.repo, args.config)
    config.llm = resolve_llm(config, backend=args.backend, profile=args.profile)
    docs = load_repo_docs(args.repo)
    guidance = parse_review_md(docs.review_md)

    angles = config.resolved_angles()
    print(f"설정: {config.source}")
    print(
        f"  탐지 {len(angles)} / 검증 {config.verifiers} / 임계값 {config.pass_threshold}"
        f" / 강도 {config.intensity}"
    )
    # 관점 목록을 그대로 찍는다. angles를 잘못 적어 범용 관점이 빠진 설정은 여기서만 보인다.
    print(f"  관점: {', '.join(f'{a.id}({a.kind})' for a in angles)}")
    print(f"  게시 상한 {config.max_findings} / nit 상한 {resolve_max_nits(config, guidance)}")
    print(f"  라운드 상한 {config.max_rounds} / 코멘트 언어 {config.comment_language}")
    print(
        f"  백엔드 {config.llm.backend} / 프로필 {config.llm.profile}"
        f" / 모델 {config.llm.model} / 추론 강도 {config.llm.reasoning_effort}"
    )
    print(f"  커맨드 {' '.join(config.llm.command)}")
    print(f"  추가 인자 {' '.join(config.llm.extra_args) if config.llm.extra_args else '없음'}")
    print(
        f"  병합 유사도 {config.merge_similarity} / 중복 판정 {config.dedupe_similarity}"
        f" / 코멘트 묶기 {'예' if config.group_comments else '아니오'}"
    )
    if any(angle.is_security for angle in angles):
        print(
            f"  보안 검증: {', '.join(config.security_checks)}"
            f" / 통과 기준 {config.security_threshold()}"
        )
    print(f"  AGENTS: {docs.agents_source or '없음'} / REVIEW.md: {docs.review_source or '없음'}")
    print(f"  SECURITY-REVIEW.md: {'있음' if docs.has_security else '없음'}")
    if not config.gate.is_empty:
        print(
            f"  게이트: 반드시 리뷰 {', '.join(config.gate.always_review) or '-'}"
            f" / 정밀 {', '.join(config.gate.deep_paths) or '-'}"
            f" / 건너뜀 {', '.join(config.gate.skip_if_only) or '-'}"
        )
    skip = guidance.skip_paths + [p for p in config.skip_paths if p not in guidance.skip_paths]
    print(f"  건너뛸 경로: {', '.join(skip) if skip else '없음'}")

    if args.pr is None:
        return 0

    repo_key = resolve_repo_key(args.repo, args.repo_slug)
    with StateStore(args.state or default_state_path()) as store:
        state = store.get_state(repo_key, args.pr)
        posted = store.list_posted(repo_key, args.pr)
        reactions = store.feedback_summary(repo_key, args.pr)

    print(f"\n{repo_key}#{args.pr}")
    print(f"  라운드 {state.rounds} / 마지막 리뷰 SHA {state.last_reviewed_sha or '-'}")
    print(f"  중단됨: {'예' if state.stopped else '아니오'}")
    print(f"  rounds-to-pass: {state.rounds_to_pass if state.rounds_to_pass else '-'}")

    if posted:
        print("\n게시한 지적:")
        for row in posted:
            label = SEVERITY_LABELS.get(row["severity"], row["severity"])
            reaction = f" <- {row['reaction']}" if row.get("reaction") else ""
            model = f" [{row['model']}]" if row.get("model") else ""
            print(
                f"  [R{row['round']}] {row['finding_id']} {label} "
                f"{row['file']}:{row['line_start']}-{row['line_end']} "
                f"{row['claim']}{model}{reaction}"
            )
    if reactions:
        detail = ", ".join(f"{key} {value}" for key, value in sorted(reactions.items()))
        print(f"\n작성자 반응: {detail}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
