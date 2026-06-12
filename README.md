# ssuAgent

숭실대학교 MCP 서버([ssuMCP](https://github.com/hoeongj/ssuMCP))에 연결하는 LangGraph 기반 캠퍼스 AI 에이전트.

## Architecture

```
User Query
    │
    ▼
ssuAgent (LangGraph ReAct Agent)
    │  Streamable HTTP (MCP 2025-03-26)
    ▼
ssuMCP Server (Spring Boot 4)
    ├── Pyxis (도서관)
    ├── u-SAINT (학사/성적)
    └── LMS (강의/과제)
```

## Why LangGraph?

| 방식 | 이유 |
|------|------|
| LangChain LCEL | 단순 체인에 적합. 상태·루프·분기 표현 어려움 |
| 직접 function calling | 오케스트레이션 코드 직접 관리. 멀티스텝 복잡도 증가 |
| **LangGraph** (채택) | StateGraph로 상태·분기·루프를 명시적 그래프로 표현. Phase 2 멀티에이전트 확장 용이 |

## Setup

```bash
pip install uv
uv sync --extra dev
```

## Run

```bash
export GOOGLE_API_KEY=<your-gemini-key>
export SSUMCP_URL=https://ssumcp.duckdns.org/mcp  # optional, this is the default
uv run python -c "
import asyncio
from ssu_agent.graph import run_query
print(asyncio.run(run_query('오늘 학식 알려줘')))
"
```

## Test

```bash
uv run pytest
```

## Phase Roadmap

| Phase | 범위 |
|-------|------|
| 1 (현재) | ReAct 단일 에이전트, 공개 도구 3종 (식단/도서관/공지) |
| 2 | 도메인별 서브그래프, 도서관 예약 인증 도구, 스트리밍 응답 |
| 3 | ssuAI 프론트엔드 연동 (웹 UI 채팅) |
