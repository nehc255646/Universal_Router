"""SQLite 持久化访问日志。"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_started = time.time()
_conn: sqlite3.Connection | None = None
_path: Path | None = None

DEFAULT_RETAIN = 5000
_CLEANUP_INTERVAL = 500  # 每 N 次插入才做一次 COUNT/清理，避免每次请求全表计数
_since_cleanup = 0


def started_at() -> float:
    return _started


def default_path() -> Path:
    env = os.getenv("UR_LOG_DB")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "access.db"


def configure(path: Path | None = None) -> None:
    global _conn, _path
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
        _path = path or default_path()


def _db() -> sqlite3.Connection:
    global _conn, _path
    if _conn is None:
        if _path is None:
            _path = default_path()
        _path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(_path), check_same_thread=False, timeout=8)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                inbound TEXT,
                model TEXT,
                provider_id TEXT,
                stream INTEGER,
                status INTEGER,
                latency_ms INTEGER,
                error TEXT,
                attempts INTEGER DEFAULT 1,
                prompt_tokens INTEGER,
                completion_tokens INTEGER
            )
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(logs)").fetchall()}
        if "prompt_tokens" not in cols:
            conn.execute("ALTER TABLE logs ADD COLUMN prompt_tokens INTEGER")
        if "completion_tokens" not in cols:
            conn.execute("ALTER TABLE logs ADD COLUMN completion_tokens INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC)")
        conn.commit()
        _conn = conn
    return _conn


def add(
    *,
    inbound: str,
    model: str,
    provider_id: str | None,
    stream: bool,
    status: int,
    latency_ms: int,
    error: str | None = None,
    attempts: int = 1,
    retain: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    from .config import config_manager
    from .secrets import collect_secrets, redact

    keep = retain if retain is not None else int(getattr(config_manager.config.server, "log_retain", DEFAULT_RETAIN) or DEFAULT_RETAIN)
    extras = collect_secrets(
        config_manager.config.server.local_api_key,
        config_manager.config.server.admin_api_key,
        *[p.api_key for p in config_manager.config.providers],
    )
    err = redact((error or "")[:2000], extras) or None
    row = (
        time.time(),
        inbound,
        model,
        provider_id,
        1 if stream else 0,
        status,
        latency_ms,
        err,
        attempts,
        prompt_tokens,
        completion_tokens,
    )
    with _lock:
        global _since_cleanup
        db = _db()
        db.execute(
            "INSERT INTO logs (ts, inbound, model, provider_id, stream, status, latency_ms, error, attempts, prompt_tokens, completion_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )
        db.commit()
        _since_cleanup += 1
        if _since_cleanup >= _CLEANUP_INTERVAL:
            _since_cleanup = 0
            cur = db.execute("SELECT COUNT(*) FROM logs")
            n = int(cur.fetchone()[0])
            if n > keep * 1.2:
                db.execute("DELETE FROM logs WHERE id IN (SELECT id FROM logs ORDER BY ts ASC LIMIT ?)", (n - keep,))
                db.commit()


def list_logs(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    with _lock:
        db = _db()
        total = int(db.execute("SELECT COUNT(*) FROM logs").fetchone()[0])
        rows = db.execute(
            "SELECT id, ts, inbound, model, provider_id, stream, status, latency_ms, error, attempts, prompt_tokens, completion_tokens FROM logs ORDER BY ts DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    items = []
    for r in rows:
        items.append(
            {
                "id": r["id"],
                "ts": r["ts"],
                "inbound": r["inbound"],
                "model": r["model"],
                "provider_id": r["provider_id"],
                "stream": bool(r["stream"]),
                "status": r["status"],
                "latency_ms": r["latency_ms"],
                "error": r["error"],
                "attempts": r["attempts"],
                "prompt_tokens": r["prompt_tokens"],
                "completion_tokens": r["completion_tokens"],
            }
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def clear() -> None:
    with _lock:
        db = _db()
        db.execute("DELETE FROM logs")
        db.commit()
