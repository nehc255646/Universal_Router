# Universal Router

[中文](README.md) | **English**

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Version](https://img.shields.io/badge/version-1.0.0-purple)
![Tests](https://img.shields.io/badge/tests-62%20passed-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

A **local three-protocol gateway**: clients can speak any API dialect, upstreams can require any protocol — everything in between is converted automatically.

Point clients that only support one API (Cursor, ChatBox, Claude Code, your own scripts…) at any upstream (official APIs, relays, OpenRouter, DeepSeek…), without caring which protocol the client speaks.

```
                 ┌─────────────────────────────────────────────┐
                 │               Universal Router              │
                 │                                             │
 OpenAI clients ─►  /v1/chat/completions ─┐                    │
 Responses clients►  /v1/responses ───────┼─► IR (intermediate)┐  │ ──► chat_completions
 Claude clients ─►  /v1/messages ─────────┘                    └──┼───► responses      ──► any Provider
                 │           auth / routing / retry / breaker    │ ──► messages
                 └─────────────────────────────────────────────┘
```

- Repo: https://github.com/nehc255646/Universal_Router
- Default listen: `127.0.0.1:8787` · MIT License

## What problem it solves

| Scenario | Universal Router's answer |
|---|---|
| Claude Code speaks Anthropic, but you only have an OpenAI relay | Inbound `messages` → upstream `chat_completions`, zero client changes |
| Cursor only supports OpenAI, but you want official Claude | Inbound `chat` → upstream `messages` |
| Multiple upstreams (official + relay + DeepSeek) with automatic switching and failover | 6 routing strategies + failover + circuit breaker |
| You want per-request token usage and cost | SQLite logs: status, latency, attempts, usage, cost estimate |

**All 9 inbound × upstream combinations are supported**; cross-protocol requests go through an IR intermediate representation that preserves text, thinking/reasoning, images, and function/tool calls:

| Inbound ↓ · Upstream → | chat_completions | responses | messages |
|---|:---:|:---:|:---:|
| **chat** (`/v1/chat/completions`) | ✅ pass-through | ✅ converted | ✅ converted |
| **responses** (`/v1/responses`) | ✅ converted | ✅ pass-through | ✅ converted |
| **messages** (`/v1/messages`) | ✅ converted | ✅ converted | ✅ pass-through |

Cross-protocol streaming rewrites the SSE event flow entirely (including the full Responses event lifecycle `created → in_progress → output_item → delta → done → completed|failed` and fragmented tool-call arguments) — to the client it looks like a native protocol.

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

**Docker**:

```bash
docker compose up -d --build
```

First run in three steps:

1. Open the admin page `http://127.0.0.1:8787/`
2. Add a provider (presets available, or fetch the model list from upstream), then run a connectivity test
3. Point your client's API URL at `http://127.0.0.1:8787/v1` with the gateway key (see [Auth rules](#auth-rules))

## Connecting clients

**OpenAI SDK / any OpenAI-compatible client**:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-local")
r = client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
print(r.choices[0].message.content)
```

**Claude Code** (even when the upstream speaks OpenAI):

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_AUTH_TOKEN=sk-local
claude
```

**Cursor / ChatBox** and other GUI clients: set the API URL to `http://127.0.0.1:8787/v1`, use the gateway key, and pick a model configured in the admin page.

**Quick curl check**:

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-local" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'

# Anthropic-style inbound (x-api-key also accepted)
curl http://127.0.0.1:8787/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-local" \
  -d '{"model":"gpt-4o","max_tokens":256,"messages":[{"role":"user","content":"hi"}]}'
```

## Auth rules

| State | Inbound `/v1/*` behavior |
|---|---|
| `server.local_api_key` is set | Requests must carry a matching key via `Authorization: Bearer` or `x-api-key` |
| Unset, but some provider has an `api_key` | Requests must match one of them (clients can reuse the upstream key) |
| Both empty | Local trusted, no auth |

`/api/*` (admin endpoints, incl. the built-in playground) uses `admin_api_key` or `local_api_key`; binding `0.0.0.0` requires one of them. **A dedicated `local_api_key` is recommended** so clients never need the upstream key.

Keys may reference environment variables: `env:OPENAI_API_KEY` / `${OPENAI_API_KEY}` / `$OPENAI_API_KEY`, avoiding plaintext on disk. Logs and error echoes are redacted automatically.

## Routing & reliability

| Strategy | Behavior |
|---|---|
| `priority` (default) | By priority; first within a tier, fall through on failure |
| `round_robin` | Even rotation within a tier |
| `weighted` | Weighted by `weight` within a tier |
| `latency` | Prefer the provider with the lowest latency |
| `health` | Prefer the provider with the highest health score |
| `cost` | Prefer the cheapest provider |

- **Retry & failover**: `retry_count` extra tries per provider, then cross-provider failover; retryable statuses 408/409/429/500/502/503/504/529
- **Circuit breaker**: opens after `circuit_fail_threshold` consecutive failures, half-open single probe after `circuit_cooldown_s`
- **Layered timeouts**: connect / first streaming byte / idle between chunks, all configurable
- **Disconnect cancel**: client disconnects cancel the upstream request — no wasted tokens
- **Model routing**: request `model` or a `provider/model` prefix; providers sharing a model automatically form a failover pool

## Management API reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check (version, provider count) |
| `GET` | `/v1/models` | Model list (OpenAI format) |
| `POST` | `/v1/chat/completions` · `/v1/responses` · `/v1/messages` | Three inbound protocols |
| `GET` / `PUT` | `/api/config` | Read / update server config |
| `GET` / `POST` | `/api/providers` | List / create providers |
| `PUT` / `DELETE` | `/api/providers/{pid}` | Update / delete a provider |
| `POST` | `/api/providers/{pid}/test` | Connectivity test |
| `POST` | `/api/providers/{pid}/models/fetch` | Fetch the model list from upstream |
| `GET` / `DELETE` | `/api/logs` | View / clear request logs |
| `GET` | `/api/health/providers` · `/api/status` | Health snapshots / runtime status |
| `POST` | `/api/play/{chat\|responses\|messages}` | Built-in playground (admin auth) |

## Configuration reference

`config.json` (gitignored — never commit keys):

| Field | Description |
|---|---|
| `server.host` / `server.port` | Listen address; restart to apply |
| `server.local_api_key` | Inbound gateway key, see auth rules |
| `server.admin_api_key` | Admin API key; required (or `local_api_key`) when bound to `0.0.0.0` |
| `server.route_strategy` | See routing strategy table |
| `server.retry_count` / `retry_backoff_ms` | Extra retries per provider and backoff |
| `server.failover` | Try the next matching provider after failure |
| `server.connect_timeout_s` / `first_token_timeout_s` / `read_idle_timeout_s` | Layered timeouts; 0 disables the streaming ones |
| `server.circuit_breaker` / `circuit_fail_threshold` / `circuit_cooldown_s` | Circuit breaker settings |
| `server.log_retain` | Log retention count (100–100000) |
| `providers[].id` | `a-z0-9-_`, used as the `provider/model` prefix |
| `providers[].base_url` | Upstream root including `/v1` |
| `providers[].api_key` | Plain text, or an `env:NAME` reference (recommended) |
| `providers[].upstream_mode` | `chat_completions` / `responses` / `messages` |
| `providers[].models` | Used for routing; fetchable from upstream in the admin page |
| `providers[].enabled` / `priority` / `weight` | Disabled, lower = higher priority, weight within a tier |
| `providers[].headers` | Extra upstream headers (values also support env references) |
| `providers[].cost_input_per_1m` / `cost_output_per_1m` | Cost routing and expense estimation |

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
pytest -q        # 62 tests
ruff check app tests
```

Layout:

```
app/            FastAPI gateway, IR, protocol converters, routing, upstream forwarding
static/         Web admin frontend (vanilla JS, no build step)
tests/          Unit / API tests
config.example.json
```

## Troubleshooting

| Symptom | Cause & fix |
|---|---|
| `Missing Authorization: Bearer <API Key> or x-api-key` | Inbound requires a gateway key; set `local_api_key` and use it, or reuse a provider key |
| Connectivity test fails on `max_output_tokens` | Some upstreams enforce parameter minimums (e.g. `>= 16`); the gateway test already sends 16 — check the upstream docs if it still fails |
| Upstream 403 region restriction | Not a gateway issue; make sure your egress IP region is supported by the upstream |
| Config save says the lock is held | Cross-process file lock prevents concurrent writes; wait or remove a stale `config.json.lock` and retry |
| Admin page can't reach the API | When bound off-loopback you must set `admin_api_key` / `local_api_key`, then unlock in the page header |

## Notes & limitations

- Streaming fails over only before the first upstream byte (incl. first-token timeout); errors after that arrive as in-band SSE error events
- Some upstream-specific fields pass through a whitelist, so Chat's `n` etc. don't leak to Anthropic and cause 400s
- Anthropic upstreams default `max_tokens` to 4096 when unspecified
- Streaming uses incremental UTF-8 decoding — multi-byte characters survive chunk boundaries; config writes use a cross-process file lock with rollback
- This is a local gateway bound to `127.0.0.1` by default; if you change to `0.0.0.0`, set `admin_api_key` or `local_api_key`

## License

[MIT](LICENSE)
