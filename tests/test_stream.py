from __future__ import annotations

import asyncio
import json

from app.converter.common import parse_sse_line
from app.stream import split_sse_buffer, to_chat_stream, _extract_events


def test_parse_sse_variants():
    assert parse_sse_line("data: [DONE]")["__done"] is True
    assert parse_sse_line("data:[DONE]")["__done"] is True
    assert parse_sse_line('data: {"a":1}')["a"] == 1
    assert parse_sse_line(": ping") is None


def test_split_sse_buffer_partial():
    events, rest = split_sse_buffer(b'data: {"x":1}\n\ndata: {"x":')
    assert len(events) == 1
    assert b'{"x":' in rest


def test_extract_reasoning():
    ev = _extract_events({"choices": [{"delta": {"reasoning_content": "think"}}]}, "chat_completions")
    assert ev[0]["kind"] == "reasoning"
    ev = _extract_events(
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}},
        "messages",
    )
    assert ev[0]["kind"] == "reasoning"
    ev = _extract_events({"type": "response.reasoning_summary_text.delta", "delta": "r"}, "responses")
    assert ev[0]["text"] == "r"


def test_extract_chat_and_anthropic():
    ev = _extract_events({"choices": [{"delta": {"content": "hi"}}]}, "chat_completions")
    assert ev == [{"kind": "text", "text": "hi"}]
    ev = _extract_events(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "yo"}},
        "messages",
    )
    assert ev[0]["text"] == "yo"
    ev = _extract_events({"type": "response.output_text.delta", "delta": "z"}, "responses")
    assert ev[0]["text"] == "z"


def test_convert_anthropic_stream_to_chat():
    async def raw():
        yield b"event: content_block_delta\ndata: " + json.dumps(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}}
        ).encode() + b"\n\n"
        yield b"event: message_delta\ndata: " + json.dumps(
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
        ).encode() + b"\n\n"

    async def collect():
        out = []
        async for c in to_chat_stream("messages", raw(), "claude"):
            out.append(c.decode())
        return "".join(out)

    text = asyncio.run(collect())
    assert "Hello" in text
    assert "[DONE]" in text
    assert "chat.completion.chunk" in text
