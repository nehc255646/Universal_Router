from __future__ import annotations

from fastapi.testclient import TestClient

from app import access_log
from app.main import app


def test_logs_persist_and_paginate():
    access_log.clear()
    for i in range(5):
        access_log.add(inbound="chat", model="m", provider_id="p", stream=False, status=200, latency_ms=i, attempts=1)
    page = access_log.list_logs(limit=2, offset=0)
    assert page["total"] == 5
    assert len(page["items"]) == 2
    with TestClient(app) as c:
        r = c.get("/api/logs?limit=3&offset=0")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 5
        assert "items" in body
        c.delete("/api/logs")
        assert c.get("/api/logs").json()["total"] == 0
