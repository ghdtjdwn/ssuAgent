# ssuAgent 설정

로컬 예시는 [`.env.example`](../.env.example)에 있다. 실제 API key, database password, agent shared
key는 `.env`, Kubernetes Secret 또는 배포 플랫폼의 secret store에만 두고 저장소에 커밋하지 않는다.

## 필수 설정

| 변수 | 로컬 기본값 | 역할 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://ssuai:dev@localhost:5432/ssuai` | LangGraph checkpoint와 thread owner 저장소 |
| `SSUMCP_URL` | `https://ssumcp.duckdns.org/mcp` | upstream MCP endpoint |
| LLM provider key | 없음 | `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `GOOGLE_API_KEY`, `OPENROUTER_API_KEY` 중 하나 이상 필요 |

설정된 provider만 Anthropic → Groq → Gemini → OpenRouter 순서로 fallback chain에 들어간다. 키가
하나도 없으면 애플리케이션은 명확한 runtime error를 반환한다.

## HTTP와 신뢰 경계

| 변수 | 로컬 기본값 | 운영 계약 |
| --- | --- | --- |
| `AGENT_API_KEY` | 비어 있음 | ssuAI server proxy와 공유하는 server-to-server credential |
| `AGENT_API_KEY_REQUIRED` | `false` | production은 `true`; 키가 없으면 startup을 거부 |
| `ALLOWED_ORIGINS` | `*` (code default) | production은 실제 ssuAI origin으로 제한 |
| `AGENT_RATE_LIMIT` | `30/minute` | process-local inbound limit; multi-replica 전 shared store 필요 |
| `AGENT_MAX_MESSAGE_CHARS` | `8000` | 단일 user message의 입력 상한 |

브라우저가 보낸 `principal`을 직접 신뢰하지 않는다. 운영에서는 API key를 검증한 ssuAI proxy만
`principal`을 전달하며, thread owner는 원문 대신 SHA-256 hash를 저장한다.

## 모델과 자원

| 변수 | 기본값 | 역할 |
| --- | --- | --- |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5` | Anthropic provider model |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Google provider model |
| `AGENT_PG_POOL_MAX_SIZE` | `5` | psycopg async connection pool 상한 |

Groq와 OpenRouter model은 현재 `ssu_agent/llm_factory.py`에서 고정한다. model 변경은 provider별
tool-call 형식과 routing/safety 평가를 함께 검증한다.

## 로컬 로딩

```bash
cp .env.example .env
set -a && source .env && set +a
uv sync --extra dev
uv run uvicorn ssu_agent.main:app --host 0.0.0.0 --port 8000
```

Kubernetes production 설정은 [배포 문서](deploy.md)와 Helm `values.yaml`을 기준으로 한다.
