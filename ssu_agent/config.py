import os

# Strip whitespace/CRLF from env vars — secrets copied on Windows can carry
# a trailing \r, which produces an illegal HTTP Authorization header and
# causes httpcore.LocalProtocolError on every request to Groq/OpenRouter.
SSUMCP_URL: str = os.getenv("SSUMCP_URL", "https://ssumcp.duckdns.org/mcp").strip()
GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "").strip()
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "").strip()
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "").strip()
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5").strip()
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql://ssuai:dev@localhost:5432/ssuai",
).strip()

# CORS allow-list. Comma-separated origins; a lone "*" means allow all.
# Default "*" preserves the previous wide-open behavior until configured.
ALLOWED_ORIGINS: list[str] = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "*").strip().split(",")
    if origin.strip()
]

# API key gate for /agent endpoints. Local development may leave it empty while
# AGENT_API_KEY_REQUIRED is false. Production sets the required flag so a missing
# key fails startup instead of exposing caller-asserted principal fields.
AGENT_API_KEY: str = os.getenv("AGENT_API_KEY", "").strip()
AGENT_API_KEY_REQUIRED: bool = os.getenv("AGENT_API_KEY_REQUIRED", "false").strip().lower() in {
    "1",
    "true",
    "yes",
}

# Per-IP inbound rate limit for /agent/* (slowapi syntax, e.g. "30/minute").
# Mirrors ssuMCP ADR 0061: these endpoints fan out to paid LLM providers, so an
# unauthenticated request flood is a cost-exhaustion / DoS vector.
AGENT_RATE_LIMIT: str = os.getenv("AGENT_RATE_LIMIT", "30/minute").strip()

# Request boundary limits. The byte cap is enforced before JSON decoding; the
# character cap remains a model-cost guard after decoding.
AGENT_MAX_REQUEST_BYTES: int = int(os.getenv("AGENT_MAX_REQUEST_BYTES", "32768"))
AGENT_MAX_MESSAGE_CHARS: int = int(os.getenv("AGENT_MAX_MESSAGE_CHARS", "8000"))

# Conversation data lifecycle. Cleanup removes at most one bounded batch per
# interval so it cannot monopolize the shared checkpointer pool.
AGENT_CONVERSATION_RETENTION_DAYS: int = int(os.getenv("AGENT_CONVERSATION_RETENTION_DAYS", "30"))
AGENT_RETENTION_CLEANUP_INTERVAL_SECONDS: int = int(
    os.getenv("AGENT_RETENTION_CLEANUP_INTERVAL_SECONDS", "3600")
)
AGENT_RETENTION_CLEANUP_BATCH_SIZE: int = int(
    os.getenv("AGENT_RETENTION_CLEANUP_BATCH_SIZE", "100")
)
AGENT_STORAGE_TIMEOUT_SECONDS: float = float(os.getenv("AGENT_STORAGE_TIMEOUT_SECONDS", "2"))

# Startup compatibility scrub for checkpoints written before request-scoped MCP
# capabilities. Work is chunked and fails closed if this total safety ceiling is
# insufficient, so old bearer values are never silently served.
AGENT_CAPABILITY_SCRUB_BATCH_SIZE: int = int(os.getenv("AGENT_CAPABILITY_SCRUB_BATCH_SIZE", "500"))
AGENT_CAPABILITY_SCRUB_MAX_ROWS: int = int(os.getenv("AGENT_CAPABILITY_SCRUB_MAX_ROWS", "100000"))

# psycopg AsyncConnectionPool ceiling for LangGraph checkpointer traffic.
AGENT_PG_POOL_MAX_SIZE: int = int(os.getenv("AGENT_PG_POOL_MAX_SIZE", "5"))
