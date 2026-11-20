# pr-review-agent

Pull Request 리뷰 에이전트입니다. 엔진은 범용으로 두고, 레포별 사정은 대상 레포의
`AGENTS.md` / `REVIEW.md` / `review.yaml`로만 들어옵니다.

## 스택과 명령

- Python 3.10+, 외부 의존성 없음 (표준 라이브러리만)
- 테스트: `python3 -m unittest discover -s tests -t tests`
- 소스는 `src/review_agent/`, 테스트는 `tests/`

## 컨벤션 (린터가 못 잡는 것만)

- 병합, 투표 집계, 필터, 라운드 상한은 **순수 함수**로 둔다. LLM 없이 단위 테스트할 수 있어야 한다.
- LLM 호출은 전부 `LLMRunner` 인터페이스를 통한다. 테스트는 `FakeLLMRunner`를 주입한다.
- 프로젝트별 하드코딩을 엔진에 넣지 않는다. 예시는 `examples/`에만 둔다.
- 비밀값(토큰 등)을 코드나 로그에 남기지 않는다. 게시 인증은 `gh` CLI에 맡긴다.
- 프롬프트 문구를 바꾸면 `tests/test_pipeline.py`의 프롬프트 검증도 함께 본다.

## 목표 / 비목표

- 목표: 진짜 버그를 잡는다. 그다음으로 리뷰 라운드를 줄인다.
- 비목표: 지적 개수를 늘린다. 스타일/포맷을 지적한다. approve를 자동화한다.

## 리뷰 지침

- 코드 리뷰를 할 때는 먼저 레포 루트의 REVIEW.md를 읽는다.
