from __future__ import annotations

import pytest

from app.config import AppConfig, ConfigManager, config_manager


@pytest.fixture(autouse=True)
def isolated_config(tmp_path):
    old_path = config_manager.path
    old_cfg = config_manager._config
    old_lock = config_manager._lock
    config_manager.path = tmp_path / "config.json"
    config_manager._config = AppConfig()
    yield config_manager
    config_manager.path = old_path
    config_manager._config = old_cfg
    config_manager._lock = old_lock


@pytest.fixture
def mgr(tmp_path):
    p = tmp_path / "other.json"
    return ConfigManager(p)
