"""Claude CLI onboarding: detect install/sign-in state and drive install and
login from the dashboard via the CLI's own subcommands. Subprocesses run with
`ANTHROPIC_API_KEY` stripped so status/login always reflect the subscription
login, never an API key.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import time
from pathlib import Path

_URL_RE = re.compile(r"https?://[^\s\"']+")


def _cli_path() -> str | None:
    """Resolve the Claude CLI exactly as claude-agent-sdk does: the binary it
    bundles takes priority, so onboarding checks/signs into the *same* CLI the
    runtime will actually run. Falls back to a system `claude` on PATH."""
    try:
        import claude_agent_sdk

        name = "claude.exe" if platform.system() == "Windows" else "claude"
        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
        if bundled.is_file():
            return str(bundled)
    except Exception:
        pass
    return shutil.which("claude")

# Short-lived cache so a per-request status check (e.g. the dashboard banner)
# doesn't spawn a subprocess on every page render.
_CACHE: dict = {"status": None, "expires": 0.0}
_TTL = 20.0

# A login subprocess kept alive while the operator completes the browser flow.
_login_proc: asyncio.subprocess.Process | None = None


def _env() -> dict:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # force subscription auth, never API key
    return env


async def _run(*args: str, timeout: float = 15.0) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_env(),
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
    except (FileNotFoundError, asyncio.TimeoutError, OSError) as exc:
        return 1, "", str(exc)


async def cli_status() -> dict:
    """Live status of the Claude CLI: available?, version, signed in?, account.

    `available` is almost always true because the SDK bundles the CLI; the real
    onboarding work is the sign-in, so `ready` is just `logged_in`.
    """
    path = _cli_path()
    if path is None:
        return {"installed": False, "logged_in": False, "ready": False, "bundled": False}

    version = None
    rc, out, _ = await _run(path, "--version", timeout=8)
    if rc == 0:
        version = out.strip().split(" ")[0] or None

    logged_in = False
    email = plan = None
    rc, out, _ = await _run(path, "auth", "status", "--json", timeout=12)
    if rc == 0 and out.strip():
        try:
            data = json.loads(out)
            logged_in = bool(data.get("loggedIn"))
            email = data.get("email")
            plan = data.get("subscriptionType")
        except json.JSONDecodeError:
            pass

    return {
        "installed": True,
        "path": path,
        "bundled": "_bundled" in path,
        "version": version,
        "logged_in": logged_in,
        "email": email,
        "plan": plan,
        "ready": logged_in,
    }


async def cli_status_cached() -> dict:
    now = time.monotonic()
    if _CACHE["status"] is not None and now < _CACHE["expires"]:
        return _CACHE["status"]
    status = await cli_status()
    _CACHE["status"] = status
    _CACHE["expires"] = now + _TTL
    return status


def invalidate_cache() -> None:
    _CACHE["status"] = None
    _CACHE["expires"] = 0.0


async def install_cli() -> dict:
    """Fallback installer. The CLI normally ships bundled with the SDK, so this
    is only needed if the bundled binary is missing (e.g. platform mismatch):
    it installs a system CLI via the official script, or updates it if present."""
    invalidate_cache()
    system_cli = shutil.which("claude")
    if system_cli:
        rc, out, err = await _run(system_cli, "install", "stable", timeout=300)
        invalidate_cache()
        return {"ok": rc == 0, "output": (out + err).strip()[:4000]}

    rc, out, err = await _run(
        "sh", "-c", "curl -fsSL https://claude.ai/install.sh | bash", timeout=300
    )
    invalidate_cache()
    return {"ok": rc == 0, "output": (out + err).strip()[:4000]}


async def logout() -> dict:
    """Sign the CLI out so a different Claude account can be connected."""
    global _login_proc
    invalidate_cache()
    path = _cli_path()
    if path is None:
        return {"ok": False, "error": "Claude CLI is not available."}

    # Drop any half-finished login attempt first.
    if _login_proc is not None and _login_proc.returncode is None:
        try:
            _login_proc.terminate()
        except ProcessLookupError:
            pass
    _login_proc = None

    rc, out, err = await _run(path, "auth", "logout", timeout=20)
    invalidate_cache()
    if rc == 0:
        return {"ok": True}
    return {"ok": False, "error": (err or out).strip()[:500] or "Sign-out failed."}


async def start_login() -> dict:
    """Kick off `claude auth login` and return the browser auth URL it prints.

    The process is kept alive so it can complete the OAuth callback while the
    operator authorizes in their browser; the dashboard then polls
    `cli_status()` until `logged_in` flips true.
    """
    global _login_proc
    invalidate_cache()
    path = _cli_path()
    if path is None:
        return {"ok": False, "error": "Claude CLI is not available."}

    # A previous attempt still running? drop it.
    if _login_proc is not None and _login_proc.returncode is None:
        try:
            _login_proc.terminate()
        except ProcessLookupError:
            pass

    try:
        _login_proc = await asyncio.create_subprocess_exec(
            path, "auth", "login", "--claudeai",
            # stdin stays open: this flow prints an auth URL, then the operator
            # authorizes in the browser, copies the code it shows, and pastes it
            # back — we feed that code to the process via submit_login_code().
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_env(),
        )
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    # Read early output for the auth URL (bounded so we don't block).
    #
    # The OAuth URL is long (>256 chars) and streams in over several chunks, so
    # we must NOT accept the first partial match — a URL captured mid-stream is
    # truncated (e.g. cut off at "&sco…"), which the OAuth server rejects with
    # "Missing scope parameter". Only accept a match once it's terminated by a
    # boundary (whitespace/newline after it), meaning the full URL has arrived.
    url = None
    buf = ""
    deadline = time.monotonic() + 10.0

    def _complete_url(text: str) -> str | None:
        m = _URL_RE.search(text)
        if m and m.end() < len(text):  # something non-URL follows → fully read
            return m.group(0).rstrip(".,)")
        return None

    while time.monotonic() < deadline and _login_proc.stdout is not None:
        try:
            chunk = await asyncio.wait_for(_login_proc.stdout.read(4096), timeout=1.5)
        except asyncio.TimeoutError:
            url = _complete_url(buf)
            if url:
                break
            continue
        if not chunk:
            break
        buf += chunk.decode(errors="replace")
        url = _complete_url(buf)
        if url:
            break

    # Loop ended (EOF/deadline) with a URL that ran to the buffer's end — the
    # stream closed right after it, so it's complete even without a trailing char.
    if url is None:
        m = _URL_RE.search(buf)
        if m:
            url = m.group(0).rstrip(".,)")

    if url:
        return {"ok": True, "url": url}
    return {
        "ok": False,
        "error": "Could not start the browser sign-in from the server. Run the terminal command below instead.",
        "output": buf.strip()[:1000],
    }


async def submit_login_code(code: str) -> dict:
    """Feed the authorization code (pasted from the browser) to the waiting
    `claude auth login` process, then wait for it to finish and re-check status.
    """
    global _login_proc
    proc = _login_proc
    if proc is None or proc.returncode is not None or proc.stdin is None:
        return {"ok": False, "error": "No sign-in is in progress — click “Sign in from here” first."}

    code = (code or "").strip()
    if not code:
        return {"ok": False, "error": "Paste the authorization code first."}

    try:
        proc.stdin.write((code + "\n").encode())
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError) as exc:
        return {"ok": False, "error": f"Sign-in process stopped accepting input: {exc}"}

    # Drain remaining output while the CLI exchanges the code, so it never blocks
    # on a full stdout pipe, and wait for it to exit.
    try:
        await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        pass

    invalidate_cache()
    status = await cli_status()
    _login_proc = None
    if status.get("logged_in"):
        return {"ok": True, "status": status}
    return {
        "ok": False,
        "error": "Sign-in didn't complete — the code may be expired or already used. Start again.",
        "status": status,
    }
