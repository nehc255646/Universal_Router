"""配置管理 — config.json 读写 + 校验"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

CONFIG_PATH = Path(os.getenv("UR_CONFIG") or Path(__file__).resolve().parent.parent / "config.json")
ID_RE = re.compile(r"^[a-z0-9\-_]+$")
UPSTREAM_MODES = {"chat_completions", "responses", "messages"}


class ModelInfo(BaseModel):
    id: str
    display_name: str = ""


class HeaderInfo(BaseModel):
    name: str
    value: str


class ProviderConfig(BaseModel):
    id: str
    display_name: str = ""
    base_url: str
    api_key: str = ""
    upstream_mode: str = "chat_completions"
    models: list[ModelInfo] = Field(default_factory=list)
    headers: list[HeaderInfo] = Field(default_factory=list)

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


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787
    local_api_key: str = ""  # 为空则不鉴权；非空时入站需 Bearer 匹配


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
        try:
            # 尝试创建 lock 文件，超时 2s
            import time as _t
            for _ in range(20):
                try:
                    fd = lock.open("x")
                    fd.close()
                    break
                except FileExistsError:
                    _t.sleep(0.1)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._config.model_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        finally:
            try:
                lock.unlink()
            except Exception:
                pass
        # 明文密钥提醒
        if any(p.api_key for p in self._config.providers):
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

    # provider CRUD
    def find_provider(self, pid: str) -> ProviderConfig | None:
        for p in self._config.providers:
            if p.id == pid:
                return p
        return None

    def find_provider_by_model(self, model: str) -> ProviderConfig | None:
        # 支持 provider/model 前缀：仅当 mid 真实归属该 provider 时才路由，否则回落全局搜索
        if "/" in model:
            prefix, mid = model.split("/", 1)
            p = self.find_provider(prefix)
            if p:
                if any(m.id == mid for m in p.models):
                    return p
                # 前缀存在但 mid 不在该 provider，尝试按 mid 全局匹配（兼容误用 prefix）
                for pp in self._config.providers:
                    if any(m.id == mid for m in pp.models):
                        return pp
                # 前缀合法但 mid 未配置，仍返回 prefix provider 以保留兼容（上游会按 mid 请求）
                return p
        for p in self._config.providers:
            for m in p.models:
                if m.id == model:
                    return p
        return None

    def all_api_keys(self) -> set[str]:
        return {p.api_key for p in self._config.providers if p.api_key}

    def all_models(self) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for p in self._config.providers:
            for m in p.models:
                counts[m.id] = counts.get(m.id, 0) + 1
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for p in self._config.providers:
            for m in p.models:
                display = m.display_name or m.id
                prefixed = f"{p.id}/{m.id}"
                # 重名时仅暴露带前缀 id，避免客户端路由歧义
                ids = [prefixed] if counts.get(m.id, 0) > 1 else [m.id, prefixed]
                for mid in ids:
                    if mid in seen:
                        continue
                    seen.add(mid)
                    out.append(
                        {
                            "id": mid,
                            "object": "model",
                            "owned_by": p.id,
                            "display_name": display,
                        }
                    )
        return out


def provider_public_dict(p: ProviderConfig) -> dict[str, Any]:
    d = p.model_dump()
    d["has_api_key"] = bool(p.api_key)
    d["api_key"] = ""
    return d


def apply_incoming_key(body: dict[str, Any], existing: ProviderConfig | None) -> dict[str, Any]:
    """空密钥且未显式 clear_api_key 时保留原密钥，避免前端脱敏后覆盖。"""
    body = dict(body)
    if body.pop("clear_api_key", False):
        body["api_key"] = ""
        return body
    incoming = (body.get("api_key") or "").strip()
    if existing and (not incoming or incoming.strip("*") == ""):
        body["api_key"] = existing.api_key
    return body


# 全局单例
config_manager = ConfigManager()
