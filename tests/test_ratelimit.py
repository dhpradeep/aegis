import asyncio

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal, init_db
from app.db.models import ApiKey, Tenant, Usage
from app.core.errors import ApiError
from app.services.ratelimit import RunGate, check_daily_cost, check_rpm


async def _setup_db(tmp_path, monkeypatch, name: str):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/name}")
    from app.core.config import get_settings
    get_settings.cache_clear()
    import app.db.base as base
    base.reset_engine()
    await init_db()


async def _make_key(db, key_id="k1", rpm=2, daily_cost_usd=10.0) -> ApiKey:
    db.add(Tenant(id="t1", name="Acme"))
    key = ApiKey(
        id=key_id, tenant_id="t1", key_hash=f"hash-{key_id}", prefix="cak_test",
        name="default", rpm=rpm, daily_cost_usd=daily_cost_usd, is_admin=False,
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)
    return key


@pytest.mark.anyio
async def test_check_rpm_raises_after_limit(tmp_path, monkeypatch):
    await _setup_db(tmp_path, monkeypatch, "rpm.db")

    async with SessionLocal() as db:
        key = await _make_key(db, rpm=2)
        await check_rpm(db, key)
        await check_rpm(db, key)
        with pytest.raises(ApiError) as exc_info:
            await check_rpm(db, key)

    err = exc_info.value
    assert err.status == 429
    assert err.retry_after is not None
    assert err.retry_after > 0


@pytest.mark.anyio
async def test_check_rpm_allows_under_limit(tmp_path, monkeypatch):
    await _setup_db(tmp_path, monkeypatch, "rpm_ok.db")

    async with SessionLocal() as db:
        key = await _make_key(db, rpm=5)
        await check_rpm(db, key)
        await check_rpm(db, key)
        # no raise


@pytest.mark.anyio
async def test_check_rpm_concurrent_requests_no_duplicate_rows(tmp_path, monkeypatch):
    """Regression test for the rate-bucket upsert race.

    Before the fix, RateBucket had no unique constraint on
    (api_key_id, window) and check_rpm did a plain read-then-write, so
    concurrent same-minute requests could each read "no row" and each INSERT
    their own row, corrupting the count (and making a later
    scalar_one_or_none() raise MultipleResultsFound). With the
    ON CONFLICT DO UPDATE upsert, concurrent calls must serialize onto a
    single row with an accurate count.
    """
    await _setup_db(tmp_path, monkeypatch, "rpm_concurrent.db")
    from app.db.models import RateBucket

    n = 8
    async with SessionLocal() as db:
        key = await _make_key(db, rpm=n)  # high enough that none raise

    async def _check() -> None:
        async with SessionLocal() as db2:
            await check_rpm(db2, key)

    results = await asyncio.gather(*[_check() for _ in range(n)], return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException):
            raise r

    async with SessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(RateBucket).where(RateBucket.api_key_id == key.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].count == n


@pytest.mark.anyio
async def test_check_daily_cost_raises_when_over_budget(tmp_path, monkeypatch):
    await _setup_db(tmp_path, monkeypatch, "cost.db")

    async with SessionLocal() as db:
        key = await _make_key(db, daily_cost_usd=1.0)
        db.add(Usage(
            tenant_id="t1", api_key_id=key.id, input_tokens=1, output_tokens=1,
            cache_read_tokens=0, cost_usd=1.5, duration_ms=1, num_turns=1,
        ))
        await db.commit()

        with pytest.raises(ApiError) as exc_info:
            await check_daily_cost(db, key)

    err = exc_info.value
    assert err.status == 429
    assert err.retry_after is not None
    assert err.retry_after > 0


@pytest.mark.anyio
async def test_check_daily_cost_allows_under_budget(tmp_path, monkeypatch):
    await _setup_db(tmp_path, monkeypatch, "cost_ok.db")

    async with SessionLocal() as db:
        key = await _make_key(db, daily_cost_usd=10.0)
        db.add(Usage(
            tenant_id="t1", api_key_id=key.id, input_tokens=1, output_tokens=1,
            cache_read_tokens=0, cost_usd=1.5, duration_ms=1, num_turns=1,
        ))
        await db.commit()

        await check_daily_cost(db, key)
        # no raise


@pytest.mark.anyio
async def test_run_gate_second_acquire_times_out():
    gate = RunGate(1)
    await gate.acquire()

    with pytest.raises(ApiError) as exc_info:
        await gate.acquire(wait_s=0.05)

    assert exc_info.value.status == 503
    gate.release()


@pytest.mark.anyio
async def test_run_gate_release_allows_next_acquire():
    gate = RunGate(1)
    await gate.acquire()
    gate.release()
    # Should not raise / time out now that a slot is free.
    await asyncio.wait_for(gate.acquire(), timeout=1.0)
    gate.release()
