"""Server-rendered admin dashboard (Jinja2 + htmx), guarded by a signed cookie.

Route handlers stay thin: all data access goes through the same service
functions the JSON admin API (`app/api/admin/api.py`) uses, so the two never
duplicate query logic.
"""

import hmac
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Body, Depends, FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ApiError
from app.db.base import get_db
from app.db.models import (
    Agent,
    ApiKey,
    AuditLog,
    BillingConfig,
    CompletionLog,
    Event,
    McpServer,
    Objective,
    Session as SessionModel,
    Tenant,
)
from app.services import admin as admin_service
from app.services import agents as agents_service
from app.services import claude_cli
from app.services import claude_usage
from app.services import login_throttle
from app.services import mcp as mcp_service
from app.services import ratelimit
from app.services.agent.session_runner import build_run_config, run_session_message
from app.services.billing import all_key_usage, all_tenant_usage
from app.services.sessions import create_session_record, delete_session
from app.services.workspaces import enforce_quota, list_files, resolve_in_workspace

# Fixed tool set offered as checkboxes on the agent form. Kept in sync with
# the tool names accepted by the claude-agent-sdk `allowed_tools` config.
AGENT_TOOL_CHOICES = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"]
AGENT_PERMISSION_MODES = ["default", "acceptEdits"]

router = APIRouter(prefix="/admin", tags=["admin-ui"])
templates = Jinja2Templates(directory="app/api/admin/templates")
templates.env.filters["from_json"] = json.loads
templates.env.globals["tenant_names"] = {}

# Cache-busting version for static assets: the newest mtime across the CSS and
# JS bundles, so any edit changes the ?v= query and browsers re-fetch instead of
# serving a stale cached copy (StaticFiles sends no Cache-Control).
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
try:
    _mtimes = [
        os.path.getmtime(os.path.join(_STATIC_DIR, f))
        for f in ("dashboard.css", "dropdown.js", "md.js", "chat.js")
    ]
    templates.env.globals["asset_version"] = str(int(max(_mtimes)))
except OSError:
    templates.env.globals["asset_version"] = "1"

COOKIE_NAME = "admin_session"
ACTOR = "admin_ui"

__all__ = ["router", "install_admin_ui_handlers", "require_admin_cookie"]


class AdminAuthRequired(Exception):
    """Raised by require_admin_cookie when the session cookie is missing/invalid.

    Handled by an app-level exception handler (see install_admin_ui_handlers)
    that turns it into a 302 redirect to the login page.
    """


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().session_secret)


def require_admin_cookie(request: Request) -> None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            data = _serializer().loads(token)
        except BadSignature:
            data = None
        if isinstance(data, dict) and data.get("admin") is True:
            return
    raise AdminAuthRequired()


def install_admin_ui_handlers(app: FastAPI) -> None:
    """Register the redirect-to-login handler. Called once from create_app()."""

    @app.exception_handler(AdminAuthRequired)
    async def _redirect_to_login(request: Request, exc: AdminAuthRequired):
        return RedirectResponse(url="/admin/login", status_code=302)


# --- auth ---------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form(...)):
    # Brute-force protection keyed on the real peer IP, never a forwarding header.
    ip = request.client.host if request.client else "unknown"
    wait = login_throttle.retry_after(ip)
    if wait > 0:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": f"Too many attempts. Try again in {wait}s."},
            status_code=429,
            headers={"Retry-After": str(wait)},
        )

    # Constant-time comparison to avoid leaking the admin password via a
    # timing side-channel.
    if not hmac.compare_digest(password, get_settings().admin_password):
        lockout = login_throttle.record_failure(ip)
        msg = (
            f"Too many attempts. Try again in {lockout}s."
            if lockout
            else "Incorrect password"
        )
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": msg},
            status_code=429 if lockout else 200,
        )

    login_throttle.record_success(ip)
    token = _serializer().dumps({"admin": True})
    response = RedirectResponse(url="/admin", status_code=302)
    # `secure` is left off (configurable) since local http dev is expected;
    # httponly + samesite="lax" still guard against JS exfiltration and
    # basic CSRF.
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax")
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response


