import json
import logging
from secrets import token_hex

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.security import generate_api_key
from app.db.models import (
    ApiKey,
    AuditLog,
    BillingConfig,
    CompletionLog,
    Objective,
    RateBucket,
    Tenant,
    Usage,
    WebhookConfig,
)
from app.db.models._base import _utcnow

logger = logging.getLogger("app.admin")

__all__ = [
    "create_tenant",
    "create_key",
    "revoke_key",
    "patch_key_limits",
    "list_keys",
    "upsert_billing_config",
    "upsert_webhook_config",
    "bootstrap_admin_if_needed",
]


def _audit(db: AsyncSession, *, actor: str, action: str, detail: dict) -> None:
    db.add(AuditLog(actor=actor, action=action, detail_json=json.dumps(detail)))


async def create_tenant(db: AsyncSession, *, name: str) -> Tenant:
    """Create a new Tenant with a generated id. Shared by the admin API and (Task 17) admin UI."""
    tenant = Tenant(id="ten_" + token_hex(8), name=name)
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return tenant


async def _get_tenant_or_404(db: AsyncSession, tenant_id: str) -> Tenant:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise ApiError.not_found("Tenant not found")
    return tenant


async def set_tenant_default_model(
    db: AsyncSession, *, actor: str, tenant_id: str, default_model: str | None
) -> Tenant:
    """Set (or clear, with None/empty) a tenant's default chat model."""
    tenant = await _get_tenant_or_404(db, tenant_id)
    value = (default_model or "").strip() or None
    tenant.default_model = value
    _audit(
        db,
        actor=actor,
        action="tenant.set_default_model",
        detail={"tenant_id": tenant_id, "default_model": value},
    )
    await db.commit()
    await db.refresh(tenant)
    return tenant


async def _get_key_or_404(db: AsyncSession, key_id: str) -> ApiKey:
    key = await db.get(ApiKey, key_id)
    if key is None:
        raise ApiError.not_found("API key not found")
    return key


async def create_key(
    db: AsyncSession,
    *,
    actor: str,
    tenant_id: str,
    name: str,
    rpm: int | None = None,
    daily_cost_usd: float | None = None,
    is_admin: bool = False,
) -> tuple[ApiKey, str]:
    """Generate and store a new API key. Returns (row, full_key) — the full key is

    shown to the caller exactly once and never persisted or logged again.
    """
    await _get_tenant_or_404(db, tenant_id)

    full_key, prefix, key_hash = generate_api_key()
    key = ApiKey(
        id="key_" + token_hex(8),
        tenant_id=tenant_id,
        key_hash=key_hash,
        prefix=prefix,
        name=name,
        rpm=rpm,
        daily_cost_usd=daily_cost_usd,
        is_admin=is_admin,
    )
    db.add(key)
    _audit(
        db,
        actor=actor,
        action="key.create",
        detail={
            "key_id": key.id,
            "tenant_id": tenant_id,
            "name": name,
            "prefix": prefix,
            "is_admin": is_admin,
        },
    )
    await db.commit()
    await db.refresh(key)
    return key, full_key


async def revoke_key(db: AsyncSession, *, actor: str, key_id: str) -> ApiKey:
    key = await _get_key_or_404(db, key_id)
    key.revoked_at = _utcnow()
    _audit(db, actor=actor, action="key.revoke", detail={"key_id": key_id})
    await db.commit()
    await db.refresh(key)
    return key


async def delete_key(db: AsyncSession, *, actor: str, key_id: str) -> None:
    """Permanently delete a revoked key. Usage, completion, and objective rows
    keep their tenant attribution but lose the key link."""
    key = await _get_key_or_404(db, key_id)
    if key.revoked_at is None:
        raise ApiError.invalid("Revoke the key before deleting it")
    for model in (Usage, CompletionLog, Objective):
        await db.execute(
            sa_update(model).where(model.api_key_id == key_id).values(api_key_id=None)
        )
    await db.execute(sa_delete(RateBucket).where(RateBucket.api_key_id == key_id))
    _audit(db, actor=actor, action="key.delete", detail={"key_id": key_id})
    await db.delete(key)
    await db.commit()


