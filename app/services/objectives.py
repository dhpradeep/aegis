"""Objective submission: creates the `Objective` row (defaulting its budgets
from the driving `Agent` when not given) and schedules `run_objective` as a
background `asyncio.Task`, mirroring `app.services.jobs.submit_job`."""

from __future__ import annotations

import asyncio
from secrets import token_hex
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models import Objective
from app.services.agent.objective_runner import run_objective
from app.services.agents import get_agent

__all__ = ["submit_objective"]

# Keep strong references to scheduled background tasks so they aren't
# garbage-collected mid-run (mirrors app.services.jobs._background_tasks).
_background_tasks: set[asyncio.Task] = set()


async def submit_objective(
    app: Any,
    db: AsyncSession,
    *,
    tenant_id: str,
    api_key_id: str,
    agent_id: str,
    goal: str,
    rubric: str,
    max_cost_usd: float | None,
    max_iterations: int | None,
    is_admin: bool = False,
) -> Objective:
    """Create a queued `Objective` and schedule its background run.

    `max_cost_usd`/`max_iterations` fall back to the driving `Agent`'s own
    budgets when omitted (`None`). Returns the `Objective` row immediately;
    the loop itself runs later in `run_objective`.

    `is_admin` mirrors the session path's `allow_admin_only` gate: a
    non-admin key may not drive an `is_admin_only` agent through the
    objective loop, even though `run_objective` itself passes
    `allow_admin_only=True` when creating the working session (the gate
    belongs at submission, not at session creation).
    """
    agent = await get_agent(db, agent_id)
    if agent is None:
        raise ApiError.not_found(f"Unknown agent: {agent_id}")
    if agent.is_admin_only and not is_admin:
        raise ApiError.forbidden("Agent requires an admin key")

    if max_cost_usd is None:
        max_cost_usd = agent.max_cost_usd
    if max_iterations is None:
        max_iterations = agent.max_iterations

    obj = Objective(
        id="obj_" + token_hex(8),
        tenant_id=tenant_id,
        api_key_id=api_key_id,
        agent_id=agent_id,
        goal=goal,
        rubric=rubric,
        max_cost_usd=max_cost_usd,
        max_iterations=max_iterations,
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)

    task = asyncio.create_task(run_objective(app, obj.id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return obj