# --- overview -------------------------------------------------------------


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)])
async def overview(request: Request, db: AsyncSession = Depends(get_db)):
    tenant_count = len((await db.execute(select(Tenant.id))).all())
    session_count = len((await db.execute(select(SessionModel.id))).all())
    keys = await admin_service.list_keys(db)
    active_keys = sum(1 for k in keys if k.revoked_at is None)
    recent_sessions = (
        (
            await db.execute(
                select(SessionModel).order_by(SessionModel.created_at.desc()).limit(6)
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "tenant_count": tenant_count,
            "session_count": session_count,
            "key_count": len(keys),
            "active_keys": active_keys,
            "recent_sessions": recent_sessions,
        },
    )


# --- keys -------------------------------------------------------------


@router.get("/keys", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)])
async def keys_page(request: Request, db: AsyncSession = Depends(get_db)):
    keys = await admin_service.list_keys(db)
    tenants = (await db.execute(select(Tenant))).scalars().all()
    return templates.TemplateResponse(
        request,
        "keys.html",
        {"keys": keys, "tenants": tenants, "tenant_names": _names(tenants), "error": None},
    )


def _names(tenants) -> dict[str, str]:
    return {t.id: t.name for t in tenants}


def _blank_to_none_int(v: str) -> int | None:
    v = (v or "").strip()
    return int(v) if v else None


def _blank_to_none_float(v: str) -> float | None:
    v = (v or "").strip()
    return float(v) if v else None


@router.post("/keys", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)])
async def keys_create(
    request: Request,
    tenant_id: str = Form(...),
    name: str = Form(...),
    rpm: str = Form(""),
    daily_cost_usd: str = Form(""),
    is_admin: bool = Form(False),
    db: AsyncSession = Depends(get_db),
):
    # Blank rpm / daily cost = unlimited (no cap).
    _key, full_key = await admin_service.create_key(
        db,
        actor=ACTOR,
        tenant_id=tenant_id,
        name=name,
        rpm=_blank_to_none_int(rpm),
        daily_cost_usd=_blank_to_none_float(daily_cost_usd),
        is_admin=is_admin,
    )
    # Re-render the keys page (rather than redirecting) so the full key can be
    # shown exactly once with a copy control — it is never retrievable again.
    keys = await admin_service.list_keys(db)
    tenants = (await db.execute(select(Tenant))).scalars().all()
    return templates.TemplateResponse(
        request,
        "keys.html",
        {"keys": keys, "tenants": tenants, "tenant_names": _names(tenants), "error": None, "new_key": full_key},
    )


@router.post(
    "/keys/{id}/revoke",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_cookie)],
)
async def keys_revoke(id: str, db: AsyncSession = Depends(get_db)):
    await admin_service.revoke_key(db, actor=ACTOR, key_id=id)
    return RedirectResponse(url="/admin/keys", status_code=302)


@router.post(
    "/keys/{id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_cookie)],
)
async def keys_delete(id: str, db: AsyncSession = Depends(get_db)):
    await admin_service.delete_key(db, actor=ACTOR, key_id=id)
    return RedirectResponse(url="/admin/keys", status_code=302)


@router.post(
    "/keys/{id}/limits",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_cookie)],
)
async def keys_patch_limits(
    id: str,
    rpm: str = Form(""),
    daily_cost_usd: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    # Blank = clear the limit to unlimited (the form always submits both fields).
    await admin_service.patch_key_limits(
        db,
        actor=ACTOR,
        key_id=id,
        rpm=_blank_to_none_int(rpm),
        daily_cost_usd=_blank_to_none_float(daily_cost_usd),
    )
    return RedirectResponse(url="/admin/keys", status_code=302)


# --- tenants --------------------------------------------------------------


@router.get("/tenants", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)])
async def tenants_page(request: Request, db: AsyncSession = Depends(get_db)):
    tenants = (
        (await db.execute(select(Tenant).order_by(Tenant.created_at.desc()))).scalars().all()
    )
    keys = await admin_service.list_keys(db)
    key_counts: dict[str, int] = {}
    for k in keys:
        key_counts[k.tenant_id] = key_counts.get(k.tenant_id, 0) + 1
    from app.services import models as models_service

    model_choices = [m["id"] for m in await models_service.get_models()]
    return templates.TemplateResponse(
        request,
        "tenants.html",
        {
            "tenants": tenants,
            "key_counts": key_counts,
            "global_default": get_settings().default_model,
            "model_choices": model_choices,
        },
    )


