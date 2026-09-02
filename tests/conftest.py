from __future__ import annotations

import os

os.environ.setdefault("UR_ALLOW_INSECURE_BIND", "1")

import pytest

from app import access_log
from app import health as provider_health
from app.config import AppConfig, ConfigManager, config_manager
from app.router import reset_rr


@pytest.fixture(autouse=True)
def isolated_config(tmp_path):
    old_path = config_manager.path
    old_cfg = config_manager._config
    old_lock = config_manager._lock
    config_manager.path = tmp_path / "config.json"
    config_manager._config = AppConfig()
    access_log.configure(tmp_path / "access.db")
    reset_rr()
    provider_health.reset_health()
    yield config_manager
    config_manager.path = old_path
    config_manager._config = old_cfg
    config_manager._lock = old_lock
    access_log.configure()


@pytest.fixture
def mgr(tmp_path):
    p = tmp_path / "other.json"
    return ConfigManager(p)
