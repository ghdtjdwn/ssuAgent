# ssuAgent

[![CI](https://github.com/ghdtjdwn/ssuAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/ghdtjdwn/ssuAgent/actions/workflows/ci.yml)

**한국어** [README.md](README.md) · **English** (this document)

> 🧩 **Soongsil Campus AI Platform** (1 of 4 services) · [ssuMCP](https://github.com/ghdtjdwn/ssuMCP) · [ssuAI](https://github.com/ghdtjdwn/ssuAI) · **ssuAgent** · [ssu-ai-service](https://github.com/ghdtjdwn/ssu-ai-service) · 🟢 [Live](https://ssuai.vercel.app)

A LangGraph-based **multi-agent** campus AI agent for Soongsil University that connects to the university's MCP server ([ssuMCP](https://github.com/ghdtjdwn/ssuMCP)). It integrates with the [ssuAI](https://github.com/ghdtjdwn/ssuAI) web chat UI via SSE streaming.

🟢 **Live** — try it in the chat: <https://ssuai.vercel.app/chat> (this agent answers over SSE)

## Architecture

```
User Query
    │
    ▼
Supervisor (LangGraph StateGraph) ── classifies the query → routes by domain
    ├── academic agent   (academics / grades / graduation / scholarships)
    ├── library agent    (seat recommendation & reservation, prepare/confirm HITL)
    └── lms agent        (courses / assignments / material export)
    │  Streamable HTTP (MCP 2025-03-26)
    ▼
ssuMCP Server (Spring Boot 4)
    ├── Pyxis (library)
    ├── u-SAINT (academics / grades)
    └── LMS (courses / assignments)
```

- **Multi-provider LLM fallback**: `llm_factory.get_llm_sequence()` falls back in the order Groq (llama-3.3-70b, free 14,400 req/day) → Gemini → OpenRouter, removing the single point of failure. Each provider is added to the sequence only when its API key is set — `GROQ_API_KEY` for Groq, `GOOGLE_API_KEY` for Gemini, `OPENROUTER_API_KEY` for OpenRouter. If no key is set at all, `create_llm()` raises an explicit `RuntimeError` (no silent misbehavior). Groq uses `ChatGroq` instead of the generic `ChatOpenAI` wrapper — the generic wrapper serializes assistant content as a list, which makes Groq return 400 on the second tool call.
- **State persistence**: conversation state is persisted with the LangGraph Postgres checkpointer.
- **Thread ownership binding**: authenticated requests bind each `thread_id` to the SHA-256 hash of the stable principal verified by the ssuAI proxy. The same user retains checkpoints across re-login and devices, while a different principal cannot read or resume them. Existing session-owned threads migrate lazily when the rightful session first supplies a principal.
- **HITL safeguard**: library write actions only ever run through the two-step flow `prepare_*` → user approval → `confirm_action`.

### Key components

| Component | File | Role |
|---|---|---|
| Supervisor | `supervisor/graph.py` | Uses LangChain `create_agent` to classify a query and route it with a `ROUTE_TO:X` marker. A routing tool returns the marker, then `post_supervisor` scans it and emits `Command(goto=X)` (ADR 0001). |
| Domain agents | `agents/{academic,library,lms}.py` | Per-domain MCP tool bundle + manual `bind_tools` fallback loop (removes the single-provider point of failure) |
| MCP client | `mcp_client.py` | Connects to ssuMCP over Streamable HTTP (MCP 2025-03-26), loads tools dynamically |
| LLM factory | `llm_factory.py` | `get_llm_sequence()` — Groq → Gemini → OpenRouter priority fallback |
| Checkpointer | LangGraph Postgres | Persists conversation state across turns |

## Why LangGraph?

| Approach | Reasoning |
|------|------|
| LangChain LCEL | Fine for simple chains. Hard to express state, loops, and branching |
| Raw function calling | Orchestration code managed by hand. Multi-step complexity grows |
| **LangGraph** (chosen) | Expresses state, branching, and loops as an explicit StateGraph. Easy to extend to the Phase 2 multi-agent design |

## Setup

```bash
pip install uv
uv sync --extra dev
```

## Run

At least one LLM provider key is required (any single one is enough; if all three are set, they are used in fallback order):

```bash
export GROQ_API_KEY=<your-groq-key>        # 1st priority (optional)
export GOOGLE_API_KEY=<your-gemini-key>    # 2nd priority (optional)
export OPENROUTER_API_KEY=<your-or-key>    # 3rd priority (optional)
export SSUMCP_URL=https://ssumcp.duckdns.org/mcp  # optional, this is the default
# Run the FastAPI app (SSE streaming endpoint)
uv run uvicorn ssu_agent.main:app --host 0.0.0.0 --port 8000

# Call it locally from another terminal (add -H "X-Agent-Key: <key>" when the gate is enabled)
curl -N -X POST http://localhost:8000/agent/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "오늘 학식 알려줘"}'   # "What's on the cafeteria menu today?"
```

## Security / configuration

Code defaults are intended for local development. The production Helm release sets `AGENT_API_KEY_REQUIRED=true` and uses a non-optional Secret.

| Env var | Default | Role |
|---|---|---|
| `ALLOWED_ORIGINS` | `*` (allow all) | CORS allow-list. Comma-separated list of origins (parsed in `config.py` → `CORSMiddleware` in `main.py`). A single `*` keeps the previous allow-all behavior. Narrowing it to the actual frontend origins enables CORS protection. |
| `AGENT_API_KEY` | empty (local gate off) | `X-Agent-Key` credential for `/agent/*`. It is mandatory in production and must match the ssuAI server proxy. When configured, `secrets.compare_digest` verifies it and a missing or wrong value gets 401. |
| `AGENT_API_KEY_REQUIRED` | `false` | Refuses startup when `true` and `AGENT_API_KEY` is empty. Production sets this to `true`; only local development permits `false`. |
| `AGENT_RATE_LIMIT` | `30/minute` | Per-IP inbound rate limit for `/agent/stream` and `/agent/resume` (slowapi syntax, the `limiter` in `main.py`). Keyed by the leftmost X-Forwarded-For hop (the real client IP behind the ingress). Exceeding it returns 429. Background in ADR 0009. |
| `AGENT_MAX_MESSAGE_CHARS` | `8000` | Maximum character count of a single request `message` (pydantic `Field(max_length=…)`). Exceeding it returns 422 (oversized-payload guard, ADR 0009). |
| LLM keys | — | Of `GROQ_API_KEY`/`GOOGLE_API_KEY`/`OPENROUTER_API_KEY`, only the ones that are set join the fallback sequence (see Architecture above). |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model name used by the Gemini provider (`llm_factory.py`; only used when `GOOGLE_API_KEY` is set). |

### Thread ownership binding

`/agent/stream` and `/agent/resume` check `thread_owners` before running the graph. The `principal` on an authenticated request is not browser-asserted input: the ssuAI server proxy verifies the access JWT and injects its stable subject. ssuAgent stores `sha256(principal)` rather than the raw value with `owner_kind='principal'`. The same principal can use the thread after session rotation or from another device; a different principal, or a request that omits the principal after promotion, receives 403.

An older session-owned row (`owner_kind='session'` or legacy `NULL`) is promoted once, when a request matching its stored `mcp_session_id` first presents a verified principal. A legacy checkpoint with no owner row is claimed by the first verified request. Only requests without Authorization retain the session/anonymous fallback. See `docs/adr/0010-agent-thread-ownership-binding.md` and `docs/adr/0011-thread-stable-principal-binding.md` (Korean) for the full contract.

### `/agent` endpoint authentication

Production `/agent/*` is protected by a mandatory API key gate. With `AGENT_API_KEY_REQUIRED=true`, a deployment without a key refuses to start. ssuAgent then requires `X-Agent-Key` to match `AGENT_API_KEY` (`verify_agent_key` in `main.py`, 401 on mismatch). The server-only ssuAI proxy injects the key and forwards only its verified principal. The browser calls same-origin `/api/agent/*`, so it cannot control either trusted value. See `docs/adr/0009-agent-edge-hardening.md` (Korean) for the design and verification evidence.

## Test

```bash
uv run pytest
```

## Phase Roadmap

| Phase | Scope | Status |
|-------|------|------|
| 1 | Single ReAct agent, 3 public tools (meals / library / notices) | ✅ Done |
| 2 | Per-domain supervisor multi-agent, authenticated library reservation tools (HITL), streaming responses | ✅ Done |
| 3 | ssuAI frontend integration (web UI chat, SSE) | ✅ Done |
| Security hardening | LLM provider key guard, env-based CORS (`ALLOWED_ORIGINS`), `/agent` API key gate (`AGENT_API_KEY`), thread ownership binding | ✅ Done |

> Implementation note: because the legacy `create_react_agent` executor exhibited a looping issue, domain agents retain a manual `bind_tools` fallback loop (removing the single-provider point of failure). The supervisor uses the supported `langchain.agents.create_agent` API. See `docs/adr/` (Korean) for the rationale and alternatives considered.
