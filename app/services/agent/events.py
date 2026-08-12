"""Normalize claude-agent-sdk messages into a stable, JSON-friendly event schema.

`normalize(msg)` accepts either a real SDK message object (SystemMessage,
AssistantMessage, UserMessage, ResultMessage) or a duck-typed test stub with
the same attribute names, and returns a list of plain-dict events.

Dispatch strategy: real SDK message/block dataclasses do not carry a `.type`
attribute (the wire dict's "type" key is consumed by the SDK's own parser and
discarded), so real objects are routed via `isinstance` against the SDK
classes. Test fakes (see tests/fakes.py) set an explicit `.type` string
attribute, which is checked first — this keeps the dispatch logic identical
for real and fake objects and lets fakes skip subclassing anything.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)


def _message_kind(msg: Any) -> str | None:
    """Return the dispatch key for a message: 'system' | 'assistant' | 'user' | 'result' | None."""
    kind = getattr(msg, "type", None)
    if kind is not None:
        return kind
    if isinstance(msg, SystemMessage):
        return "system"
    if isinstance(msg, AssistantMessage):
        return "assistant"
    if isinstance(msg, UserMessage):
        return "user"
    if isinstance(msg, ResultMessage):
        return "result"
    return None


def _usage_field(usage: Any, key: str, default: int = 0) -> int:
    """Read a usage field whether `usage` is a dict (real SDK shape) or an
    attribute-bearing object (fakes may use either)."""
    if usage is None:
        return default
    if isinstance(usage, dict):
        return usage.get(key, default)
    return getattr(usage, key, default)


def _flatten_content(content: Any) -> str:
    """Flatten a tool_result's content (str, list of content blocks, or None) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
            else:
                text = getattr(item, "text", None)
                if text is not None:
                    parts.append(text)
        return "".join(parts)
    return str(content)


def _normalize_system(msg: Any) -> list[dict]:
    subtype = getattr(msg, "subtype", None)
    if subtype != "init":
        return []
    session_id = getattr(msg, "session_id", None)
    if session_id is None:
        data = getattr(msg, "data", None) or {}
        session_id = data.get("session_id") if isinstance(data, dict) else None
    return [{"type": "init", "session_id": session_id}]


def _block_event(block: Any) -> dict | None:
    btype = getattr(block, "type", None)
    if btype == "text" or isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if btype == "thinking" or isinstance(block, ThinkingBlock):
        return {"type": "thinking", "text": getattr(block, "thinking", None)}
    if btype == "tool_use" or isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return None


def _normalize_assistant(msg: Any) -> list[dict]:
    events: list[dict] = []
    for block in getattr(msg, "content", None) or []:
        event = _block_event(block)
        if event is not None:
            events.append(event)
    return events


def _tool_result_event(block: Any) -> dict | None:
    btype = getattr(block, "type", None)
    if btype == "tool_result" or isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "is_error": bool(getattr(block, "is_error", False)),
            "content": _flatten_content(getattr(block, "content", None)),
        }
    return None


def _normalize_user(msg: Any) -> list[dict]:
    content = getattr(msg, "content", None)
    if not isinstance(content, list):
        return []
    events: list[dict] = []
    for block in content:
        event = _tool_result_event(block)
        if event is not None:
            events.append(event)
    return events


def _normalize_result(msg: Any) -> list[dict]:
    usage = getattr(msg, "usage", None)
    return [
        {
            "type": "result",
            "subtype": getattr(msg, "subtype", None),
            "is_error": bool(getattr(msg, "is_error", False)),
            "result": getattr(msg, "result", None),
            "session_id": getattr(msg, "session_id", None),
            "usage": {
                "input_tokens": _usage_field(usage, "input_tokens"),
                "output_tokens": _usage_field(usage, "output_tokens"),
                "cache_read_tokens": _usage_field(usage, "cache_read_input_tokens"),
            },
            "cost_usd": getattr(msg, "total_cost_usd", 0),
            "duration_ms": getattr(msg, "duration_ms", 0),
            "num_turns": getattr(msg, "num_turns", 0),
        }
    ]


_HANDLERS = {
    "system": _normalize_system,
    "assistant": _normalize_assistant,
    "user": _normalize_user,
    "result": _normalize_result,
}


def normalize(msg: Any) -> list[dict]:
    """Map one SDK message (real or duck-typed fake) to zero or more normalized events."""
    kind = _message_kind(msg)
    handler = _HANDLERS.get(kind) if kind is not None else None
    if handler is None:
        return []
    return handler(msg)
