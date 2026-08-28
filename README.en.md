# Universal Router v1.0

[中文](README.md) | **English**

A **local three-protocol gateway**: convert inbound OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages requests into the protocol the upstream actually supports, then forward them.

Use it to point clients that speak only one API (Cursor, ChatBox, Claude Code, your own scripts) at any upstream (official APIs, relays, OpenRouter, DeepSeek, …).

```
Client ── chat / responses / messages ──► Universal Router ── any upstream protocol ──► Provider
```

- Repo: https://github.com/nehc255646/Universal_Router
- Default listen: `127.0.0.1:8787` · MIT License

## Why v1.0

After several rounds of hardening and real-world use, the core pipeline is stable:

- 61 unit/API tests covering converters, routing, auth, circuit breaker, streaming, and logs
- Streaming parser uses incremental UTF-8 decoding — multi-byte characters survive network chunk boundaries
- Config writes use a cross-process file lock with rollback on failure; token comparison is constant-time
- Half-open circuit state allows a single concurrent probe; log cleanup is throttled instead of counting rows on every request

## Features

### Protocol conversion
- Inbound `/v1/chat/completions` · `/v1/responses` · `/v1/messages`; upstream `chat_completions` / `responses` / `messages` per provider
- IR-based translation: text, thinking/reasoning, images, function/tool calls
- Same-protocol SSE pass-through; cross-protocol streams are rewritten, replicating the full Responses event lifecycle (`created → in_progress → output_item → delta → done → completed|failed`) and fragmented tool-call arguments

### Routing & reliability
- Six routing strategies: `priority` / `round_robin` / `weighted` / `latency` / `health` / `cost`
- Retries + cross-provider failover; retryable statuses 408/409/429/500/502/503/504/529
- Circuit breaker: opens after consecutive failures, half-open single probe after cooldown
- Layered timeouts: connect / first-token / idle-between-chunks, all configurable
- Client disconnects cancel upstream requests; model routing accepts `model` or `provider/model`

### Management & security
- Web UI: provider CRUD, presets, fetch model lists from upstream, connectivity test, playground
- SQLite-backed request logs (status, latency, attempts, token usage, cost estimate)
- Inbound auth + admin API auth; keys support `env:NAME` / `${NAME}` environment references
- Keys are redacted from logs and error echoes; request body size limit (incl. chunked)

## Quick start

Requires Python 3.11+.

**Windows**: run `start.bat`, then open the admin page via `入口.url`.

**macOS / Linux**:

```bash
chmod +x start.sh
./start.sh
```

Or manually:

```bash
pip install -r requirements.txt
# Optional: copy the example config and fill in keys
cp config.example.json config.json
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

Open http://127.0.0.1:8787/ to add providers; the gateway base URL is `http://127.0.0.1:8787/v1`.

**Docker**:

```bash
docker compose up -d --build
```

Config and logs persist via volume mounts; when binding `0.0.0.0` you must set `admin_api_key` or `local_api_key`.

## Usage examples

Chat Completions:

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-local" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'
```

Python (openai SDK):

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-local")
r = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "hi"}],
)
print(r.choices[0].message.content)
```

Anthropic-style inbound (even when the upstream is OpenAI):

```bash
curl http://127.0.0.1:8787/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-local" \
  -d '{"model":"gpt-4o","max_tokens":256,"messages":[{"role":"user","content":"hi"}]}'
```

## Configuration

`config.json` (gitignored — never commit keys):

| Field | Description |
|---|---|
| `server.host` / `server.port` | Listen address; restart to apply |
| `server.local_api_key` | If set, inbound requests must match this key exactly via `Authorization: Bearer` or `x-api-key` |
| `server.admin_api_key` | Protects `/api/*`; required (or `local_api_key`) when bound to `0.0.0.0` |
| `server.route_strategy` | `priority` / `round_robin` / `weighted` / `latency` / `health` / `cost` |
| `server.retry_count` / `retry_backoff_ms` | Extra retries per provider and backoff |
| `server.failover` | Try the next matching provider after failure |
| `server.connect_timeout_s` / `first_token_timeout_s` / `read_idle_timeout_s` | Layered timeouts; 0 disables the streaming ones |
| `server.circuit_breaker` / `circuit_fail_threshold` / `circuit_cooldown_s` | Circuit breaker settings |
| `server.log_retain` | Log retention count (100–100000) |
| `providers[].api_key` | Plain text, or `env:OPENAI_API_KEY` / `${OPENAI_API_KEY}` (recommended) |
| `providers[].cost_input_per_1m` / `cost_output_per_1m` | Cost routing and expense estimation |
| `providers[].id` | `a-z0-9-_`, used as the `provider/model` prefix |
| `providers[].enabled` / `priority` / `weight` | Disabled, lower = higher priority, weight within a priority tier |
| `providers[].base_url` | Upstream root including `/v1` |
| `providers[].upstream_mode` | `chat_completions` / `responses` / `messages` |
| `providers[].models` | Used for routing; fetch from upstream in the admin page |
| `providers[].headers` | Extra upstream headers (values also support env references) |

Auth rules (tightened in v1.0):

1. If `local_api_key` is set → inbound requests must match it exactly
2. Otherwise, if any provider has an `api_key` → inbound must match one of them (clients reuse the upstream key)
3. If both are empty → local trusted, no auth
4. `/api/*`: when bound off-loopback, `admin_api_key` or `local_api_key` is mandatory; if set, requests need `Authorization` or `X-Admin-Key`

Environment variables:

| Variable | Description |
|---|---|
| `UR_CONFIG` | Config file path |
| `UR_LOG_DB` | SQLite log path, default `data/access.db` |
| `UR_CORS_ORIGINS` | Comma-separated CORS origins |
| `UR_MAX_BODY` | Request body limit, default 4MB |

## Development

```bash
pip install -e ".[dev]"
pytest -q        # 61 tests
ruff check app tests
```

Layout:

```
app/            FastAPI gateway, IR, protocol converters, routing, upstream forwarding
static/         Web admin frontend
tests/          Unit / API tests
config.example.json
```

## Notes & limitations

- Streaming fails over only before the first upstream byte (incl. first-token timeout); errors after that are delivered as in-band SSE error events
- Some upstream-specific fields are passed through via a whitelist, so Chat's `n` etc. don't leak to Anthropic and cause 400s
- Anthropic upstreams default `max_tokens` to 4096 when unspecified
- Config writes use a cross-process file lock; a stuck or stale lock fails loudly instead of silently dropping the write
- This is a local gateway bound to `127.0.0.1` by default; if you change to `0.0.0.0`, set `admin_api_key` or `local_api_key`
- Prefer `env:NAME` references for `api_key` instead of plaintext in `config.json`
