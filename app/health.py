"""提供商健康：熔断、自动恢复、延迟 EWMA、token/成本累计、动态评分。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

State = Literal["closed", "open", "half_open"]

_lock = threading.Lock()
_stats: dict[str, ProviderStats] = {}
_ALPHA = 0.3


@dataclass
class ProviderStats:
    provider_id: str
    state: State = "closed"
    ewma_latency_ms: float = 0.0
    success: int = 0
    failure: int = 0
    consecutive_failures: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    est_cost: float = 0.0
    last_error: str | None = None
    last_success_at: float | None = None
    last_fail_at: float | None = None
    opened_at: float | None = None
    half_open_probe: bool = False


def reset_health() -> None:
    with _lock:
        _stats.clear()


def prune(pids: list[str]) -> list[str]:
    """删除不在 pids 中的统计条目（provider 已删除/重命名），返回被清理的 id。"""
    with _lock:
        known = set(pids)
        stale = [k for k in _stats if k not in known]
        for k in stale:
            del _stats[k]
        return stale


def _get(pid: str) -> ProviderStats:
    s = _stats.get(pid)
    if s is None:
        s = ProviderStats(provider_id=pid)
        _stats[pid] = s
    return s


def _score(s: ProviderStats) -> float:
    if s.state == "open":
        return 0.0
    total = s.success + s.failure
    rate = (s.success / total) if total else 0.8
    lat = s.ewma_latency_ms or 800.0
    lat_score = 1000.0 / (1000.0 + lat)
    bonus = 0.05 if s.state == "closed" else 0.0
    return max(0.0, min(100.0, (rate * 70.0 + lat_score * 25.0 + bonus * 100.0)))


def snapshot(pid: str) -> dict[str, Any]:
    with _lock:
        s = _get(pid)
        return {
            "provider_id": pid,
            "state": s.state,
            "ewma_latency_ms": round(s.ewma_latency_ms, 1),
            "success": s.success,
            "failure": s.failure,
            "consecutive_failures": s.consecutive_failures,
            "tokens_in": s.tokens_in,
            "tokens_out": s.tokens_out,
            "est_cost": round(s.est_cost, 6),
            "last_error": s.last_error,
            "last_success_at": s.last_success_at,
            "last_fail_at": s.last_fail_at,
            "score": round(_score(s), 1),
        }


def all_snapshots(pids: list[str]) -> list[dict[str, Any]]:
    prune(pids)
    return [snapshot(p) for p in pids]


def latency_ms(pid: str) -> float:
    with _lock:
        return _get(pid).ewma_latency_ms


def score(pid: str) -> float:
    with _lock:
        return _score(_get(pid))


def _maybe_half_open(s: ProviderStats, cooldown_s: float) -> None:
    if s.state != "open":
        return
    if s.opened_at is None:
        s.state = "half_open"
        s.half_open_probe = False
        return
    if time.time() - s.opened_at >= cooldown_s:
        s.state = "half_open"
        s.half_open_probe = False


def is_available(pid: str, *, enabled: bool, cooldown_s: float) -> bool:
    """closed 可用；open 冷却后半开可探测一次；半开仅允许一次并发探测。"""
    if not enabled:
        return False
    with _lock:
        s = _get(pid)
        _maybe_half_open(s, cooldown_s)
        if s.state == "closed":
            return True
        if s.state == "half_open":
            if s.half_open_probe:
                return False
            s.half_open_probe = True
            return True
        return False


def record_success(
    pid: str,
    latency_ms: int,
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost: float = 0.0,
) -> None:
    with _lock:
        s = _get(pid)
        s.success += 1
        s.consecutive_failures = 0
        s.last_success_at = time.time()
        s.last_error = None
        if s.ewma_latency_ms <= 0:
            s.ewma_latency_ms = float(latency_ms)
        else:
            s.ewma_latency_ms = _ALPHA * float(latency_ms) + (1 - _ALPHA) * s.ewma_latency_ms
        s.tokens_in += max(0, int(tokens_in))
        s.tokens_out += max(0, int(tokens_out))
        s.est_cost += max(0.0, float(cost))
        s.state = "closed"
        s.opened_at = None
        s.half_open_probe = False


def record_failure(pid: str, *, error: str | None, threshold: int) -> None:
    with _lock:
        s = _get(pid)
        s.failure += 1
        s.consecutive_failures += 1
        s.last_fail_at = time.time()
        s.last_error = (error or "")[:300] or None
        s.half_open_probe = False
        if s.state == "half_open" or s.consecutive_failures >= max(1, int(threshold)):
            s.state = "open"
            s.opened_at = time.time()
