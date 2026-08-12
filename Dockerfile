# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

# Tools the bundled Claude CLI and its built-in tools use at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ripgrep curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies only (no project build) for layer caching. This pulls
# claude-agent-sdk, which ships the bundled Claude CLI, so there is no separate
# CLI install. The app runs from the source tree via PYTHONPATH.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .

# Expose the SDK's bundled CLI as `claude` so `docker exec -it aegis claude auth
# login` works for the one-time subscription sign-in.
RUN ln -sf "$(find /app/.venv -path '*/claude_agent_sdk/_bundled/claude' | head -n1)" \
      /usr/local/bin/claude || true \
    && mkdir -p /data/workspaces

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app \
    WORKSPACE_ROOT=/data/workspaces \
    DATABASE_URL=sqlite+aiosqlite:////data/aegis.db \
    RUN_MIGRATIONS_ON_STARTUP=true

EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
