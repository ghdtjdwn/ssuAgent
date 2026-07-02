"""
EPIC 6 — Routing Eval

Verifies that the supervisor's post-routing node (_post_supervisor) produces the
correct agent-handoff for each query category.  Tests are unit-level (no LLM
calls) and target the routing mechanism directly rather than the full graph,
since the full graph's routing path is exercised identically regardless of which
LLM is in use.

Rationale: The real routing decision is made by the human-readable transfer_to_*
tool descriptions + LLM (untestable without a live model).  What we *can* assert
deterministically is that the routing marker parser routes to the right node when
the LLM produces a marker — that is exactly what these tests cover.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ssu_agent.supervisor.graph import _ROUTE_PREFIX, _post_supervisor
from ssu_agent.supervisor.state import SsuAgentState

# ── Helpers ───────────────────────────────────────────────────────────────────


def _state_with_marker(query: str, marker: str) -> SsuAgentState:
    """Build a minimal state where the last ToolMessage contains a routing marker."""
    return {
        "messages": [
            HumanMessage(content=query),
            AIMessage(content=""),
            ToolMessage(content=f"{_ROUTE_PREFIX}{marker}", tool_call_id="tc-eval-1"),
        ],
        "mcp_session_id": "eval-session",
        "active_agent": None,
    }


def _state_direct_answer(query: str, answer: str) -> SsuAgentState:
    """Build a state where the supervisor answered directly (no routing marker)."""
    return {
        "messages": [
            HumanMessage(content=query),
            AIMessage(content=answer),
        ],
        "mcp_session_id": None,
        "active_agent": None,
    }


# ── Parametrized routing eval ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "expected_agent"),
    [
        ("도서관 자리 잡아줘", "library_agent"),
        ("빈 좌석 알려줘", "library_agent"),
        ("도서관 예약 해줘", "library_agent"),
    ],
)
def test_eval_library_routing(query: str, expected_agent: str) -> None:
    """Routing markers for library queries resolve to library_agent."""
    state = _state_with_marker(query, expected_agent)
    cmd = _post_supervisor(state)
    assert cmd.goto == expected_agent, f"'{query}' should route to {expected_agent}"
    assert cmd.update["active_agent"] == expected_agent


@pytest.mark.parametrize(
    ("query", "expected_agent"),
    [
        ("장학금 기준 알려줘", "academic_agent"),
        ("졸업요건 확인해줘", "academic_agent"),
        ("성적 조회해줘", "academic_agent"),
    ],
)
def test_eval_academic_routing(query: str, expected_agent: str) -> None:
    """Routing markers for academic queries resolve to academic_agent."""
    state = _state_with_marker(query, expected_agent)
    cmd = _post_supervisor(state)
    assert cmd.goto == expected_agent, f"'{query}' should route to {expected_agent}"
    assert cmd.update["active_agent"] == expected_agent


@pytest.mark.parametrize(
    ("query", "expected_agent"),
    [
        ("LMS 과제 확인해줘", "lms_agent"),
        ("강의 자료 내려받아줘", "lms_agent"),
        ("과제 마감일 알려줘", "lms_agent"),
    ],
)
def test_eval_lms_routing(query: str, expected_agent: str) -> None:
    """Routing markers for LMS queries resolve to lms_agent."""
    state = _state_with_marker(query, expected_agent)
    cmd = _post_supervisor(state)
    assert cmd.goto == expected_agent, f"'{query}' should route to {expected_agent}"
    assert cmd.update["active_agent"] == expected_agent


@pytest.mark.parametrize(
    ("query", "answer"),
    [
        ("오늘 학식 뭐야", "오늘 학식은 제육볶음입니다."),
        ("캠퍼스 시설 안내해줘", "도서관, 체육관 등이 있습니다."),
        ("안녕", "안녕하세요! 무엇을 도와드릴까요?"),
    ],
)
def test_eval_public_no_routing(query: str, answer: str) -> None:
    """Direct answers (no routing marker) resolve to END."""
    from langgraph.graph import END

    state = _state_direct_answer(query, answer)
    cmd = _post_supervisor(state)
    assert cmd.goto is END, f"'{query}' should end without sub-agent routing"


# ── Marker correctness ────────────────────────────────────────────────────────


def test_eval_unknown_marker_goes_to_end() -> None:
    """An unrecognised marker string is treated as no-route → END."""
    from langgraph.graph import END

    state: SsuAgentState = {
        "messages": [
            HumanMessage(content="???"),
            AIMessage(content="알 수 없는 요청"),
        ],
        "mcp_session_id": None,
        "active_agent": None,
    }
    cmd = _post_supervisor(state)
    assert cmd.goto is END


def test_eval_marker_survives_surrounding_text() -> None:
    """Routing marker embedded in longer text is still extracted correctly."""
    state: SsuAgentState = {
        "messages": [
            HumanMessage(content="도서관 예약"),
            AIMessage(content=""),
            ToolMessage(
                content=f"some preamble {_ROUTE_PREFIX}library_agent trailing text",
                tool_call_id="tc-embed",
            ),
        ],
        "mcp_session_id": None,
        "active_agent": None,
    }
    cmd = _post_supervisor(state)
    assert cmd.goto == "library_agent"
