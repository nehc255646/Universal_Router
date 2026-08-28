"""跨协议 SSE 流转换。同协议透传；跨协议映射文本 / tool_calls / finish。"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

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
            # 多 data 行按 SSE 规范拼接
            events.append("\n".join(data_lines) if len(data_lines) > 1 else data_lines[0])
    return events, remainder.encode(errors="ignore")


def _finish_from_anthropic(stop: str | None) -> str:
    if stop == "max_tokens":
        return "length"
    if stop == "tool_use":
        return "tool_calls"
    return "stop"


def _extract_events(data: dict[str, Any], upstream_mode: str) -> list[dict[str, Any]]:
    """归一化为内部事件: text / tool_start / tool_args / finish / error / done"""
    if data.get("__done"):
        return [{"kind": "done"}]
    if data.get("type") == "error" or "error" in data and not data.get("choices") and data.get("type") != "message_delta":
        if data.get("type") == "error" or (isinstance(data.get("error"), (dict, str)) and upstream_mode != "chat_completions"):
            return [{"kind": "error", "error": data.get("error") or data}]
        # chat 也可能是 {"error":...}
        if "error" in data and not data.get("choices") and not data.get("type"):
            return [{"kind": "error", "error": data.get("error") or data}]

    out: list[dict[str, Any]] = []
    if upstream_mode == "chat_completions":
        if data.get("error") and not data.get("choices"):
            return [{"kind": "error", "error": data["error"]}]
        choices = data.get("choices") or []
        if not choices:
            return out
        ch = choices[0]
        delta = ch.get("delta") or {}
        if delta.get("content"):
            out.append({"kind": "text", "text": delta["content"]})
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
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
        return out

    if upstream_mode == "responses":
        t = data.get("type")
        if t == "response.output_text.delta":
            if data.get("delta"):
                out.append({"kind": "text", "text": data["delta"]})
        elif t in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            if data.get("delta"):
                out.append({"kind": "reasoning", "text": data["delta"]})
        elif t == "response.output_item.added":
            item = data.get("item") or {}
            if item.get("type") == "reasoning":
                summary = item.get("summary") or []
                txt = ""
                if isinstance(summary, list):
                    txt = "".join((s.get("text") if isinstance(s, dict) else str(s)) or "" for s in summary)
                elif isinstance(summary, str):
                    txt = summary
                if txt:
                    out.append({"kind": "reasoning", "text": txt})
            elif item.get("type") == "function_call":
                out.append(
                    {
                        "kind": "tool_start",
                        "index": data.get("output_index", 0),
                        "id": item.get("call_id") or item.get("id") or "",
                        "name": item.get("name") or "",
                    }
                )
                if item.get("arguments"):
                    out.append({"kind": "tool_args", "index": data.get("output_index", 0), "arguments": item["arguments"]})
        elif t == "response.function_call_arguments.delta":
            out.append({"kind": "tool_args", "index": data.get("output_index", 0), "arguments": data.get("delta") or ""})
        elif t in ("response.completed", "response.incomplete"):
            status = (data.get("response") or {}).get("status")
            reason = "stop"
            if status == "incomplete":
                reason = "length"
            out.append({"kind": "finish", "reason": reason})
        return out

    if upstream_mode == "messages":
        t = data.get("type")
        if t == "content_block_start":
            block = data.get("content_block") or {}
            idx = data.get("index", 0)
            if block.get("type") == "tool_use":
                out.append({"kind": "tool_start", "index": idx, "id": block.get("id") or "", "name": block.get("name") or ""})
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
        elif t == "error":
            out.append({"kind": "error", "error": data.get("error") or data})
        return out

    return out


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
    async for ev in _iter_normalized(upstream_mode, raw):
        kind = ev.get("kind")
        if kind == "error":
            saw_error = True
            yield sse_format({"error": ev.get("error")})
            break
        if kind == "text":
            yield sse_format(
                {
                    "id": gen_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": ev["text"]}, "finish_reason": None}],
                }
            )
        elif kind == "reasoning":
            yield sse_format(
                {
                    "id": gen_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"reasoning_content": ev["text"]}, "finish_reason": None}],
                }
            )
        elif kind == "tool_start":
            tc: dict[str, Any] = {
                "index": ev.get("index", 0),
                "id": ev.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": ev.get("name") or "", "arguments": ""},
            }
            yield sse_format(
                {
                    "id": gen_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"tool_calls": [tc]}, "finish_reason": None}],
                }
            )
        elif kind == "tool_args":
            yield sse_format(
                {
                    "id": gen_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"tool_calls": [{"index": ev.get("index", 0), "function": {"arguments": ev.get("arguments") or ""}}]},
                            "finish_reason": None,
                        }
                    ],
                }
            )
        elif kind == "finish":
            finish = ev.get("reason") or "stop"
    if not saw_error:
        yield sse_format(
            {
                "id": gen_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            }
        )
        yield b"data: [DONE]\n\n"
    else:
        yield b"data: [DONE]\n\n"


async def to_responses_stream(upstream_mode: str, raw: AsyncIterator[bytes], model: str) -> AsyncIterator[bytes]:
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    seq = 0
    yield sse_format({"type": "response.created", "response": {"id": resp_id, "model": model}})
    yield sse_format({"type": "response.in_progress", "response": {"id": resp_id}})
    yield sse_format({"type": "response.output_item.added", "item": {"type": "message", "role": "assistant"}})
    yield sse_format({"type": "response.content_part.added", "part": {"type": "output_text"}})
    saw_error = False
    async for ev in _iter_normalized(upstream_mode, raw):
        kind = ev.get("kind")
        if kind == "error":
            saw_error = True
            yield sse_format({"type": "error", "error": ev.get("error")})
            break
        if kind == "text":
            seq += 1
            yield sse_format({"type": "response.output_text.delta", "delta": ev["text"], "sequence_number": seq, "output_index": 0})
        elif kind == "reasoning":
            seq += 1
            yield sse_format({"type": "response.reasoning_summary_text.delta", "delta": ev["text"], "sequence_number": seq})
        elif kind == "tool_start":
            yield sse_format(
                {
                    "type": "response.output_item.added",
                    "item": {
                        "type": "function_call",
                        "call_id": ev.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                        "name": ev.get("name") or "",
                        "arguments": "",
                    },
                    "output_index": ev.get("index", 1),
                }
            )
        elif kind == "tool_args":
            yield sse_format(
                {
                    "type": "response.function_call_arguments.delta",
                    "delta": ev.get("arguments") or "",
                    "output_index": ev.get("index", 1),
                }
            )
    if not saw_error:
        yield sse_format({"type": "response.output_text.done", "text": ""})
        yield sse_format({"type": "response.completed", "response": {"id": resp_id, "model": model, "status": "completed"}})
        yield b"data: [DONE]\n\n"
    else:
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
    saw_error = False
    finish = "end_turn"

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
            stop = _stop()
            if stop:
                yield stop
            src_idx = int(ev.get("index") or 0)
            yield _start("tool", {"type": "tool_use", "id": ev.get("id") or f"toolu_{uuid.uuid4().hex[:8]}", "name": ev.get("name") or "", "input": {}})
            index_map[src_idx] = open_index
        elif kind == "tool_args":
            src_idx = int(ev.get("index") or 0)
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
