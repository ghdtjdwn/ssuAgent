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
| `AGENT_RATE_LIMIT` | `30/minute` | 검증된 proxy client identity별 process-local limit; multi-replica 전 shared store 필요 |
| `AGENT_MAX_REQUEST_BYTES` | `32768` | JSON decode 전 `/agent/*` request body byte 상한 |
| `AGENT_MAX_MESSAGE_CHARS` | `8000` | 단일 user message의 입력 상한 |

브라우저가 보낸 `principal`을 직접 신뢰하지 않는다. 운영에서는 API key를 검증한 ssuAI proxy만
`principal`을 전달한다. proxy는 principal 또는 Vercel이 보장한 client IP로 만든 pseudonym을
`AGENT_API_KEY`로 서명하고, agent는 서명을 검증한 identity만 rate-limit key로 사용한다. thread owner는
principal과 legacy MCP session 모두 원문 대신 digest를 저장한다.

## 대화 데이터 수명

| 변수 | 기본값 | 역할 |
| --- | --- | --- |
| `AGENT_CONVERSATION_RETENTION_DAYS` | `30` | 마지막 접근 이후 checkpoint와 owner 보존 기간; `0` 이하는 자동 정리 비활성 |
| `AGENT_RETENTION_CLEANUP_INTERVAL_SECONDS` | `3600` | bounded cleanup 실행 간격 |
| `AGENT_RETENTION_CLEANUP_BATCH_SIZE` | `100` | 한 transaction에서 삭제할 최대 thread 수 |
| `AGENT_STORAGE_TIMEOUT_SECONDS` | `2` | readiness와 storage lifecycle 작업의 시간 상한 |
| `AGENT_CAPABILITY_SCRUB_BATCH_SIZE` | `500` | startup에서 legacy capability를 치환할 transaction당 row 상한 |
| `AGENT_CAPABILITY_SCRUB_MAX_ROWS` | `100000` | 한 startup에서 치환할 총 row 안전 상한; 초과 잔여분이 있으면 startup 실패 |

`DELETE /agent/threads/{thread_id}`는 stream/resume과 같은 API key 및 owner 검증을 거친 뒤 LangGraph
checkpoint 세 테이블을 한 transaction에서 삭제하고 owner 원문 없는 tombstone을 남긴다. concurrent
stream과 DELETE는 owner row lock으로 선형화되고, 세 checkpoint table의 trigger는 삭제 commit 이후
늦은 write를 거부한다. 따라서 성공한 DELETE 직후 세 table은 계속 비어 있다. 자동 retention이 tombstone을
포함한 owner row를 bounded batch로 최종 정리한다.

startup scrub은 LangGraph 1.2.4의 실제 저장 형식에 맞춰 `checkpoints.checkpoint` JSONB의
`channel_values.mcp_session_id`와 `checkpoint_blobs`/`checkpoint_writes`의 같은 이름 channel만 typed
`None`으로 치환한다. 다른 state나 대화는 삭제하지 않으며 재실행은 0건인 idempotent 작업이다. 설정된
총 row 상한 뒤에도 raw channel이 남으면 readiness 전에 startup이 실패한다. 운영자는 로그의 row 수를
확인해 `AGENT_CAPABILITY_SCRUB_MAX_ROWS`만 올려 재배포할 수 있다. 같은 DB trigger가 rolling rollout 중
old pod의 새 legacy write도 `None`으로 정규화해 startup 검사 직후 raw 값이 재유입되는 race를 막는다.
scrub, explicit DELETE, retention과 saver trigger는 모두 owner row를 먼저, checkpoint row를 나중에 lock한다.
여러 owner를 잡는 scrub/retention은 `thread_id` 순서로 통일해 rollout cleanup 사이의 교착을 피한다.

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
