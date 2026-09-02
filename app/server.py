"""管理 API + 代理 API"""
from __future__ import annotations

import asyncio
import hmac
import json
import time
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__, access_log
from . import health as provider_health
from .config import (
    ProviderConfig,
    ProviderExistsError,
    apply_incoming_key,
    apply_incoming_server,
    config_manager,
    is_loopback_bind,
    provider_public_dict,
    server_public_dict,
)
from .converter.chat_anthropic import anthropic_to_ir, ir_response_to_anthropic, ir_to_anthropic
from .converter.chat_responses import (
    chat_to_ir,
    ir_response_to_chat,
    ir_response_to_responses,
    ir_to_chat,
    ir_to_responses,
    responses_to_ir,
    usage_to_responses,
)
from .converter.common import sse_format as _sse
from .ir import IRResponse, IRToolCall
from .router import is_retryable_status, resolve_providers
from . import ctx
from .secrets import collect_secrets, redact, redact_any
from .stream import convert_stream, inbound_mode
from .upstream import StreamTimeoutError, UpstreamHTTPError, fetch_upstream_models, post_non_stream, raw_request, stream_upstream


def _secret_extras() -> list[str]:
    cfg = config_manager.config
    return collect_secrets(
        cfg.server.local_api_key,
        cfg.server.admin_api_key,
        *[x for p in cfg.providers for x in (p.api_key, p.inbound_key)],
    )


def _safe_error(obj: Any) -> Any:
    return redact_any(obj, _secret_extras())


def _admin_token(request: Request) -> str:
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    token = auth.removeprefix("Bearer ").removeprefix("bearer ").strip() if auth else ""
    if not token:
        token = (request.headers.get("x-admin-key") or request.headers.get("X-Admin-Key") or "").strip()
    if not token:
        token = (request.headers.get("x-api-key") or "").strip()
    return token


def _token_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


async def require_admin(request: Request) -> None:
    """保护 /api/* 。绑定非本机时必须有 admin/local key；有 key 则一律校验。"""
    cfg = config_manager.config.server
    admin_key = (cfg.admin_api_key or "").strip() or (cfg.local_api_key or "").strip()
    public_bind = not is_loopback_bind(cfg.host)
    if not admin_key:
        if public_bind:
            raise HTTPException(403, "绑定非本机地址时必须设置 admin_api_key 或 local_api_key，以保护管理 API")
        return
    token = _admin_token(request)
    if not token or not _token_eq(token, admin_key):
        raise HTTPException(401, "管理 API 需要有效的 admin_api_key（Authorization 或 X-Admin-Key）")


def _est_cost(provider: ProviderConfig, prompt_tokens: int, completion_tokens: int) -> float:
    return (max(0, prompt_tokens) * float(provider.cost_input_per_1m or 0) + max(0, completion_tokens) * float(provider.cost_output_per_1m or 0)) / 1_000_000.0


def _record_ok(provider: ProviderConfig, latency_ms: int, usage: dict[str, int] | None) -> None:
    pt = int((usage or {}).get("prompt_tokens") or 0)
    ct = int((usage or {}).get("completion_tokens") or 0)
    provider_health.record_success(provider.id, latency_ms, tokens_in=pt, tokens_out=ct, cost=_est_cost(provider, pt, ct))


def _record_fail(pid: str, error: str | None) -> None:
    thr = int(getattr(config_manager.config.server, "circuit_fail_threshold", 3) or 3)
    provider_health.record_failure(pid, error=redact(error, _secret_extras()) if error else None, threshold=thr)


manage_router = APIRouter(prefix="/api", dependencies=[Depends(require_admin)])
proxy_router = APIRouter(prefix="/v1")
STARTED = time.time()


def _hdr(provider_id: str | None = None) -> dict[str, str]:
    h: dict[str, str] = {}
    if provider_id:
        h["X-Universal-Router-Provider"] = provider_id
    return h


