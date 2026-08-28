"""chat <-> anthropic messages 互转 + IR"""
from __future__ import annotations

import json
import uuid
from typing import Any

from ..ir import IRContent, IRMessage, IRRequest, IRResponse, IRTool, IRToolCall
from .common import extract_text

ANTHROPIC_VERSION = "2023-06-01"


def _content_to_ir_anthropic(content: Any) -> str | list[IRContent]:
    if isinstance(content, str) or content is None:
        return content or ""
    if isinstance(content, list):
        out: list[IRContent] = []
        for c in content:
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t == "text":
                out.append(IRContent(type="text", text=c.get("text") or ""))
            elif t == "image":
                src = c.get("source") or {}
                url = src.get("url") or c.get("url") or ""
                # anthropic image source -> url
                if src.get("type") == "base64":
                    url = f"data:{src.get('media_type')};base64,{src.get('data')}"
                out.append(IRContent(type="image_url", image_url=url))
            elif t == "tool_use":
                # tool_use 在 content 数组里，需转为 tool_calls 在 message 层
                # 这里暂转为文本占位，实际在 anthropic_to_ir 中单独处理
                out.append(IRContent(type="text", text=f"[tool_use {c.get('name')}]"))
            elif t == "tool_result":
                # tool_result -> IR tool message
                txt = ""
                cr = c.get("content")
                if isinstance(cr, str):
                    txt = cr
                elif isinstance(cr, list):
                    txt = " ".join(x.get("text") or "" for x in cr if isinstance(x, dict))
                out.append(IRContent(type="tool_result", text=txt, tool_call_id=c.get("tool_use_id"), is_error=bool(c.get("is_error"))))
        return out
    return str(content)


def anthropic_to_ir(body: dict[str, Any]) -> IRRequest:
    model = body.get("model", "")
    messages: list[IRMessage] = []

    # anthropic system 可能是 string 或 list
    system = body.get("system")
    if system:
        if isinstance(system, str):
            messages.append(IRMessage(role="system", content=system))
        elif isinstance(system, list):
            txt = " ".join(c.get("text") or "" for c in system if isinstance(c, dict))
            messages.append(IRMessage(role="system", content=txt))

    for m in body.get("messages") or []:
        role = m.get("role", "user")
        raw = m.get("content")
        tool_calls: list[IRToolCall] | None = None

        if isinstance(raw, str):
            messages.append(IRMessage(role=role, content=raw))
            continue
        if isinstance(raw, list):
            text_parts: list[IRContent] = []
            tc_list: list[IRToolCall] = []
            tool_results: list[tuple[str, str]] = []  # (id, text)
            for c in raw:
                if not isinstance(c, dict):
                    continue
                t = c.get("type")
                if t == "text":
                    text_parts.append(IRContent(type="text", text=c.get("text") or ""))
                elif t == "image":
                    src = c.get("source") or {}
                    url = src.get("url") or ""
                    if src.get("type") == "base64":
                        url = f"data:{src.get('media_type')};base64,{src.get('data')}"
                    text_parts.append(IRContent(type="image_url", image_url=url))
                elif t == "tool_use":
                    tc_list.append(IRToolCall(id=c.get("id") or f"toolu_{uuid.uuid4().hex[:8]}", name=c.get("name") or "", arguments=json.dumps(c.get("input") or {}, ensure_ascii=False)))
                elif t == "tool_result":
                    cr = c.get("content")
                    if isinstance(cr, str):
                        txt = cr
                    elif isinstance(cr, list):
                        txt = " ".join(x.get("text") or "" for x in cr if isinstance(x, dict))
                    else:
                        txt = str(cr) if cr is not None else ""
                    tool_results.append((c.get("tool_use_id") or "", txt))
            if tc_list:
                tool_calls = tc_list
            # 多 tool_result 需拆为多条 tool 消息（Anthropic 允许 user 中多 tool_result）
            if tool_results:
                # 先发主消息（文本 + tool_calls）
                if text_parts or tc_list:
                    content: str | list[IRContent] | None
                    if not text_parts and not tc_list:
                        content = ""
                    elif len(text_parts) == 1 and text_parts[0].type == "text" and not tc_list:
                        content = text_parts[0].text or ""
                    else:
                        content = text_parts if text_parts else ""
                    # 若纯 tool_result 且无其他内容，则不发主消息
                    if not (len(tool_results) == len(raw) and not tc_list and not text_parts):
                        messages.append(IRMessage(role=role, content=content, tool_calls=tool_calls if tc_list else None))
                for tid, txt in tool_results:
                    messages.append(IRMessage(role="tool", content=txt or "", tool_call_id=tid))
                continue
            # 无 tool_result 的常规消息
            content: str | list[IRContent] | None
            if not text_parts and not tc_list:
                content = ""
            elif len(text_parts) == 1 and text_parts[0].type == "text" and not tc_list:
                content = text_parts[0].text or ""
            else:
                content = text_parts if text_parts else ""
            messages.append(IRMessage(role=role, content=content, tool_calls=tool_calls if tc_list else None))
        else:
            messages.append(IRMessage(role=role, content=str(raw) if raw is not None else ""))

    tools = None
    if body.get("tools"):
        tools = []
        for t in body["tools"]:
            tools.append(IRTool(name=t.get("name") or "", description=t.get("description"), parameters=t.get("input_schema") or t.get("parameters")))

    return IRRequest(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice=body.get("tool_choice"),
        stream=bool(body.get("stream")),
        temperature=body.get("temperature"),
        max_tokens=body.get("max_tokens"),
        top_p=body.get("top_p"),
        stop=body.get("stop_sequences"),
        extra={k: v for k, v in body.items() if k not in {"model", "system", "messages", "tools", "tool_choice", "stream", "temperature", "max_tokens", "top_p", "stop_sequences"}},
    )


