"""
FastAPI app — SSE streaming entry point for ssuAgent.

Endpoints:
  POST /agent/stream   — start or continue a conversation, stream SSE
  POST /agent/resume   — resume after HITL interrupt (user approval/denial)
  GET  /health         — liveness check

SSE event types emitted:
  {"type": "text",    "content": "..."}    — final answer text chunk
  {"type": "handoff", "agent": "library"}  — sub-agent routing started
  {"type": "tool",    "name": "..."}       — tool call started (Korean UX label via _TOOL_LABELS)
  {"type": "interrupt","data": {...}}       — HITL payload awaiting user decision
  {"type": "done"}                          — graph reached END

MCP session lifecycle (thread_id ↔ mcp_session_id ↔ principal):
  Every FastAPI request carries a `thread_id` (stable per user/device) used
  as the LangGraph checkpoint key. The `mcp_session_id` (ssuMCP private tool
  auth token) is passed in the request body but bound only to the current async
  execution context. Checkpoint state stores None in the legacy channel, while
  sub-agents can still inject the live value into private MCP tool calls outside
  model-visible prompts, schemas, results, and persisted messages.

  The three concepts are intentionally separate:
  - thread_id: conversation persistence (Postgres checkpoint)
  - mcp_session_id: ssuMCP auth (externally managed by ssuAI login flow,
    ROTATES on every re-login — never a stable per-user key)
  - principal: stable per-user subject supplied only by the authenticated ssuAI
    server proxy after it verifies the frontend access JWT. ssuAgent does not
    derive this itself — see ADR 0011. Requests without Authorization retain
    the session/anonymous compatibility path.

  A thread's ownership is claimed/verified by claim_or_verify_thread_owner:
  - principal present -> bound to the (hashed) principal. Stable across
    mcp_session_id rotation: the same principal from a different session still
    resolves to the same thread. A different principal is rejected (403).
  - principal absent, mcp_session_id present -> bound to that session only
    (legacy behavior, unchanged): a different session is rejected (403).
  - neither present -> anonymous thread (owner NULL), open to any caller, same
    as before ADR 0011.
  A pre-existing session-owned thread is lazily upgraded to principal
  ownership the first time its rightful session presents a principal (see ADR
  0011 "마이그레이션 규칙"). The graph still takes the latest mcp_session_id
  from the request for MCP tool calls after ownership is verified.

Checkpointer (Postgres):
  Uses AsyncPostgresSaver from langgraph-checkpoint-postgres backed by
  an AsyncConnectionPool (psycopg3). autocommit=True is required by LangGraph.
  setup() creates the checkpoint tables on first startup. The same pool also
  creates thread_owners, which binds client-supplied thread_id values to the
  a one-way session digest or, when supplied, a stable principal digest.

Streaming optimisation:
  astream_events(version="v2") yields rich event dicts. We filter:
  - on_chat_model_stream   → candidate answer text; supervisor routing narration
                             and sub-agent pre-tool narration are buffered/dropped
  - on_tool_start          → handoff/tool events (user sees "routing...")
  - on_chain_stream        → HITL payload when a chunk carries __interrupt__
                             (client shows approval dialog). langgraph 1.2.x does
                             NOT emit an on_interrupt event — the interrupt rides
                             inside an on_chain_stream chunk. See _extract_interrupt.
  Other on_chain_* / on_retriever_* chunks are dropped (SSE noise, and raw state
  can carry mcp_session_id — never forwarded).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from langchain_core.messages import AIMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ssu_agent import config
from ssu_agent.agents.auth_guard import contains_internal_auth_guidance
from ssu_agent.mcp_client import create_mcp_client
from ssu_agent.supervisor.graph import build_supervisor_graph
from ssu_agent.supervisor.state import bind_request_mcp_session_id

# uvicorn does not attach a handler to the root logger, so ssu_agent's INFO-level
# latency instrumentation (react_loop per-turn provider + per-tool timing) would
# fall through to logging.lastResort (WARNING) and vanish. Attach our own stream
# handler at INFO so those records reliably reach the container logs.
_ssu_logger = logging.getLogger("ssu_agent")
_ssu_logger.setLevel(logging.INFO)
if not any(isinstance(h, logging.StreamHandler) for h in _ssu_logger.handlers):
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    _ssu_logger.addHandler(_handler)
_ssu_logger.propagate = False

logger = logging.getLogger(__name__)


_SIGNED_CLIENT_ID_RE = re.compile(r"[0-9a-f]{64}")


def _rate_limit_identity(request: Request) -> str:
    """Use only a proxy-signed client ID; never trust caller-supplied forwarding."""
    client_id = request.headers.get("x-agent-client-id", "")
    signature = request.headers.get("x-agent-client-signature", "")
    key = config.AGENT_API_KEY
    if key and _SIGNED_CLIENT_ID_RE.fullmatch(client_id):
        expected = hmac.new(
            key.encode("utf-8"),
            f"v1:{client_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if signature and hmac.compare_digest(signature, expected):
            return f"proxy:{client_id}"
    # Direct/legacy traffic shares the actual socket bucket. In particular, an
    # arbitrary X-Forwarded-For header can no longer mint fresh limiter keys.
    return f"socket:{get_remote_address(request)}"


# Per-IP inbound throttle on /agent/* (mirrors ssuMCP ADR 0061): the endpoints
# fan out to paid LLM providers, so an unauthenticated flood is a cost/DoS risk.
# In-memory storage = per-process (prod runs a single replica; documented caveat).
limiter = Limiter(key_func=_rate_limit_identity)

# Graph and pool references — set during lifespan startup
_graph = None
_pool: AsyncConnectionPool | None = None
_checkpointer: AsyncPostgresSaver | None = None
_DEEP_HEALTH_MCP_TIMEOUT_SECONDS = 2.0

_THREAD_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_OPTIONAL_THREAD_ID_PATTERN = r"^(?:[A-Za-z0-9][A-Za-z0-9._:-]{0,127})?$"
_MCP_SESSION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"

_THREAD_OWNER_FORBIDDEN_DETAIL = "이 대화는 현재 세션의 소유가 아닙니다."
_UNBOUND_STREAM_SESSION = object()
_STREAM_AUTH_FALLBACK = (
    "학교 서비스 연결은 화면 상단의 ‘연결’에서 진행해 주세요. 로그인 정보나 내부 인증 값은 "
    "채팅에 입력하지 않아도 돼요."
)

_TOOL_LABELS: dict[str, str] = {
    "prepare_reserve_library_seat": "좌석 예약 준비 중...",
    "prepare_swap_library_seat": "좌석 이석 준비 중...",
    "prepare_cancel_library_seat": "좌석 반납 준비 중...",
    "confirm_action": "예약 확정 중...",
    "get_library_available_seats": "이용 가능 좌석 조회 중...",
    "get_library_seat_status": "좌석 상태 확인 중...",
    "get_library_seat_catalog": "좌석 목록 조회 중...",
    "recommend_library_seats": "좌석 추천 중...",
    "get_my_library_seat": "내 좌석 확인 중...",
    "get_my_library_loans": "대출 현황 조회 중...",
    "search_library_book": "도서 검색 중...",
    "get_auth_status": "인증 상태 확인 중...",
    "start_auth": "로그인 시작 중...",
    "get_my_grades": "성적 조회 중...",
    "get_my_schedule": "시간표 조회 중...",
    "get_my_chapel_info": "채플 정보 조회 중...",
    "get_my_scholarships": "장학금 조회 중...",
    "simulate_gpa": "GPA 시뮬레이션 중...",
    "check_graduation_requirements": "졸업 요건 확인 중...",
    "get_my_assignments": "과제 목록 조회 중...",
    "get_today_meal": "오늘 식단 조회 중...",
    "get_meal_by_date": "식단 조회 중...",
    "get_meal_weekly": "주간 식단 조회 중...",
    "get_dorm_weekly_meal": "기숙사 주간 식단 조회 중...",
    "search_campus_facilities": "캠퍼스 시설 검색 중...",
    # LMS (강의/과제/자료 내보내기)
    "get_my_lms_courses": "수강 강의 조회 중...",
    "get_my_lms_materials": "강의자료 조회 중...",
    "get_my_lms_terms": "학기 목록 조회 중...",
    "get_lms_dashboard": "LMS 대시보드 조회 중...",
    "export_all_lms_materials": "전체 강의자료 내보내기 준비 중...",
    "prepare_lms_material_export": "강의자료 내보내기 준비 중...",
    "confirm_lms_material_export": "강의자료 내보내기 확정 중...",
    # 학사일정 · 학칙/졸업/장학 근거
    "get_academic_calendar": "학사일정 조회 중...",
    "find_academic_calendar_events": "학사일정 검색 중...",
    "get_academic_policy_brief": "학칙 요약 조회 중...",
    "search_academic_policy_sources": "학칙 근거 검색 중...",
    "check_scholarship_policy": "장학 정책 확인 중...",
    "evaluate_graduation_with_policy": "졸업 요건 평가 중...",
    # 도서관 대기 · 열람실 좌석
    "get_room_available_seats": "열람실 좌석 조회 중...",
    "get_library_wait_status": "예약 상태 확인 중...",
    "wait_for_library_seat": "좌석 대기 등록 중...",
    "cancel_library_wait": "대기 취소 중...",
    # 세션
    "logout_provider": "로그아웃 중...",
    "logout_all": "전체 로그아웃 중...",
}

_AGENT_NODE_NAMES = {"library_agent", "academic_agent", "lms_agent"}


def _handoff_payload(agent: str) -> dict[str, str]:
    return {
        "type": "handoff",
        "agent": agent,
        "status": "routing",
        "message": f"{agent} 에이전트로 전환 중...",
    }


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """FastAPI lifespan: open Postgres connection pool, build graph, keep alive."""
    global _checkpointer, _graph, _pool
    _validate_security_config()
    async with AsyncConnectionPool(
        conninfo=config.DATABASE_URL,
        # Pool ceiling ~= concurrent streams x checkpointer ops. Five fits the
        # current single-pod dozens-of-users shape; raise with replicas/HPA per load test.
        max_size=config.AGENT_PG_POOL_MAX_SIZE,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    ) as pool:
        try:
            checkpointer = AsyncPostgresSaver(pool)
            await checkpointer.setup()
            await _setup_thread_owners(pool)
            none_type, none_blob = checkpointer.serde.dumps_typed(None)
            scrubbed = await _scrub_legacy_checkpoint_capabilities(
                pool,
                none_type=none_type,
                none_blob=none_blob,
            )
            if scrubbed:
                logger.info("legacy checkpoint capability scrubbed %d row(s)", scrubbed)
            _pool = pool
            _checkpointer = checkpointer
            _graph = await build_supervisor_graph(checkpointer=checkpointer)
            cleanup_task = asyncio.create_task(_retention_loop())
            try:
                yield
            finally:
                cleanup_task.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup_task
        finally:
            _graph = None
            _pool = None
            _checkpointer = None


def _validate_security_config() -> None:
    """Fail startup when production requires the trusted proxy key but it is absent."""
    if config.AGENT_API_KEY_REQUIRED and not config.AGENT_API_KEY:
        raise RuntimeError("AGENT_API_KEY is required when AGENT_API_KEY_REQUIRED=true")
    if config.AGENT_MAX_REQUEST_BYTES <= 0:
        raise RuntimeError("AGENT_MAX_REQUEST_BYTES must be positive")
    if config.AGENT_RETENTION_CLEANUP_INTERVAL_SECONDS <= 0:
        raise RuntimeError("AGENT_RETENTION_CLEANUP_INTERVAL_SECONDS must be positive")
    if config.AGENT_RETENTION_CLEANUP_BATCH_SIZE <= 0:
        raise RuntimeError("AGENT_RETENTION_CLEANUP_BATCH_SIZE must be positive")
    if config.AGENT_STORAGE_TIMEOUT_SECONDS <= 0:
        raise RuntimeError("AGENT_STORAGE_TIMEOUT_SECONDS must be positive")
    if config.AGENT_CAPABILITY_SCRUB_BATCH_SIZE <= 0:
        raise RuntimeError("AGENT_CAPABILITY_SCRUB_BATCH_SIZE must be positive")
    if config.AGENT_CAPABILITY_SCRUB_MAX_ROWS <= 0:
        raise RuntimeError("AGENT_CAPABILITY_SCRUB_MAX_ROWS must be positive")


async def _setup_thread_owners(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                # Rolling replicas must not race the trigger replacement below.
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('ssuagent_conversation_schema_v1'))"
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS thread_owners (
                        thread_id TEXT PRIMARY KEY,
                        owner TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                # ADR 0011: owner_kind distinguishes a stable-principal owner from a
                # legacy/session-scoped owner. ADD COLUMN IF NOT EXISTS keeps this
                # additive over the ADR 0010 table already live in prod — existing
                # rows get owner_kind = NULL, which claim_or_verify_thread_owner
                # treats identically to owner_kind = 'session' (see docstring there).
                await cur.execute(
                    "ALTER TABLE thread_owners ADD COLUMN IF NOT EXISTS owner_kind TEXT"
                )
                await cur.execute(
                    "ALTER TABLE thread_owners ADD COLUMN IF NOT EXISTS "
                    "last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                )
                await cur.execute(
                    "CREATE INDEX IF NOT EXISTS thread_owners_last_accessed_idx "
                    "ON thread_owners (last_accessed_at)"
                )
                await cur.execute(
                    """
                    CREATE OR REPLACE FUNCTION ssuagent_checkpoint_lifecycle_fence()
                    RETURNS trigger
                    LANGUAGE plpgsql
                    AS $fence$
                    DECLARE
                        lifecycle_kind TEXT;
                    BEGIN
                        -- Old pods can remain live during a rolling rollout. Make
                        -- the storage boundary itself normalize their legacy
                        -- capability channel so it cannot race the startup scrub.
                        IF TG_TABLE_NAME = 'checkpoints' THEN
                            IF NEW.checkpoint #> '{channel_values,mcp_session_id}' IS NOT NULL
                               AND NEW.checkpoint #> '{channel_values,mcp_session_id}'
                                   <> 'null'::jsonb THEN
                                NEW.checkpoint := jsonb_set(
                                    NEW.checkpoint,
                                    '{channel_values,mcp_session_id}',
                                    'null'::jsonb,
                                    false
                                );
                            END IF;
                        ELSIF NEW.channel = 'mcp_session_id' THEN
                            NEW.type := 'null';
                            NEW.blob := ''::bytea;
                        END IF;

                        SELECT owner_kind INTO lifecycle_kind
                        FROM thread_owners
                        WHERE thread_id = NEW.thread_id
                        FOR SHARE;

                        IF lifecycle_kind = 'deleted'
                           AND current_setting(
                               'ssuagent.capability_scrub',
                               true
                           ) IS DISTINCT FROM 'on' THEN
                            RAISE EXCEPTION 'checkpoint write rejected for deleted thread'
                                USING ERRCODE = '55000';
                        END IF;
                        RETURN NEW;
                    END;
                    $fence$
                    """
                )
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                    trigger = f"ssuagent_{table}_lifecycle_fence"
                    await cur.execute(
                        f"""
                        DO $trigger$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1 FROM pg_trigger
                                WHERE tgname = '{trigger}'
                                  AND tgrelid = '{table}'::regclass
                                  AND NOT tgisinternal
                            ) THEN
                                CREATE TRIGGER {trigger}
                                BEFORE INSERT OR UPDATE ON {table}
                                FOR EACH ROW
                                EXECUTE FUNCTION ssuagent_checkpoint_lifecycle_fence();
                            END IF;
                        END;
                        $trigger$
                        """
                    )


_CHECKPOINT_CAPABILITY_SCRUB_SQL = """
WITH scrub_candidates AS MATERIALIZED (
    SELECT ctid, thread_id
    FROM checkpoints
    WHERE checkpoint #> '{channel_values,mcp_session_id}' IS NOT NULL
      AND checkpoint #> '{channel_values,mcp_session_id}' <> 'null'::jsonb
    ORDER BY thread_id, checkpoint_ns, checkpoint_id
    LIMIT %s
),
locked_owners AS MATERIALIZED (
    SELECT owners.thread_id
    FROM thread_owners AS owners
    WHERE owners.thread_id IN (
        SELECT candidates.thread_id FROM scrub_candidates AS candidates
    )
    ORDER BY owners.thread_id
    FOR SHARE OF owners
),
owner_lock_barrier AS MATERIALIZED (
    SELECT count(*) FROM locked_owners
)
UPDATE checkpoints AS target
SET checkpoint = jsonb_set(
    target.checkpoint,
    '{channel_values,mcp_session_id}',
    'null'::jsonb,
    false
)
FROM scrub_candidates, owner_lock_barrier
WHERE target.ctid = scrub_candidates.ctid
RETURNING 1
"""

_TYPED_CAPABILITY_SCRUB_SQL = """
WITH scrub_candidates AS MATERIALIZED (
    SELECT ctid, thread_id
    FROM {table}
    WHERE channel = 'mcp_session_id'
      AND (type IS DISTINCT FROM %s OR blob IS DISTINCT FROM %s)
    ORDER BY thread_id, checkpoint_ns, channel, ctid
    LIMIT %s
),
locked_owners AS MATERIALIZED (
    SELECT owners.thread_id
    FROM thread_owners AS owners
    WHERE owners.thread_id IN (
        SELECT candidates.thread_id FROM scrub_candidates AS candidates
    )
    ORDER BY owners.thread_id
    FOR SHARE OF owners
),
owner_lock_barrier AS MATERIALIZED (
    SELECT count(*) FROM locked_owners
)
UPDATE {table} AS target
SET type = %s, blob = %s
FROM scrub_candidates, owner_lock_barrier
WHERE target.ctid = scrub_candidates.ctid
RETURNING 1
"""

_LEGACY_CAPABILITY_REMAINS_SQL = """
SELECT
    EXISTS (
        SELECT 1 FROM checkpoints
        WHERE checkpoint #> '{channel_values,mcp_session_id}' IS NOT NULL
          AND checkpoint #> '{channel_values,mcp_session_id}' <> 'null'::jsonb
    )
    OR EXISTS (
        SELECT 1 FROM checkpoint_blobs
        WHERE channel = 'mcp_session_id'
          AND (type IS DISTINCT FROM %s OR blob IS DISTINCT FROM %s)
    )
    OR EXISTS (
        SELECT 1 FROM checkpoint_writes
        WHERE channel = 'mcp_session_id'
          AND (type IS DISTINCT FROM %s OR blob IS DISTINCT FROM %s)
    )
