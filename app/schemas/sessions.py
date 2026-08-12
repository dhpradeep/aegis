from datetime import datetime

from pydantic import BaseModel


class SessionCreateRequest(BaseModel):
    agent: str | None = None
    title: str | None = None


class SessionCreateResponse(BaseModel):
    session_id: str
    profile: str
    status: str
    created_at: datetime


class SessionSummary(BaseModel):
    session_id: str
    profile: str
    status: str
    title: str | None
    created_at: datetime
    updated_at: datetime


class UsageTotals(BaseModel):
    cost_usd: float
    input_tokens: int
    output_tokens: int
    num_runs: int


class SessionDetail(BaseModel):
    session_id: str
    profile: str
    agent_id: str | None
    status: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    usage_totals: UsageTotals


class SessionDeleteResponse(BaseModel):
    status: str
