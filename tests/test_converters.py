from __future__ import annotations

from app.converter.chat_anthropic import anthropic_to_ir, ir_response_to_anthropic, ir_to_anthropic
from app.converter.chat_responses import chat_to_ir, ir_response_to_chat, ir_to_chat, ir_to_responses, responses_to_ir
from app.converter.extras import take_extras, CHAT_PASSTHROUGH
from app.ir import IRResponse, IRToolCall


def test_chat_roundtrip_text():
    body = {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
        ],
        "temperature": 0.2,
        "n": 1,
    }
    ir = chat_to_ir(body)
    out = ir_to_chat(ir)
    assert out["model"] == "gpt-4o"
    assert out["messages"][0]["role"] == "system"
    assert out["messages"][1]["content"] == "hi"
    assert out["temperature"] == 0.2
    assert out.get("n") == 1


def test_chat_tools_roundtrip():
    body = {
        "model": "m",
        "messages": [
            {"role": "user", "content": "weather"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\":\"NYC\"}"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
        ],
        "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}],
    }
    ir = chat_to_ir(body)
    assert ir.messages[1].tool_calls[0].name == "get_weather"
    anth = ir_to_anthropic(ir)
    assert anth["tools"][0]["name"] == "get_weather"
    assert anth["messages"][-1]["role"] == "user"
    assert anth["messages"][-1]["content"][0]["type"] == "tool_result"
    back = ir_to_chat(ir)
    assert back["messages"][2]["role"] == "tool"


def test_anthropic_requires_max_tokens():
    ir = chat_to_ir({"model": "claude", "messages": [{"role": "user", "content": "hi"}]})
    body = ir_to_anthropic(ir)
    assert body["max_tokens"] == 4096
    assert "n" not in body


def test_extra_does_not_leak_to_anthropic():
    ir = chat_to_ir({"model": "m", "messages": [{"role": "user", "content": "x"}], "frequency_penalty": 0.5, "n": 2})
    anth = ir_to_anthropic(ir)
    assert "frequency_penalty" not in anth
    assert "n" not in anth
    chat = ir_to_chat(ir)
    assert chat.get("n") == 2


def test_take_extras():
    assert take_extras({"n": 1, "foo": 2}, CHAT_PASSTHROUGH) == {"n": 1}


def test_anthropic_merge_consecutive_users():
    ir = chat_to_ir(
        {
            "model": "m",
            "messages": [
                {"role": "user", "content": "a"},
                {"role": "tool", "tool_call_id": "t1", "content": "r1"},
            ],
        }
    )
    body = ir_to_anthropic(ir)
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user"]
    blocks = body["messages"][0]["content"]
    types = [b["type"] for b in blocks]
    assert "text" in types and "tool_result" in types


def test_responses_string_input():
    ir = responses_to_ir({"model": "m", "input": "hello", "instructions": "be nice"})
    assert ir.messages[0].role == "system"
    assert ir.messages[1].content == "hello"
    out = ir_to_responses(ir)
    assert out["instructions"] == "be nice"
    assert out["input"][-1]["content"] == "hello" or out["input"][-1]["role"] == "user"


def test_responses_function_call_history():
    ir = responses_to_ir(
        {
            "model": "m",
            "input": [
                {"type": "function_call", "call_id": "c1", "name": "fn", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            ],
        }
    )
    assert ir.messages[0].role == "assistant"
    assert ir.messages[1].role == "tool"
    out = ir_to_responses(ir)
    types = [x.get("type") for x in out["input"]]
    assert "function_call" in types and "function_call_output" in types


def test_ir_response_to_chat_and_anthropic():
    ir = IRResponse(
        model="m",
        content="hello",
        tool_calls=[IRToolCall(id="c1", name="fn", arguments="{}")],
        finish_reason="tool_calls",
        usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    )
    chat = ir_response_to_chat(ir)
    assert chat["choices"][0]["message"]["content"] == "hello"
    assert chat["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "fn"
    anth = ir_response_to_anthropic(ir)
    assert anth["stop_reason"] == "tool_use"
    assert any(c["type"] == "tool_use" for c in anth["content"])


def test_anthropic_to_ir_system_and_image():
    ir = anthropic_to_ir(
        {
            "model": "claude",
            "system": "sys",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "see"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}},
                    ],
                }
            ],
            "max_tokens": 10,
        }
    )
    assert ir.messages[0].role == "system"
    assert isinstance(ir.messages[1].content, list)
    assert ir.messages[1].content[1].type == "image_url"
    assert ir.messages[1].content[1].image_url.startswith("data:image/png")
