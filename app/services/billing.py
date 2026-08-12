from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BillingConfig, Usage


async def usage_summary(
    db: AsyncSession,
    tenant_id: str,
    dt_from: datetime | None = None,
    dt_to: datetime | None = None,
) -> dict:
    """Aggregate Usage rows for `tenant_id`, optionally bounded by [dt_from, dt_to].

    Returns totals plus a per-session breakdown. Shared aggregation logic for
    the caller usage endpoint (Task 15) and the admin usage endpoint (Task 16).
    """
    filters = [Usage.tenant_id == tenant_id]
    if dt_from is not None:
        filters.append(Usage.created_at >= dt_from)
    if dt_to is not None:
        filters.append(Usage.created_at <= dt_to)

    total_cost_usd, input_tokens, output_tokens, runs = (
        await db.execute(
            select(
                func.coalesce(func.sum(Usage.cost_usd), 0.0),
                func.coalesce(func.sum(Usage.input_tokens), 0),
                func.coalesce(func.sum(Usage.output_tokens), 0),
                func.count(Usage.id),
            ).where(*filters)
        )
    ).one()

    session_rows = (
        await db.execute(
            select(
                Usage.session_id,
                func.coalesce(func.sum(Usage.cost_usd), 0.0),
                func.count(Usage.id),
            )
            .where(*filters)
            .group_by(Usage.session_id)
        )
    ).all()

    return {
        "total_cost_usd": total_cost_usd,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "runs": runs,
        "by_session": [
            {"session_id": session_id, "cost_usd": cost_usd, "runs": run_count}
            for session_id, cost_usd, run_count in session_rows
        ],
    }


async def all_tenant_usage(db: AsyncSession) -> list[dict]:
    """Aggregate Usage rows across every tenant, grouped by tenant_id.

    For each tenant, also attaches `priced_cost_usd`: the usage's input/output
    tokens re-priced via that tenant's BillingConfig (price per million tokens,
    with `markup` applied as a multiplier on top), or None if the tenant has no
    BillingConfig yet. Shared aggregation logic for the admin usage endpoint
    (Task 16) and the admin UI (Task 17).
    """
    rows = (
        await db.execute(
            select(
                Usage.tenant_id,
                func.coalesce(func.sum(Usage.cost_usd), 0.0),
                func.coalesce(func.sum(Usage.input_tokens), 0),
                func.coalesce(func.sum(Usage.output_tokens), 0),
                func.count(Usage.id),
            ).group_by(Usage.tenant_id)
        )
    ).all()

    configs = {
        c.tenant_id: c for c in (await db.execute(select(BillingConfig))).scalars().all()
    }

    result = []
    for tenant_id, cost_usd, input_tokens, output_tokens, runs in rows:
        config = configs.get(tenant_id)
        priced_cost_usd = None
        if config is not None:
            raw = (
                input_tokens / 1_000_000 * config.price_per_mtok_input
                + output_tokens / 1_000_000 * config.price_per_mtok_output
            )
            priced_cost_usd = raw * (1 + config.markup)
        result.append(
            {
                "tenant_id": tenant_id,
                "cost_usd": cost_usd,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "runs": runs,
                "priced_cost_usd": priced_cost_usd,
            }
        )
    return result