@router.post("/tenants", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)])
async def tenants_create(name: str = Form(...), db: AsyncSession = Depends(get_db)):
    await admin_service.create_tenant(db, name=name)
    return RedirectResponse(url="/admin/tenants", status_code=302)


@router.post(
    "/tenants/{id}/model",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_cookie)],
)
async def tenants_set_model(
    id: str, default_model: str = Form(""), db: AsyncSession = Depends(get_db)
):
    # Blank clears the tenant default (falls back to the global default).
    await admin_service.set_tenant_default_model(
        db, actor=ACTOR, tenant_id=id, default_model=default_model or None
    )
    return RedirectResponse(url="/admin/tenants", status_code=302)


# --- billing ----------------------------------------------------------


@router.get("/billing", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)])
async def billing_page(request: Request, db: AsyncSession = Depends(get_db)):
    tenants = (await db.execute(select(Tenant))).scalars().all()
    configs = (await db.execute(select(BillingConfig))).scalars().all()
    cost_log = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.action.like("billing%"))
                .order_by(AuditLog.created_at.desc())
                .limit(25)
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "billing.html",
        {"tenants": tenants, "configs": configs, "cost_log": cost_log},
    )


@router.post("/billing", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)])
async def billing_submit(
    tenant_id: str = Form(...),
    price_per_mtok_input: float = Form(...),
    price_per_mtok_output: float = Form(...),
    markup: float = Form(...),
    db: AsyncSession = Depends(get_db),
):
    await admin_service.upsert_billing_config(
        db,
        actor=ACTOR,
        tenant_id=tenant_id,
        price_per_mtok_input=price_per_mtok_input,
        price_per_mtok_output=price_per_mtok_output,
        markup=markup,
    )
    return RedirectResponse(url="/admin/billing", status_code=302)


# --- usage --------------------------------------------------------------


@router.get("/usage", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)])
async def usage_page(request: Request, db: AsyncSession = Depends(get_db)):
    rows = await all_tenant_usage(db)
    key_rows = await all_key_usage(db)
    tenants = (await db.execute(select(Tenant))).scalars().all()
    plan = await claude_usage.get_plan_usage()
    return templates.TemplateResponse(request, "usage.html",
        {"rows": rows, "key_rows": key_rows, "tenant_names": _names(tenants), "plan": plan},
    )


# --- sessions -----------------------------------------------------------


@router.get(
    "/sessions", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)]
)
async def sessions_page(
    request: Request, tenant: str = "", origin: str = "", db: AsyncSession = Depends(get_db)
):
    q = select(SessionModel).order_by(SessionModel.created_at.desc()).limit(100)
    if tenant:
        q = q.where(SessionModel.tenant_id == tenant)
    if origin:
        q = q.where(SessionModel.origin == origin)
    sessions = (await db.execute(q)).scalars().all()
    tenants = (await db.execute(select(Tenant).order_by(Tenant.name))).scalars().all()
    agents = await agents_service.list_agents(db)
    return templates.TemplateResponse(
        request,
        "sessions.html",
        {
            "sessions": sessions,
            "tenants": tenants,
            "tenant_names": _names(tenants),
            "tenant": tenant,
            "origin": origin,
            "agents": [{"id": a.id, "name": a.name} for a in agents],
        },
    )


