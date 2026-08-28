"""管理 API + 代理 API"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__, access_log
from .config import ProviderConfig, apply_incoming_key, config_manager, provider_public_dict
from .router import is_retryable_status, resolve_providers
from .converter.chat_anthropic import anthropic_to_ir, ir_response_to_anthropic, ir_to_anthropic
from .converter.chat_responses import (
    chat_to_ir,
    ir_response_to_chat,
    ir_response_to_responses,
    ir_to_chat,
    ir_to_responses,
    responses_to_ir,
)
from .ir import IRResponse, IRToolCall
from .stream import convert_stream
from .upstream import UpstreamHTTPError, fetch_upstream_models, post_non_stream, stream_upstream

manage_router = APIRouter(prefix="/api")
proxy_router = APIRouter(prefix="/v1")
STARTED = time.time()


@manage_router.get("/status")
async def api_status():
    cfg = config_manager.config
    return {
        "ok": True,
        "version": __version__,
        "uptime_s": int(time.time() - STARTED),
        "host": cfg.server.host,
        "port": cfg.server.port,
        "providers": len(cfg.providers),
        "models": len(config_manager.all_models()),
        "auth": bool((cfg.server.local_api_key or "").strip()),
    }


@manage_router.get("/config")
async def get_config():
    cfg = config_manager.config
    return {
        "server": cfg.server.model_dump(),
        "providers": [provider_public_dict(p) for p in cfg.providers],
    }


@manage_router.put("/config")
async def update_config(body: dict[str, Any]):
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
    return [provider_public_dict(p) for p in config_manager.config.providers]


@manage_router.post("/providers")
async def create_provider(body: dict[str, Any]):
    body = apply_incoming_key(body, None)
    try:
        p = ProviderConfig.model_validate(body)
    except Exception as e:
        raise HTTPException(400, str(e))
    if config_manager.find_provider(p.id):
        raise HTTPException(409, f"provider id '{p.id}' 已存在")
    config_manager.config.providers.append(p)
    await config_manager.save()
    return provider_public_dict(p)


@manage_router.put("/providers/{pid}")
async def update_provider(pid: str, body: dict[str, Any]):
    idx = next((i for i, x in enumerate(config_manager.config.providers) if x.id == pid), None)
    if idx is None:
        raise HTTPException(404, "provider not found")
    existing = config_manager.config.providers[idx]
    body["id"] = body.get("id") or pid
    body = apply_incoming_key(body, existing)
    try:
        p = ProviderConfig.model_validate(body)
    except Exception as e:
        raise HTTPException(400, str(e))
    if p.id != pid and config_manager.find_provider(p.id):
        raise HTTPException(409, f"provider id '{p.id}' 已存在")
    config_manager.config.providers[idx] = p
    await config_manager.save()
    return provider_public_dict(p)


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
    t0 = time.perf_counter()
    client: httpx.AsyncClient = request.app.state.httpx_client
    try:
        if provider.upstream_mode == "responses":
            body: dict[str, Any] = {"model": model, "input": [{"role": "user", "content": "ping"}], "max_output_tokens": 8}
        else:
            body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8}
        status, data = await post_non_stream(client, provider, body, timeout=20)
        latency = int((time.perf_counter() - t0) * 1000)
        ok = 200 <= status < 300
        preview: Any
        if isinstance(data, dict):
            preview = data
        elif isinstance(data, bytes):
            preview = data.decode(errors="ignore")[:500]
        else:
            preview = str(data)[:500]
        return {"ok": ok, "status": status, "latency_ms": latency, "data": preview}
    except Exception as e:
        latency = int((time.perf_counter() - t0) * 1000)
        return {"ok": False, "latency_ms": latency, "error": str(e)}


@manage_router.post("/providers/{pid}/models/fetch")
async def fetch_models(pid: str, request: Request):
    provider = config_manager.find_provider(pid)
    if not provider:
        raise HTTPException(404, "provider not found")
    client: httpx.AsyncClient = request.app.state.httpx_client
    status, data = await fetch_upstream_models(client, provider)
    if status >= 400:
        return JSONResponse({"ok": False, "status": status, "error": data}, status_code=status if status < 600 else 502)
    if not isinstance(data, list):
        return {"ok": False, "status": status, "error": data}
    return {"ok": True, "status": status, "models": data}


@manage_router.get("/logs")
async def get_logs(limit: int = 100, offset: int = 0):
    return access_log.list_logs(limit=limit, offset=offset)


@manage_router.delete("/logs")
async def clear_logs():
    access_log.clear()
    return {"ok": True}


def _auth_check(request: Request) -> None:
    cfg = config_manager.config.server
    local_key = (cfg.local_api_key or "").strip()
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    token = auth.removeprefix("Bearer ").removeprefix("bearer ").strip() if auth else ""
    # Anthropic 客户端走 x-api-key
    if not token:
        token = (request.headers.get("x-api-key") or "").strip()

    if local_key:
        if token == local_key:
            return
        if token and token in config_manager.all_api_keys():
            return
        raise HTTPException(401, "无效的 API Key（需匹配 server.local_api_key）")

    keys = config_manager.all_api_keys()
    if not keys:
        return
    if not token:
        raise HTTPException(401, "缺少 Authorization: Bearer <API Key> 或 x-api-key")
    if token in keys:
        return
    raise HTTPException(401, "无效的 API Key")


def _resolve_provider(model: str) -> ProviderConfig:
    found = resolve_providers(model)
    if not found:
        raise HTTPException(400, f"未知 model '{model}'，请先在管理页配置提供商与模型")
    return found[0]


def _strip_provider_prefix(model: str, provider: ProviderConfig) -> str:
    if model.startswith(provider.id + "/"):
        return model[len(provider.id) + 1 :]
    return model


def _to_ir(inbound: str, body: dict[str, Any]):
    if inbound == "chat":
        return chat_to_ir(body)
    if inbound == "responses":
        return responses_to_ir(body)
    if inbound == "messages":
        return anthropic_to_ir(body)
    raise HTTPException(400, f"unknown inbound {inbound}")


def _from_ir_to_upstream(ir, provider: ProviderConfig, stream: bool):
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


def _usage_openai(usage: Any) -> dict[str, int] | None:
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    total = usage.get("total_tokens") or (prompt + completion)
    return {"prompt_tokens": int(prompt), "completion_tokens": int(completion), "total_tokens": int(total)}


def _upstream_to_ir_response(data: dict[str, Any], upstream_mode: str, fallback_model: str) -> IRResponse:
    model = data.get("model") or fallback_model
    if upstream_mode == "chat_completions":
        choices = data.get("choices") or []
        msg = choices[0].get("message") if choices else {}
        content = msg.get("content") if isinstance(msg, dict) else ""
        tool_calls = None
        if isinstance(msg, dict) and msg.get("tool_calls"):
            tool_calls = [
                IRToolCall(
                    id=tc.get("id") or f"call_{uuid.uuid4().hex[:6]}",
                    name=tc.get("function", {}).get("name") or "",
                    arguments=tc.get("function", {}).get("arguments") or "{}",
                )
                for tc in msg["tool_calls"]
            ]
        finish = choices[0].get("finish_reason") if choices else "stop"
        reasoning = None
        if isinstance(msg, dict):
            reasoning = msg.get("reasoning_content") or msg.get("reasoning")
        return IRResponse(
            id=data.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}",
            model=model,
            content=content or "",
            tool_calls=tool_calls,
            reasoning=reasoning,
            finish_reason=finish,
            usage=_usage_openai(data.get("usage")),
            created=data.get("created") or int(time.time()),
        )
    if upstream_mode == "responses":
        output = data.get("output") or []
        text = ""
        tool_calls = None
        reasoning = None
        for item in output:
            t = item.get("type")
            if t == "message":
                for c in item.get("content") or []:
                    if c.get("type") == "output_text":
                        text += c.get("text") or ""
            elif t == "function_call":
                if tool_calls is None:
                    tool_calls = []
                tool_calls.append(
                    IRToolCall(
                        id=item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:6]}",
                        name=item.get("name") or "",
                        arguments=item.get("arguments") or "{}",
                    )
                )
            elif t == "reasoning":
                summary = item.get("summary") or []
                if isinstance(summary, list):
                    reasoning = " ".join((s.get("text") if isinstance(s, dict) else str(s)) or "" for s in summary)
                elif isinstance(summary, str):
                    reasoning = summary
        finish = "tool_calls" if tool_calls else "stop"
        if data.get("status") == "incomplete":
            finish = "length"
        return IRResponse(
            id=data.get("id") or f"resp_{uuid.uuid4().hex[:24]}",
            model=model,
            content=text,
            tool_calls=tool_calls,
            reasoning=reasoning,
            finish_reason=finish,
            usage=_usage_openai(data.get("usage")),
            created=data.get("created_at") or int(time.time()),
        )
    if upstream_mode == "messages":
        content_arr = data.get("content") or []
        text = ""
        tool_calls = None
        reasoning = None
        for c in content_arr:
            t = c.get("type")
            if t == "text":
                text += c.get("text") or ""
            elif t == "thinking":
                reasoning = (reasoning or "") + (c.get("thinking") or "")
            elif t == "tool_use":
                if tool_calls is None:
                    tool_calls = []
                tool_calls.append(
                    IRToolCall(
                        id=c.get("id") or f"toolu_{uuid.uuid4().hex[:6]}",
                        name=c.get("name") or "",
                        arguments=json.dumps(c.get("input") or {}, ensure_ascii=False),
                    )
                )
        finish = "stop"
        if data.get("stop_reason") == "max_tokens":
            finish = "length"
        elif data.get("stop_reason") == "tool_use":
            finish = "tool_calls"
        usage = data.get("usage")
        if usage:
            usage = {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            }
        return IRResponse(
            id=data.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
            model=model,
            content=text,
            tool_calls=tool_calls,
            reasoning=reasoning,
            finish_reason=finish,
            usage=usage,
            created=int(time.time()),
        )
    raise HTTPException(500, f"unknown upstream_mode {upstream_mode}")


def _upstream_resp_to_inbound(inbound: str, upstream_mode: str, data: dict[str, Any], model: str) -> dict[str, Any]:
    ir_resp = _upstream_to_ir_response(data, upstream_mode, model)
    if inbound == "chat":
        return ir_response_to_chat(ir_resp)
    if inbound == "responses":
        return ir_response_to_responses(ir_resp)
    if inbound == "messages":
        return ir_response_to_anthropic(ir_resp)
    raise HTTPException(500, "unknown inbound")


@proxy_router.get("/models")
async def list_models():
    return {"object": "list", "data": config_manager.all_models()}


async def _handle_proxy(inbound: str, request: Request):
    _auth_check(request)
    t0 = time.perf_counter()
    model = ""
    provider_id = None
    stream = False
    try:
        body = await request.json()
    except Exception:
        access_log.add(inbound=inbound, model="", provider_id=None, stream=False, status=400, latency_ms=0, error="invalid JSON")
        raise HTTPException(400, "invalid JSON body")
    model = body.get("model") or ""
    if not model:
        access_log.add(inbound=inbound, model="", provider_id=None, stream=False, status=400, latency_ms=0, error="missing model")
        raise HTTPException(400, "model 字段必填")
    candidates = resolve_providers(model)
    if not candidates:
        access_log.add(inbound=inbound, model=model, provider_id=None, stream=False, status=400, latency_ms=0, error="unknown model")
        raise HTTPException(400, f"未知 model '{model}'，请先在管理页配置提供商与模型")
    ir = _to_ir(inbound, body)
    stream = bool(body.get("stream"))
    client: httpx.AsyncClient = request.app.state.httpx_client
    server = config_manager.config.server
    backoff = max(0, int(server.retry_backoff_ms)) / 1000.0
    extra_tries = int(server.retry_count)

    def _can_retry(pi: int, attempt: int, n_tries: int, retryable: bool) -> bool:
        if not retryable:
            return False
        if attempt < n_tries - 1:
            return True
        return bool(server.failover and pi < len(candidates) - 1)

    if not stream:
        last_status = 502
        last_payload: Any = {"error": {"message": "all upstreams failed", "type": "upstream_error"}}
        last_pid = candidates[0].id
        attempts = 0
        for pi, provider in enumerate(candidates):
            upstream_body = _from_ir_to_upstream(ir, provider, stream=False)
            n_tries = 1 + extra_tries
            for attempt in range(n_tries):
                attempts += 1
                last_pid = provider.id
                try:
                    status, data = await post_non_stream(client, provider, upstream_body, timeout=provider.timeout_s)
                except Exception as e:
                    last_status = 502
                    last_payload = {"error": {"message": str(e), "type": "upstream_error"}}
                    if _can_retry(pi, attempt, n_tries, True):
                        if backoff:
                            await asyncio.sleep(backoff)
                        continue
                    ms = int((time.perf_counter() - t0) * 1000)
                    access_log.add(inbound=inbound, model=model, provider_id=last_pid, stream=False, status=502, latency_ms=ms, error=str(e), attempts=attempts)
                    return JSONResponse(content=last_payload, status_code=502, headers={"X-Universal-Router-Provider": last_pid})
                if isinstance(data, bytes):
                    text = data.decode(errors="ignore")
                    last_status = status if status >= 400 else 502
                    last_payload = {"error": text[:2000] if status >= 400 else f"upstream 非 JSON: {text[:500]}"}
                    retryable = is_retryable_status(status) or status >= 500
                    if _can_retry(pi, attempt, n_tries, retryable):
                        if backoff:
                            await asyncio.sleep(backoff)
                        continue
                    ms = int((time.perf_counter() - t0) * 1000)
                    access_log.add(inbound=inbound, model=model, provider_id=last_pid, stream=False, status=last_status, latency_ms=ms, error=text[:200], attempts=attempts)
                    return JSONResponse(content=last_payload, status_code=last_status)
                if 200 <= status < 300:
                    stripped = _strip_provider_prefix(model, provider)
                    result = _upstream_resp_to_inbound(inbound, provider.upstream_mode, data, stripped)
                    ms = int((time.perf_counter() - t0) * 1000)
                    access_log.add(inbound=inbound, model=model, provider_id=provider.id, stream=False, status=200, latency_ms=ms, attempts=attempts)
                    return JSONResponse(content=result, headers={"X-Universal-Router-Provider": provider.id})
                last_status = status
                last_payload = data if isinstance(data, dict) else {"error": str(data)}
                if _can_retry(pi, attempt, n_tries, is_retryable_status(status)):
                    if backoff:
                        await asyncio.sleep(backoff)
                    continue
                ms = int((time.perf_counter() - t0) * 1000)
                access_log.add(inbound=inbound, model=model, provider_id=last_pid, stream=False, status=status, latency_ms=ms, error=str(data)[:200], attempts=attempts)
                return JSONResponse(content=last_payload, status_code=status, headers={"X-Universal-Router-Provider": last_pid})
        ms = int((time.perf_counter() - t0) * 1000)
        access_log.add(inbound=inbound, model=model, provider_id=last_pid, stream=False, status=last_status, latency_ms=ms, error="exhausted retries", attempts=attempts)
        return JSONResponse(content=last_payload, status_code=last_status, headers={"X-Universal-Router-Provider": last_pid})

    async def gen():
        attempts = 0
        last_err = "all upstreams failed"
        last_pid = candidates[0].id
        for pi, provider in enumerate(candidates):
            upstream_body = _from_ir_to_upstream(ir, provider, stream=True)
            n_tries = 1 + extra_tries
            for attempt in range(n_tries):
                attempts += 1
                last_pid = provider.id
                try:
                    raw = stream_upstream(client, provider, upstream_body, timeout=max(provider.timeout_s, 60))
                    it = raw.__aiter__()
                    first = await it.__anext__()

                    async def chained(first_chunk=first, iterator=it):
                        yield first_chunk
                        async for c in iterator:
                            yield c

                    stripped = _strip_provider_prefix(model, provider)
                    ms = int((time.perf_counter() - t0) * 1000)
                    access_log.add(inbound=inbound, model=model, provider_id=provider.id, stream=True, status=200, latency_ms=ms, attempts=attempts)
                    async for chunk in convert_stream(inbound, provider.upstream_mode, chained(), stripped):
                        yield chunk
                    return
                except StopAsyncIteration:
                    ms = int((time.perf_counter() - t0) * 1000)
                    access_log.add(inbound=inbound, model=model, provider_id=provider.id, stream=True, status=200, latency_ms=ms, attempts=attempts)
                    return
                except UpstreamHTTPError as e:
                    last_err = e.body.decode(errors="ignore")[:500] if e.body else str(e)
                    retryable = is_retryable_status(e.status_code)
                    if _can_retry(pi, attempt, n_tries, retryable):
                        if backoff:
                            await asyncio.sleep(backoff)
                        continue
                    from .converter.common import sse_format

                    yield sse_format({"type": "error", "error": last_err, "status": e.status_code})
                    yield b"data: [DONE]\n\n"
                    ms = int((time.perf_counter() - t0) * 1000)
                    access_log.add(inbound=inbound, model=model, provider_id=last_pid, stream=True, status=e.status_code, latency_ms=ms, error=last_err[:200], attempts=attempts)
                    return
                except Exception as e:
                    last_err = str(e)
                    if _can_retry(pi, attempt, n_tries, True):
                        if backoff:
                            await asyncio.sleep(backoff)
                        continue
                    from .converter.common import sse_format

                    yield sse_format({"type": "error", "error": last_err})
                    yield b"data: [DONE]\n\n"
                    ms = int((time.perf_counter() - t0) * 1000)
                    access_log.add(inbound=inbound, model=model, provider_id=last_pid, stream=True, status=502, latency_ms=ms, error=last_err[:200], attempts=attempts)
                    return
        from .converter.common import sse_format

        yield sse_format({"type": "error", "error": last_err})
        yield b"data: [DONE]\n\n"
        ms = int((time.perf_counter() - t0) * 1000)
        access_log.add(inbound=inbound, model=model, provider_id=last_pid, stream=True, status=502, latency_ms=ms, error=last_err[:200], attempts=attempts)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive", "X-Universal-Router-Provider": candidates[0].id},
    )


@proxy_router.post("/chat/completions")
async def proxy_chat(request: Request):
    return await _handle_proxy("chat", request)


@proxy_router.post("/responses")
async def proxy_responses(request: Request):
    return await _handle_proxy("responses", request)


@proxy_router.post("/messages")
async def proxy_messages(request: Request):
    return await _handle_proxy("messages", request)
