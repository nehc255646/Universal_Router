"""Converter package — 三协议互转经 IR 中转"""
from .chat_anthropic import anthropic_to_ir, ir_response_to_anthropic, ir_to_anthropic
from .chat_responses import (
    chat_to_ir,
    ir_response_to_chat,
    ir_response_to_responses,
    ir_to_chat,
    ir_to_responses,
    responses_to_ir,
)
from .common import parse_sse_line, sse_format

__all__ = [
    "parse_sse_line",
    "sse_format",
    "chat_to_ir",
    "ir_to_chat",
    "responses_to_ir",
    "ir_to_responses",
    "anthropic_to_ir",
    "ir_to_anthropic",
    "ir_response_to_chat",
    "ir_response_to_responses",
    "ir_response_to_anthropic",
]
