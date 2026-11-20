# SECURITY-REVIEW.md

대상 레포 루트에 두는 선택 문서입니다. **보안 관점 탐지와 보안 검증 프롬프트에만** 원문으로
주입됩니다 (범용 리뷰어는 이 문서를 보지 않습니다).

엔진은 일반적인 보안 통념만 압니다. 우리 팀의 인증 방식, 금지한 API, 데이터 분류 기준은
엔진이 알 수 없으므로 여기 적습니다. 범용 리뷰와 보안 리뷰의 차이가 실제로 벌어지는 지점이
이 문서입니다.

아래는 작성 예시입니다. 그대로 쓰지 말고 실제 규칙으로 바꾸세요.

## 인증·인가

- 모든 HTTP 엔드포인트는 `@RequireAuth`를 붙인다. 공개 엔드포인트는
  `src/main/.../PublicEndpoints.kt`의 목록에 등록된 것만 허용한다.
- 리소스 소유자 검사는 서비스 계층의 `assertOwner(principal, resourceId)`로 한다.
  컨트롤러에서 직접 비교하지 않는다.
- 역할 검사는 문자열 비교가 아니라 `Role` enum으로 한다.

## 금지 API

- `ObjectMapper.enableDefaultTyping()` — 역직렬화 취약점
- `Runtime.exec(String)` — 배열 형태만 쓴다
- `MessageDigest.getInstance("MD5" | "SHA-1")` — 비밀번호·서명 용도 금지
- `new Random()` — 토큰·난수 생성에는 `SecureRandom`
- 원시 문자열 SQL — Exposed DSL 또는 파라미터 바인딩만

## 데이터 분류

| 등급 | 예시 | 규칙 |
|---|---|---|
| 기밀 | 주민등록번호, 카드번호, 비밀번호 해시 | 로그 금지, 응답 금지, 저장 시 암호화 |
| 민감 | 이메일, 전화번호, 주소, 결제 내역 | 로그 금지, 응답은 소유자에게만 |
| 내부 | 내부 ID, 감사 로그 | 응답 금지 |
| 공개 | 상품명, 카테고리 | 제약 없음 |

- 로깅은 `maskSensitive()`를 거친 값만 남긴다.
- 에러 응답에 스택 트레이스나 SQL을 담지 않는다.

## 알려진 예외

여기 적힌 것은 이미 검토해 수용한 위험입니다. **다시 지적하지 마세요.**

- `/internal/health` — 인증 없음. ingress에서 클러스터 내부로만 노출된다
  (`deploy/ingress.yaml` 참고). 2026-03-01 재검토 예정.
- `LegacyOrderRepository.findBySort()` — 정렬 컬럼을 화이트리스트로 검증한 뒤 결합한다.
  검증은 `SortWhitelist.kt`에 있다.

## 오탐으로 보고하지 않을 것

- `src/test/`, `src/testFixtures/` 아래의 더미 자격증명
- `application-local.yml`의 로컬 전용 값
- 자리표시자(`<your-token>`, `changeme`, `xxx`)
