"""Route agentic CLI conversations (requests carrying client tools) into real
Sessions. The client resends its whole transcript every turn; we recognize a
continuation by hashing the prefix we already saw and then only feed the new
turns to the resumed SDK session."""

from __future__ import annotations

import hashlib
import json
import re
from secrets import token_hex

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Session
from app.services.workspaces import create_workspace

Turn = tuple[str, str]

_CANDIDATES = 50
_TITLE_MAX = 60
_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.S)


def turns_hash(turns: list[Turn]) -> str:
    return hashlib.sha256(json.dumps(turns).encode()).hexdigest()


def prompt_from_turns(turns: list[Turn]) -> str:
    return "\n\n".join(text for _, text in turns)


def user_text(turn_text: str) -> str:
    text = turn_text.split(": ", 1)[1] if turn_text.startswith("User: ") else turn_text
    return _REMINDER_RE.sub("", text).strip()


def display_text(turns: list[Turn]) -> str:
    parts = [user_text(text) for role, text in turns if role != "system"]
    return "\n\n".join(p for p in parts if p)


def first_user_text(turns: list[Turn]) -> str:
    for role, text in turns:
        if role == "user":
            return user_text(text)
    return ""


async def find_continuation(
    db: AsyncSession, tenant_id: str, turns: list[Turn]
) -> tuple[Session, list[Turn]] | None:
    rows = (
        await db.execute(
            select(Session)
            .where(
                Session.tenant_id == tenant_id,
                Session.conv_hash.is_not(None),
                Session.conv_turns < len(turns),
                Session.status != "running",
            )
            .order_by(Session.updated_at.desc())
            .limit(_CANDIDATES)
        )
    ).scalars().all()
    for s in rows:
        if turns_hash(turns[: s.conv_turns]) == s.conv_hash:
            delta = [t for t in turns[s.conv_turns :] if t[0] != "assistant"]
            return s, delta
    return None


async def create_cli_session(db: AsyncSession, tenant_id: str, profile: str, title: str) -> Session:
    session_id = "sess_" + token_hex(8)
    workspace = create_workspace(tenant_id, session_id)
    session = Session(
        id=session_id,
        tenant_id=tenant_id,
        profile=profile,
        agent_id=None,
        overrides_json=json.dumps({}),
        mcp_names_json=json.dumps([]),
        workspace_path=str(workspace),
        status="active",
        title=(title[:_TITLE_MAX] or None),
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def remember(db: AsyncSession, session_id: str, turns: list[Turn]) -> None:
    session = await db.get(Session, session_id)
    if session is None:
        return
    session.conv_hash = turns_hash(turns)
    session.conv_turns = len(turns)
    await db.commit()
