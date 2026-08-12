from datetime import datetime

from pydantic import BaseModel


class AgentCreateRequest(BaseModel):
    name: str
    model: str
    description: str | None = None
    effort: str | None = None
    system_prompt: str | None = None
    allowed_tools: list[str]
    permission_mode: str = "default"
    mcp_names: list[str] = []
    roster: list[str] = []
    max_cost_usd: float | None = None
    max_iterations: int = 6
    is_admin_only: bool = False
    bypass_permissions: bool = False


class AgentPatchRequest(BaseModel):
    name: str | None = None
    model: str | None = None
    description: str | None = None
    effort: str | None = None
    system_prompt: str | None = None
    allowed_tools: list[str] | None = None
    permission_mode: str | None = None
    mcp_names: list[str] | None = None
    roster: list[str] | None = None
    max_cost_usd: float | None = None
    max_iterations: int | None = None
    is_admin_only: bool | None = None
    bypass_permissions: bool | None = None


class AgentResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    model: str
    effort: str | None = None
    system_prompt: str | None = None
    allowed_tools: list[str]
    permission_mode: str
    mcp_names: list[str]
    roster: list[str]
    max_cost_usd: float | None = None
    max_iterations: int
    is_admin_only: bool
    bypass_permissions: bool = False
    created_at: datetime
    updated_at: datetime
