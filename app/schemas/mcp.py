from typing import Any

from pydantic import BaseModel, Field


class McpServerCreateRequest(BaseModel):
    name: str
    type: str
    url: str
    headers: dict[str, Any] = Field(default_factory=dict)


class McpServerResponse(BaseModel):
    id: str
    name: str
    type: str
    url: str | None = None
    read_only: bool = False
