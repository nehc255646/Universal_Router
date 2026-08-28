"""上游转发 — httpx 封装"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from .config import ProviderConfig


def upstream_path_for(mode: str) -> str:
    if mode == "chat_completions":
        return "/chat/completions"
    if mode == "responses":
        return "/responses"
    if mode == "messages":
        return "/messages"
    return "/chat/completions"


def build_headers(provider: ProviderConfig) -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if provider.api_key:
        h["Authorization"] = f"Bearer {provider.api_key}"
    for hdr in provider.headers:
        h[hdr.name] = hdr.value
    if provider.upstream_mode == "messages":
        h["anthropic-version"] = "2023-06-01"
        has_xkey = any(k.lower() == "x-api-key" for k in h)
        if has_xkey and "Authorization" in h:
            # Anthropic 优先 x-api-key，移除 Bearer 避免上游校验冲突
            h.pop("Authorization", None)
    return h


async def post_non_stream(
    client: httpx.AsyncClient,
    provider: ProviderConfig,
    body: dict[str, Any],
    timeout: float = 120,
) -> tuple[int, dict[str, Any] | bytes]:
    url = provider.base_url.rstrip("/") + upstream_path_for(provider.upstream_mode)
    headers = build_headers(provider)
    # Anthropic 需 x-api-key 而非 Bearer
    if provider.upstream_mode == "messages" and provider.api_key and "x-api-key" not in {k.lower(): v for k, v in headers.items()}:
        # 如果用户未在 headers 配置 x-api-key，则将 api_key 同时设为 x-api-key
        # 保留 Authorization 兼容部分中转
        headers["x-api-key"] = provider.api_key

    resp = await client.post(url, json=body, headers=headers, timeout=timeout)
    # 尝试解析 json
    try:
        data = resp.json()
    except Exception:
        data = resp.content  # type: ignore
    return resp.status_code, data


async def stream_upstream(
    client: httpx.AsyncClient,
    provider: ProviderConfig,
    body: dict[str, Any],
    timeout: float = 300,
) -> AsyncIterator[bytes]:
    url = provider.base_url.rstrip("/") + upstream_path_for(provider.upstream_mode)
    headers = build_headers(provider)
    if provider.upstream_mode == "messages" and provider.api_key and "x-api-key" not in {k.lower(): v for k, v in headers.items()}:
        headers["x-api-key"] = provider.api_key
    headers["Accept"] = "text/event-stream"

    async with client.stream("POST", url, json=body, headers=headers, timeout=timeout) as resp:
        # 流式错误透传：上游非 2xx 时直接抛出，由 _handle_proxy 统一转为 JSON 错误
        if resp.status_code >= 400:
            # 尽力读取错误体，避免挂死
            err_body = await resp.aread()
            # 抛出带状态码的异常，上层捕获
            raise httpx.HTTPStatusError(f"upstream {resp.status_code}", request=resp.request, response=resp)
        async for chunk in resp.aiter_bytes():
            if chunk:
                yield chunk
