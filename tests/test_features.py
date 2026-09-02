from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app import health as provider_health
from app.config import AppConfig, ModelInfo, ProviderConfig, ServerConfig
from app.main import app
from app.stream import to_responses_stream


def test_version_and_request_id(isolated_config):
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.json()["version"] == "1.1.0"
        assert r.headers.get("x-request-id")
        r2 = c.get("/health", headers={"X-Request-Id": "rid-custom-1"})
        assert r2.headers.get("x-request-id") == "rid-custom-1"


def test_model_alias_rewrites_upstream_id(isolated_config, monkeypatch):
    isolated_config._config = AppConfig(
        providers=[
            ProviderConfig(
                id="ds",
                base_url="https://api.deepseek.com/v1",
                models=[ModelInfo(id="gpt-4o", display_name="alias", upstream_id="deepseek-chat")],
            )
        ]
    )
    seen: list[str] = []

    async def fake(client, provider, body, timeout=120):
        seen.append(body.get("model"))
        return 200, {
            "id": "x",
            "object": "chat.completion",
            "created": 1,
            "model": body.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }

    monkeypatch.setattr("app.server.post_non_stream", fake)
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert seen == ["deepseek-chat"]


def test_previous_response_id_requires_responses_upstream(isolated_config):
    isolated_config._config = AppConfig(
        providers=[ProviderConfig(id="c", base_url="https://example.com/v1", upstream_mode="chat_completions", models=[ModelInfo(id="m")])]
    )
    with TestClient(app) as c:
        r = c.post(
            "/v1/responses",
            json={"model": "m", "input": "hi", "previous_response_id": "resp_1"},
        )
    assert r.status_code == 400
    assert "previous_response_id" in r.json()["detail"]


def test_get_response_without_responses_provider(isolated_config):
    isolated_config._config = AppConfig(
        providers=[ProviderConfig(id="c", base_url="https://example.com/v1", models=[ModelInfo(id="m")])]
    )
    with TestClient(app) as c:
        r = c.get("/v1/responses/resp_abc")
    assert r.status_code == 400


def test_count_tokens_estimated(isolated_config):
    isolated_config._config = AppConfig(
        providers=[ProviderConfig(id="c", base_url="https://example.com/v1", models=[ModelInfo(id="m")])]
    )
    with TestClient(app) as c:
        r = c.post(
            "/v1/messages/count_tokens",
            json={"model": "m", "max_tokens": 16, "messages": [{"role": "user", "content": "hello world" * 20}]},
        )
    assert r.status_code == 200
    body = r.json()
    assert body.get("estimated") is True
    assert body["input_tokens"] >= 1


def test_health_persists_across_reset(isolated_config):
    provider_health.record_success("p1", 40, tokens_in=2, tokens_out=3)
    provider_health.reset_health()
    assert provider_health.snapshot("p1")["success"] == 0
    provider_health.load_persisted()
    snap = provider_health.snapshot("p1")
    assert snap["success"] == 1
    assert snap["tokens_in"] == 2


def test_logs_include_request_id_and_preview(isolated_config):
    isolated_config._config = AppConfig(providers=[])
    with TestClient(app) as c:
        c.post("/v1/chat/completions", json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]})
        logs = c.get("/api/logs").json()["items"]
    assert logs
    assert logs[0]["request_id"]
    assert logs[0]["inbound"] == "chat"


def test_chat_fixture_to_responses_stream():
    raw = (Path(__file__).parent / "fixtures" / "chat_text.sse").read_bytes()

    async def chunks():
        yield raw

    async def collect():
        out = []
        async for c in to_responses_stream("chat_completions", chunks(), "m"):
            out.append(c.decode())
        return "".join(out)

    text = asyncio.run(collect())
    assert "event: response.output_text.delta" in text
    assert "Hello from fixture" in text
    assert "input_tokens" in text
