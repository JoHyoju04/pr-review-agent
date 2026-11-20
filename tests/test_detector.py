from __future__ import annotations

import json
import unittest

import helpers  # noqa: F401

from review_agent.config import parse_config_text
from review_agent.context import RepoDocs, ReviewContext, ReviewGuidance
from review_agent.detector import Detector
from review_agent.llm import FakeLLMRunner, LLMError
from review_agent.models import PRMeta, SchemaError, parse_finding
from review_agent.verifier import Verifier, normalize_verdict

VALID_FINDING = {
    "file": "src/app.py",
    "line_start": 10,
    "line_end": 12,
    "category": "logic",
    "severity": "important",
    "claim": "널 체크가 없다",
    "evidence": "src/app.py:10 `user.getName()`",
    "suggested_fix": "널 체크 추가",
}


def make_context(config_text: str = "") -> ReviewContext:
    return ReviewContext(
        repo_path="/tmp/repo",
        repo_key="owner/repo",
        pr=PRMeta(number=1, title="제목", description="설명", base="base", head="head"),
        config=parse_config_text(config_text),
        docs=RepoDocs(),
        guidance=ReviewGuidance(),
        diff_text="",
        base="base",
        head="head",
    )


class ParseFindingTest(unittest.TestCase):
    def test_valid_finding(self):
        finding = parse_finding(VALID_FINDING, angle="로직·엣지케이스")
        self.assertEqual(finding.file, "src/app.py")
        self.assertEqual(finding.detected_by, ["로직·엣지케이스"])
        self.assertEqual(finding.severity_votes, ["important"])

    def test_missing_evidence_rejected(self):
        payload = {**VALID_FINDING, "evidence": ""}
        with self.assertRaises(SchemaError):
            parse_finding(payload)

    def test_missing_file_rejected(self):
        payload = {**VALID_FINDING}
        payload.pop("file")
        with self.assertRaises(SchemaError):
            parse_finding(payload)

    def test_missing_claim_rejected(self):
        payload = {**VALID_FINDING, "claim": "  "}
        with self.assertRaises(SchemaError):
            parse_finding(payload)

    def test_non_integer_line_rejected(self):
        with self.assertRaises(SchemaError):
            parse_finding({**VALID_FINDING, "line_start": "열 번째 줄"})

    def test_missing_line_end_defaults_to_start(self):
        payload = {**VALID_FINDING}
        payload.pop("line_end")
        self.assertEqual(parse_finding(payload).line_end, 10)

    def test_swapped_lines_are_normalized(self):
        finding = parse_finding({**VALID_FINDING, "line_start": 20, "line_end": 10})
        self.assertEqual((finding.line_start, finding.line_end), (10, 20))

    def test_severity_aliases(self):
        self.assertEqual(parse_finding({**VALID_FINDING, "severity": "major"}).severity, "important")
        self.assertEqual(parse_finding({**VALID_FINDING, "severity": "minor"}).severity, "nit")
        self.assertEqual(
            parse_finding({**VALID_FINDING, "severity": "pre-existing"}).severity, "pre_existing"
        )

    def test_unknown_severity_defaults_to_important(self):
        self.assertEqual(parse_finding({**VALID_FINDING, "severity": "???"}).severity, "important")

    def test_category_aliases(self):
        self.assertEqual(
            parse_finding({**VALID_FINDING, "category": "api-contract"}).category, "api_contract"
        )


