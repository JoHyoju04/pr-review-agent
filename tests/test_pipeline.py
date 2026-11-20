"""가짜 LLMRunner로 전체 파이프라인을 끝까지 돌리는 통합 테스트."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest

from helpers import commit_all, init_repo, write_file

from review_agent.llm import FakeLLMRunner
from review_agent.config import parse_config_text
from review_agent.orchestrator import Orchestrator
from review_agent.publisher import DryRunPublisher
from review_agent.state import StateStore

AGENTS_MD = """# 테스트 프로젝트

## 스택과 명령
- 언어: Python
- 테스트: python -m unittest

## 리뷰 지침
- 코드 리뷰를 할 때는 먼저 레포 루트의 REVIEW.md를 읽는다.
"""

REVIEW_MD = """# REVIEW.md

## 우선순위
1. 실제 버그

## 건너뛸 경로
- `build/`

## 코멘트 규칙
- Nit은 PR당 최대 1개.
"""

APP_BEFORE = "\n".join(f"line {n}" for n in range(1, 61)) + "\n"
APP_AFTER = APP_BEFORE.replace("line 3", "line 3 changed")
APP_AFTER2 = APP_AFTER.replace("line 50", "line 50 changed")

VERDICT_REAL = '{"verdict": "real", "reason": "코드로 확인됨", "checked": "src/app.py:3"}'


def finding(
    line: int,
    severity: str = "important",
    category: str = "logic",
    file: str = "src/app.py",
    claim: str = "",
) -> dict:
    return {
        "file": file,
        "line_start": line,
        "line_end": line,
        "category": category,
        "severity": severity,
        "claim": claim or f"{file}:{line} 에 문제가 있다",
        "evidence": f"{file}:{line} `line {line}`",
        "suggested_fix": "고친다",
    }


class PipelineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        init_repo(self.repo)
        self.store = StateStore(":memory:")
        self.stream = io.StringIO()

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def publisher(self) -> DryRunPublisher:
        return DryRunPublisher(self.stream)


class FullPipelineTest(PipelineTestCase):
    def setUp(self) -> None:
        super().setUp()
        write_file(self.repo, "AGENTS.md", AGENTS_MD)
        write_file(self.repo, "REVIEW.md", REVIEW_MD)
        write_file(self.repo, "src/app.py", APP_BEFORE)
        write_file(self.repo, "build/generated/Q.java", "class Q {}\n")
        self.base = commit_all(self.repo, "base")

        write_file(self.repo, "src/app.py", APP_AFTER)
        self.head = commit_all(self.repo, "change line 3")

        self.round1_payload = json.dumps(
            [
                finding(3, "important", claim="널 체크가 없어 터진다"),
                finding(20, "nit", claim="사소한 지적 1"),
                finding(30, "nit", claim="사소한 지적 2"),
                finding(5, "important", file="build/generated/Q.java", claim="생성 코드 지적"),
                finding(40, "nit", category="style", claim="포맷 지적"),
            ],
            ensure_ascii=False,
        )
        self.runner = FakeLLMRunner(
            rules=[("당신은 코드 리뷰 지적을 검증하는 검증자", VERDICT_REAL)],
            default=self.round1_payload,
        )
        self.orchestrator = Orchestrator(self.runner, self.store)

    def run_round(self, head: str) -> object:
        return self.orchestrator.run(
            self.repo,
            7,
            dry_run=True,
            record_state=True,
            publisher=self.publisher(),
            pr_title="테스트 PR",
            pr_body="line 3을 고친다",
            base=self.base,
            head=head,
        )

    def test_round_one_end_to_end(self):
        outcome = self.run_round(self.head)

        self.assertEqual(outcome.round_number, 1)
        self.assertFalse(outcome.diff_only)
        # 탐지 3관점 + 검증 3명 x finding 수
        self.assertEqual(len(outcome.detection.runs), 3)
        self.assertEqual(outcome.detection.failed_angles, [])

        # 같은 지적을 3관점이 모두 찾았으므로 하나로 병합되고 관점 3개가 기록된다.
        important = [f for f in outcome.posted if f.severity == "important"]
        self.assertEqual(len(important), 1)
        self.assertEqual(len(important[0].detected_by), 3)

        # REVIEW.md의 nit 상한 1개가 적용된다.
        self.assertEqual(outcome.counts, {"important": 1, "nit": 1, "pre_existing": 0})
        self.assertEqual(outcome.summary_line, "Important 1, Nit 1")

        # 심각도 순으로 출력된다.
        self.assertEqual([f.severity for f in outcome.posted], ["important", "nit"])

        reasons = {item.reason for item in outcome.dropped}
        self.assertIn("건너뛸_경로", reasons)
        self.assertIn("스타일_지적", reasons)
        self.assertIn("nit_상한", reasons)

        text = self.stream.getvalue()
        self.assertIn("Important 1, Nit 1", text)
        self.assertIn("널 체크가 없어 터진다", text)
        self.assertNotIn("생성 코드 지적", text)

    def test_repo_context_is_injected_into_prompts(self):
        self.run_round(self.head)
        detect_prompt = self.runner.calls[0][0]
        self.assertIn("언어: Python", detect_prompt)
        self.assertIn("Nit은 PR당 최대 1개", detect_prompt)
        self.assertIn("nit은 최대 1개", detect_prompt)
        self.assertIn(f"git diff {self.base}...{self.head}", detect_prompt)

    def test_state_is_recorded(self):
        self.run_round(self.head)
        state = self.store.get_state(os.path.basename(self.repo), 7)
        self.assertEqual(state.rounds, 1)
        self.assertEqual(state.last_reviewed_sha, self.head)
        self.assertFalse(state.stopped)
        self.assertEqual(len(self.store.posted_refs(os.path.basename(self.repo), 7)), 2)

    def test_second_round_is_diff_only_and_skips_posted_findings(self):
        self.run_round(self.head)

        write_file(self.repo, "src/app.py", APP_AFTER2)
        head2 = commit_all(self.repo, "change line 50")

        # 라운드 2: 이전에 게시한 지적(3번 줄)과 새 지적(50번 줄)을 함께 돌려준다.
        self.runner.rules = [
            ("당신은 코드 리뷰 지적을 검증하는 검증자", VERDICT_REAL),
            (
                "재리뷰 지침",
                json.dumps(
                    [
                        finding(3, "important", claim="널 체크가 없어 터진다"),
                        finding(50, "important", claim="새로 생긴 경계값 문제"),
                    ],
                    ensure_ascii=False,
                ),
            ),
        ]
        self.stream = io.StringIO()
        outcome = self.run_round(head2)

        self.assertEqual(outcome.round_number, 2)
        self.assertTrue(outcome.diff_only)

        claims = [f.claim for f in outcome.posted]
        self.assertEqual(claims, ["새로 생긴 경계값 문제"])
        self.assertIn("이미_게시함", {item.reason for item in outcome.dropped})

        # diff가 마지막 리뷰 SHA 이후로만 계산된다.
        rereview_prompt = next(p for p, _ in self.runner.calls if "재리뷰 지침" in p)
        self.assertIn(f"git diff {self.head}..{head2}", rereview_prompt)
        self.assertNotIn(f"{self.base}...{head2}", rereview_prompt)
        self.assertIn("널 체크가 없어 터진다", rereview_prompt)  # 반복 금지 목록

        state = self.store.get_state(os.path.basename(self.repo), 7)
        self.assertEqual(state.rounds, 2)
        self.assertEqual(state.last_reviewed_sha, head2)

    def test_votes_below_threshold_are_dropped(self):
        self.runner.rules = [
            (
                "당신은 코드 리뷰 지적을 검증하는 검증자",
                '{"verdict": "not_real", "reason": "의도된 동작"}',
            )
        ]
        outcome = self.run_round(self.head)
        self.assertEqual(outcome.posted, [])
        self.assertEqual(outcome.summary_line, "수정할 사항 없음")
        self.assertIn("투표_미달", {item.reason for item in outcome.dropped})

    def test_split_vote_passes_at_two_of_three(self):
        verdicts = iter(["real", "real", "not_real"] * 10)

        def handler(prompt: str, repo: str) -> str:
            if "당신은 코드 리뷰 지적을 검증하는 검증자" in prompt:
                return json.dumps({"verdict": next(verdicts), "reason": "판정"})
            return json.dumps([finding(3, "important")], ensure_ascii=False)

        self.runner.handler = handler
        outcome = self.run_round(self.head)
        self.assertEqual(len(outcome.posted), 1)


class NoRepoDocsTest(PipelineTestCase):
    """AGENTS.md / REVIEW.md / review.yaml이 없어도 기본값으로 동작한다."""

    def test_defaults_without_repo_docs(self):
        write_file(self.repo, "src/app.py", APP_BEFORE)
        base = commit_all(self.repo, "base")
        write_file(self.repo, "src/app.py", APP_AFTER)
        head = commit_all(self.repo, "change")

        runner = FakeLLMRunner(
            rules=[("당신은 코드 리뷰 지적을 검증하는 검증자", VERDICT_REAL)],
            default=json.dumps(
                [finding(3, "important")]
                + [finding(10 * i, "nit") for i in range(2, 7)],
                ensure_ascii=False,
            ),
        )
        outcome = Orchestrator(runner, self.store).run(
            self.repo,
            1,
            dry_run=True,
            record_state=True,
            publisher=self.publisher(),
            pr_title="제목 없음",
            pr_body="",
            base=base,
            head=head,
        )

        self.assertEqual(outcome.counts["important"], 1)
        self.assertEqual(outcome.counts["nit"], 3)  # 기본 nit 상한
        self.assertIn("AGENTS.md 없음", runner.calls[0][0])
        self.assertIn("REVIEW.md 없음", runner.calls[0][0])


class DedupeInPipelineTest(PipelineTestCase):
    """탐지기가 같은 버그에 다른 category를 매긴 경우가 끝까지 어떻게 흐르는지."""

    DUP_A = (
        "인증 상태 변경과 차단 히스토리 저장이 하나의 트랜잭션이 아니어서, 히스토리 저장 "
        "실패 시 인증만 BLOCKED로 남고 감사 이력이 유실됩니다."
    )
    DUP_B = (
        "인증 상태 변경과 차단 이력 저장이 하나의 트랜잭션이 아니므로, 이력 저장이 실패하면 "
        "인증만 BLOCKED로 커밋되어 요구된 이력이 남지 않는 부분 완료 상태가 됩니다."
    )
    OTHER = (
        "LLM 응답 객체를 널 체크 없이 역참조하므로 외부 호출이 타임아웃으로 빈 응답을 "
        "돌려주면 NullPointerException으로 비동기 스레드가 죽습니다."
    )

    def setUp(self) -> None:
        super().setUp()
        write_file(self.repo, "src/app.py", APP_BEFORE)
        self.base = commit_all(self.repo, "base")
        write_file(self.repo, "src/app.py", APP_AFTER)
        self.head = commit_all(self.repo, "change")

        def payload(line, category, claim):
            item = finding(line, "important", category=category, claim=claim)
            return json.dumps([item], ensure_ascii=False)

        self.runner = FakeLLMRunner(
            rules=[
                ("당신은 코드 리뷰 지적을 검증하는 검증자", VERDICT_REAL),
                # 같은 버그에 서로 다른 category를 매긴 두 탐지기
                ("## 관점\n조건 분기 오류", payload(3, "data", self.DUP_A)),
                ("## 관점\n경쟁 상태", payload(3, "concurrency", self.DUP_B)),
                # 같은 위치의 전혀 다른 버그
                ("## 관점\n요청/응답 필드", payload(4, "concurrency", self.OTHER)),
            ],
            default="[]",
        )
        self.orchestrator = Orchestrator(self.runner, self.store)

    def review(self):
        return self.orchestrator.run(
            self.repo,
            11,
            dry_run=True,
            record_state=True,
            publisher=self.publisher(),
            pr_title="PR",
            pr_body="",
            base=self.base,
            head=self.head,
        )

    def test_duplicate_folds_but_the_other_bug_survives(self):
        outcome = self.review()

        claims = [f.claim for f in outcome.posted]
        self.assertEqual(len(outcome.posted), 2, claims)
        # 트랜잭션 중복은 하나로 접힌다
        self.assertEqual(sum(1 for c in claims if "트랜잭션" in c), 1)
        # 같은 위치의 다른 버그는 살아남는다
        self.assertIn(self.OTHER, claims)
        self.assertIn("중복_지적", {item.reason for item in outcome.dropped})

    def test_both_land_in_a_single_comment(self):
        outcome = self.review()
        self.assertEqual(outcome.comment_count, 1)

        text = self.stream.getvalue()
        self.assertIn("한 코멘트에 묶임: 지적 2건", text)
        self.assertIn(self.OTHER, text)

    def test_folded_claim_is_not_lost(self):
        outcome = self.review()
        survivor = next(f for f in outcome.posted if "트랜잭션" in f.claim)
        self.assertTrue(survivor.alt_claims)
        kept_text = survivor.claim + " ".join(survivor.alt_claims)
        self.assertIn(self.DUP_A, kept_text)
        self.assertIn(self.DUP_B, kept_text)


class WorktreeInPipelineTest(PipelineTestCase):
    """리뷰는 PR head 기준, 리뷰 규칙은 원본 레포 기준."""

    def setUp(self) -> None:
        super().setUp()
        write_file(self.repo, "src/app.py", APP_BEFORE)
        self.base = commit_all(self.repo, "base")
        write_file(self.repo, "src/app.py", APP_AFTER)
        self.head = commit_all(self.repo, "PR head")
        # PR 이후에 더 진행된 커밋 -> 워킹트리가 head와 달라진다
        write_file(self.repo, "src/app.py", APP_AFTER2)
        commit_all(self.repo, "later work")

        # REVIEW.md는 커밋하지 않는다. worktree에는 없고 원본에만 있다.
        write_file(self.repo, "REVIEW.md", REVIEW_MD)

        # 트리는 실행이 끝나면 정리되므로, 호출 시점에 무엇을 보고 있었는지 기록해 둔다.
        self.seen: list[tuple[str, str, bool]] = []

        def handler(prompt: str, repo_path: str) -> str:
            app = os.path.join(repo_path, "src/app.py")
            content = ""
            if os.path.exists(app):
                with open(app, encoding="utf-8") as handle:
                    content = handle.read()
            self.seen.append(
                (repo_path, content, os.path.exists(os.path.join(repo_path, "REVIEW.md")))
            )
            if "당신은 코드 리뷰 지적을 검증하는 검증자" in prompt:
                return VERDICT_REAL
            return "[]"

        self.runner = FakeLLMRunner(handler=handler)
        self.orchestrator = Orchestrator(self.runner, self.store)

    def review(self, use_worktree=True):
        return self.orchestrator.run(
            self.repo,
            12,
            dry_run=True,
            record_state=False,
            publisher=self.publisher(),
            pr_title="PR",
            pr_body="",
            base=self.base,
            head=self.head,
            use_worktree=use_worktree,
        )

    def test_llm_reads_the_head_tree_not_the_working_tree(self):
        self.review()
        self.assertTrue(self.seen, "LLM이 한 번은 호출되어야 한다")
        for path, content, _ in self.seen:
            self.assertNotEqual(os.path.abspath(path), os.path.abspath(self.repo))
            # 워킹트리는 APP_AFTER2인데 리뷰 대상은 PR head인 APP_AFTER여야 한다
            self.assertEqual(content, APP_AFTER)

    def test_review_md_is_read_from_the_origin_not_the_tree(self):
        """untracked REVIEW.md는 트리에 없지만 프롬프트에는 들어가야 한다."""
        self.review()
        prompt = self.runner.calls[0][0]
        self.assertIn("Nit은 PR당 최대 1개", prompt)
        self.assertIn("nit은 최대 1개", prompt)
        # 트리 안에는 REVIEW.md가 없다 (그래서 문서를 원본에서 읽어야 한다)
        self.assertFalse(any(has_review for _, _, has_review in self.seen))

    def test_no_worktree_flag_uses_the_working_tree(self):
        self.review(use_worktree=False)
        for path, content, has_review in self.seen:
            self.assertEqual(os.path.abspath(path), os.path.abspath(self.repo))
            self.assertEqual(content, APP_AFTER2)
            self.assertTrue(has_review)

    def test_tree_is_cleaned_up_after_the_run(self):
        self.review()
        for path, _, _ in self.seen:
            self.assertFalse(os.path.exists(path), f"임시 트리가 남았다: {path}")


class RoundCapTest(PipelineTestCase):
    """라운드 상한을 넘기면 요약 코멘트 하나만 게시하고 중단한다."""

    def setUp(self) -> None:
        super().setUp()
        write_file(self.repo, "review.yaml", "max_rounds: 1\ndetectors: 1\nverifiers: 1\npass_threshold: 1\n")
        write_file(self.repo, "src/app.py", APP_BEFORE)
        self.base = commit_all(self.repo, "base")
        write_file(self.repo, "src/app.py", APP_AFTER)
        self.head = commit_all(self.repo, "change")

        self.counter = {"n": 0}

        def handler(prompt: str, repo: str) -> str:
            if "당신은 코드 리뷰 지적을 검증하는 검증자" in prompt:
                return VERDICT_REAL
            self.counter["n"] += 1
            line = 3 + 10 * self.counter["n"]
            return json.dumps(
                [finding(line, "important", claim=f"{line}번째 줄 문제")], ensure_ascii=False
            )

        self.runner = FakeLLMRunner(handler=handler)
        self.orchestrator = Orchestrator(self.runner, self.store)

    def run_round(self, head: str):
        return self.orchestrator.run(
            self.repo,
            9,
            dry_run=True,
            record_state=True,
            publisher=self.publisher(),
            pr_title="PR",
            pr_body="",
            base=self.base,
            head=head,
        )

    def test_escalates_then_stops(self):
        first = self.run_round(self.head)
        self.assertFalse(first.escalated)
        self.assertEqual(len(first.posted), 1)

        write_file(self.repo, "src/app.py", APP_AFTER2)
        head2 = commit_all(self.repo, "another change")

        self.stream = io.StringIO()
        second = self.run_round(head2)

        self.assertTrue(second.escalated)
        self.assertEqual(second.round_number, 2)
        self.assertEqual(len(second.posted), 1)
        self.assertIn("접근 방식을 다시 검토", self.stream.getvalue())

        state = self.store.get_state(os.path.basename(self.repo), 9)
        self.assertTrue(state.stopped)

        calls_before = self.runner.call_count
        third = self.run_round(head2)
        self.assertTrue(third.stopped)
        self.assertEqual(self.runner.call_count, calls_before)  # LLM을 더 부르지 않는다


if __name__ == "__main__":
    unittest.main()


class CacheScopeTest(unittest.TestCase):
    """백엔드가 캐시 키에 들어가야 한다. 없으면 --backend를 바꿔도 이전 응답이 적중한다."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = StateStore(os.path.join(self.tmp, "state.db"))
        self.orchestrator = Orchestrator(FakeLLMRunner(), self.store)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scope(self, backend: str) -> tuple:
        config = parse_config_text(f"llm:\n  backend: {backend}\n")
        runner = self.orchestrator._wrap_runner(config, os.path.join(self.tmp, "cache"), "abc123")
        return tuple(runner.scope)

    def test_backend_is_part_of_the_scope(self):
        self.assertIn("codex", self._scope("codex"))
        self.assertIn("claude", self._scope("claude"))

    def test_switching_backend_changes_the_scope(self):
        self.assertNotEqual(self._scope("codex"), self._scope("claude"))

    def test_no_cache_dir_returns_the_bare_runner(self):
        config = parse_config_text("")
        self.assertIs(self.orchestrator._wrap_runner(config, None, "abc123"), self.orchestrator.runner)


