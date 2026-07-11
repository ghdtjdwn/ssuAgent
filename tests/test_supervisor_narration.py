"""Supervisor hand-off narration must be suppressed; a direct answer must be shown.

After routing via ``transfer_to_*``, the supervisor LLM tends to also emit a filler
narration ("...에이전트에게 전달했습니다") that is NOT the real answer — the sub-agent's
reply is. ``_stream_graph`` holds supervisor text, drops it when a transfer fires, and
flushes it only when the supervisor answered directly (no routing).
"""

from __future__ import annotations

import json

from ssu_agent import main


class _Chunk:
    def __init__(self, text: str) -> None:
        self.content = text


class _FakeGraph:
    """Minimal stand-in that replays a fixed astream_events sequence."""

    def __init__(self, events: list[dict]) -> None:
        self._events = events

    async def astream_events(self, input_data, config, version):  # noqa: ARG002
        for event in self._events:
            yield event


def _model(node: str, text: str) -> dict:
    return {
        "event": "on_chat_model_stream",
        "name": "",
        "metadata": {"langgraph_node": node},
        "data": {"chunk": _Chunk(text)},
    }


def _transfer(agent: str) -> dict:
    return {
        "event": "on_tool_start",
        "name": f"transfer_to_{agent}_agent",
        "metadata": {},
        "data": {},
    }


async def _collect() -> list[dict]:
    out: list[dict] = []
    async for sse in main._stream_graph({"messages": []}, {}):
        out.append(json.loads(sse[len("data: ") :].strip()))
    return out


async def test_supervisor_handoff_narration_is_dropped(monkeypatch) -> None:
    events = [
        _model("supervisor", "도서관 2층 예약은 도서관 에이전트에게 전달했습니다."),
        _transfer("library"),
        _model("library_agent", "좌석 예약은 도서관 로그인 후 이용할 수 있어요."),
    ]
    monkeypatch.setattr(main, "_graph", _FakeGraph(events))
    out = await _collect()

    text = "".join(e["content"] for e in out if e["type"] == "text")
    assert "전달했습니다" not in text  # supervisor narration suppressed
    assert "로그인 후 이용" in text  # sub-agent's real answer shown
    assert any(e["type"] == "handoff" for e in out)


async def test_supervisor_direct_answer_is_kept(monkeypatch) -> None:
    # No routing: the supervisor's own answer IS the response and must be shown.
    events = [_model("supervisor", "안녕하세요! 무엇을 도와드릴까요?")]
    monkeypatch.setattr(main, "_graph", _FakeGraph(events))
    out = await _collect()

    text = "".join(e["content"] for e in out if e["type"] == "text")
    assert "무엇을 도와드릴까요" in text
