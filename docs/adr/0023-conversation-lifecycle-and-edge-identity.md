# ADR 0023 — 대화 capability, 수명 주기와 edge identity hardening

## 상태

Accepted — 2026-07-27

## 배경과 불변 조건

`mcp_session_id`는 private MCP tool을 실행하는 bearer capability인데 기존 `SsuAgentState`에 들어가
PostgreSQL checkpoint마다 직렬화됐다. 또한 삭제·retention 경로가 없고 readiness가 shallow health를
사용했으며, ingress limiter는 Vercel egress 또는 spoof 가능한 forwarding header를 사용자로 오인했다.

다음 조건을 유지한다.

- browser는 기존 request body와 SSE/HITL 계약을 계속 사용한다.
- private tool과 승인 후 `confirm_action`은 반드시 현재 request의 MCP capability를 사용한다.
- principal owner는 재로그인으로 MCP session이 회전해도 같은 thread를 사용한다.
- liveness는 storage/upstream 장애로 pod를 재시작시키지 않는다.
- 삭제 중 일부 테이블만 지워지는 상태는 허용하지 않는다.

## 결정

1. Next BFF와 FastAPI 모두 body 32 KiB, `thread_id`/`mcp_session_id` 128자, 제한된 identifier alphabet을
   검사한다. FastAPI ASGI middleware는 JSON decode 전 실제 chunk 합계도 제한한다.
2. HTTP entry point는 live MCP capability를 async request context에 바인딩하고 checkpoint 호환 channel에는
   `None`을 쓴다. HITL resume payload에도 raw capability를 넣지 않는다. 기존 raw session owner row는 첫
   정상 접근에서 domain-separated SHA-256 digest로 lazy migration한다. LangGraph 1.2.4가 primitive state를
   checkpoint JSONB에 inline하고 pending write를 typed blob으로 저장하는 실제 schema를 기준으로 startup
   scrub이 정확히 `mcp_session_id` channel만 serialized `None`으로 bounded·idempotent 치환한다. 동일한
   DB trigger가 rolling rollout의 old pod가 만드는 새 legacy write도 저장 전에 `None`으로 정규화한다.
   scrub은 checkpoint 후보를 lock 없이 먼저 고른 뒤 관련 `thread_owners`를 `thread_id` 순서의 `FOR SHARE`로
   lock하고 checkpoint row를 갱신한다. checkpoint-first scrub과 owner-first delete가 서로 기다리는 lock
   inversion을 금지한다.
3. `DELETE /agent/threads/{thread_id}`는 API key와 기존 owner를 검증하고 checkpoint, blob, write를 한
   PostgreSQL transaction에서 지운 뒤 owner 값 없는 `deleted` tombstone을 남긴다. concurrent stream의
   late checkpoint를 다른 caller가 재claim하지 못하게 한다. 세 checkpoint table의 write trigger가 owner
   row를 `FOR SHARE`로 잡고 DELETE는 같은 row를 `FOR UPDATE`로 잡으므로, 먼저 시작한 saver write는 DELETE가
   지우고 DELETE가 먼저 잡은 뒤의 saver write는 tombstone 확인 후 거부된다. 성공한 DELETE commit 뒤 세
   table은 계속 비어 있다. retention이 tombstone을 최종 제거한다. 모르는 thread와 이미 삭제된 thread는
   idempotent 204, owner mismatch는 403이다.
4. `last_accessed_at` 기준 기본 30일 retention을 한 시간마다 최대 100개씩 정리한다. row lock과 한
   transaction을 사용해 active access와 partial deletion을 방지한다. 여러 expired owner도
   `thread_id` 순서로 lock해 scrub·explicit delete와 동일한 owner → checkpoint lock order를 유지한다.
5. `/health`는 shallow liveness로 유지하고 `/ready`가 2초 안에 pool `SELECT 1`과 checkpointer read를 모두
   성공해야 200을 반환한다.
6. ssuAI는 verified principal 또는 Vercel의 spoof 방지 IP에서 pseudonymous client ID를 만들고 shared key로
   서명한다. 서로 충돌하는 경우 platform이 덮어쓰는 `X-Vercel-Forwarded-For`를 generic
   `X-Forwarded-For`보다 우선한다. Agent는 유효한 서명만 limiter key로 사용하며 arbitrary
   `X-Forwarded-For`는 무시한다.

## 실패, rollback과 남은 위험

- runtime context가 누락된 HTTP path는 private tool을 무인증으로 호출하지 않고 capability 없음으로
  처리한다. direct graph 테스트용 legacy fallback은 남지만 production ingress는 항상 context를 bind하고
  state를 `None`으로 덮는다.
- retention/delete transaction이 실패하면 PostgreSQL이 전체 변경을 rollback한다. cleanup timeout은 다음
  interval에 재시도하며 readiness failure는 pod를 traffic에서만 제외한다.
- capability scrub은 500-row transaction으로 나뉘며 이미 치환한 row를 다시 건드리지 않는다. 기본 총
  100,000-row 상한을 소진하고 잔여 raw channel이 있으면 startup을 fail closed한다. row별 write fence DDL과
  scrub은 advisory lock으로 rolling replica 사이에서 직렬화된다.
- lifecycle 경로는 모두 owner row를 checkpoint row보다 먼저 lock한다. 이 순서를 바꾸면 startup scrub과
  DELETE/retention 사이에 교착이 생길 수 있으므로 PostgreSQL 통합 테스트가 lifecycle delete를 owner lock
  직후 정지시키고 scrub이 checkpoint lock을 선점하지 않는지 검증한다.
- 현재 limiter storage는 process-local이다. signed identity는 한 pod 안의 Vercel egress collapse를 고치지만
  multi-replica 전역 quota는 아니다. replica를 늘리기 전 shared limiter의 가용성·fail-open/closed 정책을
  별도 결정해야 한다.
- 배포 rollback은 이전 image로 가능하지만 이미 정상 삭제된 conversation은 복구 대상이 아니다. retention
  기간 변경은 config rollback으로 이후 삭제만 멈추며 이미 만료 처리된 데이터를 되살리지 않는다.
- 이전 image로 rollback해도 additive trigger가 exact legacy channel을 계속 `None`으로 정규화하고 삭제
  fence를 유지한다. 이전 app image가 request-scoped ContextVar를 사용하지 않더라도 bearer가 DB에 다시
  남지는 않는다. hardened image를 다시 배포하면 startup scrub이 idempotent 재실행된다.

## 검증

`tests/test_main_security.py`가 identifier/body 제한, signed identity 검증, owner mismatch 삭제, atomic delete,
readiness를 검증한다. `tests/test_stream_interrupt.py`와 `tests/test_library_agent.py`는 최신 request capability로
resume하면서 checkpoint channel이 `None`인 계약을 검증한다. 전체 Ruff/pytest gate가 이 결정의 회귀
경계다. `tests/test_postgres_conversation_lifecycle.py`는 disposable PostgreSQL에서 실제
`AsyncPostgresSaver` serialization scrub과 DELETE/late-write lock 순서를 검증한다.
같은 suite가 startup scrub과 explicit DELETE/retention을 실제 PostgreSQL에서 동시에 실행해 owner-first
lock order와 deadlock 없는 완료를 검증한다.
