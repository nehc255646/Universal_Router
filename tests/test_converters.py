from __future__ import annotations

from app.converter.chat_anthropic import anthropic_to_ir, ir_response_to_anthropic, ir_to_anthropic
from app.converter.chat_responses import chat_to_ir, ir_response_to_chat, ir_response_to_responses, ir_to_chat, ir_to_responses, responses_to_ir
from app.converter.extras import CHAT_PASSTHROUGH, take_extras
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
    last = out["input"][-1]
    assert last["type"] == "message" and last["role"] == "user"
    assert last["content"][0]["type"] == "input_text"
    assert last["content"][0]["text"] == "hello"


def test_responses_message_item_and_image():
    ir = responses_to_ir(
        {
            "model": "m",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "see"},
                        {"type": "input_image", "image_url": "https://x/a.png"},
                    ],
                }
            ],
        }
    )
    assert isinstance(ir.messages[-1].content, list)
    assert ir.messages[-1].content[1].type == "image_url"
    out = ir_to_responses(ir)
    parts = out["input"][-1]["content"]
    assert any(p.get("type") == "input_image" for p in parts)


def test_responses_tools_and_choice_from_chat_shape():
    ir = responses_to_ir(
        {
            "model": "m",
            "input": "hi",
            "tools": [{"type": "function", "function": {"name": "fn", "parameters": {"type": "object"}}}],
            "tool_choice": {"type": "function", "name": "fn"},
            "text": {"format": {"type": "json_object"}},
            "reasoning": {"effort": "low"},
        }
    )
    assert ir.tools[0].name == "fn"
    chat = ir_to_chat(ir)
    assert chat["tools"][0]["function"]["name"] == "fn"
    assert chat["tool_choice"] == {"type": "function", "function": {"name": "fn"}}
    assert chat["response_format"]["type"] == "json_object"
    assert chat["reasoning_effort"] == "low"


def test_chat_structured_output_to_responses():
    ir = chat_to_ir(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "out", "schema": {"type": "object"}, "strict": True}},
            "reasoning_effort": "medium",
            "tool_choice": {"type": "function", "function": {"name": "fn"}},
        }
    )
    out = ir_to_responses(ir)
    assert out["text"]["format"]["type"] == "json_schema"
    assert out["text"]["format"]["name"] == "out"
    assert out["reasoning"]["effort"] == "medium"
    assert out["tool_choice"] == {"type": "function", "name": "fn"}


def test_responses_same_protocol_passthrough_fills_sdk_fields():
    from app.server import _upstream_resp_to_inbound

    data = {
        "id": "resp_1",
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1},
    }
    out = _upstream_resp_to_inbound("responses", "responses", data, "m")
    assert out["output_text"] == "ok"
    assert out["usage"]["input_tokens"] == 2
    assert out["output"][0]["content"][0]["text"] == "ok"


def test_ir_response_to_responses_usage_and_output_text():
    ir = IRResponse(
        model="m",
        content="hello",
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    )
    resp = ir_response_to_responses(ir)
    assert resp["object"] == "response"
    assert resp["output_text"] == "hello"
    assert resp["usage"]["input_tokens"] == 3
    assert resp["usage"]["output_tokens"] == 2
    assert resp["output"][0]["type"] == "message"
    assert resp["output"][0]["content"][0]["type"] == "output_text"
    assert resp["output"][0].get("status") == "completed"


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


def test_ir_response_reasoning_mapped():
    ir = IRResponse(model="m", content="hello", reasoning="step 1")
    chat = ir_response_to_chat(ir)
    assert chat["choices"][0]["message"]["reasoning_content"] == "step 1"
    anth = ir_response_to_anthropic(ir)
    assert anth["content"][0]["type"] == "thinking"
    from app.converter.chat_responses import ir_response_to_responses

    resp = ir_response_to_responses(ir)
    assert resp["output"][0]["type"] == "reasoning"


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
