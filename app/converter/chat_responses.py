"""chat <-> responses 互转（核心）"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

from ..ir import IRContent, IRMessage, IRRequest, IRResponse, IRTool, IRToolCall
from .common import extract_text
from .extras import CHAT_PASSTHROUGH, RESPONSES_PASSTHROUGH, take_extras

# ---------- helpers ----------


def _content_to_ir(content: Any) -> str | list[IRContent]:
    if isinstance(content, str) or content is None:
        return content or ""
    if isinstance(content, list):
        out: list[IRContent] = []
        for c in content:
            if isinstance(c, dict):
                t = c.get("type")
                if t == "text":
                    out.append(IRContent(type="text", text=c.get("text") or ""))
                elif t == "image_url":
                    url = c.get("image_url", {}).get("url") if isinstance(c.get("image_url"), dict) else c.get("image_url")
                    out.append(IRContent(type="image_url", image_url=url))
            elif isinstance(c, str):
                out.append(IRContent(type="text", text=c))
        return out
    return str(content)


def _ir_content_to_chat(content: str | list[IRContent] | None) -> Any:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    # list[IRContent] -> chat content array
    arr = []
    for c in content:
        if c.type == "text":
            arr.append({"type": "text", "text": c.text or ""})
        elif c.type == "image_url":
            arr.append({"type": "image_url", "image_url": {"url": c.image_url}})
    if len(arr) == 1 and arr[0]["type"] == "text":
        return arr[0]["text"]
    return arr


# ---------- chat <-> IR ----------


def chat_to_ir(body: dict[str, Any]) -> IRRequest:
    model = body.get("model", "")
    # 处理 provider/model 前缀，IR 保留原始 model
    messages: list[IRMessage] = []
    for m in body.get("messages") or []:
        role = m.get("role", "user")
        content = _content_to_ir(m.get("content"))
        tool_calls = None
        if m.get("tool_calls"):
            tool_calls = [
                IRToolCall(id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}", name=tc.get("function", {}).get("name") or "", arguments=tc.get("function", {}).get("arguments") or "{}")
                for tc in m["tool_calls"]
            ]
        # tool role
        if role == "tool":
            messages.append(IRMessage(role="tool", content=content, tool_call_id=m.get("tool_call_id")))
        else:
            messages.append(IRMessage(role=role, content=content, tool_calls=tool_calls, reasoning=m.get("reasoning") or m.get("reasoning_content")))

    tools = None
    if body.get("tools"):
        tools = []
        for t in body["tools"]:
            fn = t.get("function") or t
            tools.append(IRTool(name=fn.get("name") or "", description=fn.get("description"), parameters=fn.get("parameters")))

    return IRRequest(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice=body.get("tool_choice"),
        stream=bool(body.get("stream")),
        temperature=body.get("temperature"),
        max_tokens=body.get("max_tokens") or body.get("max_completion_tokens"),
        top_p=body.get("top_p"),
        stop=body.get("stop"),
        extra={k: v for k, v in body.items() if k not in {"model", "messages", "tools", "tool_choice", "stream", "temperature", "max_tokens", "max_completion_tokens", "top_p", "stop"}},
    )


def ir_to_chat(ir: IRRequest, stream: bool = False) -> dict[str, Any]:
    messages = []
    for m in ir.messages:
        msg: dict[str, Any] = {"role": m.role}
        if m.role == "tool":
            msg["content"] = _ir_content_to_chat(m.content)
            msg["tool_call_id"] = m.tool_call_id or ""
        else:
            msg["content"] = _ir_content_to_chat(m.content)
            if m.tool_calls:
                msg["tool_calls"] = [{"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}} for tc in m.tool_calls]
            if m.reasoning:
                msg["reasoning"] = m.reasoning
        messages.append(msg)

    body: dict[str, Any] = {"model": ir.model, "messages": messages}
    if ir.tools:
        body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters or {"type": "object", "properties": {}}}} for t in ir.tools]
    if ir.tool_choice is not None:
        body["tool_choice"] = ir.tool_choice
    if stream:
        body["stream"] = True
    if ir.temperature is not None:
        body["temperature"] = ir.temperature
    if ir.max_tokens is not None:
        body["max_tokens"] = ir.max_tokens
    if ir.top_p is not None:
        body["top_p"] = ir.top_p
    if ir.stop is not None:
        body["stop"] = ir.stop
    body.update(take_extras(ir.extra, CHAT_PASSTHROUGH))
    return body


def ir_response_to_chat(ir_resp: IRResponse) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant"}
    if isinstance(ir_resp.content, list):
        msg["content"] = _ir_content_to_chat(ir_resp.content)
    else:
        msg["content"] = ir_resp.content or ""
    if ir_resp.tool_calls:
        msg["tool_calls"] = [{"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}} for tc in ir_resp.tool_calls]
    if ir_resp.reasoning:
        msg["reasoning_content"] = ir_resp.reasoning
    return {
        "id": ir_resp.id,
        "object": "chat.completion",
        "created": ir_resp.created,
        "model": ir_resp.model,
        "choices": [{"index": 0, "message": msg, "finish_reason": ir_resp.finish_reason or "stop"}],
        "usage": ir_resp.usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ---------- responses <-> IR ----------


def responses_to_ir(body: dict[str, Any]) -> IRRequest:
    model = body.get("model", "")
    instructions = body.get("instructions")
    inp = body.get("input") or body.get("messages") or []

    messages: list[IRMessage] = []
    if instructions:
        messages.append(IRMessage(role="system", content=instructions))

    # input 可能是 string | list[message] | dict 含 function_call / tool 结果
    if isinstance(inp, str):
        messages.append(IRMessage(role="user", content=inp))
    elif isinstance(inp, list):
        for m in inp:
            if isinstance(m, str):
                messages.append(IRMessage(role="user", content=m))
                continue
            if not isinstance(m, dict):
                continue
            # 兼容 responses 的 function_call 顶层项：{"type":"function_call","call_id":...}
            if m.get("type") == "function_call":
                # 作为 assistant 的 tool_calls 历史
                messages.append(IRMessage(role="assistant", content="", tool_calls=[IRToolCall(id=m.get("call_id") or m.get("id") or f"call_{uuid.uuid4().hex[:8]}", name=m.get("name") or "", arguments=m.get("arguments") or "{}")]))
                continue
            if m.get("type") == "function_call_output":
                messages.append(IRMessage(role="tool", content=m.get("output") or "", tool_call_id=m.get("call_id")))
                continue
            role = m.get("role", "user")
            raw_content = m.get("content")
            # 提取 tool_calls（若存在）
            tc = None
            if m.get("tool_calls"):
                tc = [IRToolCall(id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}", name=tc.get("name") or tc.get("function", {}).get("name") or "", arguments=tc.get("arguments") or tc.get("function", {}).get("arguments") or "{}") for tc in m["tool_calls"]]
            if isinstance(raw_content, list):
                ic: list[IRContent] = []
                for c in raw_content:
                    if not isinstance(c, dict):
                        continue
                    ct = c.get("type")
                    if ct in ("input_text", "text", "output_text"):
                        ic.append(IRContent(type="text", text=c.get("text") or ""))
                    elif ct == "input_image":
                        url = c.get("image_url") or c.get("url")
                        ic.append(IRContent(type="image_url", image_url=url))
                    else:
                        ic.append(IRContent(type="text", text=c.get("text") or json.dumps(c, ensure_ascii=False)))
                content: str | list[IRContent] = ic if len(ic) != 1 or ic[0].type != "text" else ic[0].text or ""
            else:
                content = _content_to_ir(raw_content)
            # tool 角色或带 tool_call_id 的历史
            tool_call_id = m.get("tool_call_id") or m.get("call_id")
            if role == "tool" or tool_call_id:
                messages.append(IRMessage(role="tool", content=content if isinstance(content, str) else "".join(c.text or "" for c in content) if isinstance(content, list) else "", tool_call_id=tool_call_id))
            else:
                messages.append(IRMessage(role=role, content=content, tool_calls=tc))

    tools = None
    if body.get("tools"):
        tools = []
        for t in body["tools"]:
            # responses tools: {"type":"function","name":...,"parameters":...}
            tools.append(IRTool(name=t.get("name") or "", description=t.get("description"), parameters=t.get("parameters")))

    # max_output_tokens -> max_tokens
    max_tokens = body.get("max_output_tokens") or body.get("max_tokens")

    return IRRequest(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice=body.get("tool_choice"),
        stream=bool(body.get("stream")),
        temperature=body.get("temperature"),
        max_tokens=max_tokens,
        top_p=body.get("top_p"),
        extra={k: v for k, v in body.items() if k not in {"model", "instructions", "input", "messages", "tools", "tool_choice", "stream", "temperature", "max_tokens", "max_output_tokens", "top_p"}},
    )


def ir_to_responses(ir: IRRequest, stream: bool = False) -> dict[str, Any]:
    # 抽取所有 system 合并为 instructions，保留多条 system 的语义
    instructions_parts: list[str] = []
    input_msgs: list[dict[str, Any]] = []
    for m in ir.messages:
        if m.role == "system":
            if isinstance(m.content, str):
                instructions_parts.append(m.content)
            elif isinstance(m.content, list):
                instructions_parts.append(" ".join(c.text or "" for c in m.content if c.type == "text"))
            continue
        if m.role == "tool":
            txt = m.content if isinstance(m.content, str) else "".join(c.text or "" for c in (m.content or []) if isinstance(c, IRContent))
            input_msgs.append({"type": "function_call_output", "call_id": m.tool_call_id or "", "output": txt or ""})
            continue
        # 转换 content
        if isinstance(m.content, str):
            content = m.content
        elif isinstance(m.content, list):
            parts = []
            for c in m.content:
                if c.type == "text":
                    parts.append({"type": "input_text", "text": c.text or ""})
                elif c.type == "image_url":
                    parts.append({"type": "input_image", "image_url": c.image_url})
            content = parts if parts else ""
        else:
            content = ""
        entry: dict[str, Any] = {"role": m.role, "content": content}
        if m.tool_calls:
            # responses 用单独的 function_call 项追加
            input_msgs.append(entry)
            for tc in m.tool_calls:
                input_msgs.append({"type": "function_call", "call_id": tc.id, "name": tc.name, "arguments": tc.arguments})
            continue
        input_msgs.append(entry)
    instructions = "\n".join(instructions_parts) if instructions_parts else None

    body: dict[str, Any] = {"model": ir.model, "input": input_msgs}
    if instructions:
        body["instructions"] = instructions
    if ir.tools:
        body["tools"] = [{"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters} for t in ir.tools]
    if ir.tool_choice is not None:
        body["tool_choice"] = ir.tool_choice
    if stream:
        body["stream"] = True
    if ir.temperature is not None:
        body["temperature"] = ir.temperature
    if ir.max_tokens is not None:
        body["max_output_tokens"] = ir.max_tokens
    if ir.top_p is not None:
        body["top_p"] = ir.top_p
    body.update(take_extras(ir.extra, RESPONSES_PASSTHROUGH))
    return body


def ir_response_to_responses(ir_resp: IRResponse) -> dict[str, Any]:
    # responses 输出格式
    text = ""
    if isinstance(ir_resp.content, list):
        text = "".join(c.text or "" for c in ir_resp.content if c.type == "text")
    else:
        text = ir_resp.content or ""
    output = []
    if ir_resp.reasoning:
        output.append({"type": "reasoning", "summary": [{"type": "summary_text", "text": ir_resp.reasoning}]})
    if text:
        output.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
    if ir_resp.tool_calls:
        for tc in ir_resp.tool_calls:
            output.append({"type": "function_call", "call_id": tc.id, "name": tc.name, "arguments": tc.arguments})
    return {
        "id": ir_resp.id.replace("chatcmpl-", "resp_"),
        "object": "response",
        "created_at": ir_resp.created,
        "model": ir_resp.model,
        "output": output,
        "status": "completed",
        "usage": ir_resp.usage,
    }


# ---------- Stream transformers ----------

# chat chunk -> IR text delta

def chat_chunk_to_text_delta(chunk: dict[str, Any]) -> str | None:
    try:
        choices = chunk.get("choices") or []
        if not choices:
            return None
        delta = choices[0].get("delta") or {}
        return delta.get("content")
    except Exception:
        return None


def responses_chunk_to_text_delta(chunk: dict[str, Any]) -> str | None:
    # responses stream: {"type":"response.output_text.delta","delta":"..."}
    t = chunk.get("type")
    if t == "response.output_text.delta":
        return chunk.get("delta")
    if t == "response.output_text.done":
        return None
    # fallback: {"choices":...} 透传
    return None


async def chat_stream_to_ir_deltas(chunks: AsyncIterator[dict[str, Any]]) -> AsyncIterator[str]:
    async for chunk in chunks:
        d = chat_chunk_to_text_delta(chunk)
        if d:
            yield d


async def responses_stream_to_ir_deltas(chunks: AsyncIterator[dict[str, Any]]) -> AsyncIterator[str]:
    async for chunk in chunks:
        d = responses_chunk_to_text_delta(chunk)
        if d:
            yield d
