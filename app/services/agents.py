"""Agent service: CRUD over the reusable `Agent` roster, roster validation,
and seeding built-in agents from loaded `Profile`s."""

from __future__ import annotations

import json
from secrets import token_hex

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ApiError
from app.db.models import Agent
from app.db.models._base import _utcnow

__all__ = [
    "create_agent",
    "update_agent",
    "delete_agent",
    "get_agent",
    "list_agents",
    "seed_default_agent",
]

# The single built-in agent, created on first boot if absent. A safe, general
# read/write config (no Bash) — operators clone/extend it in the dashboard and
# create their own Bash/bypass "builder" agents as needed.
DEFAULT_AGENT_NAME = "default"
_DEFAULT_AGENT_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch"]

# Fields on `update_agent(**fields)` that map to a plain column of the same
# name (no JSON encoding needed).
_PLAIN_FIELDS = {
    "name",
    "description",
    "model",
    "effort",
    "system_prompt",
    "permission_mode",
    "max_cost_usd",
    "max_iterations",
    "is_admin_only",
    "bypass_permissions",
    "portal_visible",
}

# Fields that map to a `<field>_json` column and must be json.dumps'd.
_JSON_FIELDS = {"allowed_tools": "allowed_tools_json", "mcp_names": "mcp_names_json", "roster": "roster_json"}

ROSTER_MAX = 5


async def _validate_roster(db: AsyncSession, roster: list[str], self_id: str | None) -> None:
    """Validate a proposed roster list.

    - at most `ROSTER_MAX` entries
    - no self-reference (only relevant when `self_id` is not None, i.e. on update)
    - every id must reference an existing Agent
    - one level only: no rostered agent may itself have a non-empty roster
    """
    if len(roster) > ROSTER_MAX:
        raise ApiError.invalid(f"roster max {ROSTER_MAX}")

    if self_id is not None and self_id in roster:
        raise ApiError.invalid("agent cannot include itself in its own roster")

    if not roster:
        return

    result = await db.execute(select(Agent).where(Agent.id.in_(roster)))
    rows = {row.id: row for row in result.scalars().all()}

    missing = [rid for rid in roster if rid not in rows]
    if missing:
        raise ApiError.invalid(f"roster references unknown agent id(s): {missing}")

    for rid in roster:
        member = rows[rid]
        if member.roster_json and json.loads(member.roster_json):
            raise ApiError.invalid("roster members cannot themselves have a roster")