"""


async def _scrub_legacy_checkpoint_capabilities(
    pool: AsyncConnectionPool,
    *,
    none_type: str,
    none_blob: bytes,
) -> int:
    """Replace only legacy MCP capability channels with serialized ``None``.

    LangGraph keeps primitive channel values inline in checkpoint JSONB and
    pending/channel writes in typed blob rows. Transactions are individually
    capped; reruns are idempotent because already-null values no longer match.
    """
    processed = 0
    batch_size = config.AGENT_CAPABILITY_SCRUB_BATCH_SIZE
    max_rows = config.AGENT_CAPABILITY_SCRUB_MAX_ROWS

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT pg_advisory_lock(hashtext('ssuagent_capability_scrub_v1'))")
        try:
            queries = [
                (_CHECKPOINT_CAPABILITY_SCRUB_SQL, lambda limit: (limit,)),
                *[
                    (
                        _TYPED_CAPABILITY_SCRUB_SQL.format(table=table),
                        lambda limit, nt=none_type, nb=none_blob: (nt, nb, limit, nt, nb),
                    )
                    for table in ("checkpoint_blobs", "checkpoint_writes")
                ],
            ]
            for query, params_for_limit in queries:
                while processed < max_rows:
                    limit = min(batch_size, max_rows - processed)
                    async with conn.transaction():
                        async with conn.cursor() as cur:
                            # The lifecycle fence rejects all writes to deleted
                            # threads. This transaction-local flag permits only
                            # this exact maintenance path to sanitize legacy rows
                            # that predate the fence behind an existing tombstone.
                            await cur.execute(
                                "SELECT set_config('ssuagent.capability_scrub', 'on', true)"
                            )
                            await cur.execute(query, params_for_limit(limit))
                            changed = len(await cur.fetchall())
                    processed += changed
                    if changed < limit:
                        break

            async with conn.cursor() as cur:
                await cur.execute(
                    _LEGACY_CAPABILITY_REMAINS_SQL,
                    (none_type, none_blob, none_type, none_blob),
                )
                row = await cur.fetchone()
            if row and row[0]:
                raise RuntimeError(
                    "legacy checkpoint capability scrub exceeded AGENT_CAPABILITY_SCRUB_MAX_ROWS"
                )
            return processed
        finally:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT pg_advisory_unlock(hashtext('ssuagent_capability_scrub_v1'))"
                )


class RequestBodyLimitMiddleware:
    """Reject oversized agent bodies before FastAPI allocates/decodes JSON."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") not in {"POST", "DELETE"}
            or not scope.get("path", "").startswith("/agent/")
        ):
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > config.AGENT_MAX_REQUEST_BYTES:
                    await JSONResponse({"detail": "Request body too large"}, status_code=413)(
                        scope, receive, send
                    )
                    return
            except ValueError:
                await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(
                    scope, receive, send
                )
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > config.AGENT_MAX_REQUEST_BYTES:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await JSONResponse({"detail": "Request body too large"}, status_code=413)(
                scope, receive, send
            )


