"""多提供商路由：优先级 / 轮询 / 加权，失败可 failover。"""
from __future__ import annotations

import itertools
import threading
from typing import Literal

from .config import ProviderConfig, config_manager

RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
Strategy = Literal["priority", "round_robin", "weighted"]

_lock = threading.Lock()
_rr = {}  # key -> counter


def reset_rr() -> None:
    with _lock:
        _rr.clear()


def is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUS


def _enabled(providers: list[ProviderConfig]) -> list[ProviderConfig]:
    return [p for p in providers if p.enabled]


def _matches(p: ProviderConfig, mid: str) -> bool:
    return any(m.id == mid for m in p.models)


def match_providers(model: str, *, include_disabled: bool = False) -> list[ProviderConfig]:
    cfg = config_manager.config
    pool = list(cfg.providers) if include_disabled else _enabled(cfg.providers)
    pinned_id = None
    mid = model
    if "/" in model:
        pinned_id, mid = model.split("/", 1)

    hits: list[ProviderConfig] = []
    seen: set[str] = set()

    def add(p: ProviderConfig) -> None:
        if p.id not in seen:
            seen.add(p.id)
            hits.append(p)

    if pinned_id:
        pinned = next((p for p in pool if p.id == pinned_id), None)
        if pinned:
            add(pinned)
        for p in pool:
            if _matches(p, mid):
                add(p)
        return hits

    for p in pool:
        if _matches(p, mid):
            add(p)
    if not hits:
        enabled = _enabled(cfg.providers)
        if len(enabled) == 1:
            add(enabled[0])
    return hits


def _order(cands: list[ProviderConfig], strategy: str, key: str) -> list[ProviderConfig]:
    if not cands:
        return []
    ranked = sorted(cands, key=lambda p: (p.priority, p.id))
    if strategy not in ("round_robin", "weighted"):
        return ranked
    min_p = ranked[0].priority
    head = [p for p in ranked if p.priority == min_p]
    tail = [p for p in ranked if p.priority != min_p]
    with _lock:
        n = _rr.get(key, 0)
        _rr[key] = n + 1
    if strategy == "weighted":
        expanded: list[ProviderConfig] = []
        for p in head:
            expanded.extend([p] * max(1, int(p.weight or 1)))
        pick = expanded[n % len(expanded)]
        rest = [p for p in head if p.id != pick.id]
        return [pick, *rest, *tail]
    pick_i = n % len(head)
    rotated = head[pick_i:] + head[:pick_i]
    return rotated + tail


def resolve_providers(model: str) -> list[ProviderConfig]:
    """返回尝试顺序：首选 + 可选 failover 列表。"""
    server = config_manager.config.server
    cands = match_providers(model)
    ordered = _order(cands, server.route_strategy, model)
    if not ordered:
        return []
    if "/" in model:
        prefix = model.split("/", 1)[0]
        pinned = [p for p in ordered if p.id == prefix]
        others = [p for p in ordered if p.id != prefix]
        if pinned:
            ordered = pinned + others
    if not server.failover:
        return ordered[:1]
    return ordered
