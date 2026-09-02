"""按目标协议白名单透传 extra 字段，避免跨协议污染导致上游 400。"""
from __future__ import annotations

from typing import Any

CHAT_PASSTHROUGH = frozenset(
    {
        "n",
        "user",
        "seed",
        "frequency_penalty",
        "presence_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "response_format",
        "parallel_tool_calls",
        "stream_options",
        "reasoning_effort",
        "max_completion_tokens",
        "metadata",
        "service_tier",
        "modalities",
        "prediction",
        "audio",
        "store",
    }
)

RESPONSES_PASSTHROUGH = frozenset(
    {
        "previous_response_id",
        "store",
        "include",
        "metadata",
        "reasoning",
        "text",
        "truncation",
        "user",
        "service_tier",
        "prompt_cache_key",
        "parallel_tool_calls",
        "max_tool_calls",
        "instructions",
        "background",
        "conversation",
        "prompt",
    }
)

ANTHROPIC_PASSTHROUGH = frozenset(
    {
        "metadata",
        "thinking",
        "top_k",
        "service_tier",
        "cache_control",
        "stop_sequences",
        "container",
        "mcp_servers",
        "context_management",
    }
)


def take_extras(extra: dict[str, Any] | None, allowed: frozenset[str]) -> dict[str, Any]:
    if not extra:
        return {}
    return {k: v for k, v in extra.items() if k in allowed and v is not None}
