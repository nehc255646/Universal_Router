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

from .config import config_manager
from .server import manage_router, proxy_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.httpx_client = httpx.AsyncClient(timeout=httpx.Timeout(120, read=300))
    yield
    await app.state.httpx_client.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="Universal Router", version="0.1.0", lifespan=lifespan)

    # 本地网关无需宽 CORS，仅放行本地前端；如需跨域请显式配置环境变量
    allowed_origins = os.getenv("UR_CORS_ORIGINS", "").split(",") if os.getenv("UR_CORS_ORIGINS") else []
    if allowed_origins and allowed_origins != [""]:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[o.strip() for o in allowed_origins if o.strip()],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/health")
    async def health():
        return {"ok": True}

    # 限制请求体 4MB，防 OOM；可在环境变量 UR_MAX_BODY 覆盖
    max_body = int(os.getenv("UR_MAX_BODY", str(4 * 1024 * 1024)))

    @app.middleware("http")
    async def limit_body_size(request, call_next):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > max_body:
            return JSONResponse({"detail": f"请求体过大，限制 {max_body} bytes"}, status_code=413)
        return await call_next(request)

    app.include_router(manage_router)
    app.include_router(proxy_router)

    # 静态前端挂载在 / 需放在 API 之后，且 html=True 会吞 404，故加校验：仅挂载文件存在时
    # 使用 mount 但 FastAPI 会优先匹配已注册路由，故安全；为防 SPA 劫持 /v1 404，显式保留 404 透传
    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.exists():
        # 仅在非 API/非 health 路径回落到 index.html，404 仍由 FastAPI 正常返回
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


app = create_app()


def run():
    import uvicorn

    cfg = config_manager.config
    uvicorn.run("app.main:app", host=cfg.server.host, port=cfg.server.port, reload=False)


if __name__ == "__main__":
    run()
