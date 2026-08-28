"""上游转发 — httpx 封装"""
from __future__ import annotations

from typing import Any, AsyncIterator

import httpx

from .config import ProviderConfig


class UpstreamHTTPError(Exception):
    def __init__(self, status_code: int, body: bytes):
        self.status_code = status_code
        self.body = body
        super().__init__(f"upstream {status_code}")


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
        if hdr.name:
            h[hdr.name] = hdr.value
    if provider.upstream_mode == "messages":
        h.setdefault("anthropic-version", "2023-06-01")
        has_xkey = any(k.lower() == "x-api-key" for k in h)
        if has_xkey and "Authorization" in h:
            h.pop("Authorization", None)
        if provider.api_key and not has_xkey:
            h["x-api-key"] = provider.api_key
    return h


def _url(provider: ProviderConfig, path: str) -> str:
    return provider.base_url.rstrip("/") + path


async def post_non_stream(
    client: httpx.AsyncClient,
    provider: ProviderConfig,
    body: dict[str, Any],
    timeout: float = 120,
) -> tuple[int, dict[str, Any] | bytes]:
    url = _url(provider, upstream_path_for(provider.upstream_mode))
    headers = build_headers(provider)
    resp = await client.post(url, json=body, headers=headers, timeout=timeout)
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

    async with client.stream("POST", url, json=body, headers=headers, timeout=timeout) as resp:
        if resp.status_code >= 400:
            err_body = await resp.aread()
            raise UpstreamHTTPError(resp.status_code, err_body)
        async for chunk in resp.aiter_bytes():
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
    resp = await client.get(url, headers=headers, timeout=timeout)
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