class _RequestBodyTooLarge(Exception):
    pass


app = FastAPI(title="ssuAgent", version="0.2.0", lifespan=_lifespan)
app.add_middleware(RequestBodyLimitMiddleware)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    # Narrowed from "*": the API only serves POST /agent/* and GET /health.
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── Auth dependency ─────────────────────────────────────────────────────────────


async def verify_agent_key(x_agent_key: str | None = Header(default=None)) -> None:
    """API-key gate for /agent endpoints.

    Local development may leave the key empty only while the required flag is
    disabled. Production validates the key at startup and every request must
    carry a matching X-Agent-Key header.
    config is read live (not bound at import time) so the gate reflects the
    current env / test overrides. compare_digest guards against timing attacks;
    the `not x_agent_key` short-circuit avoids a TypeError when the header is
    missing (compare_digest rejects None).
    """
    expected = config.AGENT_API_KEY
    if not expected:
        return
    if not x_agent_key or not secrets.compare_digest(x_agent_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Agent-Key")


def _hash_principal(principal: str) -> str:
    """One-way digest of a trusted-proxy stable principal before it touches storage.

    ssuMCP's get_auth_status deliberately never returns a raw student id (see ADR
    0011), so ssuAgent never derives `principal` itself — it only ever receives
    the value supplied by the authenticated ssuAI proxy. Hashing it before it reaches
    `thread_owners` means a DB dump never reveals the plaintext subject, while
    equality comparisons (the only operation ownership binding needs) still work
    identically on the digest.
    """
    return hashlib.sha256(principal.encode("utf-8")).hexdigest()


def _hash_mcp_session_id(mcp_session_id: str) -> str:
    """Create a non-reversible owner lookup value from a bearer capability."""
    return hashlib.sha256(f"mcp-session:{mcp_session_id}".encode("utf-8")).hexdigest()


async def claim_or_verify_thread_owner(
    thread_id: str,
    mcp_session_id: str | None,
    principal: str | None = None,
) -> None:
    """Bind a new thread to its owner, or verify the current caller against it.

    ADR 0011. `principal` is a stable per-user subject supplied by the
    authenticated ssuAI proxy; it remains optional only for the explicit
    session/anonymous compatibility path. Resolution order:

    1. `principal` present -> the thread is owned by hash(principal). This
       survives `mcp_session_id` rotation (re-login): the same principal from a
       *different* session still matches. A *different* principal is rejected.
    2. `principal` absent, `mcp_session_id` present -> owned by that session only
       (ADR 0010 behavior, unchanged): a different session is rejected.
    3. Neither present -> anonymous thread (owner NULL), open to any caller,
       unchanged from ADR 0010.

    Lazy migration: a thread claimed under rule 2 (session-owned) is upgraded to
    rule 1 (principal-owned) the moment its rightful session presents a
    `principal` — i.e. on the first verified access from that session after the
    caller starts sending one. See docs/adr/0011 for why lazy beats a batch
    migration here (there is no batch of principals to backfill from — the
    value only exists once a caller starts sending it).
    """
    if _pool is None:
        raise HTTPException(status_code=503, detail="Agent storage is not ready")

    hashed_principal = _hash_principal(principal) if principal else None

    hashed_session = _hash_mcp_session_id(mcp_session_id) if mcp_session_id else None

    if hashed_principal is not None:
        claim_owner, claim_kind = hashed_principal, "principal"
    elif hashed_session is not None:
        claim_owner, claim_kind = hashed_session, "session_hash"
    else:
        claim_owner, claim_kind = None, None

    async with _pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO thread_owners (thread_id, owner, owner_kind)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (thread_id) DO NOTHING
                    """,
                    (thread_id, claim_owner, claim_kind),
                )
                await cur.execute(
                    "SELECT owner, owner_kind FROM thread_owners WHERE thread_id = %s FOR UPDATE",
                    (thread_id,),
                )
                row = await cur.fetchone()

                if row is None:
                    raise HTTPException(status_code=503, detail="Agent storage is not ready")

                stored_owner, stored_kind = row

                if stored_kind == "deleted":
                    raise HTTPException(status_code=410, detail="Conversation was deleted")

                if stored_owner is None:
                    await cur.execute(
                        "UPDATE thread_owners SET last_accessed_at = now() WHERE thread_id = %s",
                        (thread_id,),
                    )
                    return  # Anonymous thread — open to any caller (ADR 0010).

                if stored_kind == "principal":
                    if hashed_principal is None or hashed_principal != stored_owner:
                        raise HTTPException(status_code=403, detail=_THREAD_OWNER_FORBIDDEN_DETAIL)
                elif stored_kind == "session_hash":
                    if hashed_session is None or hashed_session != stored_owner:
                        raise HTTPException(status_code=403, detail=_THREAD_OWNER_FORBIDDEN_DETAIL)
                    if hashed_principal is not None:
                        await cur.execute(
                            "UPDATE thread_owners SET owner = %s, owner_kind = 'principal' "
                            "WHERE thread_id = %s",
                            (hashed_principal, thread_id),
                        )
                else:
                    # Rows created before this hardening stored the raw session
                    # under owner_kind='session' (or NULL). Verify once, then
                    # migrate immediately to a digest or stable principal.
                    if stored_owner != mcp_session_id:
                        raise HTTPException(status_code=403, detail=_THREAD_OWNER_FORBIDDEN_DETAIL)
                    migrated_owner = hashed_principal or hashed_session
                    migrated_kind = "principal" if hashed_principal else "session_hash"
                    await cur.execute(
                        "UPDATE thread_owners SET owner = %s, owner_kind = %s WHERE thread_id = %s",
                        (migrated_owner, migrated_kind, thread_id),
                    )

                await cur.execute(
                    "UPDATE thread_owners SET last_accessed_at = now() WHERE thread_id = %s",
                    (thread_id,),
                )


def _owner_matches(
    stored_owner: str | None,
    stored_kind: str | None,
    mcp_session_id: str | None,
    principal: str | None,
) -> bool:
    if stored_owner is None:
        return True
    if stored_kind == "principal":
        return principal is not None and hmac.compare_digest(
            stored_owner,
            _hash_principal(principal),
        )
    if stored_kind == "session_hash":
        return mcp_session_id is not None and hmac.compare_digest(
            stored_owner, _hash_mcp_session_id(mcp_session_id)
        )
    return mcp_session_id is not None and hmac.compare_digest(stored_owner, mcp_session_id)


async def delete_owned_thread(
    thread_id: str,
    mcp_session_id: str | None,
    principal: str | None,
) -> bool:
    """Atomically delete an owned thread and all LangGraph checkpoint rows."""
    if _pool is None:
        raise HTTPException(status_code=503, detail="Agent storage is not ready")
    async with _pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT owner, owner_kind FROM thread_owners WHERE thread_id = %s FOR UPDATE",
                    (thread_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    return False
                if row[1] == "deleted":
                    return False
                if not _owner_matches(row[0], row[1], mcp_session_id, principal):
                    raise HTTPException(status_code=403, detail=_THREAD_OWNER_FORBIDDEN_DETAIL)
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                    await cur.execute(f"DELETE FROM {table} WHERE thread_id = %s", (thread_id,))
                # Keep a non-identifying tombstone until normal retention. The
                # lifecycle trigger holds a shared owner-row lock for saver writes;
                # after this transaction commits it rejects every late write.
                await cur.execute(
                    "UPDATE thread_owners SET owner = NULL, owner_kind = 'deleted', "
                    "last_accessed_at = now() WHERE thread_id = %s",
                    (thread_id,),
                )
                return True


async def cleanup_expired_threads() -> int:
    """Delete one retention batch in a single rollback-safe transaction."""
    if _pool is None or config.AGENT_CONVERSATION_RETENTION_DAYS <= 0:
        return 0
    async with _pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    WITH candidates AS MATERIALIZED (
                        SELECT thread_id
                        FROM thread_owners
                        WHERE last_accessed_at < now() - (%s * interval '1 day')
                        ORDER BY last_accessed_at, thread_id
                        LIMIT %s
                    )
                    SELECT owners.thread_id
                    FROM thread_owners AS owners
                    JOIN candidates USING (thread_id)
                    WHERE owners.last_accessed_at < now() - (%s * interval '1 day')
                    ORDER BY owners.thread_id
                    FOR UPDATE OF owners SKIP LOCKED
                    """,
                    (
                        config.AGENT_CONVERSATION_RETENTION_DAYS,
                        config.AGENT_RETENTION_CLEANUP_BATCH_SIZE,
                        config.AGENT_CONVERSATION_RETENTION_DAYS,
                    ),
                )
                thread_ids = [row[0] for row in await cur.fetchall()]
                if not thread_ids:
                    return 0
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                    await cur.execute(
                        f"DELETE FROM {table} WHERE thread_id = ANY(%s)",
                        (thread_ids,),
                    )
                await cur.execute(
                    "DELETE FROM thread_owners WHERE thread_id = ANY(%s)",
                    (thread_ids,),
                )
                return len(thread_ids)


