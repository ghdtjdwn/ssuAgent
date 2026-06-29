"""
Tests for the FastAPI hardening: opt-in /agent API-key gate and open /health.

The graph/DB are never touched: _stream_graph is monkeypatched to a dummy async
generator, and TestClient is instantiated WITHOUT a context manager so the
lifespan (which opens a real Postgres pool) does not run.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ssu_agent import config, main


async def _fake_stream_graph(input_data, config):  # noqa: A002 - mirrors prod signature
    """Stand-in for _stream_graph: one dummy SSE line, no LLM/DB."""
    yield 'data: {"type": "done"}\n\n'


class _FakeOwnerCursor:
    def __init__(self, owners: dict[str, str | None]):
        self.owners = owners
        self._row: tuple[str | None] | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, query: str, params: tuple | None = None):
        normalized = " ".join(query.split()).upper()
        if normalized.startswith("INSERT INTO THREAD_OWNERS"):
            thread_id, owner = params
            self.owners.setdefault(thread_id, owner)
            self._row = None
            return
        if normalized.startswith("SELECT OWNER FROM THREAD_OWNERS"):
            (thread_id,) = params
            self._row = (self.owners[thread_id],) if thread_id in self.owners else None
            return
        if normalized.startswith("CREATE TABLE IF NOT EXISTS THREAD_OWNERS"):
            self._row = None
            return
        raise AssertionError(f"unexpected query: {query}")

    async def fetchone(self):
        return self._row


class _FakeOwnerConnection:
    def __init__(self, owners: dict[str, str | None]):
        self.owners = owners

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def cursor(self):
        return _FakeOwnerCursor(self.owners)


class _FakeOwnerPool:
    def __init__(self):
        self.owners: dict[str, str | None] = {}

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


# ── No key configured → gate is a no-op (prod behavior preserved) ───────────────


def test_stream_open_when_no_api_key(monkeypatch: pytest.MonkeyPatch, client: TestClient):
    monkeypatch.setattr(config, "AGENT_API_KEY", "")
    resp = _post_stream(client)
    assert resp.status_code == 200
    assert "done" in resp.text


def test_health_open(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "UP"


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
    assert owner_pool.owners["owned-t1"] == "mcp-a"

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
    assert owner_pool.owners["anon-t1"] is None

    resp = client.post(
        "/agent/stream",
        json={"message": "again", "thread_id": "anon-t1", "mcp_session_id": "mcp-a"},
    )
    assert resp.status_code == 200
    assert owner_pool.owners["anon-t1"] is None


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
