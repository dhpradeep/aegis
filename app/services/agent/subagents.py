"""Maps an Agent's roster (a list of rostered Agent ids) into the SDK's
`AgentDefinition` shape, keyed by agent name, for use as
`ClaudeAgentOptions.agents` / `RunConfig.agents`.
"""

from __future__ import annotations

import json

from claude_agent_sdk import AgentDefinition
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.agents import get_agent

__all__ = ["roster_to_agent_defs"]


async def roster_to_agent_defs(db: AsyncSession, roster: list[str]) -> dict[str, AgentDefinition]:
    """Load each rostered Agent id and build an `AgentDefinition` for it,
    keyed by the Agent's `name`. Returns `{}` for an empty roster.

    Rostered ids that no longer resolve to an Agent row are silently
    skipped (the roster is validated on write in `app.services.agents`, but
    this stays defensive against stale references).
    """
    defs: dict[str, AgentDefinition] = {}
    for agent_id in roster:
        agent = await get_agent(db, agent_id)
        if agent is None:
            continue
        defs[agent.name] = AgentDefinition(
            description=agent.description or agent.name,
            prompt=agent.system_prompt or "",
            tools=json.loads(agent.allowed_tools_json),
            model=agent.model,
        )
    return defs