class DetectorTest(unittest.TestCase):
    def test_runs_one_detector_per_angle(self):
        runner = FakeLLMRunner(default=json.dumps([VALID_FINDING]))
        result = Detector(runner, parse_config_text("")).detect(make_context())

        self.assertEqual(runner.call_count, 3)
        self.assertEqual(len(result.findings), 3)
        self.assertEqual(
            {run.angle for run in result.runs},
            {"로직·엣지케이스", "동시성·자원", "API·계약 변경"},
        )

    def test_detector_count_from_config(self):
        runner = FakeLLMRunner(default="[]")
        config = parse_config_text("detectors: 1\n")
        Detector(runner, config).detect(make_context("detectors: 1\n"))
        self.assertEqual(runner.call_count, 1)

    def test_empty_array_is_valid(self):
        runner = FakeLLMRunner(default="[]")
        result = Detector(runner, parse_config_text("")).detect(make_context())
        self.assertEqual(result.findings, [])
        self.assertEqual(result.failed_angles, [])

    def test_schema_failures_are_dropped_not_fatal(self):
        payload = json.dumps([VALID_FINDING, {"file": "a.py", "claim": "근거 없음"}])
        runner = FakeLLMRunner(default=payload)
        config = parse_config_text("detectors: 1\n")
        result = Detector(runner, config).detect(make_context("detectors: 1\n"))

        self.assertEqual(len(result.findings), 1)
        self.assertEqual(len(result.runs[0].schema_errors), 1)

    def test_parse_failure_retries_once_then_gives_up(self):
        runner = FakeLLMRunner(default="JSON이 아닌 응답")
        config = parse_config_text("detectors: 1\n")
        result = Detector(runner, config).detect(make_context("detectors: 1\n"))

        self.assertEqual(runner.call_count, 2)  # 최초 1회 + 재시도 1회
        self.assertEqual(result.failed_angles, ["로직·엣지케이스"])

    def test_retry_succeeds_on_second_attempt(self):
        responses = iter(["엉뚱한 출력", json.dumps([VALID_FINDING])])

        def handler(prompt: str, repo: str) -> str:
            return next(responses)

        runner = FakeLLMRunner(handler=handler)
        config = parse_config_text("detectors: 1\n")
        result = Detector(runner, config).detect(make_context("detectors: 1\n"))

        self.assertEqual(len(result.findings), 1)
        self.assertIn("재출력 요청", runner.calls[1][0])

    def test_llm_error_marks_angle_failed(self):
        def handler(prompt: str, repo: str) -> str:
            raise LLMError("백엔드 없음")

        result = Detector(FakeLLMRunner(handler=handler), parse_config_text("")).detect(
            make_context()
        )
        self.assertEqual(len(result.failed_angles), 3)
        self.assertEqual(result.findings, [])


class VerifierTest(unittest.TestCase):
    def test_runs_m_verifiers_per_finding(self):
        runner = FakeLLMRunner(default='{"verdict": "real", "reason": "확인"}')
        findings = [parse_finding(VALID_FINDING)]
        findings[0].id = "f-001"

        votes = Verifier(runner, parse_config_text("")).verify(make_context(), findings)

        self.assertEqual(runner.call_count, 3)
        self.assertEqual(votes[0].real_count, 3)
        self.assertEqual(
            {v.check for v in votes[0].verdicts},
            {"코드 근거", "의도된 동작 여부", "실제 영향 여부"},
        )

    def test_verifier_prompt_excludes_detector_reasoning(self):
        runner = FakeLLMRunner(default='{"verdict": "not_real"}')
        finding = parse_finding(VALID_FINDING, angle="로직·엣지케이스")
        finding.id = "f-001"
        finding.suggested_fix = "탐지기가 제안한 수정 방향"

        Verifier(runner, parse_config_text("")).verify(make_context(), [finding])

        prompt = runner.calls[0][0]
        self.assertIn(finding.claim, prompt)
        self.assertNotIn("탐지기가 제안한 수정 방향", prompt)
        self.assertNotIn("로직·엣지케이스", prompt.split("## 검증 대상")[1].split("##")[0])

    def test_unparseable_verdict_counts_as_not_real(self):
        runner = FakeLLMRunner(default="판정하기 어렵습니다")
        finding = parse_finding(VALID_FINDING)
        finding.id = "f-001"

        votes = Verifier(runner, parse_config_text("")).verify(make_context(), [finding])
        self.assertEqual(votes[0].real_count, 0)

    def test_no_findings_means_no_calls(self):
        runner = FakeLLMRunner()
        self.assertEqual(Verifier(runner, parse_config_text("")).verify(make_context(), []), [])
        self.assertEqual(runner.call_count, 0)


class NormalizeVerdictTest(unittest.TestCase):
    def test_real_variants(self):
        for value in ("real", "REAL", " Real "):
            self.assertEqual(normalize_verdict(value), "real")

    def test_everything_else_is_not_real(self):
        for value in ("not_real", "not-real", "maybe", "", None, "unsure"):
            self.assertEqual(normalize_verdict(value), "not_real")


if __name__ == "__main__":
    unittest.main()


SECURITY_FINDING = {
    **VALID_FINDING,
    "category": "security",
    "claim": "sort_by가 검증 없이 SQL에 결합된다",
}


