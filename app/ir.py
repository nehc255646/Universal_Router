"""中间表示 IR — 三协议统一抽象"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class IRContent(BaseModel):
    type: Literal["text", "image_url", "tool_result"] = "text"
    text: str | None = None
    image_url: str | None = None
    tool_call_id: str | None = None  # for tool_result
    is_error: bool = False


class IRToolCall(BaseModel):
    id: str
    name: str
    arguments: str  # JSON string


class IRMessage(BaseModel):
    role: Role
    content: str | list[IRContent] | None = None  # str for simple, list for multimodal/tool
    tool_calls: list[IRToolCall] | None = None
    tool_call_id: str | None = None  # for role=tool
    reasoning: str | None = None  # thinking / reasoning summary
    name: str | None = None


class IRTool(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None  # JSON Schema


class IRRequest(BaseModel):
    model: str
    messages: list[IRMessage] = Field(default_factory=list)
    tools: list[IRTool] | None = None
    tool_choice: str | dict[str, Any] | None = None  # auto | required | none | {"type":...}
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] | str | None = None
    # passthrough extras
    extra: dict[str, Any] = Field(default_factory=dict)

    def text_messages(self) -> list[dict[str, str]]:
        """简易文本视图，用于调试"""
        out = []
        for m in self.messages:
            if isinstance(m.content, str):
                out.append({"role": m.role, "content": m.content})
            elif isinstance(m.content, list):
                txt = " ".join(c.text or "" for c in m.content if c.type == "text")
                out.append({"role": m.role, "content": txt})
            else:
                out.append({"role": m.role, "content": ""})
        return out


class IRResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:24]}")
    model: str
    content: str | list[IRContent] | None = None
    tool_calls: list[IRToolCall] | None = None
    reasoning: str | None = None
    finish_reason: str | None = "stop"  # stop | length | tool_calls | content_filter
    usage: dict[str, int] | None = None
    created: int = Field(default_factory=lambda: int(time.time()))


def gen_id(prefix: str = "chatcmpl") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"