async def _retention_loop() -> None:
    while True:
        await asyncio.sleep(config.AGENT_RETENTION_CLEANUP_INTERVAL_SECONDS)
        try:
            deleted = await asyncio.wait_for(
                cleanup_expired_threads(),
                timeout=config.AGENT_STORAGE_TIMEOUT_SECONDS,
            )
            if deleted:
                logger.info("conversation retention deleted %d expired thread(s)", deleted)
        except Exception as exc:
            logger.warning("conversation retention cleanup failed: type=%s", type(exc).__name__)


# ── Request / response models ─────────────────────────────────────────────────


class AgentRequest(BaseModel):
    # Oversized-payload guard: cap the free-text message (config-tunable).
    message: str = Field(max_length=config.AGENT_MAX_MESSAGE_CHARS)
    thread_id: str = Field(default="", max_length=128, pattern=_OPTIONAL_THREAD_ID_PATTERN)
    mcp_session_id: str | None = Field(
        default=None,
        max_length=128,
        pattern=_MCP_SESSION_ID_PATTERN,
    )
    # Client-asserted library auth hint. Used only for pre-LLM UX gating; ssuMCP
    # AUTH_REQUIRED remains the real auth boundary.
    library_connected: bool = False
    # ADR 0011: optional stable per-user subject (e.g. a frontend JWT subject),
    # independent of the rotating mcp_session_id. The ssuAI proxy supplies its
    # verified subject; direct and legacy callers may omit it, so it MUST default
    # to None and every code path MUST keep working when it is never sent.
    principal: str | None = Field(default=None, min_length=1, max_length=128)


class ResumeRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=128, pattern=_THREAD_ID_PATTERN)
    approved: bool
    action_id: int | None = None
    mcp_session_id: str | None = Field(
        default=None,
        max_length=128,
        pattern=_MCP_SESSION_ID_PATTERN,
    )
    library_connected: bool = False
    principal: str | None = Field(default=None, min_length=1, max_length=128)


class ThreadAccessRequest(BaseModel):
    mcp_session_id: str | None = Field(
        default=None,
        max_length=128,
        pattern=_MCP_SESSION_ID_PATTERN,
    )
    principal: str | None = Field(default=None, min_length=1, max_length=128)


def build_resume_command(req: ResumeRequest) -> Command:
    """Build the atomic LangGraph resume command used by /agent/resume."""
    resume_payload = {
        "approved": req.approved,
        "action_id": req.action_id,
        "library_connected": req.library_connected,
    }
    return Command(
        resume=resume_payload,
        update={
            # Explicitly scrub the legacy checkpoint channel. The live bearer
            # capability is bound only for this request by _stream_graph.
            "mcp_session_id": None,
            "library_connected": req.library_connected,
        },
    )


# ── SSE helpers ───────────────────────────────────────────────────────────────


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _extract_interrupt(chunk: object) -> dict | None:
    """Return the HITL payload if this astream_events chunk carries an interrupt.

    langgraph 1.2.4's astream_events(version="v2") does NOT emit a dedicated
    on_interrupt event. When a node calls interrupt(), the graph pauses and the
    interrupt surfaces inside an on_chain_stream chunk shaped like
    {"__interrupt__": (Interrupt(value=<payload>, ...),)}. We forward only the
    first Interrupt's .value (the developer-controlled approval payload), never
    the surrounding chunk, so raw graph state is not leaked.
    """
    if isinstance(chunk, dict):
        interrupts = chunk.get("__interrupt__")
        if interrupts:
            first = interrupts[0]
            return getattr(first, "value", first)
    return None