SECURITY_REVIEW_MD = """# SECURITY-REVIEW.md

## 금지 API
- `Runtime.exec(String)`

## 알려진 예외
- `/internal/health` 는 인증 없음. 재검토 완료.
"""

SECURITY_YAML = """
security_angles: [authz, injection]
comment_language: ko
"""

GATE_YAML = """
gate:
  always_review: [".github/workflows/"]
  deep_paths: ["src/auth/"]
  skip_if_only: ["docs/", "*.md"]
"""

SECURITY_VERDICT = '{"verdict": "real", "reason": "악용 가능", "checked": "src/app.py:3"}'


class SecurityPipelineTest(PipelineTestCase):
    """보안 관점을 켠 레포를 끝까지 돌린다."""

    def setUp(self) -> None:
        super().setUp()
        write_file(self.repo, "AGENTS.md", AGENTS_MD)
        write_file(self.repo, "REVIEW.md", REVIEW_MD)
        write_file(self.repo, "SECURITY-REVIEW.md", SECURITY_REVIEW_MD)
        write_file(self.repo, "review.yaml", SECURITY_YAML)
        write_file(self.repo, "src/app.py", APP_BEFORE)
        self.base = commit_all(self.repo, "base")

        write_file(self.repo, "src/app.py", APP_AFTER)
        self.head = commit_all(self.repo, "change line 3")

    def build(self, payload: str) -> Orchestrator:
        self.runner = FakeLLMRunner(
            rules=[
                ("당신은 보안 지적을 검증하는 검증자", SECURITY_VERDICT),
                ("당신은 코드 리뷰 지적을 검증하는 검증자", VERDICT_REAL),
            ],
            default=payload,
        )
        return Orchestrator(self.runner, self.store)

    def run_round(self, payload: str):
        return self.build(payload).run(
            self.repo,
            7,
            dry_run=True,
            record_state=True,
            publisher=self.publisher(),
            pr_title="테스트 PR",
            pr_body="line 3을 고친다",
            base=self.base,
            head=self.head,
        )

    def test_security_angles_run_alongside_the_general_ones(self):
        outcome = self.run_round(json.dumps([finding(3)], ensure_ascii=False))
        angles = [run.angle for run in outcome.detection.runs]
        self.assertEqual(len(angles), 5)
        self.assertIn("인증·인가", angles)
        self.assertIn("입력 검증·인젝션", angles)
        self.assertIn("로직·엣지케이스", angles)

    def test_security_finding_is_posted_with_the_security_label(self):
        payload = json.dumps(
            [finding(3, category="security", claim="sort_by가 SQL에 결합된다")],
            ensure_ascii=False,
        )
        outcome = self.run_round(payload)

        posted = [f for f in outcome.posted if f.category == "security"]
        self.assertEqual(len(posted), 1)
        self.assertIn("보안 1건 포함", outcome.summary_line)
        self.assertIn("(security)", self.stream.getvalue())

        # 실제 PR에 달릴 인라인 코멘트와 요약에는 보안 표시가 들어간다.
        from review_agent.publisher import render_comment, render_summary

        self.assertIn("[보안]", render_comment(posted))
        self.assertIn("### 보안", render_summary(outcome.posted))

    def test_security_doc_reaches_only_the_security_prompts(self):
        self.run_round(json.dumps([finding(3, category="security")], ensure_ascii=False))

        security_prompts = [
            call[0] for call in self.runner.calls if "보안 리뷰어입니다" in call[0]
        ]
        general_prompts = [
            call[0] for call in self.runner.calls if "코드 리뷰어입니다" in call[0]
        ]
        self.assertTrue(security_prompts)
        self.assertTrue(general_prompts)
        self.assertTrue(all("Runtime.exec(String)" in p for p in security_prompts))
        self.assertTrue(all("Runtime.exec(String)" not in p for p in general_prompts))

    def test_security_finding_needs_unanimous_verdicts(self):
        self.runner = FakeLLMRunner(
            rules=[
                ("## 당신의 확인 항목: 기존 방어 존재", '{"verdict": "not_real", "reason": "방어 있음"}'),
                ("당신은 보안 지적을 검증하는 검증자", SECURITY_VERDICT),
                ("당신은 코드 리뷰 지적을 검증하는 검증자", VERDICT_REAL),
            ],
            default=json.dumps(
                [finding(3, category="security", claim="악용 가능하다")], ensure_ascii=False
            ),
        )
        outcome = Orchestrator(self.runner, self.store).run(
            self.repo,
            7,
            dry_run=True,
            record_state=True,
            publisher=self.publisher(),
            pr_title="테스트 PR",
            pr_body="",
            base=self.base,
            head=self.head,
        )

        self.assertEqual([f for f in outcome.posted if f.category == "security"], [])
        votes = [d for d in outcome.dropped if d.reason == "투표_미달"]
        self.assertTrue(votes)
        self.assertIn("보안 임계값 3", votes[0].detail)


