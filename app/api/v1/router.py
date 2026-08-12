from fastapi import APIRouter

from app.api.v1.files import router as files_router
from app.api.v1.jobs import router as jobs_router
from app.api.v1.mcp_servers import router as mcp_servers_router
from app.api.v1.messages import router as messages_router
from app.api.v1.objectives import router as objectives_router
from app.api.v1.sessions import router as sessions_router
from app.api.v1.usage import router as usage_router

v1_router = APIRouter()
v1_router.include_router(sessions_router)
v1_router.include_router(messages_router)
v1_router.include_router(files_router)
v1_router.include_router(mcp_servers_router)
v1_router.include_router(usage_router)
v1_router.include_router(jobs_router)
v1_router.include_router(objectives_router)

__all__ = ["v1_router"]