@router.post(
    "/sessions", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)]
)
async def sessions_create(
    tenant_id: str = Form(...),
    agent_id: str = Form(...),
    title: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    session = await create_session_record(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        title=title.strip() or None,
        allow_admin_only=True,  # dashboard admin may use any agent
    )
    return RedirectResponse(url=f"/admin/sessions/{session.id}", status_code=302)


def build_turns(events: list[Event]) -> list[dict]:
    # Group the raw event log into readable turns (each agent run emits one
    # `init`). The `result` event repeats the answer already captured by the
    # `text` event, so we keep only its metrics; empty `thinking` events are
    # dropped. The original events are still shown in a raw log below.
    turns: list[dict] = []
    cur: dict | None = None
    for e in events:
        try:
            payload = json.loads(e.payload_json)
        except (json.JSONDecodeError, TypeError):
            payload = {}
        etype = e.type
        if etype == "user_message":
            cur = {"index": len(turns) + 1, "started_at": e.created_at, "messages": [], "metrics": None}
            turns.append(cur)
            cur["messages"].append({"kind": "user", "text": payload.get("text", "")})
            continue
        if etype == "init":
            # Start a new turn only when one isn't already open for this run
            # (a user_message just opened it, or the previous turn finished).
            if cur is None or cur.get("metrics") is not None:
                cur = {"index": len(turns) + 1, "started_at": e.created_at, "messages": [], "metrics": None}
                turns.append(cur)
            continue
        if cur is None:
            cur = {"index": len(turns) + 1, "started_at": e.created_at, "messages": [], "metrics": None}
            turns.append(cur)
        if etype == "system_prompt":
            cur["system"] = payload.get("text", "")
        elif etype == "thinking":
            if (payload.get("text") or "").strip():
                cur["messages"].append({"kind": "thinking", "text": payload["text"]})
        elif etype == "text":
            cur["messages"].append({"kind": "text", "text": payload.get("text", "")})
        elif etype == "tool_use":
            cur["messages"].append(
                {"kind": "tool_use", "name": payload.get("name"), "input": payload.get("input")}
            )
        elif etype == "tool_result":
            cur["messages"].append(
                {"kind": "tool_result", "content": payload.get("content"), "is_error": payload.get("is_error")}
            )
        elif etype == "result":
            u = payload.get("usage") or {}
            cur["metrics"] = {
                "input_tokens": u.get("input_tokens", 0),
                "output_tokens": u.get("output_tokens", 0),
                "cost_usd": payload.get("cost_usd") or 0.0,
                "duration_ms": payload.get("duration_ms") or 0,
            }
            if not any(m["kind"] == "text" for m in cur["messages"]) and payload.get("result"):
                cur["messages"].append({"kind": "text", "text": payload["result"]})
        elif etype == "error":
            cur["messages"].append({"kind": "error", "text": payload.get("message", "error")})
    return turns


@router.get(
    "/sessions/{id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_cookie)],
)
async def session_detail_page(id: str, request: Request, db: AsyncSession = Depends(get_db)):
    session = await db.get(SessionModel, id)
    events = (
        (
            await db.execute(
                select(Event).where(Event.session_id == id).order_by(Event.seq.asc())
            )
        )
        .scalars()
        .all()
    )

    files: list[dict] = []
    if session is not None:
        try:
            files = list_files(Path(session.workspace_path))
        except OSError:
            files = []
    turns = build_turns(events)
    return templates.TemplateResponse(
        request,
        "session_detail.html",
        {"session": session, "turns": turns, "events": events, "files": files},
    )


# --- session actions: send message (SSE) + file upload/download ----------


def _sse(events: AsyncIterator[dict]) -> AsyncIterator[str]:
    async def gen():
        async for ev in events:
            yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
    return gen()


async def _tenant_key_id(db: AsyncSession, tenant_id: str) -> str:
    """An active API key for the tenant, used to attribute a run's usage.
    Admin-initiated runs have no key of their own, so we bill the tenant's."""
    row = (
        await db.execute(
            select(ApiKey)
            .where(ApiKey.tenant_id == tenant_id, ApiKey.revoked_at.is_(None))
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError.invalid(
            "This tenant has no active API key to attribute usage to — create one under API Keys."
        )
    return row.id


@router.post(
    "/sessions/{id}/message", dependencies=[Depends(require_admin_cookie)]
)
async def session_send_message(id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Run one message against the session and stream events back as SSE.
    Persists the turn (events + usage) via the same runner the API uses."""
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise ApiError.invalid("prompt required")

    session = await db.get(SessionModel, id)
    if session is None:
        raise ApiError.not_found("session not found")
    if session.status == "running":
        raise ApiError.session_busy()

    key_id = await _tenant_key_id(db, session.tenant_id)

    # Make the agent aware of files sitting in its workspace so it reads them
    # when the user refers to "this file" / an attachment (otherwise it has no
    # signal that uploads exist).
    suffix = None
    try:
        wfiles = list_files(Path(session.workspace_path))
    except OSError:
        wfiles = []
    if wfiles:
        names = ", ".join(f["path"] for f in wfiles[:50])
        suffix = (
            "\n\nFiles are present in your current working directory. Use the Read "
            "tool to read them when the user refers to a file, an attachment, or "
            f'"this file". Available files: {names}.'
        )

    session.status = "running"
    await db.commit()
    try:
        cfg = await build_run_config(
            db, session.tenant_id, session, prompt, system_suffix=suffix
        )
        await ratelimit.run_gate.acquire()
    except Exception:
        session.status = "active"
        await db.commit()
        raise

    events = run_session_message(
        request.app.state.runtime,
        cfg,
        tenant_id=session.tenant_id,
        api_key_id=key_id,
        session_id=id,
    )
    return StreamingResponse(_sse(events), media_type="text/event-stream")


@router.post(
    "/sessions/{id}/delete", dependencies=[Depends(require_admin_cookie)]
)
async def sessions_delete(id: str, db: AsyncSession = Depends(get_db)):
    await delete_session(db, id)
    return RedirectResponse(url="/admin/sessions", status_code=302)


@router.get(
    "/sessions/{id}/state", dependencies=[Depends(require_admin_cookie)]
)
async def session_state(id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Lightweight poll target for the detail page's auto-refresh: the current
    event count + status, so the page can reload when a run (from anywhere)
    adds events."""
    session = await db.get(SessionModel, id)
    if session is None:
        raise ApiError.not_found("session not found")
    count = (
        await db.execute(select(func.count(Event.seq)).where(Event.session_id == id))
    ).scalar() or 0
    return {"events": count, "status": session.status}


@router.post(
    "/sessions/{id}/files", dependencies=[Depends(require_admin_cookie)]
)
async def session_upload_file(
    id: str, file: UploadFile, db: AsyncSession = Depends(get_db)
):
    session = await db.get(SessionModel, id)
    if session is None:
        raise ApiError.not_found("session not found")
    if not file.filename:
        raise ApiError.invalid("filename required")

    ws = Path(session.workspace_path)
    dest = resolve_in_workspace(ws, file.filename)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(await file.read())
    enforce_quota(ws, get_settings().workspace_quota_mb)
    return RedirectResponse(url=f"/admin/sessions/{id}", status_code=302)


@router.get(
    "/sessions/{id}/files/{path:path}", dependencies=[Depends(require_admin_cookie)]
)
async def session_download_file(id: str, path: str, db: AsyncSession = Depends(get_db)):
    session = await db.get(SessionModel, id)
    if session is None:
        raise ApiError.not_found("session not found")
    target = resolve_in_workspace(Path(session.workspace_path), path)
    if not target.is_file():
        raise ApiError.not_found("file not found")
    return FileResponse(target)


@router.delete(
    "/sessions/{id}/files/{path:path}", dependencies=[Depends(require_admin_cookie)]
)
async def session_delete_file(id: str, path: str, db: AsyncSession = Depends(get_db)) -> dict:
    session = await db.get(SessionModel, id)
    if session is None:
        raise ApiError.not_found("session not found")
    target = resolve_in_workspace(Path(session.workspace_path), path)
    if not target.is_file():
        raise ApiError.not_found("file not found")
    target.unlink()
    return {"deleted": True}


# --- completions (OpenAI chat shim log) ---------------------------------


@router.get(
    "/completions", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)]
)
async def completions_page(
    request: Request, tenant: str = "", db: AsyncSession = Depends(get_db)
):
    q = select(CompletionLog).order_by(CompletionLog.created_at.desc()).limit(100)
    if tenant:
        q = q.where(CompletionLog.tenant_id == tenant)
    rows = (await db.execute(q)).scalars().all()
    tenants = (await db.execute(select(Tenant).order_by(Tenant.name))).scalars().all()
    return templates.TemplateResponse(
        request,
        "completions.html",
        {"rows": rows, "tenants": tenants, "tenant_names": _names(tenants), "tenant": tenant},
    )


def _completion_msg_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    return str(content)


def _completion_view_messages(raw: list) -> list[dict]:
    out = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        calls = []
        for c in m.get("tool_calls") or []:
            fn = (c or {}).get("function") or {}
            args = fn.get("arguments") or "{}"
            try:
                args = json.dumps(json.loads(args), indent=2)
            except (json.JSONDecodeError, TypeError):
                pass
            calls.append({"id": c.get("id") or "", "name": fn.get("name") or "", "args": args})
        out.append(
            {
                "role": m.get("role") or "user",
                "text": _completion_msg_text(m.get("content")),
                "tool_calls": calls,
                "tool_call_id": m.get("tool_call_id"),
            }
        )
    return out


@router.get(
    "/completions/{id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_cookie)],
)
async def completion_detail_page(id: str, request: Request, db: AsyncSession = Depends(get_db)):
    row = await db.get(CompletionLog, id)
    messages = []
    if row is not None:
        try:
            messages = _completion_view_messages(json.loads(row.request_json))
        except (json.JSONDecodeError, TypeError):
            messages = []
    return templates.TemplateResponse(
        request, "completion_detail.html", {"row": row, "messages": messages}
    )


# --- objectives -----------------------------------------------------------


@router.get(
    "/objectives", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)]
)
async def objectives_page(request: Request, db: AsyncSession = Depends(get_db)):
    objectives = (
        (await db.execute(select(Objective).order_by(Objective.created_at.desc()).limit(100)))
        .scalars()
        .all()
    )
    agents = await agents_service.list_agents(db)
    agent_names = {a.id: a.name for a in agents}

    # Launcher inputs: an objective needs a real API key (usage is attributed
    # to it), so the admin picks an active key — which also fixes the tenant.
    tenants = (await db.execute(select(Tenant))).scalars().all()
    tenant_names = {t.id: t.name for t in tenants}
    keys = await admin_service.list_keys(db)
    key_options = [
        {
            "id": k.id,
            "label": f"{tenant_names.get(k.tenant_id, k.tenant_id)} — {k.name} ({k.prefix}…)",
        }
        for k in keys
        if k.revoked_at is None
    ]
    agent_options = [{"id": a.id, "name": a.name} for a in agents]
    return templates.TemplateResponse(
        request,
        "objectives.html",
        {
            "objectives": objectives,
            "agent_names": agent_names,
            "key_options": key_options,
            "agent_options": agent_options,
        },
    )