class GatePipelineTest(PipelineTestCase):
    """실행 게이트는 LLM을 부르기 전에 평가된다."""

    def setUp(self) -> None:
        super().setUp()
        write_file(self.repo, "review.yaml", GATE_YAML)
        write_file(self.repo, "docs/guide.md", "# 가이드\n")
        write_file(self.repo, "src/app.py", APP_BEFORE)
        self.base = commit_all(self.repo, "base")

    def head_after(self, changes: dict[str, str]) -> str:
        for path, content in changes.items():
            write_file(self.repo, path, content)
        return commit_all(self.repo, "change")

    def run_with(self, head: str, payload: str = "[]"):
        self.runner = FakeLLMRunner(
            rules=[("당신은 코드 리뷰 지적을 검증하는 검증자", VERDICT_REAL)],
            default=payload,
        )
        return Orchestrator(self.runner, self.store).run(
            self.repo,
            7,
            dry_run=True,
            record_state=True,
            publisher=self.publisher(),
            pr_title="테스트 PR",
            pr_body="",
            base=self.base,
            head=head,
        )

    def test_docs_only_pr_is_skipped_without_calling_the_llm(self):
        outcome = self.run_with(self.head_after({"docs/guide.md": "# 가이드 2\n"}))

        self.assertTrue(outcome.skipped)
        self.assertEqual(self.runner.call_count, 0)
        self.assertIn("skip_if_only", outcome.reason)

    def test_a_skipped_run_does_not_consume_a_round(self):
        skipped = self.run_with(self.head_after({"docs/guide.md": "# 가이드 2\n"}))
        self.assertTrue(skipped.skipped)

        # 다음 푸시가 라운드 1이어야 한다. 건너뛴 실행이 라운드를 썼다면 2가 된다.
        reviewed = self.run_with(self.head_after({"src/app.py": APP_AFTER}))
        self.assertFalse(reviewed.skipped)
        self.assertEqual(reviewed.round_number, 1)

    def test_workflow_change_is_reviewed_even_next_to_docs(self):
        outcome = self.run_with(
            self.head_after(
                {
                    "docs/guide.md": "# 가이드 2\n",
                    ".github/workflows/ci.yml": "on: pull_request_target\n",
                }
            )
        )

        self.assertFalse(outcome.skipped)
        self.assertGreater(self.runner.call_count, 0)

    def verification_calls(self) -> int:
        return sum(1 for call in self.runner.calls if "검증자입니다" in call[0])

    def test_auth_change_raises_the_intensity(self):
        payload = json.dumps([finding(3, file="src/auth/token.py")], ensure_ascii=False)
        outcome = self.run_with(
            self.head_after({"src/auth/token.py": "TOKEN = 1\n"}), payload
        )

        self.assertFalse(outcome.skipped)
        # deep 프리셋은 검증자를 5명으로 올린다. standard면 3명이다.
        self.assertEqual(self.verification_calls(), 5)

    def test_ordinary_change_keeps_the_default_intensity(self):
        payload = json.dumps([finding(3)], ensure_ascii=False)
        outcome = self.run_with(self.head_after({"src/app.py": APP_AFTER}), payload)

        self.assertFalse(outcome.skipped)
        self.assertEqual(self.verification_calls(), 3)


