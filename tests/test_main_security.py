"""
Tests for the FastAPI hardening: opt-in /agent API-key gate and open /health.

The graph/DB are never touched: _stream_graph is monkeypatched to a dummy async
generator, and TestClient is instantiated WITHOUT a context manager so the
lifespan (which opens a real Postgres pool) does not run.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from ssu_agent import config, main

# ADR 0011: thread_owners rows are (owner, owner_kind) pairs — owner_kind is
# "principal" (stable subject), "session_hash" (hashed mcp_session_id),
# "session" (raw ADR 0010 legacy
# behavior), or None alongside owner=None for an anonymous thread.
OwnerRow = tuple[str | None, str | None]


async def _fake_stream_graph(  # noqa: A002 - mirrors prod signature
    input_data,
    config,
    mcp_session_id=None,
):
    """Stand-in for _stream_graph: one dummy SSE line, no LLM/DB."""
    yield 'data: {"type": "done"}\n\n'


class _FakeOwnerCursor:
    def __init__(self, owners: dict[str, OwnerRow]):
        self.owners = owners
        self._row: OwnerRow | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, query: str, params: tuple | None = None):
        normalized = " ".join(query.split()).upper()
        if normalized.startswith("INSERT INTO THREAD_OWNERS"):
            thread_id, owner, owner_kind = params
            self.owners.setdefault(thread_id, (owner, owner_kind))
            self._row = None
            return
        if normalized.startswith("SELECT OWNER, OWNER_KIND FROM THREAD_OWNERS"):
            (thread_id,) = params
            self._row = self.owners.get(thread_id)
            return
        if normalized.startswith("UPDATE THREAD_OWNERS SET LAST_ACCESSED_AT"):
            self._row = None
            return
        if "OWNER_KIND = 'DELETED'" in normalized:
            (thread_id,) = params
            self.owners[thread_id] = (None, "deleted")
            self._row = None
            return
        if "SET OWNER = %S, OWNER_KIND = %S" in normalized:
            owner, owner_kind, thread_id = params
            self.owners[thread_id] = (owner, owner_kind)
            self._row = None
            return
        if "SET OWNER = %S, OWNER_KIND = 'PRINCIPAL'" in normalized:
            owner, thread_id = params
            self.owners[thread_id] = (owner, "principal")
            self._row = None
            return
        if normalized.startswith("CREATE TABLE IF NOT EXISTS THREAD_OWNERS"):
            self._row = None
            return
        if normalized.startswith("ALTER TABLE THREAD_OWNERS"):
            self._row = None
            return
        if normalized.startswith("DELETE FROM THREAD_OWNERS"):
            (thread_id,) = params
            self.owners.pop(thread_id, None)
            self._row = None
            return
        if normalized.startswith(
            (
                "DELETE FROM CHECKPOINTS",
                "DELETE FROM CHECKPOINT_BLOBS",
                "DELETE FROM CHECKPOINT_WRITES",
            )
        ):
            self._row = None
            return
        raise AssertionError(f"unexpected query: {query}")

    async def fetchone(self):
        return self._row


class _FakeOwnerConnection:
    def __init__(self, owners: dict[str, OwnerRow]):
        self.owners = owners

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def cursor(self):
        return _FakeOwnerCursor(self.owners)

    def transaction(self):
        return self


class _FakeOwnerPool:
    def __init__(self):
        self.owners: dict[str, OwnerRow] = {}

    def connection(self):
        return _FakeOwnerConnection(self.owners)


@pytest.fixture
def owner_pool(monkeypatch: pytest.MonkeyPatch) -> _FakeOwnerPool:
    pool = _FakeOwnerPool()
    monkeypatch.setattr(main, "_pool", pool)
    return pool


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, owner_pool: _FakeOwnerPool) -> TestClient:
    # Endpoints resolve _stream_graph as a module global at call time, so this
    # monkeypatch takes effect without rebuilding the app.
    monkeypatch.setattr(main, "_stream_graph", _fake_stream_graph)
    # Disable per-IP rate limiting by default so functional tests are not
    # throttled; the dedicated rate-limit test re-enables it.
    monkeypatch.setattr(main.limiter, "enabled", False)
    # Bare TestClient: no `with`, so lifespan/Postgres pool is never opened.
    return TestClient(main.app)


def _post_stream(client: TestClient, headers: dict | None = None):
    return client.post(
        "/agent/stream",
        json={"message": "hi", "thread_id": "t1"},
        headers=headers or {},
    )


class _FakeResumeGraph:
    async def aupdate_state(self, config: dict, values: dict):
        raise AssertionError("resume must not call aupdate_state before Command(resume=...)")


# ── Local no-key mode and production fail-closed startup ───────────────────────


def test_stream_open_when_no_api_key(monkeypatch: pytest.MonkeyPatch, client: TestClient):
    monkeypatch.setattr(config, "AGENT_API_KEY", "")
    monkeypatch.setattr(config, "AGENT_API_KEY_REQUIRED", False)
    resp = _post_stream(client)
    assert resp.status_code == 200
    assert "done" in resp.text


def test_security_config_rejects_missing_required_api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "AGENT_API_KEY", "")
    monkeypatch.setattr(config, "AGENT_API_KEY_REQUIRED", True)

    with pytest.raises(RuntimeError, match="AGENT_API_KEY is required"):
        main._validate_security_config()


def test_security_config_accepts_configured_required_api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "AGENT_API_KEY", "configured-secret")
    monkeypatch.setattr(config, "AGENT_API_KEY_REQUIRED", True)

    main._validate_security_config()


def test_security_config_rejects_non_positive_cleanup_interval(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(config, "AGENT_RETENTION_CLEANUP_INTERVAL_SECONDS", 0)

    with pytest.raises(RuntimeError, match="CLEANUP_INTERVAL_SECONDS must be positive"):
        main._validate_security_config()


def test_health_open(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "UP"


def test_deep_health_reports_mcp_up(monkeypatch: pytest.MonkeyPatch, client: TestClient):
    class FakeMCPClient:
        def __init__(self):
            self.calls = 0

        async def get_tools(self):
            self.calls += 1
            return []

    fake_mcp_client = FakeMCPClient()
    captured: dict[str, float | None] = {}

    def fake_create_mcp_client(*, timeout_seconds: float | None = None):
        captured["timeout_seconds"] = timeout_seconds
        return fake_mcp_client

    monkeypatch.setattr(main, "create_mcp_client", fake_create_mcp_client)

    resp = client.get("/healthz/deep")

    assert resp.status_code == 200
    assert resp.json() == {"status": "UP", "mcp": "UP"}
    assert fake_mcp_client.calls == 1
    assert captured["timeout_seconds"] == main._DEEP_HEALTH_MCP_TIMEOUT_SECONDS


def test_deep_health_reports_mcp_down(monkeypatch: pytest.MonkeyPatch, client: TestClient):
    class FailingMCPClient:
        async def get_tools(self):
            raise RuntimeError("ssuMCP unavailable")

    monkeypatch.setattr(
        main,
        "create_mcp_client",
        lambda *, timeout_seconds=None: FailingMCPClient(),
    )

    resp = client.get("/healthz/deep")

    assert resp.status_code == 503
    assert resp.json() == {"status": "DEGRADED", "mcp": "DOWN"}


def test_readiness_checks_pool_and_checkpointer_with_bounded_probe(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
):
    class ReadinessCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, query):
            assert query == "SELECT 1"

        async def fetchone(self):
            return (1,)

    class ReadinessConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def cursor(self):
            return ReadinessCursor()

    class ReadinessPool:
        def connection(self):
            return ReadinessConnection()

    class ReadinessCheckpointer:
        async def aget_tuple(self, config):
            assert config == {"configurable": {"thread_id": "__readiness_probe__"}}
            return None

    monkeypatch.setattr(main, "_pool", ReadinessPool())
    monkeypatch.setattr(main, "_checkpointer", ReadinessCheckpointer())
    monkeypatch.setattr(main, "_graph", object())

    resp = client.get("/ready")

    assert resp.status_code == 200
    assert resp.json() == {"status": "UP", "postgres": "UP", "checkpointer": "UP"}


def test_agent_request_models_default_and_accept_library_connected():
    assert main.AgentRequest(message="hi").library_connected is False
    assert main.AgentRequest(message="hi", library_connected=True).library_connected is True

    assert main.ResumeRequest(thread_id="t1", approved=True).library_connected is False
    assert (
        main.ResumeRequest(thread_id="t1", approved=True, library_connected=True).library_connected
        is True
    )


def test_stream_initial_state_includes_library_connected(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
):
    captured: dict[str, object] = {}

    async def capture_stream_graph(  # noqa: A002 - mirrors prod signature
        input_data,
        config,
        mcp_session_id=None,
    ):
        captured["input_data"] = input_data
        captured["mcp_session_id"] = mcp_session_id
        yield 'data: {"type": "done"}\n\n'

    monkeypatch.setattr(main, "_stream_graph", capture_stream_graph)

    resp = client.post(
        "/agent/stream",
        json={
            "message": "도서관 좌석 알려줘",
            "thread_id": "library-connected-stream",
            "library_connected": True,
        },
    )

    assert resp.status_code == 200
    assert captured["input_data"]["library_connected"] is True
    assert captured["input_data"]["mcp_session_id"] is None


def test_resume_builds_atomic_command_without_pre_update_state(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
):
    from langgraph.types import Command

    fake_graph = _FakeResumeGraph()
    captured: dict[str, object] = {}

    async def capture_stream_graph(  # noqa: A002 - mirrors prod signature
        input_data,
        config,
        mcp_session_id=None,
    ):
        captured["input_data"] = input_data
        captured["config"] = config
        captured["mcp_session_id"] = mcp_session_id
        yield 'data: {"type": "done"}\n\n'

    monkeypatch.setattr(main, "_graph", fake_graph)
    monkeypatch.setattr(main, "_stream_graph", capture_stream_graph)

    resp = client.post(
        "/agent/resume",
        json={
            "thread_id": "library-resume-fresh-api",
            "approved": True,
            "action_id": 100,
            "mcp_session_id": "fresh-session",
            "library_connected": True,
        },
    )

    assert resp.status_code == 200
    assert captured["config"] == {"configurable": {"thread_id": "library-resume-fresh-api"}}
    assert isinstance(captured["input_data"], Command)
    assert captured["input_data"].resume == {
        "approved": True,
        "action_id": 100,
        "library_connected": True,
    }
    assert captured["input_data"].update == {
        "mcp_session_id": None,
        "library_connected": True,
    }
    assert captured["mcp_session_id"] == "fresh-session"
    assert captured["input_data"].resume["library_connected"] is True


# ── Thread ownership binding ──────────────────────────────────────────────────


def test_stream_binds_new_thread_and_allows_same_owner(
    client: TestClient,
    owner_pool: _FakeOwnerPool,
):
    resp = client.post(
        "/agent/stream",
        json={"message": "hi", "thread_id": "owned-t1", "mcp_session_id": "mcp-a"},
    )
    assert resp.status_code == 200
    assert owner_pool.owners["owned-t1"] == (
        main._hash_mcp_session_id("mcp-a"),
        "session_hash",
    )

    resp = client.post(
        "/agent/stream",
        json={"message": "again", "thread_id": "owned-t1", "mcp_session_id": "mcp-a"},
    )
    assert resp.status_code == 200


def test_stream_rejects_different_owner(client: TestClient):
    resp = client.post(
        "/agent/stream",
        json={"message": "hi", "thread_id": "owned-t2", "mcp_session_id": "mcp-a"},
    )
    assert resp.status_code == 200

    resp = client.post(
        "/agent/stream",
        json={"message": "steal", "thread_id": "owned-t2", "mcp_session_id": "mcp-b"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "이 대화는 현재 세션의 소유가 아닙니다."


def test_stream_allows_anonymous_thread(client: TestClient, owner_pool: _FakeOwnerPool):
    resp = client.post(
        "/agent/stream",
        json={"message": "hi", "thread_id": "anon-t1"},
    )
    assert resp.status_code == 200
    assert owner_pool.owners["anon-t1"] == (None, None)

    resp = client.post(
        "/agent/stream",
        json={"message": "again", "thread_id": "anon-t1", "mcp_session_id": "mcp-a"},
    )
    assert resp.status_code == 200
    assert owner_pool.owners["anon-t1"] == (None, None)


def test_resume_rejects_different_owner(client: TestClient):
    resp = client.post(
        "/agent/stream",
        json={"message": "hi", "thread_id": "resume-t1", "mcp_session_id": "mcp-a"},
    )
    assert resp.status_code == 200

    resp = client.post(
        "/agent/resume",
        json={
            "thread_id": "resume-t1",
            "approved": True,
            "action_id": 1,
            "mcp_session_id": "mcp-b",
        },
    )
    assert resp.status_code == 403


# ── ADR 0011: stable-principal thread ownership ─────────────────────────────────


def test_stream_same_principal_across_sessions_sees_same_thread(
    client: TestClient,
    owner_pool: _FakeOwnerPool,
):
    """Re-login (new mcp_session_id) with the same stable principal must still
    resolve to the thread the principal created — the whole point of ADR 0011."""
    resp = client.post(
        "/agent/stream",
        json={
            "message": "hi",
            "thread_id": "principal-t1",
            "mcp_session_id": "mcp-device-a",
            "principal": "student-123",
        },
    )
    assert resp.status_code == 200
    assert owner_pool.owners["principal-t1"][1] == "principal"

    # Different device/session (e.g. re-login issued a new mcp_session_id), same
    # principal — must be treated as the same owner, not rejected.
    resp = client.post(
        "/agent/stream",
        json={
            "message": "again from another device",
            "thread_id": "principal-t1",
            "mcp_session_id": "mcp-device-b",
            "principal": "student-123",
        },
    )
    assert resp.status_code == 200


def test_stream_rejects_different_principal(client: TestClient):
    resp = client.post(
        "/agent/stream",
        json={
            "message": "hi",
            "thread_id": "principal-t2",
            "mcp_session_id": "mcp-a",
            "principal": "student-A",
        },
    )
    assert resp.status_code == 200

    resp = client.post(
        "/agent/stream",
        json={
            "message": "steal",
            "thread_id": "principal-t2",
            "mcp_session_id": "mcp-b",
            "principal": "student-B",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "이 대화는 현재 세션의 소유가 아닙니다."


def test_stream_anonymous_flow_unchanged_when_no_principal_ever_sent(
    client: TestClient,
    owner_pool: _FakeOwnerPool,
):
    """No caller today sends `principal` (ADR 0011 is ssuAgent-side prep only) —
    the entire existing session-bound / anonymous behavior must be untouched."""
    resp = client.post(
        "/agent/stream",
        json={"message": "hi", "thread_id": "legacy-anon"},
    )
    assert resp.status_code == 200
    assert owner_pool.owners["legacy-anon"] == (None, None)

    resp = client.post(
        "/agent/stream",
        json={"message": "hi", "thread_id": "legacy-session", "mcp_session_id": "mcp-x"},
    )
    assert resp.status_code == 200
    assert owner_pool.owners["legacy-session"] == (
        main._hash_mcp_session_id("mcp-x"),
        "session_hash",
    )

    resp = client.post(
        "/agent/stream",
        json={
            "message": "steal",
            "thread_id": "legacy-session",
            "mcp_session_id": "mcp-y",
        },
    )
    assert resp.status_code == 403


def test_lazy_migration_rebinds_session_owned_thread_to_principal_once(
    client: TestClient,
    owner_pool: _FakeOwnerPool,
):
    """A thread created before any caller sent `principal` (session-owned, ADR
    0010 shape) must be lazily upgraded to principal-owned the first time its
    rightful session presents one — then a different session with that same
    principal must find it (rotation survived), while the upgrade must not
    silently re-run / re-key on every subsequent call."""
    # 1) Legacy session-only claim (no principal yet — mirrors a thread that
    #    predates this frontend rollout).
    resp = client.post(
        "/agent/stream",
        json={"message": "hi", "thread_id": "migrate-t1", "mcp_session_id": "mcp-orig"},
    )
    assert resp.status_code == 200
    assert owner_pool.owners["migrate-t1"] == (
        main._hash_mcp_session_id("mcp-orig"),
        "session_hash",
    )

    # 2) The rightful session now starts sending a principal -> lazy rebind.
    resp = client.post(
        "/agent/stream",
        json={
            "message": "now authenticated",
            "thread_id": "migrate-t1",
            "mcp_session_id": "mcp-orig",
            "principal": "student-123",
        },
    )
    assert resp.status_code == 200
    assert owner_pool.owners["migrate-t1"][1] == "principal"
    migrated_owner = owner_pool.owners["migrate-t1"][0]

    # 3) Re-login: brand new mcp_session_id, same principal -> same thread found
    #    (this is what ADR 0010 alone could never do).
    resp = client.post(
        "/agent/stream",
        json={
            "message": "after re-login",
            "thread_id": "migrate-t1",
            "mcp_session_id": "mcp-new-device",
            "principal": "student-123",
        },
    )
    assert resp.status_code == 200
    # Runs at most once: the stored owner/kind is stable across further calls,
    # not re-derived or re-written on every request.
    assert owner_pool.owners["migrate-t1"] == (migrated_owner, "principal")

    # 4) The original session, now stale for this thread, no longer matches on
    #    its own (session-only auth is no longer sufficient once a thread is
    #    principal-owned) unless it also presents the principal.
    resp = client.post(
        "/agent/stream",
        json={
            "message": "old session without principal",
            "thread_id": "migrate-t1",
            "mcp_session_id": "mcp-orig",
        },
    )
    assert resp.status_code == 403


# ── Key configured → header required ────────────────────────────────────────────


def test_stream_401_without_header(monkeypatch: pytest.MonkeyPatch, client: TestClient):
    monkeypatch.setattr(config, "AGENT_API_KEY", "s3cret")
    resp = _post_stream(client)
    assert resp.status_code == 401


def test_stream_401_with_wrong_header(monkeypatch: pytest.MonkeyPatch, client: TestClient):
    monkeypatch.setattr(config, "AGENT_API_KEY", "s3cret")
    resp = _post_stream(client, headers={"X-Agent-Key": "nope"})
    assert resp.status_code == 401


def test_stream_passes_with_correct_header(monkeypatch: pytest.MonkeyPatch, client: TestClient):
    monkeypatch.setattr(config, "AGENT_API_KEY", "s3cret")
    resp = _post_stream(client, headers={"X-Agent-Key": "s3cret"})
    assert resp.status_code == 200
    assert "done" in resp.text


def test_health_open_even_with_api_key(monkeypatch: pytest.MonkeyPatch, client: TestClient):
    monkeypatch.setattr(config, "AGENT_API_KEY", "s3cret")
    resp = client.get("/health")
    assert resp.status_code == 200


# ── Edge hardening: rate limit, payload cap, error non-disclosure ───────────────


def test_stream_rate_limited_over_limit(monkeypatch: pytest.MonkeyPatch, client: TestClient):
    # Limit is read per-request (callable), so a low override takes effect.
    monkeypatch.setattr(config, "AGENT_API_KEY", "")
    monkeypatch.setattr(config, "AGENT_RATE_LIMIT", "3/minute")
    monkeypatch.setattr(main.limiter, "enabled", True)
    statuses = [_post_stream(client).status_code for _ in range(5)]
    assert statuses[:3] == [200, 200, 200]
    assert 429 in statuses[3:]


def test_stream_rejects_oversized_message(monkeypatch: pytest.MonkeyPatch, client: TestClient):
    monkeypatch.setattr(config, "AGENT_API_KEY", "")
    huge = "x" * (config.AGENT_MAX_MESSAGE_CHARS + 1)
    resp = client.post("/agent/stream", json={"message": huge, "thread_id": "t1"})
    assert resp.status_code == 422


def test_agent_boundaries_reject_bad_ids_and_oversized_raw_body(client: TestClient):
    bad_thread = client.post(
        "/agent/stream",
        json={"message": "hi", "thread_id": "bad/thread"},
    )
    bad_session = client.post(
        "/agent/stream",
        json={"message": "hi", "thread_id": "t1", "mcp_session_id": "x" * 129},
    )
    raw_oversized = client.post(
        "/agent/stream",
        content=b"{" + b" " * config.AGENT_MAX_REQUEST_BYTES,
        headers={"Content-Type": "application/json"},
    )

    assert bad_thread.status_code == 422
    assert bad_session.status_code == 422
    assert raw_oversized.status_code == 413


def test_signed_rate_identity_is_verified_and_spoofed_forwarding_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(config, "AGENT_API_KEY", "shared-key")
    client_id = "a" * 64
    signature = main.hmac.new(
        b"shared-key",
        f"v1:{client_id}".encode(),
        main.hashlib.sha256,
    ).hexdigest()

    trusted = Request(
        {
            "type": "http",
            "headers": [
                (b"x-agent-client-id", client_id.encode()),
                (b"x-agent-client-signature", signature.encode()),
                (b"x-forwarded-for", b"198.51.100.2"),
            ],
            "client": ("127.0.0.1", 1234),
        }
    )
    spoofed = Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", b"203.0.113.9")],
            "client": ("127.0.0.1", 1234),
        }
    )

    assert main._rate_limit_identity(trusted) == f"proxy:{client_id}"
    assert main._rate_limit_identity(spoofed) == "socket:127.0.0.1"


def test_delete_thread_requires_owner_and_removes_checkpoint_rows(
    client: TestClient,
    owner_pool: _FakeOwnerPool,
):
    created = client.post(
        "/agent/stream",
        json={"message": "hi", "thread_id": "delete-t1", "mcp_session_id": "mcp-a"},
    )
    denied = client.request(
        "DELETE",
        "/agent/threads/delete-t1",
        json={"mcp_session_id": "mcp-b"},
    )
    deleted = client.request(
        "DELETE",
        "/agent/threads/delete-t1",
        json={"mcp_session_id": "mcp-a"},
    )

    assert created.status_code == 200
    assert denied.status_code == 403
    assert deleted.status_code == 204
    assert owner_pool.owners["delete-t1"] == (None, "deleted")

    blocked_recreation = client.post(
        "/agent/stream",
        json={"message": "again", "thread_id": "delete-t1", "mcp_session_id": "mcp-a"},
    )
    assert blocked_recreation.status_code == 410


async def test_stream_graph_hides_exception_detail(monkeypatch: pytest.MonkeyPatch):
    """The error SSE must not leak internal exception detail to the client."""

    class _Boom:
        def astream_events(self, *args, **kwargs):
            raise RuntimeError("internal dsn postgres://secret leaked")

    monkeypatch.setattr(main, "_graph", _Boom())
    chunks = [
        chunk
        async for chunk in main._stream_graph(
            {"messages": []}, {"configurable": {"thread_id": "t1"}}
        )
    ]
    joined = "".join(chunks)
    assert "postgres://secret" not in joined
    assert '"type": "error"' in joined
