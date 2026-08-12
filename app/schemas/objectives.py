from datetime import datetime

from pydantic import BaseModel


class ObjectiveCreateRequest(BaseModel):
    agent: str
    goal: str
    rubric: str
    max_cost_usd: float | None = None
    max_iterations: int | None = None


class ObjectiveSubmitted(BaseModel):
    """Returned with 202 from `POST /v1/objectives`."""

    objective_id: str
    status: str


class ObjectiveSummary(BaseModel):
    objective_id: str
    agent_id: str
    goal: str
    status: str
    iterations_done: int
    cost_usd: float
    created_at: datetime
    finished_at: datetime | None = None


class ObjectiveDetail(BaseModel):
    objective_id: str
    agent_id: str
    goal: str
    rubric: str
    status: str
    max_cost_usd: float | None = None
    max_iterations: int
    iterations_done: int
    cost_usd: float
    result_text: str | None = None
    session_id: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
