from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from app.config import AppConfig, ModelInfo, ProviderConfig, ServerConfig
from app.health import record_failure, record_success, reset_health, snapshot
from app.ir import items_to_messages, messages_to_items
from app.main import app
from app.router import resolve_providers
from app.secrets import is_secret_ref, redact, resolve_secret
from app.stream import _extract_events, to_chat_stream, to_responses_stream
from app.upstream import build_headers


def test_secret_refs(monkeypatch):
    monkeypatch.setenv("UR_TEST_KEY", "sk-from-env-123456")
    assert is_secret_ref("env:UR_TEST_KEY")
    assert is_secret_ref("${UR_TEST_KEY}")
    assert is_secret_ref("$UR_TEST_KEY")
    assert not is_secret_ref("sk-literal")
    assert resolve_secret("env:UR_TEST_KEY") == "sk-from-env-123456"
    assert resolve_secret("${UR_TEST_KEY}") == "sk-from-env-123456"


def test_redact_keys():
    text = "Authorization: Bearer sk-abcdefghijklmnop error"
    out = redact(text)
    assert "sk-abcdefghijklmnop" not in out
    assert "[REDACTED]" in out
    out2 = redact("token=supersecretvalue", extras=["supersecretvalue"])
    assert "supersecretvalue" not in out2


def test_build_headers_resolves_env(monkeypatch):
    monkeypatch.setenv("PKEY", "sk-resolved-xxxxx")
    p = ProviderConfig(id="p", base_url="https://example.com/v1", api_key="env:PKEY")
    h = build_headers(p)
    assert h["Authorization"] == "Bearer sk-resolved-xxxxx"


def test_admin_required_when_public_bind(isolated_config):
    isolated_config._config.server.host = "0.0.0.0"
    isolated_config._config.server.admin_api_key = ""
    isolated_config._config.server.local_api_key = ""
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/api/status").status_code == 403
    isolated_config._config.server.admin_api_key = "adm-key"
    with TestClient(app) as c:
        assert c.get("/api/status").status_code == 401
        r = c.get("/api/status", headers={"X-Admin-Key": "adm-key"})
        assert r.status_code == 200
        assert r.json()["ok"] is True


def test_config_hides_server_keys(isolated_config):
    isolated_config._config.server.local_api_key = "sk-local-secret-xx"
    isolated_config._config.server.admin_api_key = "sk-admin-secret-xx"
    with TestClient(app) as c:
        r = c.get("/api/config")
        assert r.status_code == 401
        d = c.get("/api/config", headers={"X-Admin-Key": "sk-admin-secret-xx"}).json()
        assert d["server"]["local_api_key"] == ""
        assert d["server"]["admin_api_key"] == ""
        assert d["server"]["has_local_api_key"] is True
        assert d["server"]["has_admin_api_key"] is True


def test_env_ref_shown_in_public_provider(isolated_config):
    isolated_config._config.providers = [
        ProviderConfig(id="p", base_url="https://example.com/v1", api_key="env:OPENAI_API_KEY", models=[ModelInfo(id="m")])
    ]
    with TestClient(app) as c:
        listed = c.get("/api/providers").json()
        assert listed[0]["api_key"] == "env:OPENAI_API_KEY"
        assert listed[0]["api_key_is_ref"] is True


def test_ir_items_roundtrip_tools():
    from app.converter.chat_responses import chat_to_ir, ir_to_responses

    body = {
        "model": "m",
        "messages": [
            {"role": "user", "content": "weather"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\":\"NYC\"}"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
        ],
    }
    ir = chat_to_ir(body)
    types = [i.type for i in ir.items]
    assert "function_call" in types and "function_call_output" in types
    back = items_to_messages(messages_to_items(ir.messages))
    assert any(m.role == "tool" for m in back)
    resp = ir_to_responses(ir)
    assert any(x.get("type") == "function_call" for x in resp["input"])
    assert any(x.get("type") == "function_call_output" for x in resp["input"])


def test_multi_tool_and_arg_fragments():
    ev = _extract_events(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "c1", "type": "function", "function": {"name": "a", "arguments": ""}},
                            {"index": 1, "id": "c2", "type": "function", "function": {"name": "b", "arguments": ""}},
                        ]
                    }
                }
            ]
        },
        "chat_completions",
    )
    starts = [x for x in ev if x["kind"] == "tool_start"]
    assert len(starts) == 2
    ev2 = _extract_events(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"x\":"}}]}}]},
        "chat_completions",
    )
    assert ev2[0]["kind"] == "tool_args"
    ev3 = _extract_events(
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": "1}"},
        "responses",
    )
    assert ev3[0]["arguments"] == "1}"


