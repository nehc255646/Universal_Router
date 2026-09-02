# Universal Router

**中文** | [English](README.en.md)

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Version](https://img.shields.io/badge/version-1.1.0-purple)
![Tests](https://img.shields.io/badge/tests-83%20passed-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

本地 **三协议互转网关**：客户端用哪种 API 形态都行，上游只认哪种协议都行 —— 中间全部自动转换。

把只支持某一种 API 的客户端（Cursor、ChatBox、Claude Code、自写脚本…）接到任意上游（官方 API、中转站、OpenRouter、DeepSeek…），不再受"客户端吃什么协议"的限制。

```
                 ┌─────────────────────────────────────────────┐
                 │               Universal Router              │
                 │                                             │
 OpenAI 客户端 ──►  /v1/chat/completions ─┐                    │
 Responses 客户端 ►  /v1/responses ───────┼─► IR 中间表示 ──┐  │ ──► chat_completions
 Claude 客户端 ──►  /v1/messages ─────────┘                 └──┼───► responses      ──► 任意 Provider
                 │            鉴权 / 路由 / 重试 / 熔断          │ ──► messages
                 └─────────────────────────────────────────────┘
```

- 仓库：https://github.com/nehc255646/Universal_Router
- 默认监听：`127.0.0.1:8787` · MIT License

## 它解决什么问题

| 你遇到的场景 | Universal Router 的答案 |
|---|---|
| Claude Code 只会发 Anthropic 请求，但手头只有 OpenAI 中转站 | 入站 `messages` → 上游 `chat_completions`，客户端零改动 |
| Cursor 只支持 OpenAI 协议，想用官方 Claude | 入站 `chat` → 上游 `messages` |
| 多个上游（官方 + 中转 + DeepSeek），想自动切换、故障兜底 | 6 种路由策略 + failover + 熔断器 |
| 想知道每个请求花了多少 token、多少钱 | SQLite 日志：状态、延迟、尝试次数、用量、成本估算 |

**9 种入站×上游组合全部支持**，跨协议时经 IR 中间表示转换，文本、thinking/reasoning、多模态图片、function/tool_calls 均保留：

| 入站 ↓ · 上游 → | chat_completions | responses | messages |
|---|:---:|:---:|:---:|
| **chat** (`/v1/chat/completions`) | ✅ 透传 | ✅ 转换 | ✅ 转换 |
| **responses** (`/v1/responses`) | ✅ 转换 | ✅ 透传 | ✅ 转换 |
| **messages** (`/v1/messages`) | ✅ 转换 | ✅ 转换 | ✅ 透传 |

跨协议流式请求会完整重写 SSE 事件流（含 Responses 事件生命周期 `created → in_progress → output_item → delta → done → completed|failed` 与 tool call 分片），对客户端而言就像原生协议。

## 快速开始

需要 Python 3.11+。

**Windows**：双击 `start.bat`，再用 `入口.url` 打开管理页。

**macOS / Linux**：

```bash
chmod +x start.sh
./start.sh
```

或手动：

```bash
pip install -r requirements.txt
# 可选：复制示例配置并填入密钥
cp config.example.json config.json
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

**Docker**：

```bash
docker compose up -d --build
```

首次使用三步走：

1. 打开管理页 `http://127.0.0.1:8787/`
2. 添加提供商（可用预设模板，或「从上游拉取」模型列表），点「连通测试」验证
3. 把客户端的 API 地址指向 `http://127.0.0.1:8787/v1`，Key 用网关 Key（见[鉴权规则](#鉴权规则)）

## 接入客户端

**OpenAI SDK / 任意 OpenAI 兼容客户端**：

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-local")
r = client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "你好"}])
print(r.choices[0].message.content)
```

**Claude Code**（即使上游是 OpenAI 协议）：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_AUTH_TOKEN=sk-local
claude
```

**Cursor / ChatBox** 等图形客户端：API 地址填 `http://127.0.0.1:8787/v1`，API Key 填网关 Key，模型选管理页里配置的模型即可。

