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
        for col, decl in (
            ("prompt_tokens", "INTEGER"),
            ("completion_tokens", "INTEGER"),
            ("request_id", "TEXT"),
            ("upstream_mode", "TEXT"),
            ("preview", "TEXT"),
        ):
            if col not in cols:
                conn.execute(f"ALTER TABLE logs ADD COLUMN {col} {decl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_stats (
                provider_id TEXT PRIMARY KEY,
                state TEXT,
                ewma_latency_ms REAL,
                success INTEGER,
                failure INTEGER,
                consecutive_failures INTEGER,
                tokens_in INTEGER,
                tokens_out INTEGER,
                est_cost REAL,
                last_error TEXT,
                last_success_at REAL,
                last_fail_at REAL,
                opened_at REAL
            )
            """
        )
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
    request_id: str | None = None,
    upstream_mode: str | None = None,
    preview: str | None = None,
) -> None:
    from .config import config_manager
    from .ctx import log_preview as preview_var
    from .ctx import request_id as rid_var
    from .ctx import upstream_mode as um_var
    from .secrets import collect_secrets, redact

    keep = retain if retain is not None else int(getattr(config_manager.config.server, "log_retain", DEFAULT_RETAIN) or DEFAULT_RETAIN)
    extras = collect_secrets(
        config_manager.config.server.local_api_key,
        config_manager.config.server.admin_api_key,
        *[x for p in config_manager.config.providers for x in (p.api_key, p.inbound_key)],
    )
    err = redact((error or "")[:2000], extras) or None
    prev = redact((preview or preview_var.get() or "")[:1500], extras) or None
    rid = request_id if request_id is not None else (rid_var.get() or None)
    upstream_mode = upstream_mode if upstream_mode is not None else (um_var.get() or None)
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
        rid,
        upstream_mode,
        prev,
    )
    with _lock:
        global _since_cleanup
        db = _db()
        db.execute(
            "INSERT INTO logs (ts, inbound, model, provider_id, stream, status, latency_ms, error, attempts, prompt_tokens, completion_tokens, request_id, upstream_mode, preview) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            "SELECT id, ts, inbound, model, provider_id, stream, status, latency_ms, error, attempts, prompt_tokens, completion_tokens, request_id, upstream_mode, preview FROM logs ORDER BY ts DESC LIMIT ? OFFSET ?",
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
                "request_id": r["request_id"] if "request_id" in r.keys() else None,
                "upstream_mode": r["upstream_mode"] if "upstream_mode" in r.keys() else None,
                "preview": r["preview"] if "preview" in r.keys() else None,
            }
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def clear() -> None:
    with _lock:
        db = _db()
        db.execute("DELETE FROM logs")
        db.commit()


def load_provider_stats() -> list[dict[str, Any]]:
    with _lock:
        db = _db()
        rows = db.execute("SELECT * FROM provider_stats").fetchall()
    return [dict(r) for r in rows]


def save_provider_stats(row: dict[str, Any]) -> None:
    with _lock:
        db = _db()
        db.execute(
            """
            INSERT INTO provider_stats (
                provider_id, state, ewma_latency_ms, success, failure, consecutive_failures,
                tokens_in, tokens_out, est_cost, last_error, last_success_at, last_fail_at, opened_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(provider_id) DO UPDATE SET
                state=excluded.state,
                ewma_latency_ms=excluded.ewma_latency_ms,
                success=excluded.success,
                failure=excluded.failure,
                consecutive_failures=excluded.consecutive_failures,
                tokens_in=excluded.tokens_in,
                tokens_out=excluded.tokens_out,
                est_cost=excluded.est_cost,
                last_error=excluded.last_error,
                last_success_at=excluded.last_success_at,
                last_fail_at=excluded.last_fail_at,
                opened_at=excluded.opened_at
            """,
            (
                row["provider_id"],
                row.get("state") or "closed",
                float(row.get("ewma_latency_ms") or 0),
                int(row.get("success") or 0),
                int(row.get("failure") or 0),
                int(row.get("consecutive_failures") or 0),
                int(row.get("tokens_in") or 0),
                int(row.get("tokens_out") or 0),
                float(row.get("est_cost") or 0),
                row.get("last_error"),
                row.get("last_success_at"),
                row.get("last_fail_at"),
                row.get("opened_at"),
            ),
        )
        db.commit()


def delete_provider_stats(pids_keep: list[str]) -> None:
    with _lock:
        db = _db()
        if not pids_keep:
            db.execute("DELETE FROM provider_stats")
        else:
            placeholders = ",".join("?" * len(pids_keep))
            db.execute(f"DELETE FROM provider_stats WHERE provider_id NOT IN ({placeholders})", pids_keep)
        db.commit()