class _Unset:
    """Sentinel: a limit argument that was not supplied (leave it unchanged).

    Distinct from ``None``, which explicitly means "clear to unlimited".
    """


UNSET = _Unset()


async def patch_key_limits(
    db: AsyncSession,
    *,
    actor: str,
    key_id: str,
    rpm: "int | None | _Unset" = UNSET,
    daily_cost_usd: "float | None | _Unset" = UNSET,
) -> ApiKey:
    key = await _get_key_or_404(db, key_id)
    changes: dict = {}
    if not isinstance(rpm, _Unset):
        key.rpm = rpm  # may be None → unlimited
        changes["rpm"] = rpm
    if not isinstance(daily_cost_usd, _Unset):
        key.daily_cost_usd = daily_cost_usd  # may be None → unlimited
        changes["daily_cost_usd"] = daily_cost_usd
    if changes:
        _audit(db, actor=actor, action="key.update_limits", detail={"key_id": key_id, **changes})
    await db.commit()
    await db.refresh(key)
    return key


async def list_keys(db: AsyncSession) -> list[ApiKey]:
    rows = (
        await db.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))
    ).scalars().all()
    return list(rows)


async def upsert_billing_config(
    db: AsyncSession,
    *,
    actor: str,
    tenant_id: str,
    price_per_mtok_input: float,
    price_per_mtok_output: float,
    markup: float,
) -> BillingConfig:
    await _get_tenant_or_404(db, tenant_id)

    config = await db.get(BillingConfig, tenant_id)
    if config is None:
        config = BillingConfig(tenant_id=tenant_id)
        db.add(config)
    config.price_per_mtok_input = price_per_mtok_input
    config.price_per_mtok_output = price_per_mtok_output
    config.markup = markup
    _audit(
        db,
        actor=actor,
        action="billing.upsert",
        detail={
            "tenant_id": tenant_id,
            "price_per_mtok_input": price_per_mtok_input,
            "price_per_mtok_output": price_per_mtok_output,
            "markup": markup,
        },
    )
    await db.commit()
    await db.refresh(config)
    return config


async def upsert_webhook_config(
    db: AsyncSession, *, actor: str, tenant_id: str, url: str, secret: str
) -> WebhookConfig:
    await _get_tenant_or_404(db, tenant_id)

    config = await db.get(WebhookConfig, tenant_id)
    if config is None:
        config = WebhookConfig(tenant_id=tenant_id)
        db.add(config)
    config.url = url
    config.secret = secret
    _audit(
        db,
        actor=actor,
        action="webhook.upsert",
        detail={"tenant_id": tenant_id, "url": url},
    )
    await db.commit()
    await db.refresh(config)
    return config


async def bootstrap_admin_if_needed(db: AsyncSession) -> str | None:
    """If no admin ApiKey exists yet, create a default Tenant + admin ApiKey.

    Logs the full key to stdout exactly once (via logging) since it can never
    be retrieved again. Returns the full key when one was created, else None.
    Called from app.main's lifespan when env BOOTSTRAP_ADMIN=1.
    """
    from app.core.config import get_settings

    existing = (
        await db.execute(select(ApiKey).where(ApiKey.is_admin.is_(True)))
    ).scalar_one_or_none()
    if existing is not None:
        return None

    settings = get_settings()
    tenant = Tenant(id="ten_" + token_hex(8), name="Bootstrap Admin")
    db.add(tenant)

    full_key, prefix, key_hash = generate_api_key()
    key = ApiKey(
        id="key_" + token_hex(8),
        tenant_id=tenant.id,
        key_hash=key_hash,
        prefix=prefix,
        name="bootstrap-admin",
        rpm=settings.default_rpm,
        daily_cost_usd=settings.default_daily_cost_usd,
        is_admin=True,
    )
    db.add(key)
    _audit(
        db,
        actor="system",
        action="key.bootstrap",
        detail={"key_id": key.id, "tenant_id": tenant.id, "prefix": prefix},
    )
    await db.commit()

    logger.warning(
        "Bootstrap admin key created — save this now, it will not be shown again: %s",
        full_key,
    )
    return full_key
