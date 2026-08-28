"""管理 API + 代理 API"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import AppConfig, ProviderConfig, config_manager
from .converter.chat_anthropic import anthropic_to_ir, ir_to_anthropic, ir_response_to_anthropic
from .converter.chat_responses import (
    chat_to_ir,
    ir_to_chat,
    ir_to_responses,
    ir_response_to_chat,
    ir_response_to_responses,
    responses_to_ir,
)
from .converter.common import parse_sse_line, sse_format
from .ir import IRResponse
from .upstream import post_non_stream, stream_upstream

# ---------- 管理 API ----------

manage_router = APIRouter(prefix="/api")


@manage_router.get("/config")
async def get_config():
    cfg = config_manager.config
    return {"server": cfg.server.model_dump(), "providers": [p.model_dump() for p in cfg.providers]}


@manage_router.put("/config")
async def update_config(body: dict[str, Any]):
    # 仅允许更新 server 段，避免覆盖 providers
    srv = body.get("server")
    if not srv or not isinstance(srv, dict):
        raise HTTPException(400, "需提供 server 配置")
    try:
        from .config import ServerConfig
        new_srv = ServerConfig.model_validate(srv)
    except Exception as e:
        raise HTTPException(400, str(e))
    config_manager.config.server = new_srv
    await config_manager.save()
    return {"server": new_srv.model_dump()}


@manage_router.get("/providers")
async def list_providers():
    return [p.model_dump() for p in config_manager.config.providers]


@manage_router.post("/providers")
async def create_provider(body: dict[str, Any]):
    try:
        p = ProviderConfig.model_validate(body)
    except Exception as e:
        raise HTTPException(400, str(e))
    if config_manager.find_provider(p.id):
        raise HTTPException(409, f"provider id '{p.id}' 已存在")
    config_manager.config.providers.append(p)
    await config_manager.save()
    return p.model_dump()


@manage_router.put("/providers/{pid}")
async def update_provider(pid: str, body: dict[str, Any]):
    idx = next((i for i, x in enumerate(config_manager.config.providers) if x.id == pid), None)
    if idx is None:
        raise HTTPException(404, "provider not found")
    # 允许 id 修改，但需校验
    body["id"] = body.get("id") or pid
    try:
        p = ProviderConfig.model_validate(body)
    except Exception as e:
        raise HTTPException(400, str(e))
    # 若改 id，检查冲突
    if p.id != pid and config_manager.find_provider(p.id):
        raise HTTPException(409, f"provider id '{p.id}' 已存在")
    config_manager.config.providers[idx] = p
    await config_manager.save()
    return p.model_dump()


@manage_router.delete("/providers/{pid}")
async def delete_provider(pid: str):
    idx = next((i for i, x in enumerate(config_manager.config.providers) if x.id == pid), None)
    if idx is None:
        raise HTTPException(404, "provider not found")
    config_manager.config.providers.pop(idx)
    await config_manager.save()
    return {"ok": True}


@manage_router.post("/providers/{pid}/test")
async def test_provider(pid: str, request: Request):
    provider = config_manager.find_provider(pid)
    if not provider:
        raise HTTPException(404, "provider not found")
    if not provider.models:
        raise HTTPException(400, "该提供商未配置 models，无法测试")
    model = provider.models[0].id
    # 根据 upstream_mode 构造最小请求
    t0 = time.perf_counter()
    client: httpx.AsyncClient = request.app.state.httpx_client
    try:
        if provider.upstream_mode == "chat_completions":
            body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4}
        elif provider.upstream_mode == "responses":
            body = {"model": model, "input": [{"role": "user", "content": "ping"}], "max_output_tokens": 4}
        else:
            body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4}
        status, data = await post_non_stream(client, provider, body, timeout=15)
        latency = int((time.perf_counter() - t0) * 1000)
        ok = 200 <= status < 300
        return {"ok": ok, "status": status, "latency_ms": latency, "data": data if isinstance(data, dict) else data.decode(errors="ignore")[:500] if isinstance(data, bytes) else str(data)[:500]}
    except Exception as e:
        latency = int((time.perf_counter() - t0) * 1000)
        return {"ok": False, "latency_ms": latency, "error": str(e)}


# ---------- 代理 API ----------

proxy_router = APIRouter(prefix="/v1")


def _auth_check(request: Request) -> None:
    # 鉴权优先级：
    # 1) 若 ServerConfig.local_api_key 非空，则必须匹配该 key（显式本地网关鉴权）
    # 2) 否则若存在任一 provider.api_key，则 Bearer 需匹配其中之一才放行；无头则 401（收敛原“无头放行”漏洞）
    # 3) 若两者皆空（本地可信模式），则放行
    cfg = config_manager.config.server
    local_key = (cfg.local_api_key or "").strip()
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    token = auth.removeprefix("Bearer ").removeprefix("bearer ").strip() if auth else ""

    if local_key:
        if token == local_key:
            return
        # 也兼容 provider key 误用 local_key 场景：若 token 匹配任一 provider key，仍放行并提示
        if token and token in config_manager.all_api_keys():
            return
        raise HTTPException(401, "无效的 API Key（需匹配 server.local_api_key）")

    keys = config_manager.all_api_keys()
    if not keys:
        # 无任何 provider key 且无 local_key：本地可信，放行
        return
    if not token:
        raise HTTPException(401, "缺少 Authorization: Bearer <API Key>")
    if token in keys:
        return
    raise HTTPException(401, "无效的 API Key")


def _resolve_provider(model: str) -> ProviderConfig:
    # 去除 provider/model 前缀中的 provider 部分用于查找，但保留原始 model 给上游
    p = config_manager.find_provider_by_model(model)
    if not p:
        # 若只有一个 provider，直接使用
        if len(config_manager.config.providers) == 1:
            return config_manager.config.providers[0]
        raise HTTPException(400, f"未知 model '{model}'，请先在管理页配置提供商与模型")
    return p


def _strip_provider_prefix(model: str, provider: ProviderConfig) -> str:
    if model.startswith(provider.id + "/"):
        return model[len(provider.id) + 1 :]
    return model


# ---- 转换 helpers ----


def _to_ir(inbound: str, body: dict[str, Any]):
    if inbound == "chat":
        return chat_to_ir(body)
    if inbound == "responses":
        return responses_to_ir(body)
    if inbound == "messages":
        return anthropic_to_ir(body)
    raise HTTPException(400, f"unknown inbound {inbound}")


def _from_ir_to_upstream(ir, provider: ProviderConfig, stream: bool):
    # 不可变：剥离 provider/model 前缀时复制 IR，避免污染上游重试/日志
    stripped = _strip_provider_prefix(ir.model, provider)
    if stripped != ir.model:
        ir = ir.model_copy(update={"model": stripped})
    mode = provider.upstream_mode
    if mode == "chat_completions":
        return ir_to_chat(ir, stream=stream)
    if mode == "responses":
        return ir_to_responses(ir, stream=stream)
    if mode == "messages":
        return ir_to_anthropic(ir, stream=stream)
    raise HTTPException(500, f"unknown upstream_mode {mode}")


def _upstream_resp_to_inbound(inbound: str, upstream_mode: str, data: dict[str, Any], model: str) -> dict[str, Any]:
    """非流式：上游回包 -> IRResponse -> 入站形态"""
    ir_resp = _upstream_to_ir_response(data, upstream_mode, model)
    if inbound == "chat":
        return ir_response_to_chat(ir_resp)
    if inbound == "responses":
        return ir_response_to_responses(ir_resp)
    if inbound == "messages":
        return ir_response_to_anthropic(ir_resp)
    raise HTTPException(500, "unknown inbound")


def _upstream_to_ir_response(data: dict[str, Any], upstream_mode: str, fallback_model: str) -> IRResponse:
    model = data.get("model") or fallback_model
    # chat
    if upstream_mode == "chat_completions":
        choices = data.get("choices") or []
        msg = choices[0].get("message") if choices else {}
        content = msg.get("content") if isinstance(msg, dict) else ""
        tool_calls = None
        if msg.get("tool_calls"):
            from .ir import IRToolCall

            tool_calls = [IRToolCall(id=tc.get("id") or f"call_{uuid.uuid4().hex[:6]}", name=tc.get("function", {}).get("name") or "", arguments=tc.get("function", {}).get("arguments") or "{}") for tc in msg["tool_calls"]]
        finish = choices[0].get("finish_reason") if choices else "stop"
        return IRResponse(id=data.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}", model=model, content=content or "", tool_calls=tool_calls, finish_reason=finish, usage=data.get("usage"), created=data.get("created") or int(time.time()))
    # responses
    if upstream_mode == "responses":
        output = data.get("output") or []
        text = ""
        tool_calls = None
        for item in output:
            if item.get("type") == "message":
                for c in item.get("content") or []:
                    if c.get("type") == "output_text":
                        text += c.get("text") or ""
            elif item.get("type") == "function_call":
                from .ir import IRToolCall

                if tool_calls is None:
                    tool_calls = []
                tool_calls.append(IRToolCall(id=item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:6]}", name=item.get("name") or "", arguments=item.get("arguments") or "{}"))
        return IRResponse(id=data.get("id") or f"resp_{uuid.uuid4().hex[:24]}", model=model, content=text, tool_calls=tool_calls, finish_reason="stop", usage=data.get("usage"), created=data.get("created_at") or int(time.time()))
    # messages
    if upstream_mode == "messages":
        content_arr = data.get("content") or []
        text = ""
        tool_calls = None
        for c in content_arr:
            if c.get("type") == "text":
                text += c.get("text") or ""
            elif c.get("type") == "tool_use":
                from .ir import IRToolCall
                import json as _json

                if tool_calls is None:
                    tool_calls = []
                tool_calls.append(IRToolCall(id=c.get("id") or f"toolu_{uuid.uuid4().hex[:6]}", name=c.get("name") or "", arguments=_json.dumps(c.get("input") or {}, ensure_ascii=False)))
        # anthropic stop_reason -> finish_reason
        finish = "stop"
        if data.get("stop_reason") == "max_tokens":
            finish = "length"
        elif data.get("stop_reason") == "tool_use":
            finish = "tool_calls"
        usage = data.get("usage")
        if usage:
            usage = {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0), "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)}
        return IRResponse(id=data.get("id") or f"msg_{uuid.uuid4().hex[:24]}", model=model, content=text, tool_calls=tool_calls, finish_reason=finish, usage=usage, created=int(time.time()))
    raise HTTPException(500, f"unknown upstream_mode {upstream_mode}")


# ---- 流式转换 helpers ----

def _split_sse_buffer(buffer: bytes) -> tuple[list[str], bytes]:
    """按 SSE 规范以 \\n\\n 分帧，兼容 \\r\\n；返回 (完整事件data行列表, 残余buffer)"""
    text = buffer.decode(errors="ignore")
    # 规范化换行
    text = text.replace("\r\n", "\n")
    parts = text.split("\n\n")
    # 最后一部分可能不完整
    remainder = parts.pop() if parts else ""
    events: list[str] = []
    for part in parts:
        # 每帧可能含多行，取所有 data: 行
        for line in part.split("\n"):
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                continue
            if line.startswith("data:"):
                events.append(line)
    # remainder 可能含半行，保留原始 bytes 边界：重新编码
    return events, remainder.encode(errors="ignore")


def _extract_delta(data: dict[str, Any], upstream_mode: str) -> str | None:
    if upstream_mode == "responses" and data.get("type") == "response.output_text.delta":
        return data.get("delta")
    if upstream_mode == "messages" and data.get("type") == "content_block_delta":
        d = data.get("delta") or {}
        if d.get("type") == "text_delta":
            return d.get("text")
    if upstream_mode == "chat_completions":
        choices = data.get("choices") or []
        if choices:
            return (choices[0].get("delta") or {}).get("content")
    return None


async def _passthrough_stream(raw_stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    async for chunk in raw_stream:
        if chunk:
            yield chunk


async def _convert_chat_stream_to_chat(raw: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    async for c in raw:
        yield c


async def _convert_any_to_chat_stream(
    inbound: str,
    upstream_mode: str,
    raw_stream: AsyncIterator[bytes],
    model: str,
) -> AsyncIterator[bytes]:
    """将任意上游流转为 chat SSE 流 — 按 \\n\\n 分帧容错"""
    if inbound == "chat" and upstream_mode == "chat_completions":
        async for chunk in raw_stream:
            yield chunk
        return
    created = int(time.time())
    gen_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    yield sse_format({"id": gen_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
    buffer = b""
    async for raw_chunk in raw_stream:
        buffer += raw_chunk
        events, buffer = _split_sse_buffer(buffer)
        for ev in events:
            data = parse_sse_line(ev)
            if data is None or data.get("__done"):
                continue
            delta_text = _extract_delta(data, upstream_mode)
            if delta_text:
                chunk = {"id": gen_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}]}
                yield sse_format(chunk)
    if buffer.strip():
        for line in buffer.decode(errors="ignore").split("\n"):
            data = parse_sse_line(line)
            if data and not data.get("__done"):
                delta_text = _extract_delta(data, upstream_mode)
                if delta_text:
                    yield sse_format({"id": gen_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}]})
    yield sse_format({"id": gen_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    yield b"data: [DONE]\n\n"


async def _convert_any_to_responses_stream(
    inbound: str,
    upstream_mode: str,
    raw_stream: AsyncIterator[bytes],
    model: str,
) -> AsyncIterator[bytes]:
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    seq = 0
    yield sse_format({"type": "response.created", "response": {"id": resp_id, "model": model}})
    yield sse_format({"type": "response.in_progress", "response": {"id": resp_id}})
    yield sse_format({"type": "response.output_item.added", "item": {"type": "message", "role": "assistant"}})
    yield sse_format({"type": "response.content_part.added", "part": {"type": "output_text"}})
    buffer = b""
    async for raw_chunk in raw_stream:
        buffer += raw_chunk
        events, buffer = _split_sse_buffer(buffer)
        for ev_line in events:
            data = parse_sse_line(ev_line)
            if data is None or data.get("__done"):
                continue
            delta_text = _extract_delta(data, upstream_mode)
            if delta_text:
                seq += 1
                yield sse_format({"type": "response.output_text.delta", "delta": delta_text, "sequence_number": seq, "output_index": 0})
    if buffer.strip():
        for line in buffer.decode(errors="ignore").split("\n"):
            data = parse_sse_line(line)
            if data and not data.get("__done"):
                delta_text = _extract_delta(data, upstream_mode)
                if delta_text:
                    seq += 1
                    yield sse_format({"type": "response.output_text.delta", "delta": data.get("delta"), "sequence_number": seq, "output_index": 0})
    yield sse_format({"type": "response.output_text.done", "text": ""})
    yield sse_format({"type": "response.completed", "response": {"id": resp_id, "model": model, "status": "completed"}})
    yield b"data: [DONE]\n\n"


async def _convert_any_to_anthropic_stream(
    inbound: str,
    upstream_mode: str,
    raw_stream: AsyncIterator[bytes],
    model: str,
) -> AsyncIterator[bytes]:
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    yield sse_format({"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "model": model, "content": []}}, event="message_start")
    yield sse_format({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, event="content_block_start")
    buffer = b""
    async for raw_chunk in raw_stream:
        buffer += raw_chunk
        events, buffer = _split_sse_buffer(buffer)
        for ev_line in events:
            data = parse_sse_line(ev_line)
            if data is None or data.get("__done"):
                continue
            delta_text = _extract_delta(data, upstream_mode)
            if delta_text:
                yield sse_format({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta_text}}, event="content_block_delta")
    if buffer.strip():
        for line in buffer.decode(errors="ignore").split("\n"):
            if line.strip().startswith("event:"):
                continue
            data = parse_sse_line(line)
            if data and not data.get("__done"):
                delta_text = _extract_delta(data, upstream_mode)
                if delta_text:
                    yield sse_format({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta_text}}, event="content_block_delta")
    yield sse_format({"type": "content_block_stop", "index": 0}, event="content_block_stop")
    yield sse_format({"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}}, event="message_delta")
    yield sse_format({"type": "message_stop"}, event="message_stop")


# ---------- 代理端点 ----------


@proxy_router.get("/models")
async def list_models():
    return {"object": "list", "data": config_manager.all_models()}


async def _handle_proxy(inbound: str, request: Request):
    _auth_check(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    model = body.get("model")
    if not model:
        raise HTTPException(400, "model 字段必填")
    provider = _resolve_provider(model)
    ir = _to_ir(inbound, body)
    # 使用原始 model 用于路由，但转换时去前缀
    stream = bool(body.get("stream"))
    upstream_body = _from_ir_to_upstream(ir, provider, stream=stream)

    client: httpx.AsyncClient = request.app.state.httpx_client

    if not stream:
        status, data = await post_non_stream(client, provider, upstream_body)
        if isinstance(data, bytes):
            # 上游返回非 JSON（如 HTML 错误页），透传为错误
            text = data.decode(errors="ignore")
            if status >= 400:
                return JSONResponse(content={"error": text[:2000]}, status_code=status)
            return JSONResponse(content={"error": f"upstream 非 JSON: {text[:500]}"}, status_code=502)
        if status >= 400:
            return JSONResponse(content=data if isinstance(data, dict) else {"error": str(data)}, status_code=status)
        result = _upstream_resp_to_inbound(inbound, provider.upstream_mode, data, _strip_provider_prefix(model, provider))
        return JSONResponse(content=result)

    # 流式 — 包装以透传上游 4xx/5xx 为 SSE 错误事件而非静默空流
    async def _safe_stream():
        try:
            async for chunk in stream_upstream(client, provider, upstream_body):
                yield chunk
        except httpx.HTTPStatusError as e:
            body_bytes = e.response.content if e.response is not None else b""
            try:
                err = json.loads(body_bytes.decode(errors="ignore")) if body_bytes else {"error": str(e)}
            except Exception:
                err = {"error": body_bytes.decode(errors="ignore")[:2000] or str(e)}
            # 以 SSE error 事件 + 结束标记返回，前端可感知
            yield sse_format({"type": "error", "error": err})
            yield b"data: [DONE]\n\n"
        except Exception as e:
            yield sse_format({"type": "error", "error": str(e)})
            yield b"data: [DONE]\n\n"

    raw_stream = _safe_stream()

    if inbound == "chat":
        if provider.upstream_mode == "chat_completions":
            gen = _passthrough_stream(raw_stream)
        else:
            gen = _convert_any_to_chat_stream(inbound, provider.upstream_mode, raw_stream, _strip_provider_prefix(model, provider))
        return StreamingResponse(gen, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    if inbound == "responses":
        if provider.upstream_mode == "responses":
            gen = _passthrough_stream(raw_stream)
        else:
            gen = _convert_any_to_responses_stream(inbound, provider.upstream_mode, raw_stream, _strip_provider_prefix(model, provider))
        return StreamingResponse(gen, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    if inbound == "messages":
        if provider.upstream_mode == "messages":
            gen = _passthrough_stream(raw_stream)
        else:
            gen = _convert_any_to_anthropic_stream(inbound, provider.upstream_mode, raw_stream, _strip_provider_prefix(model, provider))
        return StreamingResponse(gen, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    raise HTTPException(500, "unknown inbound")


@proxy_router.post("/chat/completions")
async def proxy_chat(request: Request):
    return await _handle_proxy("chat", request)


@proxy_router.post("/responses")
async def proxy_responses(request: Request):
    return await _handle_proxy("responses", request)


@proxy_router.post("/messages")
async def proxy_messages(request: Request):
    return await _handle_proxy("messages", request)