@router.post(
    "/objectives", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)]
)
async def objectives_create(
    request: Request,
    api_key_id: str = Form(...),
    agent_id: str = Form(...),
    goal: str = Form(...),
    rubric: str = Form(...),
    max_iterations: str = Form(""),
    max_cost_usd: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    from app.services.objectives import submit_objective

    key = await db.get(ApiKey, api_key_id)
    if key is None:
        raise ApiError.not_found("API key not found")

    obj = await submit_objective(
        request.app,
        db,
        tenant_id=key.tenant_id,
        api_key_id=key.id,
        agent_id=agent_id,
        goal=goal.strip(),
        rubric=rubric.strip(),
        max_cost_usd=float(max_cost_usd) if max_cost_usd.strip() else None,
        max_iterations=int(max_iterations) if max_iterations.strip() else None,
        is_admin=True,  # dashboard admin may drive any agent, incl. admin-only
    )
    return RedirectResponse(url=f"/admin/objectives/{obj.id}", status_code=302)


@router.get(
    "/objectives/{id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_cookie)],
)
async def objective_detail_page(id: str, request: Request, db: AsyncSession = Depends(get_db)):
    objective = await db.get(Objective, id)

    agent = None
    events: list[Event] = []
    if objective is not None:
        agent = await agents_service.get_agent(db, objective.agent_id)
    if objective is not None and objective.session_id is not None:
        events = (
            (
                await db.execute(
                    select(Event)
                    .where(Event.session_id == objective.session_id)
                    .order_by(Event.seq.asc())
                )
            )
            .scalars()
            .all()
        )

    # Group the working session's raw event log into per-iteration blocks,
    # keyed off `objective.iteration_started`. Each iteration collects the
    # assistant's text output plus the `objective.evaluation` verdict that
    # follows it; `objective.finished` is surfaced separately as the loop's
    # terminal marker (there's at most one, after the last iteration).
    iterations: list[dict] = []
    cur: dict | None = None
    finished: dict | None = None
    for e in events:
        try:
            payload = json.loads(e.payload_json)
        except (json.JSONDecodeError, TypeError):
            payload = {}
        etype = e.type
        if etype == "objective.iteration_started":
            cur = {
                "index": payload.get("iteration", len(iterations) + 1),
                "started_at": e.created_at,
                "texts": [],
                "evaluation": None,
            }
            iterations.append(cur)
        elif etype == "text":
            if cur is None:
                cur = {
                    "index": len(iterations) + 1,
                    "started_at": e.created_at,
                    "texts": [],
                    "evaluation": None,
                }
                iterations.append(cur)
            if payload.get("text"):
                cur["texts"].append(payload["text"])
        elif etype == "objective.evaluation":
            if cur is None:
                cur = {
                    "index": len(iterations) + 1,
                    "started_at": e.created_at,
                    "texts": [],
                    "evaluation": None,
                }
                iterations.append(cur)
            cur["evaluation"] = payload
        elif etype == "objective.finished":
            finished = payload

    return templates.TemplateResponse(
        request,
        "objective_detail.html",
        {
            "objective": objective,
            "agent": agent,
            "iterations": iterations,
            "finished": finished,
            "events": events,
        },
    )


# --- agents ---------------------------------------------------------------


async def _roster_options(db: AsyncSession, self_id: str | None) -> list[Agent]:
    """Agents eligible to be picked in the roster multi-select: everything
    except the agent being edited (`self_id`) and any agent that itself
    already has a non-empty roster (rosters are one level deep only)."""
    rows = await agents_service.list_agents(db)
    return [
        a
        for a in rows
        if a.id != self_id and not (a.roster_json and json.loads(a.roster_json))
    ]


async def _mcp_options(db: AsyncSession) -> list[McpServer]:
    return (await db.execute(select(McpServer).order_by(McpServer.name))).scalars().all()


async def _agent_form_context(agent: Agent | None, mcp_servers, roster_options) -> dict:
    from app.services import models as models_service

    return {
        "agent": agent,
        "tool_choices": AGENT_TOOL_CHOICES,
        "permission_modes": AGENT_PERMISSION_MODES,
        "models": await models_service.get_models(),
        "effort_levels": models_service.effort_levels(),
        "mcp_servers": mcp_servers,
        "roster_options": roster_options,
        "selected_tools": set(json.loads(agent.allowed_tools_json)) if agent else set(),
        "selected_mcp": set(json.loads(agent.mcp_names_json)) if agent else set(),
        "selected_roster": set(json.loads(agent.roster_json)) if agent else set(),
    }


@router.get("/agents", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)])
async def agents_page(request: Request, db: AsyncSession = Depends(get_db)):
    rows = await agents_service.list_agents(db)
    return templates.TemplateResponse(request, "agents.html", {"agents": rows})