def _set_preview(body: dict[str, Any]) -> None:
    slim: dict[str, Any] = {"model": body.get("model"), "stream": bool(body.get("stream"))}
    if body.get("tools"):
        slim["tools"] = True
    if body.get("previous_response_id"):
        slim["previous_response_id"] = True
    inp = body.get("input") if "input" in body else body.get("messages")
    if isinstance(inp, str):
        slim["input"] = inp[:240]
    elif isinstance(inp, list):
        slim["n_items"] = len(inp)
    ctx.log_preview.set(json.dumps(slim, ensure_ascii=False))


def _estimate_tokens(ir: Any) -> int:
    n = 0
    for m in getattr(ir, "messages", None) or []:
        c = m.content
        if isinstance(c, str):
            n += len(c)
        elif isinstance(c, list):
            n += sum(len(x.text or "") for x in c)
        if m.reasoning:
            n += len(m.reasoning)
    inst = (getattr(ir, "extra", None) or {}).get("instructions")
    if isinstance(inst, str):
        n += len(inst)
    return max(1, n // 4)


@manage_router.get("/status")
async def api_status(request: Request):
    cfg = config_manager.config
    bind_host = getattr(request.app.state, "bind_host", cfg.server.host)
    bind_port = getattr(request.app.state, "bind_port", cfg.server.port)
    return {
        "ok": True,
        "version": __version__,
        "uptime_s": int(time.time() - STARTED),
        "host": cfg.server.host,
        "port": cfg.server.port,
        "bind_host": bind_host,
        "bind_port": bind_port,
        "restart_needed": bind_host != cfg.server.host or int(bind_port) != int(cfg.server.port),
        "providers": len(cfg.providers),
        "models": len(config_manager.all_models()),
        "auth": bool((cfg.server.local_api_key or "").strip()),
        "admin_auth": bool((cfg.server.admin_api_key or cfg.server.local_api_key or "").strip()) or (not is_loopback_bind(cfg.server.host)),
        "provider_health": provider_health.all_snapshots([p.id for p in cfg.providers]),
    }


@manage_router.get("/config")
async def get_config():
    cfg = config_manager.config
    return {
        "server": server_public_dict(cfg.server),
        "providers": [provider_public_dict(p) for p in cfg.providers],
    }


@manage_router.put("/config")
async def update_config(body: dict[str, Any]):
    srv = body.get("server")
    if not srv or not isinstance(srv, dict):
        raise HTTPException(400, "需提供 server 配置")
    try:
        from .config import ServerConfig

        srv = apply_incoming_server(srv, config_manager.config.server)
        new_srv = ServerConfig.model_validate(srv)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    await config_manager.update_server(new_srv)
    return {"server": server_public_dict(new_srv)}


@manage_router.get("/providers")
async def list_providers():
    return [provider_public_dict(p) for p in config_manager.config.providers]


@manage_router.post("/providers")
async def create_provider(body: dict[str, Any]):
    body = apply_incoming_key(body, None)
    try:
        p = ProviderConfig.model_validate(body)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    try:
        await config_manager.add_provider(p)
    except ProviderExistsError as e:
        raise HTTPException(409, str(e)) from e
    return provider_public_dict(p)


@manage_router.put("/providers/{pid}")
async def update_provider(pid: str, body: dict[str, Any]):
    existing = config_manager.find_provider(pid)
    if existing is None:
        raise HTTPException(404, "provider not found")
    body["id"] = body.get("id") or pid
    body = apply_incoming_key(body, existing)
    try:
        p = ProviderConfig.model_validate(body)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    try:
        ok = await config_manager.replace_provider(pid, p)
    except ProviderExistsError as e:
        raise HTTPException(409, str(e)) from e
    if not ok:
        raise HTTPException(404, "provider not found")
    return provider_public_dict(p)


@manage_router.delete("/providers/{pid}")
async def delete_provider(pid: str):
    if not await config_manager.remove_provider(pid):
        raise HTTPException(404, "provider not found")
    return {"ok": True}


@manage_router.post("/providers/{pid}/test")
async def test_provider(pid: str, request: Request):
    provider = config_manager.find_provider(pid)
    if not provider:
        raise HTTPException(404, "provider not found")
    if not provider.models:
        raise HTTPException(400, "该提供商未配置 models，无法测试")
    model = provider.upstream_model_id(provider.models[0].id)
    t0 = time.perf_counter()
    client: httpx.AsyncClient = request.app.state.httpx_client
    try:
        if provider.upstream_mode == "responses":
            body: dict[str, Any] = {"model": model, "input": [{"role": "user", "content": "ping"}], "max_output_tokens": 16}
        else:
            body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 16}
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
        return {"ok": ok, "status": status, "latency_ms": latency, "data": _safe_error(preview)}
    except Exception as e:
        latency = int((time.perf_counter() - t0) * 1000)
        return {"ok": False, "latency_ms": latency, "error": redact(str(e), _secret_extras())}


