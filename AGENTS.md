# AGENTS.md — ssuAgent

Python LangGraph campus assistant agent connecting to ssuMCP.

## Workflow

- Claude = design/spec/review. Codex = ALL git & deploy execution. Claude does NOT commit.
- Authorship: ghdtjdwn <seongjuice999@gmail.com>. NO AI attribution anywhere.
  NO Co-Authored-By, NO "Claude"/"Codex"/"Gemini" in commits, PRs, code comments, docs.
- Decisions: web search first → evaluate (portfolio value > trend fit > completion) → confirm with user.
- Docs: Korean/English mix in README/docs. LLM-facing files (this file, prompts) = English only.

## Commands

- Install: `uv sync --extra dev`
- Test: `uv run pytest`
- Lint: `uv run ruff check . && uv run ruff format --check .`
- Run: `GOOGLE_API_KEY=<key> uv run python -c "import asyncio; from ssu_agent.graph import run_query; print(asyncio.run(run_query('오늘 학식 알려줘')))"`

## Phase Roadmap

- Phase 1 (current): single ReAct agent, public ssuMCP tools (meal/library/notice), scaffolding
- Phase 2: multi-agent sub-graphs per domain, auth tools (library reservation), streaming
- Phase 3: ssuAI frontend integration (web UI for agent chat)

## Commit Convention

Conventional Commits: feat/fix/refactor/chore/docs + scope (agent/mcp/graph/test)
Branch: feat/fix/... + kebab-case. PR required; merge via local fast-forward only.
