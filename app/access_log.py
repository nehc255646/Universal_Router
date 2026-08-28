"""内存环形访问日志，供管理页查看最近请求。"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

_lock = threading.Lock()
_logs: deque[dict[str, Any]] = deque(maxlen=200)
_started = time.time()


def started_at() -> float:
    return _started


def add(
    *,
    inbound: str,
    model: str,
    provider_id: str | None,
    stream: bool,
    status: int,
    latency_ms: int,
    error: str | None = None,
) -> None:
    entry = {
        "ts": time.time(),
        "inbound": inbound,
        "model": model,
        "provider_id": provider_id,
        "stream": stream,
        "status": status,
        "latency_ms": latency_ms,
        "error": error,
    }
    with _lock:
        _logs.appendleft(entry)


def list_logs(limit: int = 100) -> list[dict[str, Any]]:
    with _lock:
        return list(_logs)[:limit]


def clear() -> None:
    with _lock:
        _logs.clear()
