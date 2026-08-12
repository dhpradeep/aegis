"""Shared Claude credential resolution for calling Anthropic HTTP APIs on the
operator's behalf (Models API, subscription usage) without a separate API key.

Resolution order:

1. ``ANTHROPIC_API_KEY`` (if the operator opted into API-key billing) → ``x-api-key``.
2. The Claude CLI OAuth token (subscription login), read from
   ``~/.claude/.credentials.json`` (Linux/Docker) or the macOS keychain →
   ``Authorization: Bearer`` plus the ``oauth-2025-04-20`` beta header.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def claude_oauth_token() -> str | None:
    """Read the Claude CLI OAuth access token (subscription login)."""
    raw: str | None = None
    creds_file = Path.home() / ".claude" / ".credentials.json"
    if creds_file.exists():
        try:
            raw = creds_file.read_text()
        except OSError:
            raw = None
    if raw is None and sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            raw = out.stdout or None
        except (OSError, subprocess.SubprocessError):
            raw = None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data.get("claudeAiOauth", {}).get("accessToken") or data.get("accessToken")


def auth_headers() -> dict[str, str] | None:
    """Headers for an authenticated Anthropic API call, or None if no credential."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    token = claude_oauth_token()
    if token:
        return {
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
        }
    return None
