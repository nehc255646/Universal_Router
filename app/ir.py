"""中间表示 IR — 三协议统一抽象。

对话侧仍用 IRMessage（兼容现有转换器）；Responses / Agent 路径用 IRItem
（input/output item），避免继续往 IRMessage 塞字段。
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]
ItemType = Literal["message", "function_call", "function_call_output", "reasoning"]


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
    content: str | list[IRContent] | None = None
    tool_calls: list[IRToolCall] | None = None
    tool_call_id: str | None = None
    reasoning: str | None = None
    name: str | None = None


class IRItem(BaseModel):
    """Responses 风格的 input/output item，不往 IRMessage 扩展。"""

    type: ItemType = "message"
    role: Role | None = None
    content: str | list[IRContent] | None = None
    call_id: str | None = None
    name: str | None = None
    arguments: str | None = None
    output: str | None = None
    reasoning: str | None = None
    id: str | None = None
    status: str | None = None


class IRTool(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class IRRequest(BaseModel):
    model: str
    messages: list[IRMessage] = Field(default_factory=list)
    items: list[IRItem] = Field(default_factory=list)
    tools: list[IRTool] | None = None
    tool_choice: str | dict[str, Any] | None = None
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] | str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    def ensure_items(self) -> list[IRItem]:
        if self.items:
            return self.items
        self.items = messages_to_items(self.messages)
        return self.items

    def ensure_messages(self) -> list[IRMessage]:
        if self.messages:
            return self.messages
        self.messages = items_to_messages(self.items)
        return self.messages

    def text_messages(self) -> list[dict[str, str]]:
        out = []
        for m in self.ensure_messages():
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
    finish_reason: str | None = "stop"
    usage: dict[str, int] | None = None
    created: int = Field(default_factory=lambda: int(time.time()))
    items: list[IRItem] = Field(default_factory=list)


def gen_id(prefix: str = "chatcmpl") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


def _text_of(content: str | list[IRContent] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(c.text or "" for c in content if c.type in ("text", "tool_result"))


def messages_to_items(messages: list[IRMessage]) -> list[IRItem]:
    items: list[IRItem] = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "tool":
            items.append(
                IRItem(
                    type="function_call_output",
                    role="tool",
                    call_id=m.tool_call_id,
                    output=_text_of(m.content),
                    content=m.content,
                )
            )
            continue
        if m.reasoning:
            items.append(IRItem(type="reasoning", role=m.role, reasoning=m.reasoning))
        if m.tool_calls:
            if m.content:
                items.append(IRItem(type="message", role=m.role, content=m.content))
            for tc in m.tool_calls:
                items.append(
                    IRItem(
                        type="function_call",
                        role="assistant",
                        call_id=tc.id,
                        name=tc.name,
                        arguments=tc.arguments,
                    )
                )
            continue
        items.append(IRItem(type="message", role=m.role, content=m.content))
    return items


def items_to_messages(items: list[IRItem]) -> list[IRMessage]:
    messages: list[IRMessage] = []
    pending_reason: str | None = None
    for it in items:
        if it.type == "reasoning":
            pending_reason = (pending_reason or "") + (it.reasoning or "")
            continue
        if it.type == "function_call":
            tc = IRToolCall(id=it.call_id or f"call_{uuid.uuid4().hex[:8]}", name=it.name or "", arguments=it.arguments or "{}")
            if messages and messages[-1].role == "assistant":
                existing = list(messages[-1].tool_calls or [])
                existing.append(tc)
                kw: dict[str, Any] = {"tool_calls": existing}
                if pending_reason and not messages[-1].reasoning:
                    kw["reasoning"] = pending_reason
                    pending_reason = None
                messages[-1] = messages[-1].model_copy(update=kw)
            else:
                messages.append(IRMessage(role="assistant", content="", tool_calls=[tc], reasoning=pending_reason))
                pending_reason = None
            continue
        if it.type == "function_call_output":
            messages.append(IRMessage(role="tool", content=it.output or _text_of(it.content), tool_call_id=it.call_id))
            continue
        messages.append(
            IRMessage(
                role=it.role or "user",
                content=it.content,
                reasoning=pending_reason or it.reasoning,
            )
        )
        pending_reason = None
    if pending_reason:
        messages.append(IRMessage(role="assistant", content="", reasoning=pending_reason))
    return messages
