from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import AppConfig, ModelInfo, ProviderConfig, ServerConfig
from app.main import app


def _p(pid: str) -> ProviderConfig:
    return ProviderConfig(id=pid, base_url=f"https://{pid}.example.com/v1", models=[ModelInfo(id="m")])


def test_failover_on_503(isolated_config, monkeypatch):
    isolated_config._config = AppConfig(
        providers=[_p("a"), _p("b")],
        server=ServerConfig(failover=True, retry_count=0, retry_backoff_ms=0),
    )
    calls: list[str] = []

    async def fake(client, provider, body, timeout=120):
        calls.append(provider.id)
        if provider.id == "a":
            return 503, {"error": "busy"}
        return 200, {
            "id": "x",
            "object": "chat.completion",
            "created": 1,
            "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }

    monkeypatch.setattr("app.server.post_non_stream", fake)
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert calls == ["a", "b"]
    assert r.json()["choices"][0]["message"]["content"] == "ok"
    assert r.headers.get("x-universal-router-provider") == "b"


def test_same_provider_retry(isolated_config, monkeypatch):
    isolated_config._config = AppConfig(
        providers=[_p("only")],
        server=ServerConfig(failover=True, retry_count=2, retry_backoff_ms=0),
    )
    n = {"i": 0}

    async def fake(client, provider, body, timeout=120):
        n["i"] += 1
        if n["i"] < 3:
            return 429, {"error": "rate"}
        return 200, {
            "id": "x",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }

    monkeypatch.setattr("app.server.post_non_stream", fake)
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert n["i"] == 3


def test_no_retry_on_400(isolated_config, monkeypatch):
    isolated_config._config = AppConfig(
        providers=[_p("a"), _p("b")],
        server=ServerConfig(failover=True, retry_count=2, retry_backoff_ms=0),
    )
    calls: list[str] = []

    async def fake(client, provider, body, timeout=120):
        calls.append(provider.id)
        return 400, {"error": "bad request"}

    monkeypatch.setattr("app.server.post_non_stream", fake)
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400
    assert calls == ["a"]
