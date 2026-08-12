from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class MessageSendRequest(BaseModel):
    prompt: str
    stream: bool = True
    mode: str = "sync"  # "sync" | "async"


class MessageResult(BaseModel):
    """Shape of the terminal `result` event, returned as the JSON body for
    blocking (`stream=false`) sends."""

    type: str
    subtype: str | None = None
    is_error: bool = False
    result: str | None = None
    session_id: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0


class EventOut(BaseModel):
    seq: int
    type: str
    payload: dict[str, Any]
    created_at: datetime
