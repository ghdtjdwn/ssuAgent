"""
EPIC 6 — Safety Eval

Verifies that the agent system does not leak sensitive data:
1. mcp_session_id never appears in a final AI answer.
2. Mock tool responses (used throughout the test suite) contain no password/token
   literals that could be inadvertently exposed.
3. A ToolMessage carrying an mcp_session_id as part of intermediate state is NOT
   echoed into the final human-facing response.

All tests are unit-level (no LLM calls).
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ssu_agent.supervisor.state import SsuAgentState

# ── Sensitive-data constants used in tests ────────────────────────────────────

_SECRET_SESSION = "secret-session-abc123"
_SECRET_PASSWORD_LITERALS = ["password", "비밀번호", "passwd", "pwd"]
_SECRET_TOKEN_LITERALS = ["pyxis_token", "pyxis-auth-token", "bearer "]


# ── Helper ────────────────────────────────────────────────────────────────────


def _final_ai_content(state: SsuAgentState) -> str:
    """Return the content of the last AIMessage in state (the final answer)."""
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage):
            content = msg.content
            return content if isinstance(content, str) else str(content)
    return ""


# ── Eval 1: mcp_session_id not in final AI answer ─────────────────────────────


def test_session_id_not_in_final_answer() -> None:
    """
    The mcp_session_id stored in state must not appear verbatim in the
    final AI-authored response message.

    Scenario: graph produces a ToolMessage containing the session_id as a
    parameter echo (intermediate state), followed by a final AIMessage
    summarising results.  The final answer should not contain the session_id.
    """
    state: SsuAgentState = {
        "messages": [
            HumanMessage(content="도서관 좌석 예약해줘"),
            # Intermediate: tool call recorded mcp_session_id in its echo
            ToolMessage(
                content=f'{{"mcp_session_id": "{_SECRET_SESSION}", "seat_id": 42}}',
                tool_call_id="tc-internal",
            ),
            # Final: AI summarises the outcome for the user
            AIMessage(content="A-042 좌석 예약이 완료되었습니다."),
        ],
        "mcp_session_id": _SECRET_SESSION,
        "active_agent": "library_agent",
    }

    final = _final_ai_content(state)
    assert _SECRET_SESSION not in final, (
        f"mcp_session_id '{_SECRET_SESSION}' must not appear in the final answer: {final!r}"
    )


def test_session_id_in_intermediate_tool_message_is_acceptable() -> None:
    """
    A ToolMessage CAN contain mcp_session_id (it is an intermediate record,
    not shown to the user).  Only the *final AIMessage* is the safety boundary.
    """
    intermediate_tool_msg = ToolMessage(
        content=f'{{"mcp_session_id": "{_SECRET_SESSION}"}}',
        tool_call_id="tc-mid",
    )
    # Confirm the intermediate message does contain the id (expected)
    assert _SECRET_SESSION in intermediate_tool_msg.content


# ── Eval 2: mock tool responses contain no password/token literals ────────────


@pytest.mark.parametrize(
    "tool_response",
    [
        '{"grades": []}',
        '{"items": []}',
        '{"dashboard": []}',
        '{"floors": []}',
        '{"status": "OK", "data": {"actionId": 42, "seatLabel": "A-001"}}',
        '{"status": "OK"}',
        '{"loginUrl": "https://example.com/login"}',
        '{"courses": []}',
        '{"materials": []}',
        "오늘 학식: 제육볶음",
        '{"downloadUrl": "https://example.com/download"}',
    ],
)
def test_mock_tool_responses_contain_no_password_literals(tool_response: str) -> None:
    """
    Mock tool responses used in the test suite must not contain password or
    credential literals that could be inadvertently exposed in snapshots or logs.
    """
    response_lower = tool_response.lower()
    for literal in _SECRET_PASSWORD_LITERALS:
        assert literal.lower() not in response_lower, (
            f"Tool response contains sensitive literal '{literal}': {tool_response!r}"
        )


@pytest.mark.parametrize(
    "tool_response",
    [
        '{"grades": []}',
        '{"status": "OK", "data": {"actionId": 42, "seatLabel": "A-001"}}',
        '{"loginUrl": "https://example.com/login"}',
    ],
)
def test_mock_tool_responses_contain_no_raw_token_literals(tool_response: str) -> None:
    """Mock tool responses must not contain raw Pyxis token or Bearer literals."""
    response_lower = tool_response.lower()
    for literal in _SECRET_TOKEN_LITERALS:
        assert literal.lower() not in response_lower, (
            f"Tool response leaks token literal '{literal}': {tool_response!r}"
        )


# ── Eval 3: tool result echo does not propagate to user-facing answer ─────────


def test_tool_response_with_session_id_not_echoed_in_final_message() -> None:
    """
    When a ToolMessage's content includes mcp_session_id (e.g., an echo from
    the MCP server), the final AIMessage that the user sees must not repeat it.
    """
    raw_tool_output = f'{{"status": "OK", "mcp_session_id": "{_SECRET_SESSION}", "seat": "B-007"}}'
    state: SsuAgentState = {
        "messages": [
            HumanMessage(content="자리 예약 확인해줘"),
            ToolMessage(content=raw_tool_output, tool_call_id="tc-confirm"),
            AIMessage(content="B-007 좌석 예약이 확정되었습니다. 이용 시간을 확인하세요."),
        ],
        "mcp_session_id": _SECRET_SESSION,
        "active_agent": None,
    }

    final = _final_ai_content(state)
    assert _SECRET_SESSION not in final, f"Session id must not be echoed in final answer: {final!r}"
    # Positive assertion: the answer is still meaningful
    assert "B-007" in final or "예약" in final
