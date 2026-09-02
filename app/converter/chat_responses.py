"""chat <-> responses 互转（核心）"""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from ..ir import IRContent, IRMessage, IRRequest, IRResponse, IRTool, IRToolCall, messages_to_items
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
        items=messages_to_items(messages),
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
        body["tool_choice"] = responses_tool_choice_to_chat(ir.tool_choice)
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
    _apply_responses_extras_to_chat(ir.extra, body)
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


def _parse_tool(t: dict[str, Any]) -> IRTool | None:
    """兼容 Responses `{name,parameters}` 与 Chat `{function:{...}}` 两种 tools。"""
    if not isinstance(t, dict):
        return None
    typ = t.get("type") or "function"
    if typ not in ("function", "custom"):
        return None
    fn = t.get("function") if isinstance(t.get("function"), dict) else t
    name = t.get("name") or fn.get("name") or ""
    if not name:
        return None
    return IRTool(
        name=name,
        description=t.get("description") or fn.get("description"),
        parameters=t.get("parameters") or fn.get("parameters") or t.get("input_schema"),
    )


def responses_tool_choice_to_chat(tc: Any) -> Any:
    if isinstance(tc, dict) and tc.get("type") == "function" and "name" in tc and "function" not in tc:
        return {"type": "function", "function": {"name": tc.get("name")}}
    return tc


def chat_tool_choice_to_responses(tc: Any) -> Any:
    if isinstance(tc, dict) and tc.get("type") == "function" and isinstance(tc.get("function"), dict):
        return {"type": "function", "name": (tc.get("function") or {}).get("name")}
    return tc


def _text_format_to_response_format(fmt: Any) -> dict[str, Any] | None:
    if not isinstance(fmt, dict):
        return None
    typ = fmt.get("type") or "text"
    if typ == "text":
        return {"type": "text"}
    if typ == "json_object":
        return {"type": "json_object"}
    if typ == "json_schema":
        schema = fmt.get("schema") or (fmt.get("json_schema") or {}).get("schema")
        name = fmt.get("name") or (fmt.get("json_schema") or {}).get("name") or "schema"
        out: dict[str, Any] = {"type": "json_schema", "json_schema": {"name": name, "schema": schema or {}}}
        if fmt.get("strict") is not None:
            out["json_schema"]["strict"] = fmt["strict"]
        elif isinstance(fmt.get("json_schema"), dict) and fmt["json_schema"].get("strict") is not None:
            out["json_schema"]["strict"] = fmt["json_schema"]["strict"]
        return out
    return None


def _response_format_to_text_format(rf: Any) -> dict[str, Any] | None:
    if not isinstance(rf, dict):
        return None
    typ = rf.get("type") or "text"
    if typ == "json_schema":
        js = rf.get("json_schema") or rf
        out: dict[str, Any] = {"type": "json_schema", "name": js.get("name") or rf.get("name") or "schema", "schema": js.get("schema") or rf.get("schema") or {}}
        if js.get("strict") is not None:
            out["strict"] = js["strict"]
        elif rf.get("strict") is not None:
            out["strict"] = rf["strict"]
        return out
    if typ in ("json_object", "text"):
        return {"type": typ}
    return None


def _apply_responses_extras_to_chat(extra: dict[str, Any] | None, body: dict[str, Any]) -> None:
    extra = extra or {}
    if "response_format" not in body:
        text = extra.get("text")
        fmt = text.get("format") if isinstance(text, dict) else None
        mapped = _text_format_to_response_format(fmt)
        if mapped:
            body["response_format"] = mapped
    if "reasoning_effort" not in body:
        reasoning = extra.get("reasoning")
        if isinstance(reasoning, dict) and reasoning.get("effort"):
            body["reasoning_effort"] = reasoning["effort"]


def _apply_chat_extras_to_responses(extra: dict[str, Any] | None, body: dict[str, Any]) -> None:
    extra = extra or {}
    if "text" not in body:
        mapped = _response_format_to_text_format(extra.get("response_format"))
        if mapped:
            body["text"] = {"format": mapped}
    if extra.get("reasoning_effort"):
        reasoning = body.get("reasoning")
        if not isinstance(reasoning, dict):
            reasoning = {}
        if "effort" not in reasoning:
            body["reasoning"] = {**reasoning, "effort": extra["reasoning_effort"]}


def _instructions_text(instructions: Any) -> str:
    if not instructions:
        return ""
    if isinstance(instructions, str):
        return instructions
    if isinstance(instructions, list):
        parts: list[str] = []
        for x in instructions:
            if isinstance(x, str):
                parts.append(x)
            elif isinstance(x, dict):
                parts.append(x.get("text") or "")
        return "\n".join(p for p in parts if p)
    return str(instructions)


