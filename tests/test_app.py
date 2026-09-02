from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import ModelInfo, ProviderConfig
from app.main import app


def test_health():
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True


def test_status_and_models_empty(isolated_config):
    with TestClient(app) as c:
        st = c.get("/api/status").json()
        assert st["version"] == "1.1.0"
        models = c.get("/v1/models").json()
        assert models["data"] == []


def test_unknown_model(isolated_config):
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 400


def test_missing_model(isolated_config):
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 400


def test_auth_when_local_key(isolated_config):
    isolated_config._config.server.local_api_key = "sk-local"
    isolated_config._config.providers = [
        ProviderConfig(
            id="p",
            base_url="https://example.com/v1",
            models=[ModelInfo(id="m")],
        )
    ]
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 401
        r = c.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            headers={"x-api-key": "wrong"},
        )
        assert r.status_code == 401


def test_playground_route_skips_inbound_key(isolated_config):
    isolated_config._config.providers = [
        ProviderConfig(
            id="p",
            base_url="http://127.0.0.1:1/v1",
            api_key="sk-upstream",
            models=[ModelInfo(id="m")],
        )
    ]
    with TestClient(app) as c:
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        assert c.post("/v1/chat/completions", json=body).status_code == 401
        r = c.post("/api/play/chat", json=body)
        assert r.status_code == 502
        assert r.headers["X-Universal-Router-Provider"] == "p"
        assert c.post("/api/play/nope", json=body).status_code == 404


def test_inbound_key_used_for_auth(isolated_config):
    isolated_config._config.providers = [
        ProviderConfig(
            id="p",
            base_url="http://127.0.0.1:1/v1",
            api_key="sk-upstream-secret",
            inbound_key="sk-client-custom",
            models=[ModelInfo(id="m")],
        )
    ]
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    with TestClient(app) as c:
        assert c.post("/v1/chat/completions", json=body).status_code == 401
        assert c.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer sk-upstream-secret"}).status_code == 401
        r = c.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer sk-client-custom"})
        assert r.status_code == 502


def test_create_provider_from_env_name(isolated_config):
    with TestClient(app) as c:
        r = c.post(
            "/api/providers",
            json={
                "id": "envp",
                "base_url": "https://api.openai.com/v1",
                "api_key": "OPENAI_API_KEY",
                "api_key_from_env": True,
                "inbound_key": "sk-local-custom",
                "models": [{"id": "gpt-4o"}],
            },
        )
        assert r.status_code == 200
        d = r.json()
        assert d["api_key"] == "env:OPENAI_API_KEY"
        assert d["api_key_is_ref"] is True
        assert d["has_inbound_key"] is True
        assert d["inbound_key"] == ""
        stored = isolated_config.find_provider("envp")
        assert stored.api_key == "env:OPENAI_API_KEY"
        assert stored.inbound_key == "sk-local-custom"


def test_provider_crud_masks_key(isolated_config):
    with TestClient(app) as c:
        r = c.post(
            "/api/providers",
            json={
                "id": "demo",
                "display_name": "Demo",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-secret",
                "upstream_mode": "chat_completions",
                "models": [{"id": "gpt-4o", "display_name": "4o"}],
            },
        )
        assert r.status_code == 200
        assert r.json()["api_key"] == ""
        assert r.json()["has_api_key"] is True
        listed = c.get("/api/providers").json()
        assert listed[0]["api_key"] == ""
        r2 = c.put(
            "/api/providers/demo",
            json={
                "id": "demo",
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "models": [{"id": "gpt-4o"}],
            },
        )
        assert r2.status_code == 200
        assert isolated_config.find_provider("demo").api_key == "sk-secret"
        c.delete("/api/providers/demo")
        assert c.get("/api/providers").json() == []
