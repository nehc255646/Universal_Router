"""入口 — FastAPI app"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import config_manager
from .server import manage_router, proxy_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.httpx_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15, read=300, write=60, pool=15),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    yield
    await app.state.httpx_client.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Universal Router",
        version=__version__,
        description="本地三协议互转网关：OpenAI Chat Completions / Responses / Anthropic Messages",
        lifespan=lifespan,
    )

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

    max_body = int(os.getenv("UR_MAX_BODY", str(4 * 1024 * 1024)))

    @app.middleware("http")
    async def limit_body_size(request, call_next):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > max_body:
            return JSONResponse({"detail": f"请求体过大，限制 {max_body} bytes"}, status_code=413)
        return await call_next(request)

    app.include_router(manage_router)
    app.include_router(proxy_router)

    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


app = create_app()


def run():
    import uvicorn

    cfg = config_manager.config
    uvicorn.run("app.main:app", host=cfg.server.host, port=cfg.server.port, reload=False)


if __name__ == "__main__":
    run()
