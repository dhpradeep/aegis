"""Run-orchestration for the message-send endpoint.

`build_run_config` merges a session's profile + stored overrides + resolved
MCP servers + workspace into a `RunConfig`. `run_session_message` drives the
runtime and persists every event (plus init/result side effects) against a
dedicated DB session, re-yielding the same event dicts to the caller.

`run_session_message` deliberately opens its own `SessionLocal` rather than
reusing the caller's request-scoped session: for the SSE path the generator
is consumed by Starlette while streaming the response body, well after the
route function itself has returned, so the request-scoped session's
lifecycle is not a safe fit. It also owns releasing the run gate and
resetting the session back to "active" in a `finally`, so both happen even
if the client disconnects mid-stream.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ApiError
from app.db.base import SessionLocal
from app.db.models import Event, Session, Usage
from app.services import ratelimit
from app.services.agent.runtime import AgentRuntime, RunConfig
from app.services.agent.subagents import roster_to_agent_defs
from app.services.agents import get_agent
from app.services.mcp import resolve_mcp

__all__ = ["build_run_config", "run_session_message"]

# Fixed max_turns for agent-backed sessions (agent-defined runs don't expose
# a per-run override the way profile overrides do).
_AGENT_MAX_TURNS = 30


def _run_config(
    *,
    prompt: str,
    session: Session,
    system_prompt: str | None,
    allowed_tools: list[str],
    permission_mode: str,
    mcp_servers: dict,
    model: str | None,
    max_turns: int,
    agents: dict | None = None,
    effort: str | None = None,
) -> RunConfig:
    """Shared RunConfig construction tail for both the profile and
    agent-backed paths below."""
    return RunConfig(
        prompt=prompt,
        cwd=session.workspace_path,
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        mcp_servers=mcp_servers,
        model=model,
        max_turns=max_turns,
        resume=session.sdk_session_id,
        timeout_s=get_settings().run_timeout_s,
        agents=agents,
        effort=effort,
    )


def _combine_system(base: str | None, suffix: str | None) -> str | None:
    """Append `suffix` to a base system prompt (either may be None)."""
    if not suffix:
        return base
    return f"{base}{suffix}" if base else suffix


async def build_run_config(
    db: AsyncSession,
    tenant_id: str,
    session: Session,
    prompt: str,
    *,
    system_suffix: str | None = None,
) -> RunConfig:
    """Build a `RunConfig` for this run from the session's admin-defined
    `Agent` (allowed tools, permission mode, model, effort, system prompt,
    resolved MCP servers, and roster mapped to SDK subagents).

    `system_suffix` is appended to the resolved system prompt (used by the
    objective loop to inject its autonomy directive). When an agent has
    `bypass_permissions` set, the run's permission mode becomes
    `"bypassPermissions"` regardless of the agent's stored `permission_mode`.
    """
    if session.agent_id is None:
        raise ApiError.invalid("session has no agent")

    agent = await get_agent(db, session.agent_id)
    if agent is None:
        raise ApiError.not_found(f"Unknown agent: {session.agent_id}")

    mcp_names = json.loads(agent.mcp_names_json)
    mcp_servers = await resolve_mcp(db, tenant_id, mcp_names, [])
    agents = await roster_to_agent_defs(db, json.loads(agent.roster_json))
    permission_mode = (
        "bypassPermissions" if agent.bypass_permissions else agent.permission_mode
    )
    try:
        overrides = json.loads(session.overrides_json or "{}")
    except (json.JSONDecodeError, TypeError):
        overrides = {}

    return _run_config(
        prompt=prompt,
        session=session,
        system_prompt=_combine_system(agent.system_prompt, system_suffix),
        allowed_tools=json.loads(agent.allowed_tools_json),
        permission_mode=permission_mode,
        mcp_servers=mcp_servers,
        model=overrides.get("model") or agent.model,
        max_turns=_AGENT_MAX_TURNS,
        agents=agents,
        effort=overrides.get("effort") or agent.effort,
    )


async def _next_seq(db: AsyncSession, session_id: str) -> int:
    result = await db.execute(select(func.max(Event.seq)).where(Event.session_id == session_id))
    return result.scalar() or 0


async def run_session_message(
    runtime: AgentRuntime,
    cfg: RunConfig,
    *,
    tenant_id: str,
    api_key_id: str,
    session_id: str,
    display_prompt: str | None = None,
    system_prompt: str | None = None,
) -> AsyncIterator[dict]:
    """Drive `runtime.stream(cfg)`, persisting each event (and side effects)
    against a dedicated DB session, and re-yield the same event dicts.

    - Every event is written as an `Event` row with an incrementing `seq`
      continuing from the session's existing max seq.
    - The `init` event's `session_id` is captured onto `session.sdk_session_id`.
    - The `result` event writes a `Usage` row.
    - Regardless of how the generator is exhausted (fully consumed, or the
      caller stops iterating early, e.g. a client disconnect during SSE),
      the run gate is released and the session status is reset to "active".
    """
    async with SessionLocal() as db:
        seq = await _next_seq(db, session_id)
        # Persist the user's prompt as the first event of this turn so the
        # conversation record shows both sides (it is stored, not streamed
        # back — the client already has it).
        seq += 1
        db.add(
            Event(
                session_id=session_id,
                seq=seq,
                type="user_message",
                payload_json=json.dumps(
                    {"type": "user_message", "text": display_prompt or cfg.prompt}
                ),
            )
        )
        if system_prompt:
            seq += 1
            db.add(
                Event(
                    session_id=session_id,
                    seq=seq,
                    type="system_prompt",
                    payload_json=json.dumps({"type": "system_prompt", "text": system_prompt}),
                )
            )
        await db.commit()
        try:
            async for ev in runtime.stream(cfg):
                seq += 1
                db.add(
                    Event(
                        session_id=session_id,
                        seq=seq,
                        type=ev["type"],
                        payload_json=json.dumps(ev),
                    )
                )
                if ev["type"] == "init":
                    session = await db.get(Session, session_id)
                    if session is not None:
                        session.sdk_session_id = ev.get("session_id")
                elif ev["type"] == "result":
                    usage = ev.get("usage") or {}
                    db.add(
                        Usage(
                            tenant_id=tenant_id,
                            api_key_id=api_key_id,
                            session_id=session_id,
                            input_tokens=usage.get("input_tokens", 0),
                            output_tokens=usage.get("output_tokens", 0),
                            cache_read_tokens=usage.get("cache_read_tokens", 0),
                            cost_usd=ev.get("cost_usd") or 0.0,
                            duration_ms=ev.get("duration_ms") or 0,
                            num_turns=ev.get("num_turns") or 0,
                        )
                    )
                await db.commit()
                yield ev
        finally:
            session = await db.get(Session, session_id)
            if session is not None:
                session.status = "active"
                await db.commit()
            if ratelimit.run_gate is not None:
                ratelimit.run_gate.release()
