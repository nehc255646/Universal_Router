"""入口 — FastAPI app"""
from __future__ import annotations

import os
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, health as provider_health
from .config import config_manager, is_loopback_bind
from .ctx import request_id as request_id_var
from .server import manage_router, proxy_router

ASGIApp = Callable[[dict, Callable, Callable], Awaitable[None]]
Message = dict


class _BodyTooLarge(Exception):
    pass


class RequestIdMiddleware:
    """为每个请求分配 X-Request-Id，并写入响应头。"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"x-request-id") or headers.get(b"X-Request-Id") or b""
        rid = raw.decode("utf-8", errors="ignore").strip()[:128] or uuid.uuid4().hex[:24]
        token = request_id_var.set(rid)

        async def wrapped_send(msg: Message) -> None:
            if msg["type"] == "http.response.start":
                hdrs = list(msg.get("headers") or [])
                if not any(k.lower() == b"x-request-id" for k, _ in hdrs):
                    hdrs.append((b"x-request-id", rid.encode("utf-8")))
                msg = {**msg, "headers": hdrs}
            await send(msg)

        try:
            await self.app(scope, receive, wrapped_send)
        finally:
            request_id_var.reset(token)


def _effective_bind_host() -> str:
    return (os.getenv("UR_BIND_HOST") or config_manager.config.server.host or "127.0.0.1").strip()


def is_auth_lockdown() -> bool:
    if os.getenv("UR_ALLOW_INSECURE_BIND", "").strip() in ("1", "true", "yes"):
        return False
    cfg = config_manager.config.server
    if is_loopback_bind(_effective_bind_host()):
        return False
    return not ((cfg.admin_api_key or "").strip() or (cfg.local_api_key or "").strip())


class AuthLockdownMiddleware:
    """绑定非本机且未配置 Key 时，除 /health 外一律 403。"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if is_auth_lockdown() and path not in ("/health", "/health/"):
            await JSONResponse({"detail": "绑定非本机地址时必须设置 admin_api_key 或 local_api_key"}, status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    """限制请求体大小；无 Content-Length（chunked）时按实际接收字节累计校验。"""

    def __init__(self, app: ASGIApp, max_body: int):
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        detail = {"detail": f"请求体过大，限制 {self.max_body} bytes"}
        headers = dict(scope.get("headers") or [])
        cl = headers.get(b"content-length")
        if cl and cl.isdigit() and int(cl) > self.max_body:
            await JSONResponse(detail, status_code=413)(scope, receive, send)
            return
        total = 0
        response_started = False

        async def wrapped_receive() -> Message:
            nonlocal total
            msg = await receive()
            if msg["type"] == "http.request":
                total += len(msg.get("body") or b"")
                if total > self.max_body:
                    raise _BodyTooLarge()
            return msg

        async def wrapped_send(msg: Message) -> None:
            nonlocal response_started
            if msg["type"] == "http.response.start":
                response_started = True
            await send(msg)

        try:
            await self.app(scope, wrapped_receive, wrapped_send)
        except _BodyTooLarge:
            if not response_started:
                await JSONResponse(detail, status_code=413)(scope, receive, send)
            # 响应已开始时无法再回 413，直接断开连接


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = config_manager.config.server
    app.state.bind_host = _effective_bind_host()
    app.state.bind_port = cfg.port
    app.state.httpx_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15, read=300, write=60, pool=15),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    try:
        provider_health.load_persisted()
    except Exception:
        pass
    if is_auth_lockdown():
        print("[security] 绑定非本机且未设置 Key：/v1 与 /api 已锁定，仅 /health 可用")
    yield
    await app.state.httpx_client.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Universal Router",
        version=__version__,
        description="本地三协议互转网关：OpenAI Chat Completions / Responses / Anthropic Messages",
        lifespan=lifespan,
    )

    max_body = int(os.getenv("UR_MAX_BODY", str(4 * 1024 * 1024)))
    app.add_middleware(BodySizeLimitMiddleware, max_body=max_body)
    app.add_middleware(AuthLockdownMiddleware)
    app.add_middleware(RequestIdMiddleware)

    allowed_origins = [o.strip() for o in os.getenv("UR_CORS_ORIGINS", "").split(",") if o.strip()]
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/health")
    async def health():
        cfg = config_manager.config
        return {
            "ok": True,
            "version": __version__,
            "providers": len(cfg.providers),
        }

    app.include_router(manage_router)
    app.include_router(proxy_router)

    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


app = create_app()


def run():
    import sys

    import uvicorn

    cfg = config_manager.config
    if is_auth_lockdown():
        print("绑定非本机地址时必须设置 admin_api_key 或 local_api_key", file=sys.stderr)
        raise SystemExit(2)
    uvicorn.run("app.main:app", host=cfg.server.host, port=cfg.server.port, reload=False)


if __name__ == "__main__":
    run()