async def create_agent(
    db: AsyncSession,
    *,
    name: str,
    model: str,
    description: str | None = None,
    effort: str | None = None,
    system_prompt: str | None = None,
    allowed_tools: list[str],
    permission_mode: str = "default",
    mcp_names: list[str] | None = None,
    roster: list[str] | None = None,
    max_cost_usd: float | None = None,
    max_iterations: int = 6,
    is_admin_only: bool = False,
    bypass_permissions: bool = False,
    portal_visible: bool = False,
) -> Agent:
    """Create a new Agent row. `name` must be unique. Validates `roster` via
    `_validate_roster` before persisting."""
    mcp_names = mcp_names or []
    roster = roster or []

    existing = (
        await db.execute(select(Agent).where(Agent.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        raise ApiError.invalid("agent name exists")

    await _validate_roster(db, roster, self_id=None)

    agent = Agent(
        id="agt_" + token_hex(8),
        name=name,
        description=description,
        model=model,
        effort=effort,
        system_prompt=system_prompt,
        allowed_tools_json=json.dumps(allowed_tools),
        permission_mode=permission_mode,
        mcp_names_json=json.dumps(mcp_names),
        roster_json=json.dumps(roster),
        max_cost_usd=max_cost_usd,
        max_iterations=max_iterations,
        is_admin_only=is_admin_only,
        bypass_permissions=bypass_permissions,
        portal_visible=portal_visible,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


async def update_agent(db: AsyncSession, agent_id: str, **fields) -> Agent:
    """Patch the given fields on an existing Agent. Only columns listed in
    `_PLAIN_FIELDS` / `_JSON_FIELDS` are settable; anything else is ignored.
    Re-validates the roster if `roster` is among the patched fields.
    """
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise ApiError.not_found("agent not found")

    if "name" in fields and fields["name"] != agent.name:
        dup = (
            await db.execute(
                select(Agent).where(Agent.name == fields["name"], Agent.id != agent_id)
            )
        ).scalar_one_or_none()
        if dup is not None:
            raise ApiError.invalid("agent name exists")

    if "roster" in fields:
        await _validate_roster(db, fields["roster"], self_id=agent_id)
        if fields["roster"]:
            # Roster nesting is one level deep only: this agent can't be
            # given its own roster if it's already used as a subagent
            # somewhere else (that would make it a two-level roster).
            all_agents = await list_agents(db)
            for other in all_agents:
                if other.id == agent_id:
                    continue
                if other.roster_json and agent_id in json.loads(other.roster_json):
                    raise ApiError.invalid(
                        "an agent that is used as a subagent cannot have its own roster"
                    )

    for key, value in fields.items():
        if key in _JSON_FIELDS:
            setattr(agent, _JSON_FIELDS[key], json.dumps(value))
        elif key in _PLAIN_FIELDS:
            setattr(agent, key, value)

    agent.updated_at = _utcnow()

    await db.commit()
    await db.refresh(agent)
    return agent


async def delete_agent(db: AsyncSession, agent_id: str) -> None:
    """Delete an Agent by id. Raises `ApiError.not_found` if it doesn't exist."""
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise ApiError.not_found("agent not found")
    await db.delete(agent)
    await db.commit()


async def get_agent(db: AsyncSession, agent_id: str) -> Agent | None:
    return await db.get(Agent, agent_id)


async def list_agents(db: AsyncSession) -> list[Agent]:
    result = await db.execute(select(Agent))
    return list(result.scalars().all())


async def seed_default_agent(db: AsyncSession) -> None:
    """Create the single built-in `default` agent if it doesn't exist yet.
    Idempotent — a no-op once the agent is present."""
    existing = (
        await db.execute(select(Agent).where(Agent.name == DEFAULT_AGENT_NAME))
    ).scalar_one_or_none()
    if existing is not None:
        return

    db.add(
        Agent(
            id="agt_" + token_hex(8),
            name=DEFAULT_AGENT_NAME,
            description="Built-in general-purpose read/write agent.",
            model=get_settings().default_model,
            effort=None,
            system_prompt=None,
            allowed_tools_json=json.dumps(_DEFAULT_AGENT_TOOLS),
            permission_mode="acceptEdits",
            mcp_names_json=json.dumps([]),
            roster_json=json.dumps([]),
            max_cost_usd=None,
            max_iterations=6,
            is_admin_only=False,
            bypass_permissions=False,
        )
    )
    await db.commit()


CHAT_AGENT_NAME = "chat"
_CHAT_AGENT_TOOLS = ["WebSearch"]


async def seed_chat_agent(db: AsyncSession) -> None:
    existing = (
        await db.execute(select(Agent).where(Agent.name == CHAT_AGENT_NAME))
    ).scalar_one_or_none()
    if existing is not None:
        return
    db.add(
        Agent(
            id="agt_" + token_hex(8),
            name=CHAT_AGENT_NAME,
            description="Conversational assistant with web search.",
            model=get_settings().default_model,
            effort=None,
            system_prompt=None,
            allowed_tools_json=json.dumps(_CHAT_AGENT_TOOLS),
            permission_mode="default",
            mcp_names_json=json.dumps([]),
            roster_json=json.dumps([]),
            max_cost_usd=None,
            max_iterations=6,
            is_admin_only=False,
            bypass_permissions=False,
            portal_visible=True,
        )
    )
    await db.commit()


async def list_portal_agents(db: AsyncSession) -> list[Agent]:
    return (
        (
            await db.execute(
                select(Agent)
                .where(Agent.portal_visible.is_(True), Agent.is_admin_only.is_(False))
                .order_by(Agent.name)
            )
        )
        .scalars()
        .all()
    )
