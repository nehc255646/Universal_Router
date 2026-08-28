from __future__ import annotations

from app.config import AppConfig, ModelInfo, ProviderConfig, apply_incoming_key, provider_public_dict


def _p(pid: str, models: list[str], **kw) -> ProviderConfig:
    return ProviderConfig(
        id=pid,
        base_url="https://api.example.com/v1",
        api_key=kw.get("api_key", ""),
        upstream_mode=kw.get("upstream_mode", "chat_completions"),
        models=[ModelInfo(id=m) for m in models],
    )


def test_find_by_prefix_and_plain(isolated_config):
    isolated_config._config = AppConfig(providers=[_p("openai", ["gpt-4o"]), _p("ds", ["deepseek-chat"])])
    assert isolated_config.find_provider_by_model("gpt-4o").id == "openai"
    assert isolated_config.find_provider_by_model("openai/gpt-4o").id == "openai"
    assert isolated_config.find_provider_by_model("missing") is None


def test_duplicate_model_ids_prefixed(isolated_config):
    isolated_config._config = AppConfig(
        providers=[_p("a", ["same"]), _p("b", ["same"])]
    )
    ids = [m["id"] for m in isolated_config.all_models()]
    assert "a/same" in ids and "b/same" in ids
    assert ids.count("same") == 0


def test_unique_model_lists_plain_and_prefixed(isolated_config):
    isolated_config._config = AppConfig(providers=[_p("a", ["gpt-4o"])])
    ids = [m["id"] for m in isolated_config.all_models()]
    assert "gpt-4o" in ids and "a/gpt-4o" in ids


def test_redact_and_preserve_key():
    p = _p("x", ["m"], api_key="sk-secret")
    pub = provider_public_dict(p)
    assert pub["api_key"] == ""
    assert pub["has_api_key"] is True
    merged = apply_incoming_key({"id": "x", "base_url": "https://api.example.com/v1", "api_key": ""}, p)
    assert merged["api_key"] == "sk-secret"
    cleared = apply_incoming_key({"api_key": "", "clear_api_key": True}, p)
    assert cleared["api_key"] == ""
