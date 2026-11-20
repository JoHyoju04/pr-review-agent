# REVIEW.md

## 우선순위
1. 실제 버그 (잘못된 필터 결과, 투표 집계 오류, 라운드 상태 꼬임, 게시 누락/중복)
2. 비밀값이 로그나 코멘트에 새는 경로
3. 그 외는 지적하지 않는다

## 영역별 "실패"의 정의
- `src/review_agent/filters.py`, `merger.py`: 진짜 버그를 걸러 버리거나 오탐을 통과시킨다
- `src/review_agent/orchestrator.py`: 라운드 수나 diff 범위를 잘못 계산해 같은 지적을 또 올린다
- `src/review_agent/state.py`: 게시 지문이나 피드백이 유실된다
- `src/review_agent/publisher.py`: 코멘트를 엉뚱한 라인에 달거나 비밀값을 본문에 넣는다
- `src/review_agent/llm.py`: 읽기 전용 보장이 깨진다
- `tests/`: 통과하는데도 회귀를 잡지 못한다

## 건너뛸 경로
- `build/`, `dist/`, `*.egg-info/`, `__pycache__/`, `.review-agent/`

## 코멘트 규칙
- 모든 지적에 `파일:라인`과 근거 코드를 붙인다.
- Nit은 PR당 최대 3개.
- 포맷팅, 네이밍 취향, import 순서는 언급하지 않는다.
- 이 PR이 만들지 않은 기존 문제는 `pre_existing`으로 표시한다.

## 요약 형식
- 첫 줄은 개수로 시작한다. 예: "Important 1, Nit 2"
- 지적할 게 없으면 "수정할 사항 없음"이라고만 쓴다.
