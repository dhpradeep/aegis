"""Session creation shared by the tenant API (`POST /v1/sessions`) and the
admin dashboard (create-session form)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from secrets import token_hex

from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ApiError
from app.db.models import Event, Job, Objective, Session, Usage
from app.services.agents import get_agent
from app.services.workspaces import create_workspace

__all__ = ["create_session_record", "delete_session"]


async def create_session_record(
    db: AsyncSession,
    *,
    tenant_id: str,
    agent_id: str,
    title: str | None = None,
    allow_admin_only: bool = False,
) -> Session:
    """Provision the workspace and persist a new `active` agent-backed session
    for `tenant_id`. Returns the created row.

    `allow_admin_only` gates admin-only agents (True for admin callers / the
    dashboard, False for a normal tenant key). The session is configured
    entirely by the referenced `Agent` via `build_run_config`; the
    `Session.profile` column carries the agent's name as a display label.
    """
    agent = await get_agent(db, agent_id)
    if agent is None:
        raise ApiError.not_found(f"Unknown agent: {agent_id}")
    if agent.is_admin_only and not allow_admin_only:
        raise ApiError.forbidden("Agent requires an admin key")

    session_id = "sess_" + token_hex(8)
    workspace = create_workspace(tenant_id, session_id)

    session = Session(
        id=session_id,
        tenant_id=tenant_id,
        profile=agent.name,
        agent_id=agent.id,
        overrides_json=json.dumps({}),
        mcp_names_json=json.dumps([]),
        workspace_path=str(workspace),
        status="active",
        title=title,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def delete_session(db: AsyncSession, session_id: str) -> None:
    """Permanently delete a session: its events/jobs and its isolated
    workspace directory are removed, while `Usage` and `Objective` rows are
    detached (session_id set NULL) so billing and objective history survive.
    """
    session = await db.get(Session, session_id)
    if session is None:
        raise ApiError.not_found("session not found")

    # Preserve billing + objective records by detaching them from the session.
    await db.execute(
        sa_update(Usage).where(Usage.session_id == session_id).values(session_id=None)
    )
    await db.execute(
        sa_update(Objective).where(Objective.session_id == session_id).values(session_id=None)
    )
    # Remove rows that require the session (non-null FK).
    await db.execute(sa_delete(Job).where(Job.session_id == session_id))
    await db.execute(sa_delete(Event).where(Event.session_id == session_id))

    # Remove the workspace directory (only if it lives under the configured
    # workspace root — never rmtree an arbitrary path).
    ws = Path(session.workspace_path)
    root = Path(get_settings().workspace_root)
    try:
        if root in ws.parents:
            shutil.rmtree(ws, ignore_errors=True)
    except OSError:
        pass

    await db.delete(session)
    await db.commit()