_CAPACITY_SIGNALS = (
    "exhausted",
    "rate limit",
    "rate-limit",
    "ratelimit",
    "429",
    "quota",
    "resource_exhausted",
    "too many requests",
)


def _is_capacity_error(exc: BaseException) -> bool:
    """True if the failure is an upstream LLM capacity / rate-limit / quota exhaustion (429).

    Walks the exception chain so a RateLimitError wrapped in a
    RuntimeError("All LLM providers exhausted") is still detected. Only class
    names and message text are inspected — nothing is surfaced to the client.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__.lower()
        if "ratelimit" in name or getattr(current, "status_code", None) == 429:
            return True
        if any(sig in str(current).lower() for sig in _CAPACITY_SIGNALS):
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_resume_stream_input(input_data: dict | object) -> bool:
    return isinstance(input_data, Command) and getattr(input_data, "resume", None) is not None


_FN_OPEN = "<function"
_FN_CLOSE = "</function>"


class _FunctionTagStripper:
    """Drop leaked ``<function=name>{...}</function>`` tool-call text from the stream.

    Some free/weaker LLM providers emit a tool call as *plain text* in this format
    instead of a structured tool call, so it streams straight to the user as visible
    content. This stateful, streaming-safe filter removes those blocks (and any
    unterminated ``<function`` tail at end-of-stream) while passing normal text
    through, holding back just enough to catch a delimiter split across token chunks.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._dropping = False

    def feed(self, text: str) -> str:
        self._buf += text
        out: list[str] = []
        while self._buf:
            if self._dropping:
                idx = self._buf.find(_FN_CLOSE)
                if idx == -1:
                    # Still inside a tag; keep only a possible partial close delimiter.
                    if len(self._buf) >= len(_FN_CLOSE):
                        self._buf = self._buf[-(len(_FN_CLOSE) - 1) :]
                    break
                self._buf = self._buf[idx + len(_FN_CLOSE) :]
                self._dropping = False
                continue
            idx = self._buf.find(_FN_OPEN)
            if idx == -1:
                # Emit everything except a tail that could start a split "<function".
                cut = max(0, len(self._buf) - (len(_FN_OPEN) - 1))
                out.append(self._buf[:cut])
                self._buf = self._buf[cut:]
                if self._buf and not any(
                    _FN_OPEN.startswith(self._buf[i:]) for i in range(len(self._buf))
                ):
                    out.append(self._buf)
                    self._buf = ""
                break
            out.append(self._buf[:idx])
            self._buf = self._buf[idx:]
            self._dropping = True
        return "".join(out)

    def flush(self) -> str:
        # End of stream: a held partial that never became a tag is emitted as-is;
        # text still inside an unterminated tag is dropped.
        if self._dropping:
            self._buf = ""
            return ""
        out, self._buf = self._buf, ""
        return out


