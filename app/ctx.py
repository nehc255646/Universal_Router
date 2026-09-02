"""请求级上下文：X-Request-Id、失败摘要。"""
from __future__ import annotations

from contextvars import ContextVar

request_id: ContextVar[str] = ContextVar("ur_request_id", default="")
log_preview: ContextVar[str] = ContextVar("ur_log_preview", default="")
upstream_mode: ContextVar[str] = ContextVar("ur_upstream_mode", default="")