def _image_url_of(c: dict[str, Any]) -> str | None:
    url = c.get("image_url") or c.get("url")
    if isinstance(url, dict):
        url = url.get("url")
    if url:
        return str(url)
    file_id = c.get("file_id")
    return f"file:{file_id}" if file_id else None


def _parse_responses_content(raw_content: Any) -> str | list[IRContent]:
    if isinstance(raw_content, list):
        ic: list[IRContent] = []
        for c in raw_content:
            if not isinstance(c, dict):
                if isinstance(c, str):
                    ic.append(IRContent(type="text", text=c))
                continue
            ct = c.get("type")
            if ct in ("input_text", "text", "output_text", "summary_text"):
                ic.append(IRContent(type="text", text=c.get("text") or ""))
            elif ct in ("input_image", "image_url"):
                ic.append(IRContent(type="image_url", image_url=_image_url_of(c)))
            elif ct == "refusal":
                ic.append(IRContent(type="text", text=c.get("refusal") or c.get("text") or ""))
            elif ct == "input_file":
                fid = c.get("file_id") or c.get("filename") or ""
                ic.append(IRContent(type="text", text=f"[file:{fid}]" if fid else (c.get("text") or "")))
            else:
                ic.append(IRContent(type="text", text=c.get("text") or json.dumps(c, ensure_ascii=False)))
        if len(ic) == 1 and ic[0].type == "text":
            return ic[0].text or ""
        return ic
    return _content_to_ir(raw_content)


def responses_to_ir(body: dict[str, Any]) -> IRRequest:
    model = body.get("model", "")
    inp = body.get("input") or body.get("messages") or []
    if isinstance(inp, dict):
        inp = [inp]

    messages: list[IRMessage] = []
    inst = _instructions_text(body.get("instructions"))
    if inst:
        messages.append(IRMessage(role="system", content=inst))

    if isinstance(inp, str):
        messages.append(IRMessage(role="user", content=inp))
    elif isinstance(inp, list):
        for m in inp:
            if isinstance(m, str):
                messages.append(IRMessage(role="user", content=m))
                continue
            if not isinstance(m, dict):
                continue
            typ = m.get("type")
            if typ == "function_call":
                messages.append(
                    IRMessage(
                        role="assistant",
                        content="",
                        tool_calls=[
                            IRToolCall(
                                id=m.get("call_id") or m.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                                name=m.get("name") or "",
                                arguments=m.get("arguments") or "{}",
                            )
                        ],
                    )
                )
                continue
            if typ == "function_call_output":
                messages.append(IRMessage(role="tool", content=m.get("output") or "", tool_call_id=m.get("call_id")))
                continue
            if typ == "reasoning":
                summary = m.get("summary") or m.get("content") or []
                txt = ""
                if isinstance(summary, list):
                    txt = "".join((s.get("text") if isinstance(s, dict) else str(s)) or "" for s in summary)
                elif isinstance(summary, str):
                    txt = summary
                messages.append(IRMessage(role="assistant", content="", reasoning=txt or ""))
                continue
            if typ in ("item_reference", "web_search_call", "file_search_call", "computer_call", "mcp_call", "image_generation_call", "code_interpreter_call"):
                continue
            role = m.get("role") or "user"
            if role in ("developer", "system"):
                role = "system"
            content = _parse_responses_content(m.get("content"))
            tc = None
            if m.get("tool_calls"):
                tc = [
                    IRToolCall(
                        id=x.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                        name=x.get("name") or (x.get("function") or {}).get("name") or "",
                        arguments=x.get("arguments") or (x.get("function") or {}).get("arguments") or "{}",
                    )
                    for x in m["tool_calls"]
                ]
            tool_call_id = m.get("tool_call_id") or (m.get("call_id") if role == "tool" else None)
            if role == "tool" or tool_call_id:
                text = content if isinstance(content, str) else "".join(c.text or "" for c in content) if isinstance(content, list) else ""
                messages.append(IRMessage(role="tool", content=text, tool_call_id=tool_call_id))
            else:
                messages.append(IRMessage(role=role, content=content, tool_calls=tc))

    tools = None
    if body.get("tools"):
        parsed = [_parse_tool(t) for t in body["tools"] if isinstance(t, dict)]
        tools = [t for t in parsed if t is not None] or None

    max_tokens = body.get("max_output_tokens") or body.get("max_tokens")
    return IRRequest(
        model=model,
        messages=messages,
        items=messages_to_items(messages),
        tools=tools,
        tool_choice=body.get("tool_choice"),
        stream=bool(body.get("stream")),
        temperature=body.get("temperature"),
        max_tokens=max_tokens,
        top_p=body.get("top_p"),
        extra={k: v for k, v in body.items() if k not in {"model", "instructions", "input", "messages", "tools", "tool_choice", "stream", "temperature", "max_tokens", "max_output_tokens", "top_p"}},
    )


