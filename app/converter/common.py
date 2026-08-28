"""通用工具 — SSE 解析/格式化 + 文本抽取"""
from __future__ import annotations

import json
from typing import Any


def parse_sse_line(line: str) -> dict[str, Any] | None:
    """解析单行 SSE，返回 JSON 或 None。容错：非 data: 前缀、[DONE]、空行"""
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if line == "data: [DONE]":
        return {"__done": True}
    if line.startswith("data: "):
        payload = line[6:]
        if payload == "[DONE]":
            return {"__done": True}
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None
    return None


def sse_format(data: dict[str, Any] | str, event: str | None = None) -> bytes:
    """格式化为 SSE bytes"""
    if isinstance(data, dict):
        payload = json.dumps(data, ensure_ascii=False)
    else:
        payload = data
    out = ""
    if event:
        out += f"event: {event}\n"
    out += f"data: {payload}\n\n"
    return out.encode("utf-8")


def extract_text(content: Any) -> str:
    """从多种 content 形态抽取纯文本"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(c.get("text") or "")
                elif c.get("type") == "image_url":
                    continue
            elif isinstance(c, str):
                parts.append(c)
        return "".join(parts)
    return str(content)