class EmptyScopePipelineTest(PipelineTestCase):
    """리뷰할 변경이 없으면 LLM을 부르지 않는다.

    이 테스트가 막는 회귀: 머지된 PR을 돌리면 `base...head` 세 점 diff가 비는데,
    예전에는 그대로 진행해서 탐지기가 diff 대신 현재 파일을 읽었다. 그러면 이 PR이
    바꾸지 않은 것을 이 PR의 문제처럼 보고한다.
    """

    def setUp(self) -> None:
        super().setUp()
        write_file(self.repo, "src/app.py", APP_BEFORE)
        self.first = commit_all(self.repo, "first")
        write_file(self.repo, "src/app.py", APP_AFTER)
        self.second = commit_all(self.repo, "second")

    def run_range(self, base: str, head: str):
        self.runner = FakeLLMRunner(default=json.dumps([finding(3)], ensure_ascii=False))
        return Orchestrator(self.runner, self.store).run(
            self.repo,
            7,
            dry_run=True,
            record_state=True,
            publisher=self.publisher(),
            pr_title="테스트 PR",
            pr_body="",
            base=base,
            head=head,
        )

    def test_already_merged_pr_is_skipped_with_an_actionable_reason(self):
        # head(first)가 base(second)의 조상 — 머지된 PR과 같은 상황이다.
        outcome = self.run_range(base=self.second, head=self.first)

        self.assertTrue(outcome.skipped)
        self.assertEqual(self.runner.call_count, 0)
        self.assertIn("이미 base에 머지", outcome.reason)
        self.assertIn("--base", outcome.reason)

    def test_a_skipped_empty_scope_does_not_consume_a_round(self):
        self.run_range(base=self.second, head=self.first)
        reviewed = self.run_range(base=self.first, head=self.second)
        self.assertFalse(reviewed.skipped)
        self.assertEqual(reviewed.round_number, 1)

    def test_identical_base_and_head_is_skipped(self):
        outcome = self.run_range(base=self.second, head=self.second)
        self.assertTrue(outcome.skipped)
        self.assertEqual(self.runner.call_count, 0)

    def test_a_real_range_still_runs(self):
        outcome = self.run_range(base=self.first, head=self.second)
        self.assertFalse(outcome.skipped)
        self.assertGreater(self.runner.call_count, 0)