@router.get(
    "/agents/new", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)]
)
async def agent_new_form(request: Request, db: AsyncSession = Depends(get_db)):
    mcp_servers = await _mcp_options(db)
    roster_options = await _roster_options(db, self_id=None)
    return templates.TemplateResponse(
        request,
        "agent_edit.html",
        await _agent_form_context(None, mcp_servers, roster_options),
    )


@router.get(
    "/agents/{id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_cookie)],
)
async def agent_edit_form(id: str, request: Request, db: AsyncSession = Depends(get_db)):
    agent = await agents_service.get_agent(db, id)
    if agent is None:
        return RedirectResponse(url="/admin/agents", status_code=302)
    mcp_servers = await _mcp_options(db)
    roster_options = await _roster_options(db, self_id=id)
    return templates.TemplateResponse(
        request,
        "agent_edit.html",
        await _agent_form_context(agent, mcp_servers, roster_options),
    )


@router.post("/agents", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)])
async def agents_create(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    await agents_service.create_agent(
        db,
        name=form.get("name", ""),
        description=(form.get("description") or "").strip() or None,
        model=form.get("model", ""),
        effort=(form.get("effort") or "").strip() or None,
        system_prompt=(form.get("system_prompt") or "").strip() or None,
        allowed_tools=form.getlist("allowed_tools"),
        permission_mode=form.get("permission_mode") or "default",
        mcp_names=form.getlist("mcp_names"),
        roster=form.getlist("roster"),
        max_cost_usd=_blank_to_none_float(form.get("max_cost_usd", "")),
        max_iterations=_blank_to_none_int(form.get("max_iterations", "")) or 6,
        is_admin_only=bool(form.get("is_admin_only")),
        bypass_permissions=bool(form.get("bypass_permissions")),
        portal_visible=bool(form.get("portal_visible")),
    )
    return RedirectResponse(url="/admin/agents", status_code=302)


@router.post(
    "/agents/{id}", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)]
)
async def agents_update(id: str, request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    await agents_service.update_agent(
        db,
        id,
        name=form.get("name", ""),
        description=(form.get("description") or "").strip() or None,
        model=form.get("model", ""),
        effort=(form.get("effort") or "").strip() or None,
        system_prompt=(form.get("system_prompt") or "").strip() or None,
        allowed_tools=form.getlist("allowed_tools"),
        permission_mode=form.get("permission_mode") or "default",
        mcp_names=form.getlist("mcp_names"),
        roster=form.getlist("roster"),
        max_cost_usd=_blank_to_none_float(form.get("max_cost_usd", "")),
        max_iterations=_blank_to_none_int(form.get("max_iterations", "")) or 6,
        is_admin_only=bool(form.get("is_admin_only")),
        bypass_permissions=bool(form.get("bypass_permissions")),
        portal_visible=bool(form.get("portal_visible")),
    )
    return RedirectResponse(url="/admin/agents", status_code=302)


@router.post(
    "/agents/{id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_cookie)],
)
async def agents_delete(id: str, db: AsyncSession = Depends(get_db)):
    await agents_service.delete_agent(db, id)
    return RedirectResponse(url="/admin/agents", status_code=302)


# --- MCP servers --------------------------------------------------------


@router.get("/mcp", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)])
async def mcp_page(request: Request, db: AsyncSession = Depends(get_db)):
    servers = await mcp_service.list_mcp_servers(db)
    tenants = (await db.execute(select(Tenant))).scalars().all()
    tenant_names = {t.id: t.name for t in tenants}
    return templates.TemplateResponse(
        request,
        "mcp.html",
        {
            "servers": servers,
            "tenants": [{"id": t.id, "name": t.name} for t in tenants],
            "tenant_names": tenant_names,
        },
    )


