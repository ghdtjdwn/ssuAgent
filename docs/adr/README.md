# ADR 색인

ssuAgent의 아키텍처 결정 기록이다. 번호는 작성 순서이며 0002, 0006–0008은 발행되지 않았다.
날짜와 상태는 각 문서에 적힌 값 그대로다. 결정이 바뀐 경우 본문을 고치지 않고 상단에 갱신 주석을
둔다.

| 번호 | 제목 | 날짜 | 상태 | 요약 |
| --- | --- | --- | --- | --- |
| [0001](0001-supervisor-architecture.md) | 멀티에이전트 수퍼바이저 아키텍처 | 2026-06-14 | 확정(구현 완료) | supervisor가 academic·library·LMS specialist로 라우팅하고 도서관 예약은 HITL 승인 게이트를 거친다. 체크포인터 부분은 0003으로 대체 |
| [0003](0003-postgres-checkpointer.md) | LangGraph 체크포인터 마이그레이션 (SqliteSaver → AsyncPostgresSaver) | 2026-06-14 | Accepted | 컨테이너 재시작 시 대화·승인 대기 상태가 사라지는 문제를 PostgreSQL 체크포인터와 `AsyncConnectionPool`로 해결 |
| [0004](0004-multi-provider-llm-fallback.md) | 멀티 LLM 프로바이더 자동 Fallback | 2026-06-15 | Accepted | 쿼터 소진 시 retry로 hang되는 대신 provider 시퀀스를 agent별 loop에서 순차 시도 |
| [0005](0005-router-bug-fix-prepare-collision.md) | LMS 내보내기 라우팅과 라우터 prefix 충돌 수정 | 2026-06-16 | Accepted | `prepare_`/`confirm_action` prefix 매칭을 정확한 도서관 도구 이름 집합으로 교체 |
| [0009](0009-agent-edge-hardening.md) | `/agent/*` 엣지 하드닝 | 2026-06-23 | Accepted | per-IP rate limit, payload cap, 오류 비노출, CORS 축소 |
| [0010](0010-agent-thread-ownership-binding.md) | Agent thread ownership binding | 미기재 | Accepted | `thread_owners` 테이블로 `thread_id`를 호출자에 묶어 다른 사용자의 resume·읽기를 차단 |
| [0011](0011-thread-stable-principal-binding.md) | 회전 세션 ID 대신 안정 principal로 thread 소유권 재바인딩 | 2026-07-09 | Accepted | ssuAI가 검증한 principal의 hash를 소유자로 사용하고 세션 소유 thread를 lazily 승격 |
| [0012](0012-supervisor-narration-suppression.md) | Supervisor narration suppression by runnable tags | 2026-07-11 (2026-07-16 갱신) | Accepted | `supervisor_llm` 태그로 라우팅 서술을 SSE에서 걸러냄 |
| [0013](0013-library-reservation-preauth-gate.md) | 도서관 좌석 예약 사전 인증 게이트와 resume 세션 최신화 | 2026-07-11 | Accepted | 로그인 없는 예약 의도는 LLM 호출 전에 고정 안내로 응답 |
| [0014](0014-mcp-connection-resilience.md) | MCP tool-call retry and deep health endpoint | 2026-07-11 | Accepted | MCP 도구 호출 retry wrapper와 `/healthz/deep` |
| [0015](0015-optional-anthropic-provider.md) | Optional Anthropic Claude provider | 2026-07-11 | Accepted (2026-11-09 상태 갱신) | `ANTHROPIC_API_KEY`가 있을 때만 provider 시퀀스 맨 앞에 추가 |
| [0016](0016-mcp-content-block-hitl-unwrap.md) | MCP content-block 언랩으로 도서관 예약 HITL 승인 복구 | 2026-07-12 | Accepted | MCP content-block 응답을 풀어 승인 payload를 복구 |
| [0017](0017-hitl-resume-command-and-tool-pair-sanitizer.md) | HITL resume는 Command로 원자 처리, tool pair는 호출 경계에서 정리 | 2026-07-12 | Accepted | resume 전 `aupdate_state` 금지, `build_resume_command`로 단일 경로 |
| [0018](0018-deterministic-library-routing-and-wait-followthrough.md) | 도서관 결정적 선라우팅과 예약 대기 상태 후속 확인 | 2026-07-12 | Accepted | 도서관 키워드는 LLM 전에 `library_agent`로 보수적 라우팅 |
| [0019](0019-hitl-turn-scope-routing-and-fallback-dedup.md) | HITL 턴 스코프, 도서관 태그 라우팅, fallback dedup 회귀 수정 | 2026-07-12 | Accepted | pending action 탐색을 현재 턴으로 제한하고 중복 fallback 응답 제거 |
| [0020](0020-personal-academic-auth-gating.md) | 개인 학사 조회의 인증 게이트와 내부 세션 은닉 | 2026-07-16 | Accepted | 개인 학사 현황 요청은 세션 없으면 LLM 전에 고정 안내 |
| [0021](0021-deterministic-public-seat-availability.md) | 공개 좌석 빈자리 조회의 결정적 도구 선택 | 2026-07-16 | Accepted | 층 지정 빈자리 질의를 `fullmatch` 문법으로 분류 |
| [0022](0022-deterministic-lms-export-download.md) | LMS 전체 자료 내보내기의 결정적 다운로드 응답 | 2026-07-17 | Accepted | 명확한 LMS 요청을 `lms_agent`로 직접 라우팅하고 결정적 링크 생성 |
| [0023](0023-conversation-lifecycle-and-edge-identity.md) | 대화 capability, 수명 주기와 edge identity hardening | 2026-07-27 | Accepted | request-scoped MCP capability, 소유권 검증 삭제 API, 30일 retention, 서명된 edge identity |