@manage_router.post("/providers/{pid}/models/fetch")
async def fetch_models(pid: str, request: Request):
    provider = config_manager.find_provider(pid)
    if not provider:
        raise HTTPException(404, "provider not found")
    client: httpx.AsyncClient = request.app.state.httpx_client
    status, data = await fetch_upstream_models(client, provider)
    if status >= 400:
        return JSONResponse({"ok": False, "status": status, "error": _safe_error(data)}, status_code=status if status < 600 else 502)
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


@manage_router.get("/health/providers")
async def provider_health_api():
    return {"items": provider_health.all_snapshots([p.id for p in config_manager.config.providers])}


def _auth_check(request: Request) -> None:
    cfg = config_manager.config.server
    local_key = (cfg.local_api_key or "").strip()
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    token = auth.removeprefix("Bearer ").removeprefix("bearer ").strip() if auth else ""
    # Anthropic 客户端走 x-api-key
    if not token:
        token = (request.headers.get("x-api-key") or "").strip()

    if local_key:
        if token and _token_eq(token, local_key):
            return
        raise HTTPException(401, "无效的 API Key（需匹配 server.local_api_key）")

    keys = config_manager.all_api_keys()
    if not keys:
        return
    if not token:
        raise HTTPException(401, "缺少 Authorization: Bearer <API Key> 或 x-api-key")
    if any(_token_eq(token, k) for k in keys):
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
    send_id = provider.upstream_model_id(stripped)
    if send_id != ir.model:
        ir = ir.model_copy(update={"model": send_id})
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


def _normalize_responses_object(data: dict[str, Any]) -> dict[str, Any]:
    """同协议透传时补齐官方 SDK 常用字段，不改写 output 结构。"""
    out = dict(data)
    out.setdefault("object", "response")
    out.setdefault("status", "completed")
    if "output_text" not in out:
        text = ""
        for item in out.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "output_text":
                    text += c.get("text") or ""
        out["output_text"] = text
    usage = out.get("usage")
    if isinstance(usage, dict) and "input_tokens" not in usage:
        out["usage"] = usage_to_responses(usage)
    return out


def _upstream_resp_to_inbound(inbound: str, upstream_mode: str, data: dict[str, Any], model: str) -> dict[str, Any]:
    if inbound_mode(inbound) == upstream_mode:
        if inbound == "responses" and isinstance(data, dict):
            return _normalize_responses_object(data)
        return data
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


async def _watch_disconnect(request: Request, interval: float = 0.5) -> bool:
    """轮询客户端断连；返回 True 表示已断开。"""
    while True:
        if await request.is_disconnected():
            return True
        await asyncio.sleep(interval)


