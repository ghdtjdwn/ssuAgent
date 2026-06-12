from __future__ import annotations

import pytest

from ssu_agent.graph import build_graph


@pytest.mark.asyncio
async def test_build_graph_with_mocks(mock_llm, mock_mcp_tools):
    """Graph builds successfully with injected mock LLM and tools."""
    graph = await build_graph(llm=mock_llm, tools=mock_mcp_tools)
    assert graph is not None


@pytest.mark.asyncio
async def test_meal_query_returns_response(mock_llm, mock_mcp_tools):
    """Agent returns a non-empty response for a meal query."""
    graph = await build_graph(llm=mock_llm, tools=mock_mcp_tools)
    result = await graph.ainvoke({"messages": [{"role": "user", "content": "오늘 학식 알려줘"}]})
    messages = result.get("messages", [])
    assert len(messages) > 0
    last = messages[-1]
    content = last.content if hasattr(last, "content") else str(last)
    assert len(content) > 0
