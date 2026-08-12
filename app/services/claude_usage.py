"""Claude subscription quota (the 5-hour session window, weekly and per-model
weekly limits, and extra-usage credits) — the same data the CLI's ``/usage``
command shows, fetched from ``GET /api/oauth/usage`` with the subscription OAuth
token and normalized into a display-ready shape of utilization percentages with
reset timestamps.
"""

from __future__ import annotations

import time

import httpx

from app.services.claude_auth import auth_headers

_USAGE_API = "https://api.anthropic.com/api/oauth/usage"

_CACHE: dict = {"data": None, "expires": 0.0}
_TTL = 60.0  # plan usage moves slowly; a minute of caching is plenty.


def _bar(block: dict | None) -> dict | None:
    """Normalize one {utilization, resets_at, ...} block to a display bar."""
    if not isinstance(block, dict):
        return None
    util = block.get("utilization")
    if util is None:
        return None
    return {
        "percent": round(float(util)),
        "resets_at": block.get("resets_at"),
    }


def _scoped_limits(payload: dict) -> list[dict]:
    """Per-model weekly limits from the normalized `limits` array."""
    out: list[dict] = []
    for lim in payload.get("limits") or []:
        if not isinstance(lim, dict) or lim.get("kind") != "weekly_scoped":
            continue
        scope = lim.get("scope") or {}
        model = (scope.get("model") or {}).get("display_name")
        out.append(
            {
                "label": model or "Scoped",
                "percent": round(float(lim.get("percent") or 0)),
                "resets_at": lim.get("resets_at"),
                "severity": lim.get("severity") or "normal",
            }
        )
    return out


def _normalize(payload: dict) -> dict:
    extra = payload.get("extra_usage") or {}
    return {
        "available": True,
        "session": _bar(payload.get("five_hour")),
        "weekly": _bar(payload.get("seven_day")),
        "weekly_opus": _bar(payload.get("seven_day_opus")),
        "weekly_sonnet": _bar(payload.get("seven_day_sonnet")),
        "scoped": _scoped_limits(payload),
        "extra_usage": {
            "enabled": bool(extra.get("is_enabled")),
            "utilization": extra.get("utilization"),
            "monthly_limit": extra.get("monthly_limit"),
            "used_credits": extra.get("used_credits"),
            "currency": extra.get("currency"),
        },
    }


async def _fetch() -> dict | None:
    headers = auth_headers()
    if headers is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(_USAGE_API, headers=headers)
        if resp.status_code != 200:
            return None
        return _normalize(resp.json())
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


async def get_plan_usage() -> dict:
    """Live subscription usage, cached briefly. Returns ``{"available": False}``
    when no subscription credential is present or the call fails, so callers can
    render a graceful 'not connected' state."""
    now = time.monotonic()
    if _CACHE["data"] is not None and now < _CACHE["expires"]:
        return _CACHE["data"]
    data = await _fetch()
    if data is None:
        data = {"available": False}
    _CACHE["data"] = data
    _CACHE["expires"] = now + _TTL
    return data


def invalidate_cache() -> None:
    _CACHE["data"] = None
    _CACHE["expires"] = 0.0
