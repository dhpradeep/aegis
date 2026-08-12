"""Shared test doubles for the agent runtime / SDK message layer.

These fakes are duck-typed stand-ins for claude-agent-sdk message and block
objects. They carry an explicit `.type` attribute (real SDK dataclasses do
not) plus the same field names as the real classes, so
`app.services.agent.events.normalize()` handles them identically to real SDK
objects. Used by tests/test_events.py and by later route/runtime tests
(Tasks 10, 12, 19, 20).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any


def fake_init(session_id: str) -> SimpleNamespace:
    """A system message with subtype 'init'."""
    return SimpleNamespace(
        type="system",
        subtype="init",
        session_id=session_id,
        data={"session_id": session_id},
    )


def fake_text(t: str) -> SimpleNamespace:
    """A TextBlock-shaped content block."""
    return SimpleNamespace(type="text", text=t)


def fake_thinking(t: str) -> SimpleNamespace:
    """A ThinkingBlock-shaped content block."""
    return SimpleNamespace(type="thinking", thinking=t)


def fake_tool_use(id: str, name: str, inp: dict) -> SimpleNamespace:
    """A ToolUseBlock-shaped content block."""
    return SimpleNamespace(type="tool_use", id=id, name=name, input=inp)


def fake_tool_result(tool_use_id: str, content: Any = "", is_error: bool = False) -> SimpleNamespace:
    """A ToolResultBlock-shaped content block."""
    return SimpleNamespace(
        type="tool_result",
        tool_use_id=tool_use_id,
        content=content,
        is_error=is_error,
    )


def fake_assistant(*blocks: Any) -> SimpleNamespace:
    """An AssistantMessage-shaped object wrapping the given content blocks."""
    return SimpleNamespace(type="assistant", content=list(blocks))


def fake_user(*blocks: Any) -> SimpleNamespace:
    """A UserMessage-shaped object wrapping the given content blocks."""
    return SimpleNamespace(type="user", content=list(blocks))


def fake_result(
    session_id: str,
    cost: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    duration_ms: int = 0,
    num_turns: int = 1,
    subtype: str = "success",
    result: str = "ok",
    is_error: bool = False,
) -> SimpleNamespace:
    """A ResultMessage-shaped object."""
    return SimpleNamespace(
        type="result",
        subtype=subtype,
        is_error=is_error,
        result=result,
        session_id=session_id,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
        },
        total_cost_usd=cost,
        duration_ms=duration_ms,
        num_turns=num_turns,
    )


class FakeAgentRuntime:
    """Test double for the agent runtime used by route/runtime tests.

    Constructed with a pre-normalized list of event dicts; `stream()` simply
    yields them back, ignoring the passed-in config. This lets route tests
    exercise the streaming/response-shaping layer without ever touching the
    real claude-agent-sdk CLI subprocess.
    """

    def __init__(self, events: list[dict]) -> None:
        self.events = events

    async def stream(self, cfg: Any = None) -> AsyncIterator[dict]:
        for event in self.events:
            yield event