def ir_to_anthropic(ir: IRRequest, stream: bool = False) -> dict[str, Any]:
    system = None
    msgs: list[dict[str, Any]] = []
    for m in ir.messages:
        if m.role == "system":
            if isinstance(m.content, str):
                txt = m.content
            elif isinstance(m.content, list):
                txt = " ".join(c.text or "" for c in m.content if c.type == "text")
            else:
                txt = ""
            if system is None:
                system = txt
            else:
                system += "\n" + txt
            continue
        if m.role == "tool":
            # tool -> user with tool_result
            txt = m.content if isinstance(m.content, str) else "".join(c.text or "" for c in (m.content or []) if isinstance(c, IRContent))
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": m.tool_call_id or "", "content": txt}]})
            continue
        # user / assistant
        content: Any
        if m.tool_calls:
            parts: list[dict[str, Any]] = []
            # 文本部分
            if isinstance(m.content, str) and m.content:
                parts.append({"type": "text", "text": m.content})
            elif isinstance(m.content, list):
                for c in m.content:
                    if c.type == "text" and c.text:
                        parts.append({"type": "text", "text": c.text})
                    elif c.type == "image_url":
                        parts.append({"type": "image", "source": {"type": "url", "url": c.image_url}})
            for tc in m.tool_calls:
                try:
                    inp = json.loads(tc.arguments) if tc.arguments else {}
                except Exception:
                    inp = {"_raw": tc.arguments}
                parts.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": inp})
            content = parts
        else:
            if isinstance(m.content, str):
                content = m.content
            elif isinstance(m.content, list):
                parts2: list[dict[str, Any]] = []
                for c in m.content:
                    if c.type == "text":
                        parts2.append({"type": "text", "text": c.text or ""})
                    elif c.type == "image_url":
                        parts2.append({"type": "image", "source": {"type": "url", "url": c.image_url}})
                if len(parts2) == 1 and parts2[0]["type"] == "text":
                    content = parts2[0]["text"]
                else:
                    content = parts2
            else:
                content = ""
        msgs.append({"role": m.role, "content": content})

    # 规范化 tool_choice：OpenAI -> Anthropic
    def _to_anthropic_tc(tc: Any) -> Any:
        if tc is None:
            return None
        if isinstance(tc, str):
            if tc == "auto":
                return {"type": "auto"}
            if tc == "required":
                return {"type": "any"}
            if tc == "none":
                return {"type": "auto", "disable_parallel_tool_use": True}  # 近似
            return {"type": "auto"}
        if isinstance(tc, dict):
            # {"type":"function","function":{"name":...}}
            if tc.get("type") == "function" and tc.get("function", {}).get("name"):
                return {"type": "tool", "name": tc["function"]["name"]}
            if tc.get("type") in ("auto", "any", "tool"):
                return tc
            return tc
        return tc

    body: dict[str, Any] = {"model": ir.model, "messages": msgs}
    if system:
        body["system"] = system
    if ir.tools:
        body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters or {"type": "object", "properties": {}}} for t in ir.tools]
    if ir.tool_choice is not None:
        body["tool_choice"] = _to_anthropic_tc(ir.tool_choice)
    if stream:
        body["stream"] = True
    if ir.temperature is not None:
        body["temperature"] = ir.temperature
    if ir.max_tokens is not None:
        body["max_tokens"] = ir.max_tokens
    if ir.top_p is not None:
        body["top_p"] = ir.top_p
    if ir.stop is not None:
        body["stop_sequences"] = ir.stop if isinstance(ir.stop, list) else [ir.stop]
    body.update(ir.extra)
    return body


def ir_response_to_anthropic(ir_resp: IRResponse) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if isinstance(ir_resp.content, list):
        for c in ir_resp.content:
            if c.type == "text" and c.text:
                content.append({"type": "text", "text": c.text})
            elif c.type == "image_url":
                content.append({"type": "image", "source": {"type": "url", "url": c.image_url}})
    else:
        if ir_resp.content:
            content.append({"type": "text", "text": ir_resp.content})
    if ir_resp.tool_calls:
        for tc in ir_resp.tool_calls:
            try:
                inp = json.loads(tc.arguments) if tc.arguments else {}
            except Exception:
                inp = {"_raw": tc.arguments}
            content.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": inp})
    # finish_reason 映射
    stop_reason = "end_turn"
    if ir_resp.finish_reason == "length":
        stop_reason = "max_tokens"
    elif ir_resp.finish_reason == "tool_calls":
        stop_reason = "tool_use"

    return {
        "id": ir_resp.id.replace("chatcmpl-", "msg_"),
        "type": "message",
        "role": "assistant",
        "model": ir_resp.model,
        "content": content,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": (ir_resp.usage or {}).get("prompt_tokens", 0), "output_tokens": (ir_resp.usage or {}).get("completion_tokens", 0)},
    }
