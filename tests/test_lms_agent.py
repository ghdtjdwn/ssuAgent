from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from ssu_agent.agents.lms import (
    _LMS_LOGIN_MESSAGE,
    _LMS_STATUS_UNAVAILABLE_MESSAGE,
    build_lms_agent,
)
from ssu_agent.supervisor.state import SsuAgentState


class _SpyLmsLLM(FakeMessagesListChatModel):
    bind_tools_calls: int = 0
    visible_properties: list[set[str]] = []

    def bind_tools(self, tools, **kwargs):
        self.bind_tools_calls += 1
        self.visible_properties = [
            set(tool.tool_call_schema.model_json_schema().get("properties", {})) for tool in tools
        ]
        return self


@tool
def get_my_assignments(mcp_session_id: str) -> str:
    """LMS assignments lookup."""
    return '{"status":"OK","mcpSessionId":"secret","data":[]}'


@tool("get_auth_status")
def disconnected_lms_status(mcp_session_id: str) -> str:
    """Disconnected LMS provider status."""
    return (
        '{"status":"OK","mcpSessionId":"secret",'
        '"providers":[{"provider":"LMS","linked":false,"health":"UNKNOWN"}]}'
    )


@tool("get_auth_status")
def connected_lms_status(mcp_session_id: str) -> str:
    """Connected LMS provider status."""
    return (
        '{"status":"OK","mcpSessionId":"secret",'
        '"providers":[{"provider":"LMS","linked":true,"health":"VALID"}]}'
    )


def _state(session_id: str | None) -> SsuAgentState:
    return {
        "messages": [HumanMessage(content="이번 학기 과제 보여줘")],
        "mcp_session_id": session_id,
        "library_connected": False,
        "active_agent": "lms",
    }


@pytest.mark.asyncio
async def test_lms_request_without_session_skips_llm():
    llm = _SpyLmsLLM(responses=[AIMessage(content="사용하면 안 되는 응답")])
    graph = build_lms_agent([get_my_assignments], llm=llm).compile()

    result = await graph.ainvoke(_state(None))

    assert result["messages"][-1].content == f"[LMS 에이전트] {_LMS_LOGIN_MESSAGE}"
    assert llm.bind_tools_calls == 0


@pytest.mark.asyncio
async def test_lms_missing_status_contract_fails_safe_without_llm():
    llm = _SpyLmsLLM(responses=[AIMessage(content="사용하면 안 되는 응답")])
    graph = build_lms_agent([get_my_assignments], llm=llm).compile()

    result = await graph.ainvoke(_state("lms-session"))

    assert result["messages"][-1].content == (f"[LMS 에이전트] {_LMS_STATUS_UNAVAILABLE_MESSAGE}")
    assert llm.bind_tools_calls == 0


@pytest.mark.asyncio
async def test_lms_provider_preflight_blocks_disconnected_session():
    llm = _SpyLmsLLM(responses=[AIMessage(content="MCP session ID를 알려주세요.")])
    graph = build_lms_agent(
        [disconnected_lms_status, get_my_assignments],
        llm=llm,
    ).compile()

    result = await graph.ainvoke(_state("saint-only-session"))

    assert result["messages"][-1].content == f"[LMS 에이전트] {_LMS_LOGIN_MESSAGE}"
    assert llm.bind_tools_calls == 0


@pytest.mark.asyncio
async def test_connected_lms_tools_hide_session_argument_from_model():
    llm = _SpyLmsLLM(responses=[AIMessage(content="과제 조회 결과입니다.")])
    graph = build_lms_agent(
        [connected_lms_status, get_my_assignments],
        llm=llm,
    ).compile()

    result = await graph.ainvoke(_state("lms-session"))

    assert result["messages"][-1].content == "[LMS 에이전트] 과제 조회 결과입니다."
    assert llm.visible_properties == [set()]
