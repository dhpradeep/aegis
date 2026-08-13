"""AgentRuntime: runs claude-agent-sdk queries and yields normalized events.

`AgentRuntime.stream(cfg)` drives `query()` from claude-agent-sdk (or an
injected fake, for tests) and maps each raw SDK message through
`app.services.agent.events.normalize` into the stable event-dict schema.

Auth is subscription-only: `_options` never sets `env` (and therefore never
leaks `ANTHROPIC_API_KEY` into the subprocess environment).

`query()` from the SDK has an unusual error shape: on some failures it yields
a final error-flagged ResultMessage and *then* raises. `stream` accounts for
that by tracking whether a `result` event has already been emitted before
deciding whether a caught exception should surface as a synthetic `error`
event.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import query as _default_query

from app.services.agent.events import normalize


@dataclass
class RunConfig:
    prompt: str
    cwd: str
    system_prompt: str | None
    allowed_tools: list[str]
    permission_mode: str
    mcp_servers: dict
    model: str | None
    max_turns: int
    resume: str | None
    timeout_s: int
    agents: dict | None = None
    effort: str | None = None
    # Base set of built-in tools ([] disables them all); None keeps the default.
    tools: list | None = None


class AgentRuntime:
    """Wraps claude-agent-sdk's `query()` and yields normalized event dicts."""

    def __init__(self, query_fn: Callable[..., Any] = _default_query) -> None:
        self._query = query_fn

    def _options(self, cfg: RunConfig) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions for a run.

        NEVER sets `env` (subscription-only auth: setting env with
        ANTHROPIC_API_KEY would override the user's subscription login).
        Optional fields are only included when not None.
        """
        kw: dict[str, Any] = dict(
            cwd=cfg.cwd,
            allowed_tools=cfg.allowed_tools,
            permission_mode=cfg.permission_mode,
            mcp_servers=cfg.mcp_servers,
            max_turns=cfg.max_turns,
            setting_sources=[],
        )
        if cfg.system_prompt is not None:
            kw["system_prompt"] = cfg.system_prompt
        if cfg.model is not None:
            kw["model"] = cfg.model
        if cfg.resume is not None:
            kw["resume"] = cfg.resume
        if cfg.agents is not None:
            kw["agents"] = cfg.agents
        if cfg.effort is not None:
            kw["effort"] = cfg.effort
        if cfg.tools is not None:
            kw["tools"] = cfg.tools
        return ClaudeAgentOptions(**kw)

    async def stream(self, cfg: RunConfig) -> AsyncIterator[dict]:
        saw_result = False

        async def _run() -> AsyncIterator[dict]:
            nonlocal saw_result
            try:
                async for msg in self._query(prompt=cfg.prompt, options=self._options(cfg)):
                    for ev in normalize(msg):
                        if ev["type"] == "result":
                            saw_result = True
                        yield ev
            except Exception as exc:  # query() may raise after an error result
                if not saw_result:
                    yield {"type": "error", "message": str(exc)}

        agen = _run()
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(agen.__anext__(), timeout=cfg.timeout_s)
                except StopAsyncIteration:
                    break
                yield ev
        except asyncio.TimeoutError:
            yield {"type": "error", "message": "run timed out"}
        finally:
            await agen.aclose()


async def run_and_collect(
    runtime: AgentRuntime,
    cfg: RunConfig,
    on_event: Callable[[dict], Awaitable[None]] | None,
) -> dict:
    """Drive `runtime.stream(cfg)` to completion, forwarding each event to
    `on_event` (if given), and return the final `result` event.

    If no `result` event was ever emitted (e.g. the run errored out early),
    the last `error` event is returned instead. Reused by the message route
    and the OpenAI-compatible shim, which both need the terminal event but
    differ in how they surface intermediate events.
    """
    final: dict | None = None
    async for ev in runtime.stream(cfg):
        if on_event is not None:
            await on_event(ev)
        if ev["type"] == "result":
            final = ev
        elif ev["type"] == "error" and final is None:
            final = ev
    return final
