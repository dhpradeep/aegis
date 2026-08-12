import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models import ApiKey, RateBucket, Usage


def _window_key(now: datetime) -> str:
    return now.strftime("%Y%m%d%H%M")


def _seconds_to_next_minute(now: datetime) -> int:
    next_minute = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
    return max(1, int((next_minute - now).total_seconds()))


def _seconds_to_midnight(now: datetime) -> int:
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


async def check_rpm(db: AsyncSession, key: ApiKey) -> None:
    """Atomically increment the current-minute RateBucket for `key`; raise if over `key.rpm`.

    Uses SQLite's `INSERT ... ON CONFLICT DO UPDATE` (upsert) against the
    `(api_key_id, window)` unique constraint so concurrent same-minute
    requests can't race a read-then-write and create duplicate rows (which
    would make a later `scalar_one_or_none()` raise `MultipleResultsFound`
    and undercount requests).
    """
    if key.rpm is None:
        return  # unlimited — no per-minute cap

    now = datetime.now(timezone.utc)
    window = _window_key(now)

    stmt = sqlite_insert(RateBucket).values(
        api_key_id=key.id, window=window, count=1, cost_usd=0.0, window_start=now
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[RateBucket.api_key_id, RateBucket.window],
        set_={"count": RateBucket.count + 1},
    ).returning(RateBucket.count)

    count = (await db.execute(stmt)).scalar_one()
    await db.commit()

    if count > key.rpm:
        raise ApiError.rate_limited(retry_after=_seconds_to_next_minute(now))


async def check_daily_cost(db: AsyncSession, key: ApiKey) -> None:
    """Sum Usage.cost_usd for `key` since UTC midnight; raise if >= key.daily_cost_usd."""
    if key.daily_cost_usd is None:
        return  # unlimited — no daily cost cap

    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    spent = (
        await db.execute(
            select(func.coalesce(func.sum(Usage.cost_usd), 0.0)).where(
                Usage.api_key_id == key.id, Usage.created_at >= midnight
            )
        )
    ).scalar_one()

    if spent >= key.daily_cost_usd:
        raise ApiError.rate_limited(retry_after=_seconds_to_midnight(now))


class RunGate:
    def __init__(self, n: int):
        self._sem = asyncio.Semaphore(n)
        self._capacity = n
        self._in_flight = 0

    async def acquire(self, wait_s: float = 5.0):
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=wait_s)
        except asyncio.TimeoutError:
            raise ApiError.overloaded()
        self._in_flight += 1

    def release(self):
        self._in_flight -= 1
        self._sem.release()

    def in_flight(self) -> int:
        """Number of runs currently holding a slot (not yet released)."""
        return self._in_flight


run_gate: RunGate | None = None


def init_run_gate(n: int):
    global run_gate
    run_gate = RunGate(n)
