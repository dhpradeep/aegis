import json

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin_key
from app.core.errors import ApiError
from app.db.models import Agent, ApiKey
from app.schemas.admin import (
    AdminUsageRow,
    BillingConfigRequest,
    BillingConfigResponse,
    KeyCreateRequest,
    KeyCreateResponse,
    KeyPatchRequest,
    KeySummary,
    TenantCreateRequest,
    TenantPatchRequest,
    TenantResponse,
    WebhookConfigRequest,
    WebhookConfigResponse,
)
from app.schemas.agents import AgentCreateRequest, AgentPatchRequest, AgentResponse
from app.services import admin as admin_service
from app.services import agents as agents_service
from app.services import models as models_service
from app.services.billing import all_tenant_usage

router = APIRouter(prefix="/admin/api", tags=["admin"])

__all__ = ["router"]


def _to_response(agent: Agent) -> AgentResponse:
    return AgentResponse(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        model=agent.model,
        effort=agent.effort,
        system_prompt=agent.system_prompt,
        allowed_tools=json.loads(agent.allowed_tools_json),
        permission_mode=agent.permission_mode,
        mcp_names=json.loads(agent.mcp_names_json),
        roster=json.loads(agent.roster_json),
        max_cost_usd=agent.max_cost_usd,
        max_iterations=agent.max_iterations,
        is_admin_only=agent.is_admin_only,
        bypass_permissions=agent.bypass_permissions,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


@router.post("/tenants", response_model=TenantResponse)
async def create_tenant(
    body: TenantCreateRequest,
    key: ApiKey = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> TenantResponse:
    tenant = await admin_service.create_tenant(db, name=body.name)
    return TenantResponse(
        id=tenant.id, name=tenant.name, default_model=tenant.default_model
    )


@router.patch("/tenants/{id}", response_model=TenantResponse)
async def patch_tenant(
    id: str,
    body: TenantPatchRequest,
    key: ApiKey = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> TenantResponse:
    tenant = await admin_service.set_tenant_default_model(
        db, actor=key.id, tenant_id=id, default_model=body.default_model
    )
    return TenantResponse(
        id=tenant.id, name=tenant.name, default_model=tenant.default_model
    )


@router.post("/keys", response_model=KeyCreateResponse)
async def create_key(
    body: KeyCreateRequest,
    key: ApiKey = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> KeyCreateResponse:
    new_key, full_key = await admin_service.create_key(
        db,
        actor=key.id,
        tenant_id=body.tenant_id,
        name=body.name,
        rpm=body.rpm,
        daily_cost_usd=body.daily_cost_usd,
        is_admin=body.is_admin,
    )
    return KeyCreateResponse(api_key=full_key, prefix=new_key.prefix, id=new_key.id)


@router.post("/keys/{id}/revoke", response_model=KeySummary)
async def revoke_key(
    id: str,
    key: ApiKey = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> KeySummary:
    revoked = await admin_service.revoke_key(db, actor=key.id, key_id=id)
    return KeySummary.model_validate(revoked, from_attributes=True)


@router.patch("/keys/{id}", response_model=KeySummary)
async def patch_key(
    id: str,
    body: KeyPatchRequest,
    key: ApiKey = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> KeySummary:
    # Only fields explicitly present in the request body are applied; omitted
    # fields stay unchanged. An explicit null clears a limit to unlimited.
    fields = body.model_dump(exclude_unset=True)
    updated = await admin_service.patch_key_limits(
        db,
        actor=key.id,
        key_id=id,
        rpm=fields.get("rpm", admin_service.UNSET),
        daily_cost_usd=fields.get("daily_cost_usd", admin_service.UNSET),
    )
    return KeySummary.model_validate(updated, from_attributes=True)


@router.get("/keys", response_model=list[KeySummary])
async def list_keys(
    key: ApiKey = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> list[KeySummary]:
    rows = await admin_service.list_keys(db)
    return [KeySummary.model_validate(row, from_attributes=True) for row in rows]


@router.put("/billing/{tenant_id}", response_model=BillingConfigResponse)
async def put_billing_config(
    tenant_id: str,
    body: BillingConfigRequest,
    key: ApiKey = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> BillingConfigResponse:
    config = await admin_service.upsert_billing_config(
        db,
        actor=key.id,
        tenant_id=tenant_id,
        price_per_mtok_input=body.price_per_mtok_input,
        price_per_mtok_output=body.price_per_mtok_output,
        markup=body.markup,
    )
    return BillingConfigResponse.model_validate(config, from_attributes=True)


@router.put("/webhooks/{tenant_id}", response_model=WebhookConfigResponse)
async def put_webhook_config(
    tenant_id: str,
    body: WebhookConfigRequest,
    key: ApiKey = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> WebhookConfigResponse:
    config = await admin_service.upsert_webhook_config(
        db, actor=key.id, tenant_id=tenant_id, url=body.url, secret=body.secret
    )
    return WebhookConfigResponse(tenant_id=config.tenant_id, url=config.url)


@router.get("/usage", response_model=list[AdminUsageRow])
async def get_all_usage(
    key: ApiKey = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> list[AdminUsageRow]:
    rows = await all_tenant_usage(db)
    return [AdminUsageRow(**row) for row in rows]


@router.get("/models")
async def list_models(
    key: ApiKey = Depends(require_admin_key),
) -> dict:
    """The selectable model catalog + reasoning-effort levels. Sourced live from
    the Anthropic Models API via the operator's subscription credentials (cached),
    with a curated fallback when offline. The agent form's picker reads this."""
    return {
        "models": await models_service.get_models(),
        "effort_levels": models_service.effort_levels(),
    }


@router.post("/agents", response_model=AgentResponse)
async def create_agent(
    body: AgentCreateRequest,
    key: ApiKey = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> AgentResponse:
    agent = await agents_service.create_agent(
        db,
        name=body.name,
        model=body.model,
        description=body.description,
        effort=body.effort,
        system_prompt=body.system_prompt,
        allowed_tools=body.allowed_tools,
        permission_mode=body.permission_mode,
        mcp_names=body.mcp_names,
        roster=body.roster,
        max_cost_usd=body.max_cost_usd,
        max_iterations=body.max_iterations,
        is_admin_only=body.is_admin_only,
        bypass_permissions=body.bypass_permissions,
    )
    return _to_response(agent)


@router.get("/agents", response_model=list[AgentResponse])
async def list_agents(
    key: ApiKey = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> list[AgentResponse]:
    rows = await agents_service.list_agents(db)
    return [_to_response(row) for row in rows]


@router.get("/agents/{id}", response_model=AgentResponse)
async def get_agent(
    id: str,
    key: ApiKey = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> AgentResponse:
    agent = await agents_service.get_agent(db, id)
    if agent is None:
        raise ApiError.not_found("agent not found")
    return _to_response(agent)


@router.patch("/agents/{id}", response_model=AgentResponse)
async def patch_agent(
    id: str,
    body: AgentPatchRequest,
    key: ApiKey = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> AgentResponse:
    fields = body.model_dump(exclude_unset=True)
    agent = await agents_service.update_agent(db, id, **fields)
    return _to_response(agent)


@router.delete("/agents/{id}")
async def delete_agent(
    id: str,
    key: ApiKey = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await agents_service.delete_agent(db, id)
    return {"deleted": True}
