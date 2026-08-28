from __future__ import annotations

import asyncio

from fastapi.responses import JSONResponse
from starlette.requests import Request

from app import server as srv
from app.config import ProviderConfig
from app.health import all_snapshots, prune, record_success, snapshot


def _disconnect_request() -> Request:
    async def receive():
        return {"type": "http.disconnect"}

    return Request({"type": "http", "method": "POST", "path": "/"}, receive)


def test_health_prune_removes_stale():
    record_success("gone", 10)
    record_success("keep", 20)
    assert snapshot("gone")["success"] == 1
    removed = prune(["keep"])
    assert removed == ["gone"]
    assert snapshot("gone")["success"] == 0
    assert snapshot("keep")["success"] == 1


def test_all_snapshots_prunes_stale():
    record_success("stale", 10)
    snaps = all_snapshots(["live"])
    assert [s["provider_id"] for s in snaps] == ["live"]


def test_watch_disconnect_true():
    request = _disconnect_request()
    assert asyncio.run(srv._watch_disconnect(request, interval=0.01)) is True


def test_post_with_disconnect_watch_cancels_upstream(monkeypatch):
    started = asyncio.Event()

    async def slow_post(*args, **kwargs):
        started.set()
        await asyncio.sleep(10)
        return 200, {}

    monkeypatch.setattr(srv, "post_non_stream", slow_post)
    provider = ProviderConfig(id="p", base_url="https://example.com/v1")
    request = _disconnect_request()

    async def run():
        return await srv._post_with_disconnect_watch(None, provider, {}, 10, request)

    assert asyncio.run(run()) is None


def test_post_with_disconnect_watch_returns_result(monkeypatch):
    async def fast_post(*args, **kwargs):
        return 200, {"ok": True}

    monkeypatch.setattr(srv, "post_non_stream", fast_post)
    provider = ProviderConfig(id="p", base_url="https://example.com/v1")

    async def receive():
        await asyncio.sleep(10)  # never disconnects during the call
        return {"type": "http.disconnect"}

    request = Request({"type": "http", "method": "POST", "path": "/"}, receive)

    async def run():
        return await srv._post_with_disconnect_watch(None, provider, {}, 10, request)

    assert asyncio.run(run()) == (200, {"ok": True})


def test_body_size_limit_middleware_chunked():
    from app.main import BodySizeLimitMiddleware

    received: dict = {}

    async def endpoint(scope, receive, send):
        body = b""
        while True:
            msg = await receive()
            if msg["type"] == "http.request":
                body += msg.get("body") or b""
                if not msg.get("more_body"):
                    break
            elif msg["type"] == "http.disconnect":
                break
        received["len"] = len(body)
        await JSONResponse({"len": len(body)})(scope, receive, send)

    mw = BodySizeLimitMiddleware(endpoint, max_body=10)

    def make_receive(chunks):
        it = iter(chunks)

        async def receive():
            try:
                chunk = next(it)
            except StopIteration:
                return {"type": "http.disconnect"}
            return {"type": "http.request", "body": chunk, "more_body": bool(chunk)}

        return receive

    async def run(chunks):
        sent: list[dict] = []

        async def send(msg):
            sent.append(msg)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"host", b"t")],
            "query_string": b"",
        }
        await mw(scope, make_receive(chunks), send)
        return sent

    # 9 + 5 = 14 > 10 → 413
    sent = asyncio.run(run([b"123456789", b"12345", b""]))
    assert sent[0]["status"] == 413
    assert received == {}

    # 3 + 3 = 6 <= 10 → OK
    sent2 = asyncio.run(run([b"123", b"123", b""]))
    assert sent2[0]["status"] == 200
    assert received["len"] == 6


def test_body_size_limit_content_length_reject():
    from app.main import BodySizeLimitMiddleware

    async def endpoint(scope, receive, send):
        await JSONResponse({"ok": True})(scope, receive, send)

    mw = BodySizeLimitMiddleware(endpoint, max_body=10)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-length", b"999")],
        "query_string": b"",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    asyncio.run(mw(scope, receive, send))
    assert sent[0]["status"] == 413
