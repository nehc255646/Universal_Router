# Universal Router

本地 **三协议互转网关**：把客户端的 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages 请求，转换成上游真正支持的协议再转发。

适合把只支持某一种 API 的客户端（Cursor、ChatBox、自写脚本、Claude Code 等）接到任意上游（官方、中转、OpenRouter、DeepSeek…）。

- 仓库：https://github.com/nehc255646/Universal_Router
- 默认监听：`127.0.0.1:8787`
- 版本：0.2.0 · MIT

## 功能

- 入站 `/v1/chat/completions` · `/v1/responses` · `/v1/messages`
- 上游 `chat_completions` / `responses` / `messages`，按提供商配置
- 经 IR 中间表示互转：文本、多模态图片、function/tool_calls（含流式文本与工具增量）
- Web 管理页：提供商 CRUD、预设、拉取模型、连通测试、试聊、请求日志、curl/Python 示例
- 可选本地鉴权；管理 API 返回时脱敏 `api_key`
- 模型路由支持 `model` 或 `provider/model`

```
客户端  --chat/responses/messages-->  Universal Router  --任一上游协议-->  Provider
```

## 快速开始

需要 Python 3.11+。

Windows：

```bat
start.bat
```

macOS / Linux：

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

打开 http://127.0.0.1:8787/ 添加提供商。网关地址为 http://127.0.0.1:8787/v1 。

## 接入示例

Chat Completions：

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-local" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"你好"}]}'
```

Python（`openai` SDK）：

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
| `server.local_api_key` | 非空则入站必须 `Authorization: Bearer` 或 `x-api-key` 匹配 |
| `providers[].id` | `a-z0-9-_`，用于 `provider/model` 前缀 |
| `providers[].base_url` | 含 `/v1` 的上游根路径 |
| `providers[].upstream_mode` | `chat_completions` / `responses` / `messages` |
| `providers[].models` | 用于路由；可用管理页「从上游拉取」 |

鉴权规则：

1. 配置了 `local_api_key` → 必须匹配（也接受任一 provider key）
2. 否则若任意 provider 有 `api_key` → Bearer / x-api-key 须匹配其中之一
3. 都为空 → 本地可信，不鉴权

环境变量：

| 变量 | 说明 |
|---|---|
| `UR_CONFIG` | 配置文件路径 |
| `UR_CORS_ORIGINS` | 逗号分隔的 CORS 来源 |
| `UR_MAX_BODY` | 请求体上限，默认 4MB |

## 开发

```bash
pip install -e ".[dev]"
pytest -q
```

## 目录

```
app/            FastAPI 网关、IR、转换器、上游转发
static/         管理前端
tests/          单元 / API 测试
config.example.json
```

## 说明与限制

- 同协议默认 SSE 透传；跨协议会重写事件流（文本 + tool_calls + finish）
- 部分上游专有字段按白名单透传，避免 Chat 的 `n` 等泄漏到 Anthropic 导致 400
- Anthropic 上游若未指定 `max_tokens`，默认补 4096
- 这是本地网关，默认只绑 `127.0.0.1`；若改成 `0.0.0.0` 请务必设置 `local_api_key`
