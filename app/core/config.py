import os
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite+aiosqlite:///./data/app.db"
    workspace_root: str = "~/.aegis/workspaces"
    admin_password: str = "changeme"
    session_secret: str = "changeme-secret"
    max_concurrent_runs: int = 3
    run_timeout_s: int = 1800
    default_rpm: int = 30
    default_daily_cost_usd: float = 10.0
    workspace_quota_mb: int = 512
    workspace_ttl_days: int = 7
    # Fallback model for the OpenAI-compat endpoint when a request omits `model`.
    default_model: str = "claude-sonnet-5"
    # Apply Alembic migrations on startup (the test suite builds via create_all).
    run_migrations_on_startup: bool = True
    # Fetch the model catalog live from Anthropic (off in tests).
    models_live_fetch: bool = True

    @field_validator("workspace_root")
    @classmethod
    def _absolutize_workspace_root(cls, v: str) -> str:
        return os.path.abspath(os.path.expanduser(v))


@lru_cache
def get_settings() -> Settings:
    return Settings()