def test_responses_lifecycle_from_chat_tools():
    async def raw():
        yield (
            b"data: "
            + json.dumps(
                {
                    "choices": [
                        {"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "fn", "arguments": ""}}]}}
                    ]
                }
            ).encode()
            + b"\n\n"
        )
        yield (
            b"data: "
            + json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"a\":1}"}}]}}]}).encode()
            + b"\n\n"
        )
        yield b"data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}).encode() + b"\n\n"

    async def collect():
        out = []
        async for c in to_responses_stream("chat_completions", raw(), "m"):
            out.append(c.decode())
        return "".join(out)

    text = asyncio.run(collect())
    for ev in (
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
        "response.completed",
    ):
        assert ev in text


def test_midstream_error_to_chat():
    async def raw():
        yield b"data: " + json.dumps({"choices": [{"delta": {"content": "hi"}}]}).encode() + b"\n\n"
        yield b"data: " + json.dumps({"error": {"message": "boom"}}).encode() + b"\n\n"

    async def collect():
        out = []
        async for c in to_chat_stream("chat_completions", raw(), "m"):
            out.append(c.decode())
        return "".join(out)

    text = asyncio.run(collect())
    assert "hi" in text
    assert "boom" in text
    assert "[DONE]" in text


def test_circuit_opens_and_recovers(isolated_config):
    reset_health()
    isolated_config._config = AppConfig(
        providers=[
            ProviderConfig(id="a", base_url="https://a.example.com/v1", models=[ModelInfo(id="m")], priority=1),
            ProviderConfig(id="b", base_url="https://b.example.com/v1", models=[ModelInfo(id="m")], priority=2),
        ],
        server=ServerConfig(circuit_breaker=True, circuit_fail_threshold=2, circuit_cooldown_s=60, failover=True),
    )
    record_failure("a", error="x", threshold=2)
    record_failure("a", error="x", threshold=2)
    assert snapshot("a")["state"] == "open"
    ids = [p.id for p in resolve_providers("m")]
    assert ids[0] == "b"
    record_success("a", 20)
    assert snapshot("a")["state"] == "closed"


def test_put_config_preserves_keys(isolated_config):
    isolated_config._config.server.local_api_key = "keep-me-secret"
    isolated_config._config.server.admin_api_key = "keep-admin"
    with TestClient(app) as c:
        r = c.put(
            "/api/config",
            headers={"X-Admin-Key": "keep-admin"},
            json={"server": {"host": "127.0.0.1", "port": 8787, "local_api_key": "", "admin_api_key": "", "route_strategy": "priority"}},
        )
        assert r.status_code == 200
        assert isolated_config.config.server.local_api_key == "keep-me-secret"
        assert isolated_config.config.server.admin_api_key == "keep-admin"


def test_latency_and_cost_strategies(isolated_config):
    reset_health()
    isolated_config._config = AppConfig(
        providers=[
            ProviderConfig(id="slow", base_url="https://s.example.com/v1", models=[ModelInfo(id="m")], priority=1, cost_input_per_1m=10),
            ProviderConfig(id="fast", base_url="https://f.example.com/v1", models=[ModelInfo(id="m")], priority=1, cost_input_per_1m=1),
        ],
        server=ServerConfig(route_strategy="latency", failover=True),
    )
    record_success("slow", 800)
    record_success("fast", 40)
    assert resolve_providers("m")[0].id == "fast"
    isolated_config._config.server.route_strategy = "cost"
    assert resolve_providers("m")[0].id == "fast"
    isolated_config._config.server.route_strategy = "health"
    assert resolve_providers("m")[0].id in ("fast", "slow")
