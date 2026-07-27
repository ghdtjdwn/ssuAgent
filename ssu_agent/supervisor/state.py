"""
SsuAgentState — LangGraph multi-agent shared state.

Design decision (ADR): single shared TypedDict across supervisor + sub-agents.
- messages: Annotated[list, add_messages] is the merge channel (all agents append here).
- active_agent: set by supervisor when routing, cleared when sub-agent finishes.
- mcp_session_id: passed from the FastAPI client, threaded to all private MCP tool calls.
- library_connected: client-asserted library auth hint for pre-LLM UX short-circuits.

Why a single state rather than per-agent TypedDicts:
  LangGraph subgraphs that share a parent state use channel-level merging via reducers.
  For this project, only `messages` needs cross-agent merging (add_messages is the reducer).
  Other fields are updated by exactly one owner, so a plain override is correct.
  A separate per-agent TypedDict would require explicit input/output transforms at every
  subgraph boundary — unnecessary complexity at this scale.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Annotated

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

_UNBOUND = object()
_request_mcp_session_id: ContextVar[str | None | object] = ContextVar(
    "request_mcp_session_id",
    default=_UNBOUND,
)


@contextmanager
def bind_request_mcp_session_id(mcp_session_id: str | None):
    """Bind a capability to this async request without checkpointing it."""
    token = _request_mcp_session_id.set(mcp_session_id)
    try:
        yield
    finally:
        _request_mcp_session_id.reset(token)


def request_mcp_session_id(state: SsuAgentState) -> str | None:
    """Return the request capability, with legacy direct-graph compatibility.

    Production HTTP entry points always bind the ContextVar and overwrite the
    historical state channel with ``None``. The state fallback exists only for
    direct graph callers and old tests while that compatibility surface is
    phased out.
    """
    bound = _request_mcp_session_id.get()
    if bound is not _UNBOUND:
        return bound if isinstance(bound, str) else None
    return state.get("mcp_session_id")


class SsuAgentState(TypedDict):
    # ── Conversation ──────────────────────────────────────────────────────────
    messages: Annotated[list[BaseMessage], add_messages]

    # ── Session binding ───────────────────────────────────────────────────────
    # Lifecycle: FastAPI thread_id (LangGraph) ↔ mcp_session_id (ssuMCP auth).
    # Compatibility channel only. HTTP entry points always write None and bind
    # the live capability in a request-scoped ContextVar, so checkpoints never
    # receive a reusable MCP session ID.
    mcp_session_id: str | None
    # Client-asserted hint from ssuAI's useLibraryAuth().isConnected. This is
    # only a best-effort UX signal; ssuMCP AUTH_REQUIRED remains enforcement.
    library_connected: bool

    # ── Routing ───────────────────────────────────────────────────────────────
    # Set by supervisor before routing; cleared by sub-agent on return.
    # Used to detect re-entry to supervisor after sub-agent completion.
    active_agent: str | None
