from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.security import hash_key
from app.db.base import get_db
from app.db.models import ApiKey

__all__ = ["get_db", "require_key", "require_admin_key", "bearer_scheme"]

# Declaring this HTTPBearer scheme registers "bearerAuth" in the OpenAPI spec,
# which makes the Swagger UI "Authorize" button appear. auto_error=False so we
# raise our own uniform ApiError envelope instead of FastAPI's default 403.
bearer_scheme = HTTPBearer(
    scheme_name="ApiKey",
    description="Your tenant API key (starts with `cak_`). Sent as `Authorization: Bearer <key>`.",
    auto_error=False,
)


async def require_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> ApiKey:
    if credentials is None or not credentials.credentials:
        raise ApiError.auth()
    token = credentials.credentials
    row = (
        await db.execute(
            select(ApiKey).where(
                ApiKey.key_hash == hash_key(token), ApiKey.revoked_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError.auth()
    request.state.api_key = row
    request.state.tenant_id = row.tenant_id
    return row


async def require_admin_key(key: ApiKey = Depends(require_key)) -> ApiKey:
    if not key.is_admin:
        raise ApiError.forbidden()
    return key
