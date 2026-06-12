from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.tools import tool


@pytest.fixture
def mock_mcp_tools():
    @tool
    def get_today_meal(input: str = "") -> str:
        """Get today's cafeteria meal."""
        return "오늘 학식: 된장찌개, 밥, 김치"

    @tool
    def get_library_available_seats(input: str = "") -> str:
        """Get available library seats."""
        return "2층: 30석 가용"

    return [get_today_meal, get_library_available_seats]


class FakeChatModelWithTools(FakeListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def mock_llm(mock_mcp_tools):
    # Return a plain answer (no tool call) for simplicity
    return FakeChatModelWithTools(responses=["오늘 학식: 된장찌개, 밥, 김치"])
