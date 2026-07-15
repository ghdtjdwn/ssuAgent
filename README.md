# ssuAgent

[![CI](https://github.com/ghdtjdwn/ssuAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/ghdtjdwn/ssuAgent/actions/workflows/ci.yml)

> 🇺🇸 English version: [README.en.md](README.en.md)

> 🧩 **숭실대 캠퍼스 AI 플랫폼** (4-서비스 중 하나) · [ssuMCP](https://github.com/ghdtjdwn/ssuMCP) · [ssuAI](https://github.com/ghdtjdwn/ssuAI) · **ssuAgent** · [ssu-ai-service](https://github.com/ghdtjdwn/ssu-ai-service) · 🟢 [Live](https://ssuai.vercel.app)

숭실대학교 MCP 서버([ssuMCP](https://github.com/ghdtjdwn/ssuMCP))에 연결하는 LangGraph 기반 **멀티에이전트** 캠퍼스 AI 에이전트. [ssuAI](https://github.com/ghdtjdwn/ssuAI) 웹 채팅 UI에 SSE 스트리밍으로 연동된다.

🟢 **Live** — 챗봇에서 바로 사용: <https://ssuai.vercel.app/chat> (이 에이전트가 SSE로 응답)

## Architecture

```
User Query
    │
    ▼
Supervisor (LangGraph StateGraph) ── 질문 분류 → 도메인 라우팅
    ├── academic agent   (학사/성적/졸업/장학)
    ├── library agent    (좌석 추천·예약, prepare/confirm HITL)
    └── lms agent        (강의/과제/자료 내보내기)
    │  Streamable HTTP (MCP 2025-03-26)
    ▼
ssuMCP Server (Spring Boot 4)
    ├── Pyxis (도서관)
    ├── u-SAINT (학사/성적)
    └── LMS (강의/과제)
```

- **멀티 프로바이더 LLM 폴백**: `llm_factory.get_llm_sequence()`가 Groq(llama-3.3-70b, 무료 14,400 req/day) → Gemini → OpenRouter 순으로 폴백(단일 장애점 제거). 각 프로바이더는 해당 API 키가 설정된 경우에만 시퀀스에 추가된다 — Groq는 `GROQ_API_KEY`, Gemini는 `GOOGLE_API_KEY`, OpenRouter는 `OPENROUTER_API_KEY`. 키가 하나도 없으면 `create_llm()`이 명확한 `RuntimeError`를 던진다(조용한 오작동 방지). Groq는 `ChatOpenAI` 래퍼 대신 `ChatGroq`를 쓴다 — 제네릭 래퍼가 assistant content를 list로 직렬화해 2번째 tool call에서 Groq가 400을 내기 때문.
- **상태 영속화**: LangGraph Postgres checkpointer로 대화 상태를 저장한다.
- **대화 소유권 바인딩**: 인증된 요청은 ssuAI 프록시가 검증한 stable principal의 SHA-256 해시에 `thread_id`를 묶는다. 같은 사용자는 재로그인·멀티기기에서도 checkpoint를 이어가고, 다른 principal은 읽기·resume이 거부된다. 기존 세션 소유 thread는 정당한 세션이 principal을 처음 제시할 때 lazy migration된다.
- **HITL 안전장치**: 도서관 write action은 `prepare_*` → 사용자 승인 → `confirm_action` 2단계로만 실행된다.

### 주요 구성요소

| 구성요소 | 파일 | 역할 |
|---|---|---|
| Supervisor | `supervisor/graph.py` | LangChain `create_agent`로 질문을 분류하고 `ROUTE_TO:X` 마커로 도메인을 라우팅한다. 라우팅 도구가 마커 문자열을 반환하면 `post_supervisor` 노드가 스캔해 `Command(goto=X)`를 낸다(ADR 0001). |
| 도메인 에이전트 | `agents/{academic,library,lms}.py` | 도메인별 MCP 도구 묶음 + 수동 `bind_tools` 폴백 루프(프로바이더 장애점 제거) |
| MCP 클라이언트 | `mcp_client.py` | ssuMCP에 Streamable HTTP(MCP 2025-03-26)로 연결, 도구 동적 로드 |
| LLM 팩토리 | `llm_factory.py` | `get_llm_sequence()` — Groq→Gemini→OpenRouter 우선순위 폴백 |
| 체크포인터 | LangGraph Postgres | 대화 상태(turn 간) 영속 |

## Why LangGraph?

| 방식 | 이유 |
|------|------|
| LangChain LCEL | 단순 체인에 적합. 상태·루프·분기 표현 어려움 |
| 직접 function calling | 오케스트레이션 코드 직접 관리. 멀티스텝 복잡도 증가 |
| **LangGraph** (채택) | StateGraph로 상태·분기·루프를 명시적 그래프로 표현. Phase 2 멀티에이전트 확장 용이 |

## Setup

```bash
pip install uv
uv sync --extra dev
```

## Run

최소 하나의 LLM 프로바이더 키가 필요하다(아래 중 하나면 충분, 셋 다 설정하면 폴백 순서대로 사용):

```bash
export GROQ_API_KEY=<your-groq-key>        # 1순위(선택)
export GOOGLE_API_KEY=<your-gemini-key>    # 2순위(선택)
export OPENROUTER_API_KEY=<your-or-key>    # 3순위(선택)
export SSUMCP_URL=https://ssumcp.duckdns.org/mcp  # optional, this is the default
# FastAPI 앱 실행 (SSE 스트리밍 엔드포인트)
uv run uvicorn ssu_agent.main:app --host 0.0.0.0 --port 8000

# 다른 터미널에서 로컬 호출 (키 게이트를 켰다면 -H "X-Agent-Key: <key>" 추가)
curl -N -X POST http://localhost:8000/agent/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "오늘 학식 알려줘"}'
```

## Security / configuration

주요 런타임 환경변수의 코드 기본값은 로컬 개발용이다. 운영 Helm은 `AGENT_API_KEY_REQUIRED=true`와 non-optional Secret을 사용한다.

| 환경변수 | 기본값 | 역할 |
|---|---|---|
| `ALLOWED_ORIGINS` | `*` (전체 허용) | CORS allow-list. 콤마로 구분한 origin 목록(`config.py`에서 파싱 → `main.py` `CORSMiddleware`). 단일 `*`이면 기존처럼 전체 허용. 실제 프론트엔드 origin으로 좁히면 CORS 보호가 활성화된다. |
| `AGENT_API_KEY` | 비어 있음(로컬 게이트 off) | `/agent/*`의 `X-Agent-Key` 자격증명. 운영에서는 필수이며 ssuAI 서버 프록시의 값과 일치해야 한다. 설정하면 `secrets.compare_digest`로 검증하고 없거나 틀리면 401을 반환한다. |
| `AGENT_API_KEY_REQUIRED` | `false` | `true`인데 `AGENT_API_KEY`가 비어 있으면 시작을 거부한다. 운영 값은 `true`이고 로컬 개발에서만 `false`를 허용한다. |
| `AGENT_RATE_LIMIT` | `30/minute` | `/agent/stream`·`/agent/resume`의 per-IP 인바운드 rate limit(slowapi 문법, `main.py`의 `limiter`). 키는 X-Forwarded-For 좌측 홉(ingress 뒤 실클라이언트 IP). 초과 시 429. 배경은 ADR 0009. |
| `AGENT_MAX_MESSAGE_CHARS` | `8000` | 단일 요청 `message`의 최대 문자 수(pydantic `Field(max_length=…)`). 초과 시 422(oversized-payload 가드, ADR 0009). |
| LLM 키 | — | `GROQ_API_KEY`/`GOOGLE_API_KEY`/`OPENROUTER_API_KEY` 중 설정된 것만 폴백 시퀀스에 포함(위 Architecture 참조). |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini 프로바이더가 사용할 모델명(`llm_factory.py`, `GOOGLE_API_KEY` 설정 시에만 사용). |

### Thread ownership binding

`/agent/stream`과 `/agent/resume`은 그래프 실행 전에 `thread_owners`를 확인한다. 로그인 요청의 `principal`은 브라우저가 주장한 값이 아니라 ssuAI 서버 프록시가 access JWT를 검증해 주입한 stable subject다. ssuAgent는 원문 대신 `sha256(principal)`을 `owner_kind='principal'`로 저장한다. 같은 principal은 세션이 회전하거나 기기가 달라도 같은 thread를 사용할 수 있고, 다른 principal이나 principal이 누락된 요청은 403을 받는다.

이전 세션 기반 행(`owner_kind='session'` 또는 legacy `NULL`)은 저장된 `mcp_session_id`와 일치하는 정당한 요청이 검증된 principal을 처음 제시할 때 한 번만 principal 소유로 승격된다. owner row가 없는 오래된 checkpoint는 첫 검증 요청이 claim한다. Authorization이 없는 익명 호출만 기존 session/anonymous 폴백을 사용한다. 자세한 계약은 `docs/adr/0010-agent-thread-ownership-binding.md`와 `docs/adr/0011-thread-stable-principal-binding.md`를 참조한다.

### `/agent` 엔드포인트 인증

운영 `/agent/*`는 API 키 게이트로 보호된다. `AGENT_API_KEY_REQUIRED=true`이므로 키가 없는 배포는 시작하지 않으며, ssuAgent는 `AGENT_API_KEY`와 일치하는 `X-Agent-Key`를 강제한다(`main.py`의 `verify_agent_key`, 불일치 시 401). ssuAI 서버 전용 proxy가 키를 주입하고 검증한 principal만 전달한다. 브라우저는 same-origin `/api/agent/*`만 호출하므로 키와 신뢰된 principal을 직접 제어할 수 없다. 설계 배경과 검증 절차는 `docs/adr/0009-agent-edge-hardening.md`를 참조한다.

## Test

```bash
uv run pytest
```

## Phase Roadmap

| Phase | 범위 | 상태 |
|-------|------|------|
| 1 | ReAct 단일 에이전트, 공개 도구 3종 (식단/도서관/공지) | ✅ 완료 |
| 2 | 도메인별 supervisor 멀티에이전트, 도서관 예약 인증 도구(HITL), 스트리밍 응답 | ✅ 완료 |
| 3 | ssuAI 프론트엔드 연동 (웹 UI 채팅, SSE) | ✅ 완료 |
| 보안 하드닝 | LLM 프로바이더 키 가드, env 기반 CORS(`ALLOWED_ORIGINS`), `/agent` API 키 게이트(`AGENT_API_KEY`), thread ownership binding | ✅ 완료 |

> 구현 메모: 기존 `create_react_agent` executor에서 확인된 루핑 이슈 때문에 도메인 에이전트는 수동 `bind_tools` 폴백 루프를 유지한다(단일 프로바이더 장애점 제거). Supervisor는 지원되는 `langchain.agents.create_agent` API를 사용한다. 근거·대안은 `docs/adr/` 참조.