async def _request_scoped_graph_events(
    input_data: dict | object,
    config: dict,
    mcp_session_id: str | None | object,
):
    if mcp_session_id is _UNBOUND_STREAM_SESSION:
        async for event in _graph.astream_events(input_data, config=config, version="v2"):
            yield event
        return
    with bind_request_mcp_session_id(mcp_session_id):
        async for event in _graph.astream_events(input_data, config=config, version="v2"):
            yield event


async def _stream_graph(
    input_data: dict | object,
    config: dict,
    mcp_session_id: str | None | object = _UNBOUND_STREAM_SESSION,
):
    """Yield SSE strings from graph.astream_events."""
    stripper = _FunctionTagStripper()
    streamed_message_ids: set[str] = set()
    # Supervisor text is held, not streamed live: when it routes to a sub-agent it
    # tends to also emit a filler narration ("...에이전트에게 전달했습니다") that must
    # NOT reach the user — the sub-agent's answer is the real response. Dropped on a
    # transfer_to_*; flushed only if the supervisor answered directly (no routing).
    supervisor_buf = ""
    # Sub-agent models can emit a user-facing preamble in the same message as a
    # tool call ("5층 현황을 확인해드리겠습니다."). Buffer their text until the
    # next event proves whether a tool follows. A tool start drops that preamble;
    # only the final no-tool answer is flushed at the end of the turn.
    subagent_buf = ""
    supervisor_routed = False
    handoff_emitted = False
    suppress_chain_start_handoff = _is_resume_stream_input(input_data)
    try:
        async for event in _request_scoped_graph_events(input_data, config, mcp_session_id):
            etype = event.get("event", "")
            name = event.get("name", "")

            if etype == "on_chat_model_start":
                if "supervisor_llm" in (event.get("tags") or []):
                    supervisor_buf = ""
                else:
                    subagent_buf = ""

            elif etype == "on_chat_model_stream":
                tags = event.get("tags") or []
                chunk = event["data"]["chunk"]
                content = chunk.content if hasattr(chunk, "content") else str(chunk)
                if isinstance(content, list):
                    parts: list[str] = []
                    for item in content:
                        if isinstance(item, str):
                            parts.append(item)
                        elif (
                            isinstance(item, dict)
                            and item.get("type") == "text"
                            and isinstance(item.get("text"), str)
                        ):
                            parts.append(item["text"])
                    content = "".join(parts)
                if content:
                    chunk_id = getattr(chunk, "id", None)
                    if chunk_id:
                        streamed_message_ids.add(chunk_id)
                    if "supervisor_llm" in tags:
                        if not supervisor_routed:
                            supervisor_buf += content  # hold; may be routing narration
                    else:
                        subagent_buf += content  # hold; may precede a tool call

            elif etype == "on_chat_model_error":
                if "supervisor_llm" in (event.get("tags") or []):
                    supervisor_buf = ""
                else:
                    subagent_buf = ""

            elif etype == "on_chain_start":
                if (
                    name in _AGENT_NODE_NAMES
                    and not handoff_emitted
                    and not suppress_chain_start_handoff
                ):
                    supervisor_routed = True
                    supervisor_buf = ""
                    handoff_emitted = True
                    yield _sse(_handoff_payload(name.replace("_agent", "")))

            elif etype == "on_tool_start":
                if name.startswith("transfer_to_"):
                    supervisor_routed = True
                    supervisor_buf = ""  # drop the supervisor's hand-off narration
                    agent = name.replace("transfer_to_", "").replace("_agent", "")
                    if not handoff_emitted:
                        handoff_emitted = True
                        yield _sse(_handoff_payload(agent))
                else:
                    subagent_buf = ""  # drop the sub-agent's pre-tool narration
                    label = _TOOL_LABELS.get(name, name)
                    yield _sse({"type": "tool", "name": name, "label": label})

            elif etype == "on_chain_stream":
                # langgraph surfaces an interrupt() pause inside a chain-stream
                # chunk (not via a dedicated event). Forward the HITL payload and
                # stop; the client shows the approval card and calls /agent/resume.
                chunk = event.get("data", {}).get("chunk")
                interrupt_data = _extract_interrupt(chunk)
                if interrupt_data is not None:
                    supervisor_buf = ""  # a HITL pause supersedes any pending narration
                    subagent_buf = ""
                    tail = stripper.flush()
                    if tail:
                        yield _sse({"type": "text", "content": tail})
                    yield _sse({"type": "interrupt", "data": interrupt_data})
                    return  # Pause SSE; client waits for /agent/resume
                # Code-generated node replies (pre-auth gates, deterministic
                # fallbacks) do not emit on_chat_model_stream chunks. Stream the
                # library agent node's new AIMessage content, but skip supervisor
                # chain chunks and messages already streamed token-by-token.
                if name in {"agent", "check_approval"} and "supervisor_llm" not in (
                    event.get("tags") or []
                ):
                    messages = chunk.get("messages") if isinstance(chunk, dict) else None
                    if isinstance(messages, list):
                        for msg in messages:
                            if not isinstance(msg, AIMessage):
                                continue
                            msg_id = getattr(msg, "id", None)
                            if msg_id and msg_id in streamed_message_ids:
                                continue
                            content = msg.content if isinstance(msg.content, str) else ""
                            if not content or msg.tool_calls:
                                continue
                            if msg_id:
                                streamed_message_ids.add(msg_id)
                            # A graph-generated reply supersedes any buffered raw
                            # model text (for example, an auth-URL hallucination
                            # replaced by a deterministic connection notice).
                            subagent_buf = ""
                            if contains_internal_auth_guidance(content):
                                content = _STREAM_AUTH_FALLBACK
                            cleaned = stripper.feed(content)
                            if cleaned:
                                yield _sse({"type": "text", "content": cleaned})

    except Exception as exc:
        # Do not reflect or log exception text: upstream adapter errors can
        # contain authentication arguments. The exception type is sufficient
        # for aggregate diagnosis and fixed client messaging.
        logger.warning("agent stream failed: type=%s", type(exc).__name__)
        # LLM providers (free-tier or the optional paid Anthropic key, ADR 0015) are
        # frequently rate-limited/quota-exhausted (429). Surface a clear, honest message
        # for that case instead of a generic "error" so users (and portfolio viewers)
        # understand it is a capacity limit, not a crash. Message deliberately does not
        # say "무료" (free) — it must stay accurate whichever provider tier is active.
        # Message is still a fixed string — no exception detail is leaked.
        if _is_capacity_error(exc):
            message = "지금 AI 요청이 많아 잠시 처리가 어려워요. 잠시 후 다시 시도해 주세요."
        else:
            message = "처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
        yield _sse({"type": "error", "message": message})
        return

    # Supervisor answered directly (no routing): flush the held text as the real answer.
    if supervisor_buf and not supervisor_routed:
        if contains_internal_auth_guidance(supervisor_buf):
            supervisor_buf = _STREAM_AUTH_FALLBACK
        cleaned = stripper.feed(supervisor_buf)
        if cleaned:
            yield _sse({"type": "text", "content": cleaned})
    if subagent_buf:
        if contains_internal_auth_guidance(subagent_buf):
            subagent_buf = _STREAM_AUTH_FALLBACK
        cleaned = stripper.feed(subagent_buf)
        if cleaned:
            yield _sse({"type": "text", "content": cleaned})
    tail = stripper.flush()
    if tail:
        yield _sse({"type": "text", "content": tail})
    yield _sse({"type": "done"})


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.post("/agent/stream", dependencies=[Depends(verify_agent_key)])
@limiter.limit(lambda: config.AGENT_RATE_LIMIT)
async def stream_agent(request: Request, req: AgentRequest):
    """Start or continue a conversation. Streams SSE events."""
    thread_id = req.thread_id or str(uuid.uuid4())
    await claim_or_verify_thread_owner(thread_id, req.mcp_session_id, req.principal)
    initial_state = {
        "messages": [{"role": "user", "content": req.message}],
        # Scrub the legacy checkpoint channel. The live value is bound only to
        # this request's async execution below.
        "mcp_session_id": None,
        "library_connected": req.library_connected,
        "active_agent": None,
    }
    config = {"configurable": {"thread_id": thread_id}}

    return StreamingResponse(
        _stream_graph(initial_state, config, req.mcp_session_id),
        media_type="text/event-stream",
        headers={
            "X-Thread-Id": thread_id,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.post("/agent/resume", dependencies=[Depends(verify_agent_key)])
@limiter.limit(lambda: config.AGENT_RATE_LIMIT)
async def resume_agent(request: Request, req: ResumeRequest):
    """Resume a graph paused by a library HITL interrupt.

    The client sends {approved: bool, action_id: int} after the user decides.
    LangGraph re-enters the library check_approval_node, where interrupt()
    returns the resume payload; approval calls confirm_action and denial emits
    the cancellation message.
    """
    await claim_or_verify_thread_owner(req.thread_id, req.mcp_session_id, req.principal)
    config = {"configurable": {"thread_id": req.thread_id}}

    return StreamingResponse(
        _stream_graph(build_resume_command(req), config, req.mcp_session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.get("/health")
async def health():
    return {"status": "UP", "version": app.version}


@app.get("/ready")
async def readiness():
    if _pool is None or _checkpointer is None or _graph is None:
        return JSONResponse(
            status_code=503,
            content={"status": "DOWN", "postgres": "DOWN", "checkpointer": "DOWN"},
        )

    async def check_storage() -> None:
        async with _pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                await cur.fetchone()
        await _checkpointer.aget_tuple({"configurable": {"thread_id": "__readiness_probe__"}})

    try:
        await asyncio.wait_for(check_storage(), timeout=config.AGENT_STORAGE_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("readiness storage check failed: type=%s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={"status": "DOWN", "postgres": "DOWN", "checkpointer": "DOWN"},
        )
    return {"status": "UP", "postgres": "UP", "checkpointer": "UP"}


@app.delete("/agent/threads/{thread_id}", dependencies=[Depends(verify_agent_key)])
@limiter.limit(lambda: config.AGENT_RATE_LIMIT)
async def delete_thread(
    request: Request,
    access: ThreadAccessRequest,
    thread_id: Annotated[
        str,
        Path(min_length=1, max_length=128, pattern=_THREAD_ID_PATTERN),
    ],
):
    try:
        await asyncio.wait_for(
            delete_owned_thread(thread_id, access.mcp_session_id, access.principal),
            timeout=config.AGENT_STORAGE_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="Agent storage operation timed out") from exc
    return Response(status_code=204)


@app.get("/healthz/deep")
async def deep_health():
    try:
        client = create_mcp_client(timeout_seconds=_DEEP_HEALTH_MCP_TIMEOUT_SECONDS)
        await asyncio.wait_for(
            client.get_tools(),
            timeout=_DEEP_HEALTH_MCP_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("deep health MCP check failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "DEGRADED", "mcp": "DOWN"},
        )
    return {"status": "UP", "mcp": "UP"}
