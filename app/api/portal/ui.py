"""Tenant chat portal (`/chat`): log in with an API key, chat with the
portal-visible agents in real Sessions. Reuses the admin templates/static and
the same session services as the API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.ui import build_turns, templates
from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.security import hash_key
from app.db.base import get_db
from app.db.models import ApiKey, Event, Session as SessionModel, Tenant
from app.services import login_throttle, ratelimit
from app.services.agent.session_runner import build_run_config, run_session_message
from app.services.agents import get_agent, list_portal_agents
from app.services.billing import usage_summary
from app.services.models import EFFORT_LEVELS, get_models
from app.services.ratelimit import check_daily_cost, check_rpm
from app.services.sessions import create_session_record, delete_session

router = APIRouter(prefix="/chat", tags=["portal"])
COOKIE_NAME = "portal_session"

__all__ = ["router", "install_portal_handlers", "require_portal_key"]


class PortalAuthRequired(Exception):
    pass


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().session_secret, salt="portal")


async def require_portal_key(request: Request, db: AsyncSession = Depends(get_db)) -> ApiKey:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise PortalAuthRequired()
    try:
        data = _serializer().loads(token)
    except BadSignature:
        raise PortalAuthRequired()
    key = await db.get(ApiKey, data.get("key")) if isinstance(data, dict) else None
    if key is None or key.revoked_at is not None:
        raise PortalAuthRequired()
    return key


def install_portal_handlers(app: FastAPI) -> None:
    @app.exception_handler(PortalAuthRequired)
    async def _redirect(request: Request, exc: PortalAuthRequired):
        return RedirectResponse(url="/chat/login", status_code=302)


async def _owned(db: AsyncSession, key: ApiKey, session_id: str) -> SessionModel:
    session = await db.get(SessionModel, session_id)
    if session is None or session.tenant_id != key.tenant_id or session.origin != "portal":
        raise ApiError.not_found("session not found")
    return session


async def _threads(db: AsyncSession, key: ApiKey) -> list[SessionModel]:
    return (
        (
            await db.execute(
                select(SessionModel)
                .where(SessionModel.tenant_id == key.tenant_id, SessionModel.origin == "portal")
                .order_by(SessionModel.updated_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )


def _overrides(session: SessionModel) -> dict:
    try:
        return json.loads(session.overrides_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


async def _choices(db: AsyncSession, key: ApiKey) -> tuple[list[dict], list[str], str]:
    models = await get_models()
    tenant = await db.get(Tenant, key.tenant_id)
    default_model = (tenant.default_model if tenant else None) or get_settings().default_model
    return models, list(EFFORT_LEVELS), default_model


def _clean_settings(model: str, effort: str, models: list[dict]) -> dict:
    ids = {m["id"] for m in models}
    out: dict = {}
    if model and model in ids:
        out["model"] = model
    if effort and effort in EFFORT_LEVELS:
        out["effort"] = effort
    return out


def _render(request: Request, name: str, key: ApiKey | None, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"portal_key": key, **ctx})


# --- auth ---------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return _render(request, "portal_login.html", None, error=None)


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, api_key: str = Form(...), db: AsyncSession = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    wait = login_throttle.retry_after(ip)
    if wait > 0:
        resp = _render(request, "portal_login.html", None, error=f"Too many attempts. Try again in {wait}s.")
        resp.status_code = 429
        resp.headers["Retry-After"] = str(wait)
        return resp
    row = (
        await db.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_key(api_key.strip()), ApiKey.revoked_at.is_(None))
        )
    ).scalar_one_or_none()
    if row is None:
        lockout = login_throttle.record_failure(ip)
        msg = f"Too many attempts. Try again in {lockout}s." if lockout else "Unknown or revoked API key"
        resp = _render(request, "portal_login.html", None, error=msg)
        if lockout:
            resp.status_code = 429
        return resp
    login_throttle.record_success(ip)
    response = RedirectResponse(url="/chat", status_code=302)
    response.set_cookie(COOKIE_NAME, _serializer().dumps({"key": row.id}), httponly=True, samesite="lax")
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/chat/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response


# --- sessions -------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def sessions_page(
    request: Request, key: ApiKey = Depends(require_portal_key), db: AsyncSession = Depends(get_db)
):
    agents = await list_portal_agents(db)
    models, efforts, default_model = await _choices(db, key)
    return _render(
        request,
        "portal_sessions.html",
        key,
        threads=await _threads(db, key),
        current=None,
        agents=agents,
        models=models,
        efforts=efforts,
        default_model=default_model,
    )


@router.post("/sessions", response_class=HTMLResponse)
async def sessions_create(
    agent_id: str = Form(...),
    title: str = Form(""),
    model: str = Form(""),
    effort: str = Form(""),
    key: ApiKey = Depends(require_portal_key),
    db: AsyncSession = Depends(get_db),
):
    agent = await get_agent(db, agent_id)
    if agent is None or not agent.portal_visible or agent.is_admin_only:
        raise ApiError.forbidden("Agent is not available in the portal")
    session = await create_session_record(
        db, tenant_id=key.tenant_id, agent_id=agent_id, title=title.strip() or None, origin="portal"
    )
    models, _, _ = await _choices(db, key)
    session.overrides_json = json.dumps(_clean_settings(model, effort, models))
    await db.commit()
    return RedirectResponse(url=f"/chat/s/{session.id}", status_code=302)


@router.post("/s/{id}/settings")
async def session_settings(
    id: str,
    model: str = Form(""),
    effort: str = Form(""),
    key: ApiKey = Depends(require_portal_key),
    db: AsyncSession = Depends(get_db),
):
    session = await _owned(db, key, id)
    models, _, _ = await _choices(db, key)
    session.overrides_json = json.dumps(_clean_settings(model, effort, models))
    await db.commit()
    return RedirectResponse(url=f"/chat/s/{id}", status_code=302)


@router.get("/s/{id}", response_class=HTMLResponse)
async def chat_page(
    id: str, request: Request, key: ApiKey = Depends(require_portal_key), db: AsyncSession = Depends(get_db)
):
    session = await _owned(db, key, id)
    events = (
        (await db.execute(select(Event).where(Event.session_id == id).order_by(Event.seq.asc())))
        .scalars()
        .all()
    )
    models, efforts, default_model = await _choices(db, key)
    return _render(
        request,
        "portal_chat.html",
        key,
        threads=await _threads(db, key),
        current=session.id,
        session=session,
        turns=build_turns(events),
        events=events,
        settings=_overrides(session),
        models=models,
        efforts=efforts,
        default_model=default_model,
    )


async def _sse(events: AsyncIterator[dict]) -> AsyncIterator[str]:
    async for ev in events:
        yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"


@router.post("/s/{id}/message")
async def send_message(
    id: str, request: Request, key: ApiKey = Depends(require_portal_key), db: AsyncSession = Depends(get_db)
):
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise ApiError.invalid("prompt required")
    session = await _owned(db, key, id)
    if session.status == "running":
        raise ApiError.session_busy()
    await check_rpm(db, key)
    await check_daily_cost(db, key)

    session.status = "running"
    if not session.title:
        session.title = prompt[:60]
    await db.commit()
    try:
        cfg = await build_run_config(db, session.tenant_id, session, prompt)
        await ratelimit.run_gate.acquire()
    except Exception:
        session.status = "active"
        await db.commit()
        raise

    events = run_session_message(
        request.app.state.runtime, cfg, tenant_id=session.tenant_id, api_key_id=key.id, session_id=id
    )
    return StreamingResponse(_sse(events), media_type="text/event-stream")


@router.get("/s/{id}/state")
async def session_state(
    id: str, key: ApiKey = Depends(require_portal_key), db: AsyncSession = Depends(get_db)
) -> dict:
    session = await _owned(db, key, id)
    count = (await db.execute(select(func.count(Event.seq)).where(Event.session_id == id))).scalar() or 0
    return {"events": count, "status": session.status}


@router.post("/s/{id}/delete")
async def session_delete(
    id: str, key: ApiKey = Depends(require_portal_key), db: AsyncSession = Depends(get_db)
):
    await _owned(db, key, id)
    await delete_session(db, id)
    return RedirectResponse(url="/chat", status_code=302)


# --- usage ----------------------------------------------------------------


@router.get("/usage", response_class=HTMLResponse)
async def usage_page(
    request: Request, key: ApiKey = Depends(require_portal_key), db: AsyncSession = Depends(get_db)
):
    summary = await usage_summary(db, key.tenant_id)
    return _render(
        request, "portal_usage.html", key, summary=summary, threads=await _threads(db, key), current=None
    )
