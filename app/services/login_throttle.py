"""Brute-force protection for the admin login.

Keyed strictly on the real TCP peer address (`request.client.host`), never on
forwarding headers like `X-Forwarded-For`, which a client can set to any value
and thereby dodge a per-IP limit. In-memory and per-process — fine for the
single-container deployment; a horizontally-scaled deployment would need a
shared store (Redis) instead.
"""

from __future__ import annotations

import math
import time

MAX_ATTEMPTS = 5          # failures within WINDOW before a lockout kicks in
WINDOW = 300.0            # seconds over which failures are counted
BASE_LOCKOUT = 60.0       # first lockout length; doubles on repeat lockouts
MAX_LOCKOUT = 3600.0      # cap on the escalated lockout
_PRUNE_AT = 10_000        # bound the state dict under a distributed attack

# ip -> {"fails": [monotonic ts...], "locked_until": float, "strikes": int}
_STATE: dict[str, dict] = {}


def _now() -> float:
    return time.monotonic()


def _prune(now: float) -> None:
    if len(_STATE) < _PRUNE_AT:
        return
    stale = [
        ip
        for ip, s in _STATE.items()
        if s.get("locked_until", 0.0) <= now
        and all(now - t >= WINDOW for t in s.get("fails", []))
    ]
    for ip in stale:
        _STATE.pop(ip, None)


def retry_after(ip: str) -> int:
    """Seconds this IP must wait before another attempt, or 0 if not locked."""
    s = _STATE.get(ip)
    if not s:
        return 0
    remaining = s.get("locked_until", 0.0) - _now()
    return math.ceil(remaining) if remaining > 0 else 0


def record_failure(ip: str) -> int:
    """Record a failed attempt; returns the resulting lockout in seconds (0 if
    the IP is still under the threshold)."""
    now = _now()
    _prune(now)
    s = _STATE.setdefault(ip, {"fails": [], "locked_until": 0.0, "strikes": 0})
    s["fails"] = [t for t in s["fails"] if now - t < WINDOW]
    s["fails"].append(now)
    if len(s["fails"]) >= MAX_ATTEMPTS:
        s["strikes"] += 1
        lockout = min(BASE_LOCKOUT * (2 ** (s["strikes"] - 1)), MAX_LOCKOUT)
        s["locked_until"] = now + lockout
        s["fails"] = []  # clean slate; the lockout is the deterrent now
        return int(lockout)
    return 0


def record_success(ip: str) -> None:
    """Clear all state for an IP after a successful login."""
    _STATE.pop(ip, None)


def reset() -> None:
    """Test hook: wipe all throttle state."""
    _STATE.clear()
