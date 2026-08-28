# Universal Router v1.0

**中文** | [English](README.en.md)

本地 **三协议互转网关**：把客户端的 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages 请求，转换成上游真正支持的协议再转发。

适合把只支持某一种 API 的客户端（Cursor、ChatBox、Claude Code、自写脚本等）接到任意上游（官方 API、中转站、OpenRouter、DeepSeek…）。

```
客户端 ── chat / responses / messages ──► Universal Router ── 任一上游协议 ──► Provider
```

- 仓库：https://github.com/nehc255646/Universal_Router
- 默认监听：`127.0.0.1:8787` · MIT License

## 为什么是 v1.0

经过多轮加固与实战验证，核心链路已达到稳定可用：

- 61 个单元 / API 测试覆盖转换器、路由、鉴权、熔断、流式与日志
- 流式解析使用增量 UTF-8 解码，多字节字符跨网络分块不乱码
- 配置写入带跨进程文件锁与变更回滚；密钥比较使用常数时间算法
- 熔断半开状态限制单并发探测；访问日志清理节流，不再每请求全表计数

## 功能

### 协议互转
- 入站 `/v1/chat/completions` · `/v1/responses` · `/v1/messages` 三选一，上游 `chat_completions` / `responses` / `messages` 按提供商配置
- 经 IR 中间表示互转：文本、thinking/reasoning、多模态图片、function/tool_calls
- 同协议 SSE 透传；跨协议重写事件流，完整复刻 Responses 事件生命周期（`created → in_progress → output_item → delta → done → completed|failed`）与 tool call 分片

### 路由与可靠性
- 六种路由策略：`priority` / `round_robin` / `weighted` / `latency` / `health` / `cost`
- 失败重试 + 跨提供商 failover；可重试状态码 408/409/429/500/502/503/504/529
- 熔断器：连续失败打开，冷却后半开单探测恢复
- 分层超时：connect / 流式首字节 / 相邻 chunk 空闲，均可配置
- 客户端断连自动取消上游请求；模型路由支持 `model` 或 `provider/model` 前缀

### 管理与安全
- Web 管理页：提供商 CRUD、预设模板、从上游拉取模型列表、连通测试、试聊
- SQLite 持久化请求日志（状态、延迟、尝试次数、token 用量、成本估算）
- 入站鉴权 + 管理 API 鉴权；密钥支持 `env:NAME` / `${NAME}` 引用环境变量
- 日志与错误回显自动脱敏 Key；请求体大小上限（含 chunked）

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

打开 http://127.0.0.1:8787/ 添加提供商，网关地址即 `http://127.0.0.1:8787/v1`。

**Docker**：

```bash
docker compose up -d --build
```

配置与日志通过卷挂载持久化；绑定 `0.0.0.0` 时必须设置 `admin_api_key` 或 `local_api_key`。

## 接入示例

Chat Completions：

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-local" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"你好"}]}'
```

Python（openai SDK）：

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-local")
r = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "你好"}],
)
print(r.choices[0].message.content)
```

Anthropic 形态入站（即使上游是 OpenAI）：

```bash
curl http://127.0.0.1:8787/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-local" \
  -d '{"model":"gpt-4o","max_tokens":256,"messages":[{"role":"user","content":"你好"}]}'
```

## 配置

`config.json`（已被 gitignore，勿提交密钥）：

| 字段 | 说明 |
|---|---|
| `server.host` / `server.port` | 监听地址，改完需重启 |
| `server.local_api_key` | 非空则入站必须 `Authorization: Bearer` 或 `x-api-key` 精确匹配此 Key |
| `server.admin_api_key` | 保护 `/api/*`；绑定 `0.0.0.0` 时必须设置此项或 `local_api_key` |
| `server.route_strategy` | `priority` / `round_robin` / `weighted` / `latency` / `health` / `cost` |
| `server.retry_count` / `retry_backoff_ms` | 同一提供商额外重试次数与退避 |
| `server.failover` | 失败后尝试下一个匹配提供商 |
| `server.connect_timeout_s` / `first_token_timeout_s` / `read_idle_timeout_s` | 分层超时，流式两项 0 为关闭 |
| `server.circuit_breaker` / `circuit_fail_threshold` / `circuit_cooldown_s` | 熔断器参数 |
| `server.log_retain` | 日志保留条数（100–100000） |
| `providers[].api_key` | 明文，或 `env:OPENAI_API_KEY` / `${OPENAI_API_KEY}`（推荐） |
| `providers[].cost_input_per_1m` / `cost_output_per_1m` | 成本路由与费用估算 |
| `providers[].id` | `a-z0-9-_`，用于 `provider/model` 前缀 |
| `providers[].enabled` / `priority` / `weight` | 停用、越小越优先、同优先级权重 |
| `providers[].base_url` | 含 `/v1` 的上游根路径 |
| `providers[].upstream_mode` | `chat_completions` / `responses` / `messages` |
| `providers[].models` | 用于路由；可在管理页「从上游拉取」 |
| `providers[].headers` | 额外上游请求头（值同样支持 env 引用） |

鉴权规则（v1.0 起收紧）：

1. 配置了 `local_api_key` → 入站必须精确匹配该 Key
2. 否则若任意 provider 配置了 `api_key` → 入站须匹配其中之一（客户端直接复用上游 Key）
3. 都为空 → 本地可信，不鉴权
4. `/api/*`：绑定非本机时必须设置 `admin_api_key` 或 `local_api_key`；已设置则需 `Authorization` 或 `X-Admin-Key`

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
pytest -q        # 61 tests
ruff check app tests
```

目录结构：

```
app/            FastAPI 网关、IR、协议转换器、路由、上游转发
static/         Web 管理前端
tests/          单元 / API 测试
config.example.json
```

## 说明与限制

- 流式仅在与上游交换首字节前 failover（含 first-token 超时）；首字节后的错误会以带内 error 事件下发
- 部分上游专有字段按白名单透传，避免 Chat 的 `n` 等泄漏到 Anthropic 导致 400
- Anthropic 上游若未指定 `max_tokens`，默认补 4096
- 配置写入使用跨进程文件锁；锁被长期占用或残留时保存会明确报错，不会静默丢写
- 这是本地网关，默认只绑 `127.0.0.1`；改成 `0.0.0.0` 必须设置 `admin_api_key` 或 `local_api_key`
- 建议把 `api_key` 写成 `env:NAME` 引用环境变量，而不是明文写入 `config.json`