@router.post("/mcp", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)])
async def mcp_create(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    raw_headers = (form.get("headers") or "").strip()
    headers = None
    if raw_headers:
        try:
            headers = json.loads(raw_headers)
        except json.JSONDecodeError:
            raise ApiError.invalid("Headers must be valid JSON (e.g. {\"Authorization\": \"Bearer …\"})")
        if not isinstance(headers, dict):
            raise ApiError.invalid("Headers must be a JSON object")
    await mcp_service.create_mcp_server(
        db,
        name=form.get("name", ""),
        kind=form.get("kind", "http"),
        url=form.get("url", ""),
        headers=headers,
        tenant_id=(form.get("tenant_id") or "").strip() or None,
    )
    return RedirectResponse(url="/admin/mcp", status_code=302)


@router.post(
    "/mcp/{id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_cookie)],
)
async def mcp_delete(id: str, db: AsyncSession = Depends(get_db)):
    await mcp_service.delete_mcp_server(db, id)
    return RedirectResponse(url="/admin/mcp", status_code=302)


# --- onboarding (Claude CLI setup) --------------------------------------


@router.get("/onboarding", dependencies=[Depends(require_admin_cookie)])
async def onboarding_page():
    # Setup lives on the System page; keep this path working for bookmarks.
    return RedirectResponse("/admin/system", status_code=307)


@router.get("/onboarding/status", dependencies=[Depends(require_admin_cookie)])
async def onboarding_status() -> dict:
    return await claude_cli.cli_status_cached()


@router.post("/onboarding/install", dependencies=[Depends(require_admin_cookie)])
async def onboarding_install() -> dict:
    return await claude_cli.install_cli()


@router.post("/onboarding/login", dependencies=[Depends(require_admin_cookie)])
async def onboarding_login() -> dict:
    return await claude_cli.start_login()


@router.post("/onboarding/login/code", dependencies=[Depends(require_admin_cookie)])
async def onboarding_login_code(payload: dict = Body(...)) -> dict:
    return await claude_cli.submit_login_code(payload.get("code", ""))


@router.post("/onboarding/logout", dependencies=[Depends(require_admin_cookie)])
async def onboarding_logout() -> dict:
    return await claude_cli.logout()


# --- system -------------------------------------------------------------


@router.get("/system", response_class=HTMLResponse, dependencies=[Depends(require_admin_cookie)])
async def system_page(request: Request):
    settings = get_settings()
    status = await claude_cli.cli_status()
    return templates.TemplateResponse(
        request,
        "system.html",
        {
            "status": status,
            "max_concurrent_runs": settings.max_concurrent_runs,
            "run_timeout_s": settings.run_timeout_s,
        },
    )