**curl 直接验证**：

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-local" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"你好"}]}'

# Anthropic 形态入站（x-api-key 同样接受）
curl http://127.0.0.1:8787/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-local" \
  -d '{"model":"gpt-4o","max_tokens":256,"messages":[{"role":"user","content":"你好"}]}'
```

## 鉴权规则

| 状态 | 入站 `/v1/*` 行为 |
|---|---|
| 设置了 `server.local_api_key` | 必须携带匹配该 Key 的 `Authorization: Bearer` 或 `x-api-key` |
| 未设置网关 Key，但某 provider 配了 `inbound_key` | 必须匹配该提供商的调用密钥 |
| 未设置网关 Key 与调用密钥，但配了 `api_key` | 必须匹配上游 Key 之一（客户端可直接复用） |
| 都为空 | 本地可信，不鉴权 |

`/api/*`（管理接口，含内置试聊）使用 `admin_api_key` 或 `local_api_key` 鉴权；绑定 `0.0.0.0` 时必须设置其中之一，否则除 `/health` 外全部锁定。**推荐单独设置 `local_api_key`**；提供商还可设 `inbound_key` 作为该渠道的调用密钥。

密钥支持引用环境变量：`env:OPENAI_API_KEY` / `${OPENAI_API_KEY}` / `$OPENAI_API_KEY`，避免明文落盘。日志与错误回显自动脱敏。

## 路由与可靠性

| 策略 | 行为 |
|---|---|
| `priority`（默认） | 按优先级，同级取第一个；失败顺延 |
| `round_robin` | 同级轮询分摊 |
| `weighted` | 同级按 `weight` 加权 |
| `latency` | 优先延迟最低的提供商 |
| `health` | 优先健康分最高的提供商 |
| `cost` | 优先成本最低的提供商 |

- **重试与 failover**：同一提供商重试 `retry_count` 次，失败后跨提供商切换；可重试状态码 408/409/429/500/502/503/504/529
- **熔断器**：连续失败 `circuit_fail_threshold` 次后打开，冷却 `circuit_cooldown_s` 后半开、单探测恢复
- **分层超时**：connect / 流式首字节 / 相邻 chunk 空闲，各自可配置
- **断连取消**：客户端断开时自动取消上游请求，不浪费 token
- **模型路由**：请求 `model` 或 `provider/model` 前缀均可；多个提供商配同一模型即自动组成备选池
- **模型别名**：`models[].upstream_id` 把客户端 id 映射成上游真实模型名（例如客户端 `gpt-4o` → 上游 `deepseek-chat`）
- **请求追踪**：响应头 `X-Request-Id`（可自带），失败日志含协议路径与脱敏摘要；提供商健康/熔断统计落 SQLite，重启不丢

## 管理 API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查（含版本号、提供商数） |
| `GET` | `/v1/models` | 模型列表（OpenAI 格式） |
| `POST` | `/v1/chat/completions` · `/v1/responses` · `/v1/messages` | 三协议入站 |
| `GET` / `DELETE` | `/v1/responses/{id}` | 透传到 responses 上游；无此类上游则 400 |
| `POST` | `/v1/messages/count_tokens` | Anthropic 计 token；非 messages 上游则估算 |
| `GET` / `PUT` | `/api/config` | 读取 / 更新 server 配置 |
| `GET` / `POST` | `/api/providers` | 列出 / 新增提供商 |
| `PUT` / `DELETE` | `/api/providers/{pid}` | 修改 / 删除提供商 |
| `POST` | `/api/providers/{pid}/test` | 连通测试 |
| `POST` | `/api/providers/{pid}/models/fetch` | 从上游拉取模型列表 |
| `GET` / `DELETE` | `/api/logs` | 查看 / 清空请求日志 |
| `GET` | `/api/health/providers` · `/api/status` | 健康快照 / 运行状态 |
| `POST` | `/api/play/{chat\|responses\|messages}` | 管理页内置试聊（走管理鉴权） |

## 配置参考

`config.json`（已被 gitignore，勿提交密钥）：

| 字段 | 说明 |
|---|---|
| `server.host` / `server.port` | 监听地址，改完需重启 |
| `server.local_api_key` | 入站网关 Key，见鉴权规则 |
| `server.admin_api_key` | 管理 API Key；绑定 `0.0.0.0` 时必须设置此项或 `local_api_key` |
| `server.route_strategy` | 见路由策略表 |
| `server.retry_count` / `retry_backoff_ms` | 同提供商额外重试次数与退避 |
| `server.failover` | 失败后尝试下一个匹配提供商 |
| `server.connect_timeout_s` / `first_token_timeout_s` / `read_idle_timeout_s` | 分层超时，流式两项 0 为关闭 |
| `server.circuit_breaker` / `circuit_fail_threshold` / `circuit_cooldown_s` | 熔断器参数 |
| `server.log_retain` | 日志保留条数（100–100000） |
| `providers[].id` | `a-z0-9-_`，用于 `provider/model` 前缀 |
| `providers[].base_url` | 含 `/v1` 的上游根路径 |
| `providers[].api_key` | 明文，或 `env:NAME` 引用（推荐） |
| `providers[].upstream_mode` | `chat_completions` / `responses` / `messages` |
| `providers[].models` | 用于路由；`upstream_id` 可选，发给上游的真实模型 id |
| `providers[].inbound_key` | 客户端调用密钥；为空则回退上游 `api_key` |
| `providers[].enabled` / `priority` / `weight` | 停用、越小越优先、同优先级权重 |
| `providers[].headers` | 额外上游请求头（值同样支持 env 引用） |
| `providers[].cost_input_per_1m` / `cost_output_per_1m` | 成本路由与费用估算 |

环境变量：

| 变量 | 说明 |
|---|---|
| `UR_CONFIG` | 配置文件路径 |
| `UR_LOG_DB` | SQLite 日志路径，默认 `data/access.db` |
| `UR_CORS_ORIGINS` | 逗号分隔的 CORS 来源 |
| `UR_MAX_BODY` | 请求体上限，默认 4MB |

## 开发

```bash
pip install -e ".[dev]"
pytest -q
ruff check app tests
```

目录结构：

```
app/            FastAPI 网关、IR、协议转换器、路由、上游转发
static/         Web 管理前端（原生 JS，无构建步骤）
tests/          单元 / API 测试
config.example.json
```

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| `缺少 Authorization: Bearer <API Key> 或 x-api-key` | 入站需要网关 Key；设置 `local_api_key` 后用它，或复用 provider 的 Key |
| 连通测试报 `max_output_tokens` 参数错误 | 部分上游对参数有最小值要求（如 `>= 16`）；网关测试已按 16 发送，若仍报错请检查上游文档 |
| 上游 403 地区限制 | 与网关无关；确认出口 IP 所在地区被上游支持 |
| 保存配置时提示锁被占用 | 跨进程文件锁防并发写；等待或清理残留的 `config.json.lock` 后重试 |
| 管理页打不开 API | 绑定非本机时需设置 `admin_api_key` / `local_api_key`，页面右上角解锁 |

## 说明与限制

- 流式仅在与上游交换首字节前 failover（含 first-token 超时）；首字节后的错误以带内 error 事件下发
- `previous_response_id` / `GET /v1/responses/{id}` 需要 responses 上游；跨协议无法伪造服务端会话
- 管理页静态资源在 `static/vendor/`，不依赖外网 CDN
- 部分上游专有字段按白名单透传，避免 Chat 的 `n` 等泄漏到 Anthropic 导致 400
- Anthropic 上游若未指定 `max_tokens`，默认补 4096
- 流式解析使用增量 UTF-8 解码，多字节字符跨网络分块不乱码；配置写入带跨进程文件锁与变更回滚
- 这是本地网关，默认只绑 `127.0.0.1`；改成 `0.0.0.0` 必须设置 `admin_api_key` 或 `local_api_key`

## License

[MIT](LICENSE)
