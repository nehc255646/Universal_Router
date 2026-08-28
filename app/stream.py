"""跨协议 SSE 流转换。同协议透传；跨协议映射文本 / thinking / tool_calls / finish。"""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from .converter.common import parse_sse_line, sse_format
from .upstream import UpstreamHTTPError


def inbound_mode(inbound: str) -> str:
    return {"chat": "chat_completions", "responses": "responses", "messages": "messages"}.get(inbound, inbound)


async def safe_upstream_stream(raw: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    try:
        async for chunk in raw:
            if chunk:
                yield chunk
    except UpstreamHTTPError as e:
        err: Any
        try:
            err = json.loads(e.body.decode(errors="ignore")) if e.body else {"error": str(e)}
        except Exception:
            err = {"error": (e.body.decode(errors="ignore")[:2000] if e.body else str(e))}
        yield sse_format({"type": "error", "error": err, "status": e.status_code})
        yield b"data: [DONE]\n\n"
    except Exception as e:
        yield sse_format({"type": "error", "error": str(e)})
        yield b"data: [DONE]\n\n"


def split_sse_buffer(buffer: bytes) -> tuple[list[str], bytes]:
    text = buffer.decode(errors="ignore").replace("\r\n", "\n")
    parts = text.split("\n\n")
    remainder = parts.pop() if parts else ""
    events: list[str] = []
    for part in parts:
        data_lines: list[str] = []
        for line in part.split("\n"):
            s = line.strip()
            if not s or s.startswith(":") or s.startswith("event:"):
                continue
            if s.startswith("data:"):
                data_lines.append(s)
        if data_lines:
            events.append("\n".join(data_lines) if len(data_lines) > 1 else data_lines[0])
    return events, remainder.encode(errors="ignore")


def _finish_from_anthropic(stop: str | None) -> str:
    if stop == "max_tokens":
        return "length"
    if stop == "tool_use":
        return "tool_calls"
    return "stop"


def _usage_event(usage: Any) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    return {"kind": "usage", "prompt_tokens": int(prompt or 0), "completion_tokens": int(completion or 0)}


def _error_event(data: dict[str, Any], upstream_mode: str) -> list[dict[str, Any]] | None:
    t = data.get("type")
    if t == "error" or (isinstance(data.get("error"), (dict, str)) and not data.get("choices") and t not in ("message_delta", "content_block_delta")):
        if t == "error" or (isinstance(data.get("error"), (dict, str)) and upstream_mode != "chat_completions"):
            return [{"kind": "error", "error": data.get("error") or data}]
        if "error" in data and not data.get("choices") and not t:
            return [{"kind": "error", "error": data.get("error") or data}]
    return None


def _events_chat(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if data.get("error") and not data.get("choices"):
        return [{"kind": "error", "error": data["error"]}]
    usage = _usage_event(data.get("usage"))
    choices = data.get("choices") or []
    if not choices:
        return [usage] if usage else out
    ch = choices[0]
    delta = ch.get("delta") or {}
    if delta.get("content"):
        out.append({"kind": "text", "text": delta["content"]})
    if delta.get("refusal"):
        out.append({"kind": "text", "text": delta["refusal"]})
    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking")
    if reasoning:
        out.append({"kind": "reasoning", "text": reasoning})
    for tc in delta.get("tool_calls") or []:
        idx = tc.get("index", 0)
        fn = tc.get("function") or {}
        if tc.get("id") or fn.get("name"):
            out.append({"kind": "tool_start", "index": idx, "id": tc.get("id") or "", "name": fn.get("name") or ""})
        if fn.get("arguments"):
            out.append({"kind": "tool_args", "index": idx, "arguments": fn["arguments"]})
    if ch.get("finish_reason"):
        out.append({"kind": "finish", "reason": ch["finish_reason"]})
    if usage:
        out.append(usage)
    return out


def _events_responses(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    t = data.get("type")
    if t in ("response.failed", "error"):
        err = data.get("error") or (data.get("response") or {}).get("error") or data
        return [{"kind": "error", "error": err}]
    if t == "response.output_text.delta" or t == "response.refusal.delta":
        if data.get("delta"):
            out.append({"kind": "text", "text": data["delta"]})
    elif t in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
        if data.get("delta"):
            out.append({"kind": "reasoning", "text": data["delta"]})
    elif t == "response.output_item.added":
        item = data.get("item") or {}
        idx = data.get("output_index", 0)
        itype = item.get("type")
        if itype == "reasoning":
            summary = item.get("summary") or []
            txt = ""
            if isinstance(summary, list):
                txt = "".join((s.get("text") if isinstance(s, dict) else str(s)) or "" for s in summary)
            elif isinstance(summary, str):
                txt = summary
            if txt:
                out.append({"kind": "reasoning", "text": txt})
        elif itype == "function_call":
            out.append(
                {
                    "kind": "tool_start",
                    "index": idx,
                    "id": item.get("call_id") or item.get("id") or "",
                    "name": item.get("name") or "",
                }
            )
            if item.get("arguments"):
                out.append({"kind": "tool_args", "index": idx, "arguments": item["arguments"]})
    elif t == "response.function_call_arguments.delta":
        out.append({"kind": "tool_args", "index": data.get("output_index", 0), "arguments": data.get("delta") or ""})
    elif t in ("response.completed", "response.incomplete"):
        resp = data.get("response") or {}
        status = resp.get("status")
        reason = "stop"
        if t == "response.incomplete" or status == "incomplete":
            reason = "length"
        out_items = resp.get("output") or []
        if any(isinstance(x, dict) and x.get("type") == "function_call" for x in out_items):
            reason = "tool_calls"
        out.append({"kind": "finish", "reason": reason})
        usage = _usage_event(resp.get("usage"))
        if usage:
            out.append(usage)
    return out


def _events_messages(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    t = data.get("type")
    if t == "content_block_start":
        block = data.get("content_block") or {}
        idx = data.get("index", 0)
        if block.get("type") == "tool_use":
            out.append({"kind": "tool_start", "index": idx, "id": block.get("id") or "", "name": block.get("name") or ""})
            inp = block.get("input")
            if inp:
                out.append({"kind": "tool_args", "index": idx, "arguments": json.dumps(inp, ensure_ascii=False)})
        elif block.get("type") in ("thinking", "redacted_thinking"):
            thought = block.get("thinking") or ""
            if thought:
                out.append({"kind": "reasoning", "text": thought})
    elif t == "content_block_delta":
        d = data.get("delta") or {}
        if d.get("type") == "text_delta" and d.get("text"):
            out.append({"kind": "text", "text": d["text"]})
        elif d.get("type") == "thinking_delta":
            txt = d.get("thinking") or d.get("text") or ""
            if txt:
                out.append({"kind": "reasoning", "text": txt})
        elif d.get("type") == "input_json_delta" and d.get("partial_json"):
            out.append({"kind": "tool_args", "index": data.get("index", 0), "arguments": d["partial_json"]})
    elif t == "message_delta":
        stop = (data.get("delta") or {}).get("stop_reason")
        if stop:
            out.append({"kind": "finish", "reason": _finish_from_anthropic(stop)})
        usage = _usage_event(data.get("usage"))
        if usage:
            out.append(usage)
    elif t == "error":
        out.append({"kind": "error", "error": data.get("error") or data})
    return out


def _extract_events(data: dict[str, Any], upstream_mode: str) -> list[dict[str, Any]]:
    """归一化为内部事件: text / reasoning / tool_start / tool_args / finish / error / usage / done"""
    if data.get("__done"):
        return [{"kind": "done"}]
    err = _error_event(data, upstream_mode)
    if err is not None:
        return err
    if upstream_mode == "chat_completions":
        return _events_chat(data)
    if upstream_mode == "responses":
        return _events_responses(data)
    if upstream_mode == "messages":
        return _events_messages(data)
    return []


async def _iter_normalized(upstream_mode: str, raw: AsyncIterator[bytes]) -> AsyncIterator[dict[str, Any]]:
    buffer = b""
    async for raw_chunk in raw:
        buffer += raw_chunk
        events, buffer = split_sse_buffer(buffer)
        for ev in events:
            data = parse_sse_line(ev)
            if data is None:
                continue
            for item in _extract_events(data, upstream_mode):
                yield item
    if buffer.strip():
        for line in buffer.decode(errors="ignore").split("\n"):
            data = parse_sse_line(line)
            if data is None:
                continue
            for item in _extract_events(data, upstream_mode):
                yield item


def _new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:8]}"


async def to_chat_stream(upstream_mode: str, raw: AsyncIterator[bytes], model: str) -> AsyncIterator[bytes]:
    created = int(time.time())
    gen_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    yield sse_format(
        {
            "id": gen_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    )
    finish = "stop"
    saw_error = False
    saw_tool = False
    tool_ids: dict[int, str] = {}

    def chunk(delta: dict[str, Any], reason: str | None = None) -> bytes:
        return sse_format(
            {
                "id": gen_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
            }
        )

    async for ev in _iter_normalized(upstream_mode, raw):
        kind = ev.get("kind")
        if kind == "error":
            saw_error = True
            yield sse_format({"error": ev.get("error")})
            break
        if kind == "text":
            yield chunk({"content": ev["text"]})
        elif kind == "reasoning":
            yield chunk({"reasoning_content": ev["text"]})
        elif kind == "tool_start":
            saw_tool = True
            idx = int(ev.get("index") or 0)
            tid = ev.get("id") or tool_ids.get(idx) or _new_call_id()
            tool_ids[idx] = tid
            tc: dict[str, Any] = {
                "index": idx,
                "id": tid,
                "type": "function",
                "function": {"name": ev.get("name") or "", "arguments": ""},
            }
            yield chunk({"tool_calls": [tc]})
        elif kind == "tool_args":
            saw_tool = True
            idx = int(ev.get("index") or 0)
            if idx not in tool_ids:
                tid = _new_call_id()
                tool_ids[idx] = tid
                yield chunk({"tool_calls": [{"index": idx, "id": tid, "type": "function", "function": {"name": "", "arguments": ""}}]})
            yield chunk({"tool_calls": [{"index": idx, "function": {"arguments": ev.get("arguments") or ""}}]})
        elif kind == "finish":
            finish = ev.get("reason") or "stop"
    if not saw_error:
        if saw_tool and finish == "stop":
            finish = "tool_calls"
        yield chunk({}, finish)
        yield b"data: [DONE]\n\n"
    else:
        yield b"data: [DONE]\n\n"


async def to_responses_stream(upstream_mode: str, raw: AsyncIterator[bytes], model: str) -> AsyncIterator[bytes]:
    """尽量复刻 Responses 事件生命周期：created → in_progress → item/part/delta → done → completed/failed。"""
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    seq = 0
    saw_error = False
    finish = "stop"
    text_acc = ""
    reason_acc = ""
    args_acc: dict[int, str] = {}
    tool_ids: dict[int, str] = {}
    tool_names: dict[int, str] = {}
    open_kind: str | None = None  # reasoning | message | tool
    open_item_id: str | None = None
    open_index = -1
    open_src_idx = -1
    next_index = 0

    def emit(payload: dict[str, Any]) -> bytes:
        nonlocal seq
        seq += 1
        payload.setdefault("sequence_number", seq)
        return sse_format(payload)

    def new_item_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    yield emit(
        {
            "type": "response.created",
            "response": {"id": resp_id, "object": "response", "model": model, "status": "in_progress", "output": []},
        }
    )
    yield emit({"type": "response.in_progress", "response": {"id": resp_id, "status": "in_progress"}})

    def close_open() -> list[bytes]:
        nonlocal open_kind, open_item_id, open_index, open_src_idx
        chunks: list[bytes] = []
        if open_kind == "message":
            chunks.append(emit({"type": "response.output_text.done", "item_id": open_item_id, "output_index": open_index, "text": text_acc}))
            chunks.append(emit({"type": "response.content_part.done", "item_id": open_item_id, "output_index": open_index, "part": {"type": "output_text", "text": text_acc}}))
            chunks.append(emit({"type": "response.output_item.done", "output_index": open_index, "item": {"id": open_item_id, "type": "message", "status": "completed", "role": "assistant"}}))
        elif open_kind == "reasoning":
            chunks.append(emit({"type": "response.reasoning_summary_text.done", "item_id": open_item_id, "output_index": open_index, "text": reason_acc}))
            chunks.append(emit({"type": "response.output_item.done", "output_index": open_index, "item": {"id": open_item_id, "type": "reasoning", "status": "completed"}}))
        elif open_kind == "tool":
            src = open_src_idx if open_src_idx >= 0 else open_index
            args = args_acc.get(src, "")
            chunks.append(
                emit(
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": open_item_id,
                        "output_index": open_index,
                        "arguments": args,
                    }
                )
            )
            chunks.append(
                emit(
                    {
                        "type": "response.output_item.done",
                        "output_index": open_index,
                        "item": {
                            "id": open_item_id,
                            "type": "function_call",
                            "status": "completed",
                            "call_id": tool_ids.get(src, ""),
                            "name": tool_names.get(src, ""),
                            "arguments": args,
                        },
                    }
                )
            )
        open_kind = None
        open_item_id = None
        open_index = -1
        open_src_idx = -1
        return chunks

    def start_message() -> list[bytes]:
        nonlocal open_kind, open_item_id, open_index, next_index, open_src_idx
        chunks = close_open()
        open_kind = "message"
        open_index = next_index
        next_index += 1
        open_item_id = new_item_id("msg")
        chunks.append(emit({"type": "response.output_item.added", "output_index": open_index, "item": {"id": open_item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}}))
        chunks.append(emit({"type": "response.content_part.added", "item_id": open_item_id, "output_index": open_index, "part": {"type": "output_text", "text": ""}}))
        return chunks

    def start_reasoning() -> list[bytes]:
        nonlocal open_kind, open_item_id, open_index, next_index, open_src_idx
        chunks = close_open()
        open_kind = "reasoning"
        open_index = next_index
        next_index += 1
        open_item_id = new_item_id("rs")
        chunks.append(emit({"type": "response.output_item.added", "output_index": open_index, "item": {"id": open_item_id, "type": "reasoning", "status": "in_progress", "summary": []}}))
        return chunks

    def start_tool(src_idx: int, call_id: str, name: str) -> list[bytes]:
        nonlocal open_kind, open_item_id, open_index, next_index, open_src_idx
        chunks = close_open()
        open_kind = "tool"
        open_index = next_index
        next_index += 1
        open_src_idx = src_idx
        open_item_id = new_item_id("fc")
        tool_ids[src_idx] = call_id
        tool_names[src_idx] = name
        args_acc.setdefault(src_idx, "")
        chunks.append(
            emit(
                {
                    "type": "response.output_item.added",
                    "output_index": open_index,
                    "item": {
                        "id": open_item_id,
                        "type": "function_call",
                        "status": "in_progress",
                        "call_id": call_id,
                        "name": name,
                        "arguments": "",
                    },
                }
            )
        )
        return chunks

    src_to_out: dict[int, int] = {}

    async for ev in _iter_normalized(upstream_mode, raw):
        kind = ev.get("kind")
        if kind == "error":
            saw_error = True
            for c in close_open():
                yield c
            yield emit({"type": "response.failed", "response": {"id": resp_id, "status": "failed", "error": ev.get("error")}})
            yield emit({"type": "error", "error": ev.get("error")})
            break
        if kind == "text":
            text_acc += ev["text"]
            if open_kind != "message":
                for c in start_message():
                    yield c
            yield emit({"type": "response.output_text.delta", "item_id": open_item_id, "output_index": open_index, "delta": ev["text"]})
        elif kind == "reasoning":
            reason_acc += ev["text"]
            if open_kind != "reasoning":
                for c in start_reasoning():
                    yield c
            yield emit({"type": "response.reasoning_summary_text.delta", "item_id": open_item_id, "output_index": open_index, "delta": ev["text"]})
        elif kind == "tool_start":
            src_idx = int(ev.get("index") or 0)
            call_id = ev.get("id") or tool_ids.get(src_idx) or _new_call_id()
            name = ev.get("name") or tool_names.get(src_idx) or ""
            for c in start_tool(src_idx, call_id, name):
                yield c
            src_to_out[src_idx] = open_index
        elif kind == "tool_args":
            src_idx = int(ev.get("index") or 0)
            if src_idx not in src_to_out:
                call_id = tool_ids.get(src_idx) or _new_call_id()
                name = tool_names.get(src_idx) or ""
                for c in start_tool(src_idx, call_id, name):
                    yield c
                src_to_out[src_idx] = open_index
            out_idx = src_to_out.get(src_idx, open_index)
            piece = ev.get("arguments") or ""
            args_acc[src_idx] = args_acc.get(src_idx, "") + piece
            yield emit({"type": "response.function_call_arguments.delta", "output_index": out_idx, "delta": piece})
        elif kind == "finish":
            finish = ev.get("reason") or "stop"
    if saw_error:
        yield b"data: [DONE]\n\n"
        return
    for c in close_open():
        yield c
    status = "incomplete" if finish == "length" else "completed"
    yield emit({"type": "response.completed" if status == "completed" else "response.incomplete", "response": {"id": resp_id, "model": model, "status": status}})
    yield b"data: [DONE]\n\n"


async def to_anthropic_stream(upstream_mode: str, raw: AsyncIterator[bytes], model: str) -> AsyncIterator[bytes]:
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    yield sse_format(
        {"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "model": model, "content": []}},
        event="message_start",
    )
    open_kind: str | None = None
    open_index = -1
    next_index = 0
    index_map: dict[int, int] = {}
    tool_ids: dict[int, str] = {}
    saw_error = False
    finish = "end_turn"
    saw_tool = False

    def _start(kind: str, block: dict[str, Any]) -> bytes:
        nonlocal open_kind, open_index, next_index
        idx = next_index
        next_index += 1
        open_kind = kind
        open_index = idx
        return sse_format({"type": "content_block_start", "index": idx, "content_block": block}, event="content_block_start")

    def _stop() -> bytes | None:
        nonlocal open_kind, open_index
        if open_kind is None:
            return None
        payload = sse_format({"type": "content_block_stop", "index": open_index}, event="content_block_stop")
        open_kind = None
        return payload

    async for ev in _iter_normalized(upstream_mode, raw):
        kind = ev.get("kind")
        if kind == "error":
            saw_error = True
            stop = _stop()
            if stop:
                yield stop
            yield sse_format({"type": "error", "error": ev.get("error")}, event="error")
            break
        if kind == "reasoning":
            if open_kind != "thinking":
                stop = _stop()
                if stop:
                    yield stop
                yield _start("thinking", {"type": "thinking", "thinking": ""})
            yield sse_format(
                {"type": "content_block_delta", "index": open_index, "delta": {"type": "thinking_delta", "thinking": ev["text"]}},
                event="content_block_delta",
            )
        elif kind == "text":
            if open_kind != "text":
                stop = _stop()
                if stop:
                    yield stop
                yield _start("text", {"type": "text", "text": ""})
            yield sse_format(
                {"type": "content_block_delta", "index": open_index, "delta": {"type": "text_delta", "text": ev["text"]}},
                event="content_block_delta",
            )
        elif kind == "tool_start":
            saw_tool = True
            stop = _stop()
            if stop:
                yield stop
            src_idx = int(ev.get("index") or 0)
            tid = ev.get("id") or tool_ids.get(src_idx) or f"toolu_{uuid.uuid4().hex[:8]}"
            tool_ids[src_idx] = tid
            yield _start("tool", {"type": "tool_use", "id": tid, "name": ev.get("name") or "", "input": {}})
            index_map[src_idx] = open_index
        elif kind == "tool_args":
            saw_tool = True
            src_idx = int(ev.get("index") or 0)
            if src_idx not in index_map:
                stop = _stop()
                if stop:
                    yield stop
                tid = tool_ids.get(src_idx) or f"toolu_{uuid.uuid4().hex[:8]}"
                tool_ids[src_idx] = tid
                yield _start("tool", {"type": "tool_use", "id": tid, "name": "", "input": {}})
                index_map[src_idx] = open_index
            dst = index_map.get(src_idx, open_index)
            yield sse_format(
                {"type": "content_block_delta", "index": dst, "delta": {"type": "input_json_delta", "partial_json": ev.get("arguments") or ""}},
                event="content_block_delta",
            )
        elif kind == "finish":
            r = ev.get("reason")
            if r == "length":
                finish = "max_tokens"
            elif r == "tool_calls":
                finish = "tool_use"
            else:
                finish = "end_turn"
    if saw_error:
        yield sse_format({"type": "message_stop"}, event="message_stop")
        return
    stop = _stop()
    if stop:
        yield stop
    if saw_tool and finish == "end_turn":
        finish = "tool_use"
    yield sse_format({"type": "message_delta", "delta": {"stop_reason": finish, "stop_sequence": None}, "usage": {"output_tokens": 0}}, event="message_delta")
    yield sse_format({"type": "message_stop"}, event="message_stop")


async def convert_stream(inbound: str, upstream_mode: str, raw: AsyncIterator[bytes], model: str) -> AsyncIterator[bytes]:
    raw = safe_upstream_stream(raw)
    if inbound_mode(inbound) == upstream_mode:
        async for chunk in raw:
            yield chunk
        return
    if inbound == "chat":
        async for chunk in to_chat_stream(upstream_mode, raw, model):
            yield chunk
        return
    if inbound == "responses":
        async for chunk in to_responses_stream(upstream_mode, raw, model):
            yield chunk
        return
    if inbound == "messages":
        async for chunk in to_anthropic_stream(upstream_mode, raw, model):
            yield chunk
        return
    async for chunk in raw:
        yield chunk