def _item_content(content: str | list[IRContent] | None, *, as_output: bool = False) -> list[dict[str, Any]]:
    text_type = "output_text" if as_output else "input_text"
    if isinstance(content, str):
        return [{"type": text_type, "text": content}]
    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for c in content:
            if c.type == "text":
                parts.append({"type": text_type, "text": c.text or ""})
            elif c.type == "image_url":
                url = c.image_url or ""
                if url.startswith("file:"):
                    parts.append({"type": "input_image", "file_id": url[5:], "detail": "auto"})
                else:
                    parts.append({"type": "input_image", "image_url": url, "detail": "auto"})
        return parts or [{"type": text_type, "text": ""}]
    return [{"type": text_type, "text": ""}]


def ir_to_responses(ir: IRRequest, stream: bool = False) -> dict[str, Any]:
    instructions_parts: list[str] = []
    for m in ir.messages:
        if m.role != "system":
            continue
        if isinstance(m.content, str):
            instructions_parts.append(m.content)
        elif isinstance(m.content, list):
            instructions_parts.append(" ".join(c.text or "" for c in m.content if c.type == "text"))
    input_msgs: list[dict[str, Any]] = []
    for it in ir.ensure_items():
        if it.type == "function_call":
            input_msgs.append({"type": "function_call", "call_id": it.call_id or "", "name": it.name or "", "arguments": it.arguments or "{}"})
            continue
        if it.type == "function_call_output":
            if it.output is not None:
                txt = it.output if isinstance(it.output, str) else json.dumps(it.output, ensure_ascii=False)
            else:
                txt = "".join(p.get("text") or "" for p in _item_content(it.content) if isinstance(p, dict))
            input_msgs.append({"type": "function_call_output", "call_id": it.call_id or "", "output": txt or ""})
            continue
        if it.type == "reasoning":
            input_msgs.append({"type": "reasoning", "summary": [{"type": "summary_text", "text": it.reasoning or ""}]})
            continue
        as_output = (it.role or "user") == "assistant"
        input_msgs.append({"type": "message", "role": it.role or "user", "content": _item_content(it.content, as_output=as_output)})
    instructions = "\n".join(instructions_parts) if instructions_parts else None

    body: dict[str, Any] = {"model": ir.model, "input": input_msgs}
    if instructions:
        body["instructions"] = instructions
    if ir.tools:
        body["tools"] = [{"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters} for t in ir.tools]
    if ir.tool_choice is not None:
        body["tool_choice"] = chat_tool_choice_to_responses(ir.tool_choice)
    if stream:
        body["stream"] = True
    if ir.temperature is not None:
        body["temperature"] = ir.temperature
    if ir.max_tokens is not None:
        body["max_output_tokens"] = ir.max_tokens
    if ir.top_p is not None:
        body["top_p"] = ir.top_p
    body.update(take_extras(ir.extra, RESPONSES_PASSTHROUGH))
    _apply_chat_extras_to_responses(ir.extra, body)
    return body


def usage_to_responses(usage: dict[str, Any] | None) -> dict[str, Any]:
    usage = usage or {}
    inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (inp + out))
    in_det = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    out_det = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": total,
        "input_tokens_details": {"cached_tokens": int(in_det.get("cached_tokens") or 0)},
        "output_tokens_details": {"reasoning_tokens": int(out_det.get("reasoning_tokens") or 0)},
    }


def ir_response_to_responses(ir_resp: IRResponse) -> dict[str, Any]:
    text = ""
    if isinstance(ir_resp.content, list):
        text = "".join(c.text or "" for c in ir_resp.content if c.type == "text")
    else:
        text = ir_resp.content or ""
    output: list[dict[str, Any]] = []
    if ir_resp.reasoning:
        output.append(
            {
                "id": f"rs_{uuid.uuid4().hex[:24]}",
                "type": "reasoning",
                "status": "completed",
                "summary": [{"type": "summary_text", "text": ir_resp.reasoning}],
            }
        )
    if text or not ir_resp.tool_calls:
        output.append(
            {
                "id": f"msg_{uuid.uuid4().hex[:24]}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    if ir_resp.tool_calls:
        for tc in ir_resp.tool_calls:
            output.append(
                {
                    "id": f"fc_{uuid.uuid4().hex[:24]}",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                }
            )
    status = "incomplete" if ir_resp.finish_reason == "length" else "completed"
    resp_id = ir_resp.id if str(ir_resp.id).startswith("resp") else str(ir_resp.id).replace("chatcmpl-", "resp_")
    if not str(resp_id).startswith("resp"):
        resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    return {
        "id": resp_id,
        "object": "response",
        "created_at": ir_resp.created,
        "status": status,
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "model": ir_resp.model,
        "output": output,
        "output_text": text,
        "usage": usage_to_responses(ir_resp.usage),
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