async def _post_with_disconnect_watch(
    client: httpx.AsyncClient,
    provider: ProviderConfig,
    upstream_body: dict[str, Any],
    timeout: float,
    request: Request,
) -> tuple[int, dict[str, Any] | bytes] | None:
    """非流式上游调用；客户端断连时取消上游请求，返回 None。"""
    task = asyncio.create_task(post_non_stream(client, provider, upstream_body, timeout=timeout))
    watch = asyncio.create_task(_watch_disconnect(request))
    try:
        done, _ = await asyncio.wait({task, watch}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        task.cancel()
        watch.cancel()
        raise
    if task not in done:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        return None
    watch.cancel()
    return task.result()


def _bad_request(inbound: str, error: str, message: str | None = None):
    access_log.add(inbound=inbound, model="", provider_id=None, stream=False, status=400, latency_ms=0, error=error)
    return HTTPException(400, message or "invalid JSON body")


async def _parse_proxy_request(inbound: str, request: Request) -> tuple[dict[str, Any], str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise _bad_request(inbound, "invalid JSON") from None
    if not isinstance(body, dict):
        raise _bad_request(inbound, "invalid JSON")
    model = str(body.get("model") or "")
    if not model:
        raise _bad_request(inbound, "missing model", "model 字段必填")
    ir = _to_ir(inbound, body)
    return body, model, ir


async def _handle_proxy(inbound: str, request: Request, *, skip_auth: bool = False):
    if not skip_auth:
        _auth_check(request)
    t0 = time.perf_counter()
    body, model, ir = await _parse_proxy_request(inbound, request)
    _set_preview(body)
    candidates = resolve_providers(model)
    if (ir.extra or {}).get("previous_response_id"):
        only = [p for p in candidates if p.upstream_mode == "responses"]
        if not only:
            access_log.add(inbound=inbound, model=model, provider_id=None, stream=False, status=400, latency_ms=0, error="previous_response_id requires responses upstream")
            raise HTTPException(400, "previous_response_id 仅在上游为 responses 时可用，请为该模型配置 responses 提供商")
        candidates = only
    if not candidates:
        access_log.add(inbound=inbound, model=model, provider_id=None, stream=False, status=400, latency_ms=0, error="unknown model")
        raise HTTPException(400, f"未知 model '{model}'，请先在管理页配置提供商与模型")
    client: httpx.AsyncClient = request.app.state.httpx_client
    if not body.get("stream"):
        return await _proxy_non_stream(inbound, request, client, ir, model, candidates, t0)
    return _proxy_stream(inbound, client, ir, model, candidates, t0)


async def _proxy_non_stream(
    inbound: str,
    request: Request,
    client: httpx.AsyncClient,
    ir: Any,
    model: str,
    candidates: list[ProviderConfig],
    t0: float,
):
    server = config_manager.config.server
    backoff = max(0, int(server.retry_backoff_ms)) / 1000.0
    extra_tries = int(server.retry_count)

    def _can_retry(pi: int, attempt: int, n_tries: int, retryable: bool) -> bool:
        if not retryable:
            return False
        if attempt < n_tries - 1:
            return True
        return bool(server.failover and pi < len(candidates) - 1)

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
            ctx.upstream_mode.set(provider.upstream_mode)
            try:
                result = await _post_with_disconnect_watch(client, provider, upstream_body, provider.timeout_s, request)
            except Exception as e:
                last_status = 502
                last_payload = {"error": {"message": redact(str(e), _secret_extras()), "type": "upstream_error"}}
                _record_fail(last_pid, str(e))
                if _can_retry(pi, attempt, n_tries, True):
                    if backoff:
                        await asyncio.sleep(backoff)
                    continue
                ms = int((time.perf_counter() - t0) * 1000)
                access_log.add(inbound=inbound, model=model, provider_id=last_pid, stream=False, status=502, latency_ms=ms, error=str(e), attempts=attempts)
                return JSONResponse(content=last_payload, status_code=502, headers={"X-Universal-Router-Provider": last_pid})
            if result is None:
                ms = int((time.perf_counter() - t0) * 1000)
                access_log.add(inbound=inbound, model=model, provider_id=last_pid, stream=False, status=499, latency_ms=ms, error="client disconnected", attempts=attempts)
                return JSONResponse(content={"error": {"message": "client disconnected", "type": "client_disconnected"}}, status_code=499)
            status, data = result
            if isinstance(data, bytes):
                text = data.decode(errors="ignore")
                last_status = status if status >= 400 else 502
                last_payload = {"error": redact(text[:2000] if status >= 400 else f"upstream 非 JSON: {text[:500]}", _secret_extras())}
                _record_fail(last_pid, text[:200])
                retryable = is_retryable_status(status) or status >= 500
                if _can_retry(pi, attempt, n_tries, retryable):
                    if backoff:
                        await asyncio.sleep(backoff)
                    continue
                ms = int((time.perf_counter() - t0) * 1000)
                access_log.add(inbound=inbound, model=model, provider_id=last_pid, stream=False, status=last_status, latency_ms=ms, error=text[:200], attempts=attempts)
                return JSONResponse(content=last_payload, status_code=last_status, headers={"X-Universal-Router-Provider": last_pid})
            if 200 <= status < 300:
                stripped = _strip_provider_prefix(model, provider)
                result_inbound = _upstream_resp_to_inbound(inbound, provider.upstream_mode, data, stripped)
                ms = int((time.perf_counter() - t0) * 1000)
                usage = result_inbound.get("usage") if isinstance(result_inbound, dict) else None
                if not isinstance(usage, dict) and isinstance(data, dict):
                    usage = _usage_openai(data.get("usage"))
                pt = int((usage or {}).get("prompt_tokens") or (usage or {}).get("input_tokens") or 0)
                ct = int((usage or {}).get("completion_tokens") or (usage or {}).get("output_tokens") or 0)
                _record_ok(provider, ms, {"prompt_tokens": pt, "completion_tokens": ct})
                access_log.add(
                    inbound=inbound,
                    model=model,
                    provider_id=provider.id,
                    stream=False,
                    status=200,
                    latency_ms=ms,
                    attempts=attempts,
                    prompt_tokens=pt or None,
                    completion_tokens=ct or None,
                )
                return JSONResponse(content=result_inbound, headers={"X-Universal-Router-Provider": provider.id})
            last_status = status
            last_payload = _safe_error(data if isinstance(data, dict) else {"error": str(data)})
            _record_fail(last_pid, str(data)[:200])
            if _can_retry(pi, attempt, n_tries, is_retryable_status(status)):
                if backoff:
                    await asyncio.sleep(backoff)
                continue
            ms = int((time.perf_counter() - t0) * 1000)
            access_log.add(inbound=inbound, model=model, provider_id=last_pid, stream=False, status=status, latency_ms=ms, error=str(data)[:200], attempts=attempts)
            return JSONResponse(content=last_payload, status_code=status, headers={"X-Universal-Router-Provider": last_pid})
    ms = int((time.perf_counter() - t0) * 1000)
    access_log.add(inbound=inbound, model=model, provider_id=last_pid, stream=False, status=last_status, latency_ms=ms, error="exhausted retries", attempts=attempts)
    return JSONResponse(content=_safe_error(last_payload), status_code=last_status, headers={"X-Universal-Router-Provider": last_pid})


def _proxy_stream(
    inbound: str,
    client: httpx.AsyncClient,
    ir: Any,
    model: str,
    candidates: list[ProviderConfig],
    t0: float,
):
    server = config_manager.config.server
    backoff = max(0, int(server.retry_backoff_ms)) / 1000.0
    extra_tries = int(server.retry_count)

    def _can_retry(pi: int, attempt: int, n_tries: int, retryable: bool) -> bool:
        if not retryable:
            return False
        if attempt < n_tries - 1:
            return True
        return bool(server.failover and pi < len(candidates) - 1)

    async def gen():
        attempts = 0
        last_err = "all upstreams failed"
        last_pid = candidates[0].id
        try:
            for pi, provider in enumerate(candidates):
                upstream_body = _from_ir_to_upstream(ir, provider, stream=True)
                n_tries = 1 + extra_tries
                for attempt in range(n_tries):
                    attempts += 1
                    last_pid = provider.id
                    ctx.upstream_mode.set(provider.upstream_mode)
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
                        # 首块已到达；中途错误通过 error_sink 回传，流结束后统一记账
                        sink: list[str] = []
                        async for chunk in convert_stream(inbound, provider.upstream_mode, chained(), stripped, error_sink=sink):
                            yield chunk
                        ms = int((time.perf_counter() - t0) * 1000)
                        if sink:
                            _record_fail(provider.id, sink[0])
                            access_log.add(inbound=inbound, model=model, provider_id=provider.id, stream=True, status=502, latency_ms=ms, error=sink[0][:200], attempts=attempts)
                        else:
                            _record_ok(provider, ms, None)
                            access_log.add(inbound=inbound, model=model, provider_id=provider.id, stream=True, status=200, latency_ms=ms, attempts=attempts)
                        return
                    except StopAsyncIteration:
                        ms = int((time.perf_counter() - t0) * 1000)
                        _record_ok(provider, ms, None)
                        access_log.add(inbound=inbound, model=model, provider_id=provider.id, stream=True, status=200, latency_ms=ms, attempts=attempts)
                        return
                    except StreamTimeoutError as e:
                        last_err = str(e)
                        _record_fail(last_pid, last_err)
                        if e.kind == "first_token" and _can_retry(pi, attempt, n_tries, True):
                            if backoff:
                                await asyncio.sleep(backoff)
                            continue
                        yield _sse({"type": "error", "error": last_err, "timeout": e.kind})
                        yield b"data: [DONE]\n\n"
                        ms = int((time.perf_counter() - t0) * 1000)
                        access_log.add(inbound=inbound, model=model, provider_id=last_pid, stream=True, status=504, latency_ms=ms, error=last_err[:200], attempts=attempts)
                        return
                    except UpstreamHTTPError as e:
                        last_err = redact(e.body.decode(errors="ignore")[:500] if e.body else str(e), _secret_extras())
                        _record_fail(last_pid, last_err)
                        retryable = is_retryable_status(e.status_code)
                        if _can_retry(pi, attempt, n_tries, retryable):
                            if backoff:
                                await asyncio.sleep(backoff)
                            continue
                        yield _sse({"type": "error", "error": last_err, "status": e.status_code})
                        yield b"data: [DONE]\n\n"
                        ms = int((time.perf_counter() - t0) * 1000)
                        access_log.add(inbound=inbound, model=model, provider_id=last_pid, stream=True, status=e.status_code, latency_ms=ms, error=last_err[:200], attempts=attempts)
                        return
                    except Exception as e:
                        last_err = redact(str(e), _secret_extras())
                        _record_fail(last_pid, last_err)
                        if _can_retry(pi, attempt, n_tries, True):
                            if backoff:
                                await asyncio.sleep(backoff)
                            continue
                        yield _sse({"type": "error", "error": last_err})
                        yield b"data: [DONE]\n\n"
                        ms = int((time.perf_counter() - t0) * 1000)
                        access_log.add(inbound=inbound, model=model, provider_id=last_pid, stream=True, status=502, latency_ms=ms, error=last_err[:200], attempts=attempts)
                        return
            yield _sse({"type": "error", "error": last_err})
            yield b"data: [DONE]\n\n"
            ms = int((time.perf_counter() - t0) * 1000)
            access_log.add(inbound=inbound, model=model, provider_id=last_pid, stream=True, status=502, latency_ms=ms, error=last_err[:200], attempts=attempts)
        except asyncio.CancelledError:
            ms = int((time.perf_counter() - t0) * 1000)
            access_log.add(inbound=inbound, model=model, provider_id=last_pid, stream=True, status=499, latency_ms=ms, error="client disconnected", attempts=attempts)
            raise

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
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


@proxy_router.post("/messages/count_tokens")
async def proxy_count_tokens(request: Request):
    _auth_check(request)
    body, model, ir = await _parse_proxy_request("messages", request)
    _set_preview(body)
    candidates = resolve_providers(model)
    if not candidates:
        raise HTTPException(400, f"未知 model '{model}'")
    native = [p for p in candidates if p.upstream_mode == "messages"]
    client: httpx.AsyncClient = request.app.state.httpx_client
    if native:
        provider = native[0]
        ctx.upstream_mode.set(provider.upstream_mode)
        send = _from_ir_to_upstream(ir, provider, stream=False)
        send.pop("stream", None)
        try:
            status, data = await raw_request(client, provider, "POST", "/messages/count_tokens", json_body=send, timeout=20)
        except Exception as e:
            raise HTTPException(502, redact(str(e), _secret_extras())) from e
        if isinstance(data, dict) and 200 <= status < 300:
            return JSONResponse(content=data, headers=_hdr(provider.id))
        if isinstance(data, dict):
            return JSONResponse(content=_safe_error(data), status_code=status if status >= 400 else 502, headers=_hdr(provider.id))
    n = _estimate_tokens(ir)
    access_log.add(inbound="messages", model=model, provider_id=candidates[0].id, stream=False, status=200, latency_ms=0, error=None, preview="count_tokens estimated")
    return JSONResponse(content={"input_tokens": n, "estimated": True}, headers=_hdr(candidates[0].id))


async def _proxy_stored_response(request: Request, rid: str, method: str):
    _auth_check(request)
    providers = [p for p in config_manager.config.providers if p.enabled and p.upstream_mode == "responses"]
    if not providers:
        raise HTTPException(400, f"{method} /v1/responses/{{id}} 需要至少一个 responses 上游（有状态检索无法从 chat/messages 还原）")
    client: httpx.AsyncClient = request.app.state.httpx_client
    last_status = 404
    last_data: Any = {"error": {"message": "response not found"}}
    last_pid = providers[0].id
    for p in providers:
        ctx.upstream_mode.set(p.upstream_mode)
        try:
            status, data = await raw_request(client, p, method, f"/responses/{rid}", timeout=30)
        except Exception as e:
            last_status = 502
            last_data = {"error": {"message": redact(str(e), _secret_extras())}}
            last_pid = p.id
            continue
        last_status, last_data, last_pid = status, data, p.id
        if 200 <= status < 300:
            body = data if isinstance(data, dict) else {"data": data.decode(errors="ignore") if isinstance(data, bytes) else data}
            return JSONResponse(content=body, status_code=status, headers=_hdr(p.id))
        if status not in (404, 400):
            payload = _safe_error(data) if isinstance(data, dict) else {"error": {"message": "upstream error"}}
            return JSONResponse(content=payload, status_code=status, headers=_hdr(p.id))
    payload = _safe_error(last_data) if isinstance(last_data, dict) else {"error": {"message": "not found"}}
    return JSONResponse(content=payload, status_code=last_status, headers=_hdr(last_pid))


@proxy_router.get("/responses/{rid}")
async def get_response(rid: str, request: Request):
    return await _proxy_stored_response(request, rid, "GET")


@proxy_router.delete("/responses/{rid}")
async def delete_response(rid: str, request: Request):
    return await _proxy_stored_response(request, rid, "DELETE")


@manage_router.post("/play/{inbound}")
async def play_proxy(inbound: str, request: Request):
    """管理页试聊：复用 /api 鉴权；provider Key 不回传前端，试聊不能要求入站 Key。"""
    if inbound not in ("chat", "responses", "messages"):
        raise HTTPException(404, "unknown inbound")
    return await _handle_proxy(inbound, request, skip_auth=True)
