# ADR 0010: Agent thread ownership binding

## 상태

Accepted

## 배경

ssuAgent의 `/agent/stream`, `/agent/resume` 엔드포인트는 클라이언트가 보낸
`thread_id`를 LangGraph Postgres checkpointer의 checkpoint key로 사용한다.
기존 구조에서는 `thread_id`가 어떤 호출자의 대화인지 별도로 저장하지 않았기
때문에, 다른 사용자의 `thread_id`를 알게 된 호출자가 같은 값을 보내면 해당
대화를 이어서 읽거나 재개할 수 있었다.

즉 IDOR(Insecure Direct Object Reference) 위험이 있었다. 대화 기밀성은 사실상
122-bit `randomUUID` 기반 `thread_id`의 비밀성, 그리고 TLS 전송 보호에만
의존했다. UUID가 충분히 크더라도 한 번 노출되면 서버가 소유권을 확인할 근거가
없다는 점이 문제다.

## 대안

### A. 프록시가 JWT에서 안정적인 principal을 추출해 주입

API 게이트웨이 또는 프록시가 ssuAI JWT를 검증하고, 안정적인 사용자 principal을
ssuAgent 요청 헤더에 넣는 방식이다.

채택하지 않았다. ssuAgent가 ssuAI 인증 모델에 직접 결합되고, 프론트엔드 또는
프록시가 JWT 내용을 읽고 전달하는 추가 변경이 필요하다. 이 작업은 보안 효과는
크지만 cross-repo coupling이 커서 이번 후속 작업 범위를 넘는다.

### B. 122-bit randomUUID만 계속 신뢰

현 상태를 유지하고 `thread_id`를 충분히 긴 bearer secret으로 취급하는 방식이다.

채택하지 않았다. 추측 난이도는 높지만, 로그·브라우저 스토리지·디버깅 화면·지원
채널 등을 통해 값이 노출되는 순간 서버 측 권한 확인이 없다. 대화 기밀성이
식별자의 비밀성에만 의존하는 구조는 IDOR 방어로 부족하다.

### C. 세션별 owner 테이블로 thread 소유권 인덱스 생성

채택한 방식이다. ssuAgent 안에 `thread_owners(thread_id, owner, created_at)`
테이블을 두고, `thread_id`를 처음 본 요청의 `mcp_session_id`를 owner로 기록한다.
이후 같은 `thread_id` 요청은 저장된 owner와 현재 요청의 `mcp_session_id`가
일치해야 통과한다.

이 방식은 ssuMCP follow-up #1의 `(owner, conversationId)` 모델을 ssuAgent
checkpointer 앞단에 맞춘 것이다. 또한 대화 데이터에 대한 별도 ownership index를
두는 업계의 `thread_views ownership index` 패턴과도 맞다.

## 결정

`thread_owners` 테이블을 LangGraph checkpointer와 같은 Postgres pool에서
생성하고, `/agent/stream`과 `/agent/resume`에서 그래프 실행 전에
`thread_id` 소유권을 claim 또는 verify한다.

## 동작 방식

1. 서버 시작 시 `checkpointer.setup()` 이후 같은 `AsyncConnectionPool`로 다음
   테이블을 생성한다.

   ```sql
   CREATE TABLE IF NOT EXISTS thread_owners (
       thread_id TEXT PRIMARY KEY,
       owner TEXT,
       created_at TIMESTAMPTZ NOT NULL DEFAULT now()
   )
   ```

2. `/agent/stream`은 `req.thread_id`가 없으면 새 UUID를 만들고, 그래프 상태를
   만들기 전에 `claim_or_verify_thread_owner(thread_id, req.mcp_session_id)`를
   호출한다.

3. `/agent/resume`은 필수 `thread_id`에 대해 같은 helper를 먼저 호출하고, 통과한
   경우에만 LangGraph resume을 실행한다.

4. helper는 먼저 아래 쿼리로 첫 요청만 owner를 기록한다.

   ```sql
   INSERT INTO thread_owners (thread_id, owner)
   VALUES (%s, %s)
   ON CONFLICT (thread_id) DO NOTHING
   ```

5. 이어서 `SELECT owner FROM thread_owners WHERE thread_id = %s`로 저장된 owner를
   읽는다. 저장된 owner가 `NULL`이면 anonymous thread로 보고 허용한다. 저장된
   owner가 있고 현재 `mcp_session_id`와 다르면 403을 반환한다.

## 구현 선택

- `thread_id TEXT PRIMARY KEY`: LangGraph checkpoint key와 같은 문자열 값을 그대로
  인덱싱한다. 별도 surrogate key가 없어도 소유권 확인 경로가 단순하다.
- `owner TEXT`: `mcp_session_id`를 그대로 저장한다. 요청에 세션이 없으면 `NULL`을
  저장해 anonymous fallback을 명시한다.
- `ON CONFLICT DO NOTHING`: 동시에 같은 thread를 생성하려는 요청이 들어와도 첫
  insert만 성공하고 나머지는 select로 기존 owner를 확인한다. autocommit pool에서
  race-safe claim이 된다.
- `owner IS NULL` 허용: 세션 없는 요청으로 처음 만들어진 thread는 계속 anonymous
  thread로 남긴다. 이후 세션 있는 요청이 오더라도 owner를 upgrade하지 않는다.
  단순성과 기존 no-session 흐름 호환성을 우선한다.
- 기존 checkpoint 호환성: 배포 전부터 있던 thread는 `thread_owners` row가 없다.
  배포 후 첫 접근자가 `ON CONFLICT DO NOTHING` 경로로 owner를 claim한다. 기존
  `thread_id`가 122-bit client secret이라는 전제 아래 허용 가능한 migration이다.
- `mcp_session_id` rotation: `mcp_session_id`는 재로그인 시 바뀔 수 있다. 그래서
  ssuAI 쪽 변경으로 logout 시 `ssuagent_thread_id`를 지워야 한다. 그러면 사용자가
  재로그인 후 이전 session owner가 묶인 thread를 보내 self-403을 만나는 흐름을
  피할 수 있다.

## 결과

대화 resume/read 권한이 단순한 `thread_id` 지식에서 `thread_id`와 최초 생성
`mcp_session_id`의 결합으로 강화된다. anonymous thread와 배포 전 checkpoint는
호환성을 위해 허용하지만, 인증 세션이 있는 대화는 다른 세션에서 재사용할 수 없다.
