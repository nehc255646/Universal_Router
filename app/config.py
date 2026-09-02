"""配置管理 — config.json 读写 + 校验"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .secrets import is_secret_ref, resolve_secret, to_env_ref

CONFIG_PATH = Path(os.getenv("UR_CONFIG") or Path(__file__).resolve().parent.parent / "config.json")
ID_RE = re.compile(r"^[a-z0-9\-_]+$")
UPSTREAM_MODES = {"chat_completions", "responses", "messages"}


class ModelInfo(BaseModel):
    id: str  # 客户端请求的模型 id
    display_name: str = ""
    upstream_id: str = ""  # 发给上游的 id；为空则与 id 相同


class HeaderInfo(BaseModel):
    name: str
    value: str


class ProviderConfig(BaseModel):
    id: str
    display_name: str = ""
    base_url: str
    api_key: str = ""
    inbound_key: str = ""  # 客户端调用本网关时使用；为空则与上游 api_key 相同
    upstream_mode: str = "chat_completions"
    models: list[ModelInfo] = Field(default_factory=list)
    headers: list[HeaderInfo] = Field(default_factory=list)
    enabled: bool = True
    priority: int = 100  # 越小越优先
    weight: int = 1  # 同优先级加权轮询
    timeout_s: float = 120
    cost_input_per_1m: float = 0.0
    cost_output_per_1m: float = 0.0

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError("id 必须匹配 ^[a-z0-9-_]+$")
        return v

    @field_validator("upstream_mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in UPSTREAM_MODES:
            raise ValueError(f"upstream_mode 必须是 {UPSTREAM_MODES}")
        return v

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v.startswith("http"):
            raise ValueError("base_url 必须以 http 开头")
        return v

    @field_validator("weight")
    @classmethod
    def validate_weight(cls, v: int) -> int:
        return max(1, min(int(v), 100))

    @field_validator("timeout_s")
    @classmethod
    def validate_timeout(cls, v: float) -> float:
        return max(5.0, min(float(v), 600.0))

    @field_validator("cost_input_per_1m", "cost_output_per_1m")
    @classmethod
    def validate_cost(cls, v: float) -> float:
        return max(0.0, float(v))

    def upstream_model_id(self, mid: str) -> str:
        """客户端 model id → 上游真实 id。"""
        for m in self.models:
            if m.id == mid:
                mapped = (m.upstream_id or "").strip()
                return mapped or mid
        return mid


ROUTE_STRATEGIES = {"priority", "round_robin", "weighted", "latency", "health", "cost"}


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787
    local_api_key: str = ""  # 为空则不鉴权；非空时入站需 Bearer 匹配
    admin_api_key: str = ""  # 管理 /api/* ；非本机绑定时与 local_api_key 至少设一个
    retry_count: int = 1  # 同一提供商额外重试次数
    retry_backoff_ms: int = 200
    failover: bool = True  # 失败后尝试下一个匹配提供商
    route_strategy: str = "priority"  # priority | round_robin | weighted | latency | health | cost
    log_retain: int = 5000
    connect_timeout_s: float = 15
    first_token_timeout_s: float = 45
    read_idle_timeout_s: float = 90
    circuit_breaker: bool = True
    circuit_fail_threshold: int = 3
    circuit_cooldown_s: float = 30

    @field_validator("route_strategy")
    @classmethod
    def validate_strategy(cls, v: str) -> str:
        if v not in ROUTE_STRATEGIES:
            raise ValueError(f"route_strategy 必须是 {ROUTE_STRATEGIES}")
        return v

    @field_validator("retry_count")
    @classmethod
    def validate_retry(cls, v: int) -> int:
        return max(0, min(int(v), 8))

    @field_validator("log_retain")
    @classmethod
    def validate_retain(cls, v: int) -> int:
        return max(100, min(int(v), 100000))

    @field_validator("connect_timeout_s")
    @classmethod
    def validate_connect(cls, v: float) -> float:
        return max(1.0, min(float(v), 120.0))

    @field_validator("first_token_timeout_s", "read_idle_timeout_s")
    @classmethod
    def validate_stream_timeout(cls, v: float) -> float:
        return max(0.0, min(float(v), 600.0))

    @field_validator("circuit_fail_threshold")
    @classmethod
    def validate_circuit_n(cls, v: int) -> int:
        return max(1, min(int(v), 20))

    @field_validator("circuit_cooldown_s")
    @classmethod
    def validate_cooldown(cls, v: float) -> float:
        return max(1.0, min(float(v), 3600.0))


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    providers: list[ProviderConfig] = Field(default_factory=list)


class ConfigManager:
    def __init__(self, path: Path | None = None):
        self.path = path or CONFIG_PATH
        self._lock = asyncio.Lock()
        self._config: AppConfig = AppConfig()
        self.load_sync()

    def load_sync(self) -> AppConfig:
        if not self.path.exists():
            self._config = AppConfig()
            self.save_sync()
            return self._config
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._config = AppConfig.model_validate(data)
        except Exception as e:
            # 保留旧配置，避免启动失败
            print(f"[config] load failed: {e}, using empty")
            self._config = AppConfig()
        return self._config

    def save_sync(self) -> None:
        # 原子写 + 简单文件锁（跨进程），Windows 用独占创建兼容
        lock = self.path.with_suffix(".lock")
        acquired = False
        try:
            for _ in range(20):
                try:
                    fd = lock.open("x")
                    fd.write(str(os.getpid()))
                    fd.close()
                    acquired = True
                    break
                except FileExistsError:
                    # 陈旧锁（超过 10s）视为残留，清理后重试
                    try:
                        if time.time() - lock.stat().st_mtime > 10:
                            lock.unlink()
                            continue
                    except OSError:
                        pass
                    time.sleep(0.1)
            if not acquired:
                raise RuntimeError("config.json.lock 被其他进程长期占用，放弃写入")
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._config.model_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        finally:
            if acquired:
                try:
                    lock.unlink()
                except Exception:
                    pass
        # 明文密钥提醒（每进程只提示一次）
        global _KEY_WARNED
        if not _KEY_WARNED and any(p.api_key for p in self._config.providers):
            _KEY_WARNED = True
            print("[config] 提醒: config.json 含明文 api_key，已被 .gitignore 忽略，请勿提交仓库")

    @property
    def config(self) -> AppConfig:
        return self._config

    async def reload(self) -> AppConfig:
        async with self._lock:
            return self.load_sync()

    async def save(self) -> None:
        async with self._lock:
            self.save_sync()

    async def update(self, new: AppConfig) -> AppConfig:
        async with self._lock:
            self._config = new
            self.save_sync()
            return self._config

    async def update_server(self, srv: ServerConfig) -> AppConfig:
        """带锁更新 server 配置；保存失败时回滚内存。"""
        async with self._lock:
            prev = self._config.server
            self._config.server = srv
            try:
                self.save_sync()
            except Exception:
                self._config.server = prev
                raise
            return self._config

    async def add_provider(self, p: ProviderConfig) -> None:
        """带锁新增 provider；id 冲突抛 ProviderExistsError。"""
        async with self._lock:
            if any(x.id == p.id for x in self._config.providers):
                raise ProviderExistsError(f"provider id '{p.id}' 已存在")
            self._config.providers.append(p)
            try:
                self.save_sync()
            except Exception:
                self._config.providers.pop()
                raise

    async def replace_provider(self, pid: str, p: ProviderConfig) -> bool:
        """带锁替换 provider；不存在返回 False，id 冲突抛 ProviderExistsError。"""
        async with self._lock:
            idx = next((i for i, x in enumerate(self._config.providers) if x.id == pid), None)
            if idx is None:
                return False
            if p.id != pid and any(x.id == p.id for x in self._config.providers):
                raise ProviderExistsError(f"provider id '{p.id}' 已存在")
            prev = self._config.providers[idx]
            self._config.providers[idx] = p
            try:
                self.save_sync()
            except Exception:
                self._config.providers[idx] = prev
                raise
            return True

    async def remove_provider(self, pid: str) -> bool:
        """带锁删除 provider；不存在返回 False。"""
        async with self._lock:
            idx = next((i for i, x in enumerate(self._config.providers) if x.id == pid), None)
            if idx is None:
                return False
            removed = self._config.providers.pop(idx)
            try:
                self.save_sync()
            except Exception:
                self._config.providers.insert(idx, removed)
                raise
            return True

    # provider CRUD
    def find_provider(self, pid: str) -> ProviderConfig | None:
        for p in self._config.providers:
            if p.id == pid:
                return p
        return None

    def find_provider_by_model(self, model: str) -> ProviderConfig | None:
        from .router import resolve_providers

        found = resolve_providers(model)
        return found[0] if found else None

    def all_api_keys(self) -> set[str]:
        keys: set[str] = set()
        for p in self._config.providers:
            keys.update(provider_auth_keys(p))
        return keys

    def all_models(self) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for p in self._config.providers:
            if not p.enabled:
                continue
            for m in p.models:
                counts[m.id] = counts.get(m.id, 0) + 1
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for p in self._config.providers:
            if not p.enabled:
                continue
            for m in p.models:
                display = m.display_name or m.id
                prefixed = f"{p.id}/{m.id}"
                # 重名时仅暴露带前缀 id，避免客户端路由歧义
                ids = [prefixed] if counts.get(m.id, 0) > 1 else [m.id, prefixed]
                for mid in ids:
                    if mid in seen:
                        continue
                    seen.add(mid)
                    item = {
                        "id": mid,
                        "object": "model",
                        "owned_by": p.id,
                        "display_name": display,
                    }
                    if m.upstream_id and m.upstream_id != m.id:
                        item["upstream_id"] = m.upstream_id
                    out.append(item)
        return out


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class ProviderExistsError(Exception):
    pass


_KEY_WARNED = False


def is_loopback_bind(host: str | None) -> bool:
    return (host or "127.0.0.1").strip().lower() in LOOPBACK_HOSTS


def _keys_from_raw(raw: str) -> set[str]:
    keys: set[str] = set()
    v = (raw or "").strip()
    if not v:
        return keys
    resolved = resolve_secret(v)
    if resolved:
        keys.add(resolved)
    if not is_secret_ref(v):
        keys.add(v)
    return keys


def provider_auth_keys(p: ProviderConfig) -> set[str]:
    """入站鉴权使用的密钥：自定义 inbound_key 优先，否则用上游 api_key。"""
    inbound = (p.inbound_key or "").strip()
    return _keys_from_raw(inbound) if inbound else _keys_from_raw(p.api_key)


def provider_public_dict(p: ProviderConfig) -> dict[str, Any]:
    d = p.model_dump()
    d["has_api_key"] = bool(p.api_key)
    d["api_key_is_ref"] = is_secret_ref(p.api_key)
    d["api_key"] = p.api_key if is_secret_ref(p.api_key) else ""
    d["has_inbound_key"] = bool(p.inbound_key)
    d["inbound_key_is_ref"] = is_secret_ref(p.inbound_key)
    d["inbound_key"] = p.inbound_key if is_secret_ref(p.inbound_key) else ""
    return d


def server_public_dict(s: ServerConfig) -> dict[str, Any]:
    d = s.model_dump()
    d["has_local_api_key"] = bool(s.local_api_key)
    d["has_admin_api_key"] = bool(s.admin_api_key)
    d["local_api_key"] = ""
    d["admin_api_key"] = ""
    return d


def apply_incoming_server(body: dict[str, Any], existing: ServerConfig) -> dict[str, Any]:
    body = dict(body)
    if body.pop("clear_admin_api_key", False):
        body["admin_api_key"] = ""
    else:
        incoming = (body.get("admin_api_key") or "").strip()
        if not incoming or incoming.strip("*") == "":
            body["admin_api_key"] = existing.admin_api_key
    if body.pop("clear_local_api_key", False):
        body["local_api_key"] = ""
    else:
        incoming = (body.get("local_api_key") or "").strip()
        if not incoming or incoming.strip("*") == "":
            body["local_api_key"] = existing.local_api_key
    return body


def _preserve_secret(body: dict[str, Any], field: str, existing_val: str, *, clear_flag: str) -> None:
    if body.pop(clear_flag, False):
        body[field] = ""
        return
    incoming = (body.get(field) or "").strip()
    if not incoming or incoming.strip("*") == "":
        body[field] = existing_val


def apply_incoming_key(body: dict[str, Any], existing: ProviderConfig | None) -> dict[str, Any]:
    """空密钥且未显式 clear_* 时保留原值；api_key_from_env 时规范为 env:NAME。"""
    body = dict(body)
    from_env = bool(body.pop("api_key_from_env", False))
    if body.pop("clear_api_key", False):
        body["api_key"] = ""
    else:
        incoming = (body.get("api_key") or "").strip()
        if existing and (not incoming or incoming.strip("*") == ""):
            body["api_key"] = existing.api_key
        elif from_env and incoming:
            body["api_key"] = to_env_ref(incoming)
    existing_inbound = existing.inbound_key if existing else ""
    _preserve_secret(body, "inbound_key", existing_inbound, clear_flag="clear_inbound_key")
    return body


# 全局单例
config_manager = ConfigManager()
