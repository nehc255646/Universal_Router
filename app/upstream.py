"""上游转发 — httpx 封装，区分 connect / first-token / idle 超时。"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import ProviderConfig, config_manager
from .secrets import resolve_secret


class UpstreamHTTPError(Exception):
    def __init__(self, status_code: int, body: bytes):
        self.status_code = status_code
        self.body = body
        super().__init__(f"upstream {status_code}")


class StreamTimeoutError(Exception):
    def __init__(self, kind: str, timeout: float):
        self.kind = kind  # first_token | idle
        self.timeout = timeout
        super().__init__(f"{kind} timeout after {timeout}s")


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
    key = resolve_secret(provider.api_key)
    if key:
        h["Authorization"] = f"Bearer {key}"
    for hdr in provider.headers:
        if hdr.name:
            val = resolve_secret(hdr.value) if hdr.value else hdr.value
            h[hdr.name] = val
    if provider.upstream_mode == "messages":
        h.setdefault("anthropic-version", "2023-06-01")
        has_xkey = any(k.lower() == "x-api-key" for k in h)
        if has_xkey and "Authorization" in h:
            h.pop("Authorization", None)
        if key and not has_xkey:
            h["x-api-key"] = key
    return h


def _url(provider: ProviderConfig, path: str) -> str:
    return provider.base_url.rstrip("/") + path


def _timeouts(provider: ProviderConfig, read: float) -> tuple[httpx.Timeout, float, float]:
    srv = config_manager.config.server
    connect = float(getattr(srv, "connect_timeout_s", 15) or 15)
    first = float(getattr(srv, "first_token_timeout_s", 45) or 0)
    idle = float(getattr(srv, "read_idle_timeout_s", 90) or 0)
    read_s = max(float(read), idle or 0, first or 0, 30)
    return httpx.Timeout(connect=connect, read=read_s, write=60, pool=15), first, idle


async def post_non_stream(
    client: httpx.AsyncClient,
    provider: ProviderConfig,
    body: dict[str, Any],
    timeout: float = 120,
) -> tuple[int, dict[str, Any] | bytes]:
    url = _url(provider, upstream_path_for(provider.upstream_mode))
    headers = build_headers(provider)
    t, _, _ = _timeouts(provider, timeout)
    resp = await client.post(url, json=body, headers=headers, timeout=t)
    try:
        data = resp.json()
    except Exception:
        data = resp.content
    return resp.status_code, data


async def stream_upstream(
    client: httpx.AsyncClient,
    provider: ProviderConfig,
    body: dict[str, Any],
    timeout: float = 300,
) -> AsyncIterator[bytes]:
    url = _url(provider, upstream_path_for(provider.upstream_mode))
    headers = build_headers(provider)
    headers["Accept"] = "text/event-stream"
    t, first, idle = _timeouts(provider, timeout)

    async with client.stream("POST", url, json=body, headers=headers, timeout=t) as resp:
        if resp.status_code >= 400:
            err_body = await resp.aread()
            raise UpstreamHTTPError(resp.status_code, err_body)
        aiter = resp.aiter_bytes().__aiter__()
        awaiting_first = True
        while True:
            limit = first if awaiting_first else idle
            try:
                if limit and limit > 0:
                    chunk = await asyncio.wait_for(aiter.__anext__(), timeout=limit)
                else:
                    chunk = await aiter.__anext__()
            except StopAsyncIteration:
                break
            except TimeoutError as e:
                kind = "first_token" if awaiting_first else "idle"
                raise StreamTimeoutError(kind, float(limit or 0)) from e
            awaiting_first = False
            if chunk:
                yield chunk


async def fetch_upstream_models(
    client: httpx.AsyncClient,
    provider: ProviderConfig,
    timeout: float = 20,
) -> tuple[int, list[dict[str, Any]] | dict[str, Any] | str]:
    url = _url(provider, "/models")
    headers = build_headers(provider)
    headers.pop("Content-Type", None)
    t, _, _ = _timeouts(provider, timeout)
    resp = await client.get(url, headers=headers, timeout=t)
    try:
        data = resp.json()
    except Exception:
        return resp.status_code, resp.text[:2000]
    if resp.status_code >= 400:
        return resp.status_code, data if isinstance(data, dict) else {"error": str(data)}
    models: list[dict[str, Any]] = []
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        for item in data["data"]:
            if not isinstance(item, dict):
                continue
            mid = item.get("id") or item.get("name")
            if mid:
                models.append({"id": str(mid), "display_name": str(item.get("display_name") or item.get("name") or mid)})
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                models.append({"id": item, "display_name": item})
            elif isinstance(item, dict) and item.get("id"):
                models.append({"id": str(item["id"]), "display_name": str(item.get("display_name") or item["id"])})
    return resp.status_code, models
