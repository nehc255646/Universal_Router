# Universal Router

[中文](README.md) | **English**

A **local three-protocol gateway**: convert inbound OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages requests into the protocol the upstream actually supports, then forward them.

Use it to point clients that speak only one API (Cursor, ChatBox, Claude Code, your own scripts) at any upstream (official APIs, relays, OpenRouter, DeepSeek, …).

- Repo: https://github.com/nehc255646/Universal_Router
- Default listen: `127.0.0.1:8787`
- Version: 0.4.0 · MIT

## Features

- Inbound `/v1/chat/completions` · `/v1/responses` · `/v1/messages`
- Upstream `chat_completions` / `responses` / `messages`, per provider
- IR translation: text, thinking/reasoning, images, function/tool calls (including streaming)
- Multi-provider routing: priority / round-robin / weighted / latency / health / cost, retries, failover, circuit breaker and recovery
- Streaming: connect / first-token / idle-read timeouts; cross-protocol Responses event lifecycle and fragmented tool-call arguments
- Web UI: provider CRUD, presets, fetch models, connectivity test, playground, persistent request logs (with tokens)
- Inbound auth; `/api/*` is required when bound off-loopback; keys may be `env:NAME` references
- Model routing accepts `model` or `provider/model`

```
Client  --chat/responses/messages-->  Universal Router  --any upstream protocol-->  Provider
```

## Quick start

Python 3.11+ required.

Windows: double-click `start.bat` to start the gateway, then open `入口.url` for the admin UI.

macOS / Linux:

```bash
chmod +x 启动.sh
./启动.sh
```

Or manually:

```bash
pip install -r requirements.txt
# optional: copy the example config and fill in keys
cp config.example.json config.json
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

Open http://127.0.0.1:8787/ and add a provider. The gateway base URL is http://127.0.0.1:8787/v1 .

## Client examples

Chat Completions:

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-local" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}]}'
```

Python (`openai` SDK):

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-local")
r = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "hello"}],
)
print(r.choices[0].message.content)
```

Anthropic-shaped inbound (even when the upstream is OpenAI):

```bash
curl http://127.0.0.1:8787/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-local" \
  -d '{"model":"gpt-4o","max_tokens":256,"messages":[{"role":"user","content":"hello"}]}'
```

## Configuration

`config.json` (gitignored — do not commit secrets):

| Field | Meaning |
|---|---|
| `server.host` / `server.port` | Listen address; restart after change |
| `server.local_api_key` | If set, inbound must send matching `Authorization: Bearer` or `x-api-key` |
| `server.admin_api_key` | Protects `/api/*`; required (or `local_api_key`) when binding `0.0.0.0` |
| `server.route_strategy` | `priority` / `round_robin` / `weighted` / `latency` / `health` / `cost` |
| `server.retry_count` | Extra retries on the same provider |
| `server.failover` | Try the next matching provider after failure |
| `server.connect_timeout_s` | Upstream TCP/TLS connect timeout |
| `server.first_token_timeout_s` | Streaming first-byte timeout; `0` disables |
| `server.read_idle_timeout_s` | Idle timeout between stream chunks; `0` disables |
| `server.circuit_breaker` | Open the circuit after consecutive failures; half-open probe after cooldown |
| `providers[].api_key` | Literal key, or `env:OPENAI_API_KEY` / `${OPENAI_API_KEY}` |
| `providers[].cost_input_per_1m` / `cost_output_per_1m` | Cost routing and estimates |
| `providers[].id` | `a-z0-9-_`, used as the `provider/model` prefix |
| `providers[].enabled` / `priority` / `weight` | Disable, lower = first, weight within the same priority |
| `providers[].base_url` | Upstream root including `/v1` |
| `providers[].upstream_mode` | `chat_completions` / `responses` / `messages` |
| `providers[].models` | Used for routing; “fetch from upstream” in the UI |

Auth rules:

1. If `local_api_key` is set → inbound must match it (resolved provider keys are also accepted)
2. Else if any provider has an `api_key` → Bearer / `x-api-key` must match one of them
3. If all empty → trusted local mode, no auth
4. `/api/*`: non-loopback bind requires `admin_api_key` or `local_api_key`; when set, send `Authorization` or `X-Admin-Key`

Environment variables:

| Variable | Meaning |
|---|---|
| `UR_CONFIG` | Path to the config file |
| `UR_LOG_DB` | SQLite log path, default `data/access.db` |
| `UR_CORS_ORIGINS` | Comma-separated CORS origins |
| `UR_MAX_BODY` | Request body limit, default 4MB |

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

## Layout

```
app/            FastAPI gateway, IR, converters, upstream client
static/         Admin UI
tests/          Unit / API tests
config.example.json
```

## Notes and limits

- Same-protocol streams are passed through as SSE; cross-protocol streams are rewritten (text + thinking + multi tool-call fragments + finish + errors)
- Responses inbound (cross-protocol) emits `created / in_progress / output_item / delta / done / completed|failed`
- Retryable status codes: 408 / 409 / 429 / 500 / 502 / 503 / 504 / 529; streaming failovers only before the first byte (including first-token timeout)
- Provider-specific extras are allow-listed so Chat fields such as `n` do not leak into Anthropic and cause 400s
- Anthropic upstream defaults `max_tokens` to 4096 when omitted
- Chat/Messages still use IR messages; the Responses path uses items (`function_call` / `function_call_output` / `reasoning`) instead of stuffing more fields onto `IRMessage`
- This is a local gateway and binds `127.0.0.1` by default; if you bind `0.0.0.0`, set `admin_api_key` or `local_api_key`
- Logs and error payloads redact keys; prefer `api_key` as `env:NAME` instead of storing plaintext in `config.json`
