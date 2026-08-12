"""Console entry point: `aegis` starts the server.

Host/port/reload are read from the environment (HOST, PORT, RELOAD) so the same
command works in dev and prod.
"""

import os
import socket
import sys

import uvicorn


def _port_available(host: str, port: int) -> bool:
    """True if we can bind host:port right now (i.e. nothing else is on it)."""
    bind_host = "" if host in ("0.0.0.0", "::") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((bind_host, port))
            return True
        except OSError:
            return False


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    reload = os.environ.get("RELOAD") == "1"

    if not _port_available(host, port):
        sys.stderr.write(
            f"\n  Port {port} is already in use. Something is already listening there.\n"
            f"  Fix it by:\n"
            f"    • starting Aegis on a different port:  PORT={port + 1} uv run aegis\n\n"
        )
        raise SystemExit(1)

    uvicorn.run("app.main:create_app", factory=True, host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()
