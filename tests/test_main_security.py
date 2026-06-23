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


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
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
