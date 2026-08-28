from __future__ import annotations

from app.config import AppConfig, ModelInfo, ProviderConfig, ServerConfig
from app.router import match_providers, reset_rr, resolve_providers


def _p(pid: str, models: list[str], **kw) -> ProviderConfig:
    return ProviderConfig(
        id=pid,
        base_url="https://api.example.com/v1",
        models=[ModelInfo(id=m) for m in models],
        enabled=kw.get("enabled", True),
        priority=kw.get("priority", 100),
        weight=kw.get("weight", 1),
    )


def test_disabled_skipped(isolated_config):
    isolated_config._config = AppConfig(providers=[_p("a", ["m"], enabled=False), _p("b", ["m"])])
    ids = [p.id for p in resolve_providers("m")]
    assert ids == ["b"]


def test_priority_order(isolated_config):
    isolated_config._config = AppConfig(
        providers=[_p("slow", ["m"], priority=50), _p("fast", ["m"], priority=10)]
    )
    assert [p.id for p in resolve_providers("m")] == ["fast", "slow"]


def test_prefix_pins_then_failover(isolated_config):
    isolated_config._config = AppConfig(
        providers=[_p("a", ["m"], priority=10), _p("b", ["m"], priority=1)],
        server=ServerConfig(failover=True),
    )
    ids = [p.id for p in resolve_providers("a/m")]
    assert ids[0] == "a"
    assert "b" in ids


def test_no_failover_single(isolated_config):
    isolated_config._config = AppConfig(
        providers=[_p("a", ["m"]), _p("b", ["m"])],
        server=ServerConfig(failover=False),
    )
    assert [p.id for p in resolve_providers("m")] == ["a"]


def test_round_robin(isolated_config):
    reset_rr()
    isolated_config._config = AppConfig(
        providers=[_p("a", ["m"], priority=1), _p("b", ["m"], priority=1)],
        server=ServerConfig(route_strategy="round_robin", failover=True),
    )
    firsts = [resolve_providers("m")[0].id for _ in range(4)]
    assert set(firsts) == {"a", "b"}
    assert firsts[0] != firsts[1]


def test_match_plain(isolated_config):
    isolated_config._config = AppConfig(providers=[_p("x", ["gpt-4o"])])
    assert match_providers("gpt-4o")[0].id == "x"
