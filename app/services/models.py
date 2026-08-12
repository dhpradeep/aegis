"""Model catalog, sourced live from the Anthropic Models API using the
operator's subscription credentials (the SDK exposes no model list). Cached
in-process for 1h; falls back to a small curated list when no credential is
available or the call fails.
"""

from __future__ import annotations

import json
import time

import httpx

from app.core.config import get_settings
from app.services.claude_auth import auth_headers

_MODELS_API = "https://api.anthropic.com/v1/models"

# Reasoning-effort levels accepted by the SDK, ordered low → high.
EFFORT_LEVELS: list[str] = ["low", "medium", "high", "xhigh", "max"]

# Fallback ONLY — used when the live Models API can't be reached.
FALLBACK_MODELS: list[dict[str, str]] = [
    {"id": "claude-opus-5", "label": "Claude Opus 5"},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5"},
    {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
    {"id": "opus", "label": "opus (latest alias)"},
    {"id": "sonnet", "label": "sonnet (latest alias)"},
    {"id": "haiku", "label": "haiku (latest alias)"},
]

_ALIASES = [
    {"id": "opus", "label": "opus (latest alias)"},
    {"id": "sonnet", "label": "sonnet (latest alias)"},
    {"id": "haiku", "label": "haiku (latest alias)"},
]

_CACHE: dict = {"models": None, "expires": 0.0}
_TTL_OK = 3600.0
_TTL_FALLBACK = 300.0


def effort_levels() -> list[str]:
    return list(EFFORT_LEVELS)


async def _fetch_live() -> list[dict[str, str]] | None:
    headers = auth_headers()
    if headers is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(_MODELS_API, headers=headers, params={"limit": 100})
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", [])
    except (httpx.HTTPError, json.JSONDecodeError):
        return None
    models = [
        {"id": m["id"], "label": m.get("display_name") or m["id"]}
        for m in data
        if m.get("id")
    ]
    if not models:
        return None
    return models + _ALIASES


async def get_models() -> list[dict[str, str]]:
    """Live model catalog (cached), with the curated fallback on failure."""
    now = time.monotonic()
    if _CACHE["models"] is not None and now < _CACHE["expires"]:
        return _CACHE["models"]

    if get_settings().models_live_fetch:
        live = await _fetch_live()
        if live:
            _CACHE["models"] = live
            _CACHE["expires"] = now + _TTL_OK
            return live

    _CACHE["models"] = list(FALLBACK_MODELS)
    _CACHE["expires"] = now + _TTL_FALLBACK
    return _CACHE["models"]