class SecurityVerifierTest(unittest.TestCase):
    """보안 finding은 보안 체크를 받는다. 범용 경로는 그대로다."""

    def verify(self, payload: dict, config_text: str = ""):
        runner = FakeLLMRunner(default='{"verdict": "real", "reason": "확인"}')
        finding = parse_finding(payload)
        finding.id = "f-001"
        votes = Verifier(runner, parse_config_text(config_text)).verify(
            make_context(config_text), [finding]
        )
        return runner, votes

    def test_security_finding_gets_security_checks(self):
        runner, votes = self.verify(SECURITY_FINDING)
        self.assertEqual(
            {v.check for v in votes[0].verdicts},
            {"도달 가능성", "기존 방어 존재", "악용 영향"},
        )
        self.assertEqual(runner.call_count, 3)

    def test_general_finding_keeps_general_checks(self):
        _, votes = self.verify(VALID_FINDING)
        self.assertEqual(
            {v.check for v in votes[0].verdicts},
            {"코드 근거", "의도된 동작 여부", "실제 영향 여부"},
        )

    def test_security_checks_do_not_cycle_when_verifiers_is_higher(self):
        # deep 프리셋은 verifiers를 5로 올린다. 범용은 순환하지만(기존 동작) 보안은
        # 순환하면 한 항목에 2표가 실려 AND 의미가 깨지므로 3개로 고정이다.
        runner, votes = self.verify(SECURITY_FINDING, "intensity: deep")
        self.assertEqual(runner.call_count, 3)
        self.assertEqual(len(votes[0].verdicts), 3)

        runner, votes = self.verify(VALID_FINDING, "intensity: deep")
        self.assertEqual(runner.call_count, 5)

    def test_security_prompt_rejects_intent_as_grounds(self):
        runner, _ = self.verify(SECURITY_FINDING)
        prompt = runner.calls[0][0]
        self.assertIn("의도한 구현인지는 판정 근거가 아닙니다", prompt)
        self.assertIn("클라이언트 측 제한", prompt)
        self.assertIn("내부망이라 안전하다", prompt)
        # 범용 템플릿의 "의도된 동작이다 -> not_real" 조건이 섞이면 안 된다.
        self.assertNotIn("의도된 동작이다, 이미 다른 곳에서 처리된다", prompt)

    def test_security_prompt_renders_per_check_refutation(self):
        runner, _ = self.verify(SECURITY_FINDING)
        prompts_by_check = {
            call[0].split("## 당신의 확인 항목: ")[1].splitlines()[0]: call[0]
            for call in runner.calls
        }
        self.assertIn(
            "막는 방어를 `파일:라인`으로 찾은 경우",
            prompts_by_check["기존 방어 존재"],
        )
        self.assertIn(
            "공격자가 제어하는 입력이 이 코드에 닿지 않음",
            prompts_by_check["도달 가능성"],
        )

    def test_general_prompt_still_asks_about_intent(self):
        runner, _ = self.verify(VALID_FINDING)
        joined = "\n".join(call[0] for call in runner.calls)
        self.assertIn("의도된 동작이다, 이미 다른 곳에서 처리된다", joined)

    def test_deterministic_finding_is_not_verified(self):
        runner = FakeLLMRunner(default='{"verdict": "real"}')
        finding = parse_finding(SECURITY_FINDING)
        finding.id = "g-001"
        finding.deterministic = True

        votes = Verifier(runner, parse_config_text("")).verify(make_context(), [finding])

        self.assertEqual(runner.call_count, 0)
        self.assertEqual(votes[0].verdicts, [])


class DeterministicFlagIsEngineOnlyTest(unittest.TestCase):
    def test_llm_cannot_set_deterministic(self):
        # LLM이 출력에 넣어도 무시되어야 한다. 읽으면 검증을 우회할 수 있다.
        finding = parse_finding({**SECURITY_FINDING, "deterministic": True})
        self.assertFalse(finding.deterministic)

    def test_flag_survives_copy_and_round_trips_through_dict(self):
        finding = parse_finding(SECURITY_FINDING)
        self.assertNotIn("deterministic", finding.to_dict())
        finding.deterministic = True
        self.assertTrue(finding.copy().deterministic)
        self.assertTrue(finding.to_dict()["deterministic"])


class SecurityDocInVerifyPromptTest(unittest.TestCase):
    def context_with_security_doc(self) -> ReviewContext:
        ctx = make_context()
        ctx.docs = RepoDocs(security_md="금지 API: MessageDigest MD5")
        return ctx

    def run_verify(self, payload: dict) -> str:
        runner = FakeLLMRunner(default='{"verdict": "real"}')
        finding = parse_finding(payload)
        finding.id = "f-001"
        Verifier(runner, parse_config_text("")).verify(
            self.context_with_security_doc(), [finding]
        )
        return "\n".join(call[0] for call in runner.calls)

    def test_security_verifier_sees_the_repo_security_rules(self):
        self.assertIn("MessageDigest MD5", self.run_verify(SECURITY_FINDING))

    def test_general_verifier_does_not(self):
        self.assertNotIn("MessageDigest MD5", self.run_verify(VALID_FINDING))
