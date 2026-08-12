from datetime import datetime

from pydantic import BaseModel


class TenantCreateRequest(BaseModel):
    name: str


class TenantPatchRequest(BaseModel):
    # None or "" clears the tenant default (falls back to the global default).
    default_model: str | None = None


class TenantResponse(BaseModel):
    id: str
    name: str
    default_model: str | None = None


class KeyCreateRequest(BaseModel):
    tenant_id: str
    name: str
    rpm: int | None = None  # None = unlimited requests/minute
    daily_cost_usd: float | None = None  # None = unlimited daily cost
    is_admin: bool = False


class KeyCreateResponse(BaseModel):
    api_key: str
    prefix: str
    id: str


class KeyPatchRequest(BaseModel):
    rpm: int | None = None
    daily_cost_usd: float | None = None


class KeySummary(BaseModel):
    id: str
    prefix: str
    tenant_id: str
    name: str
    rpm: int | None
    daily_cost_usd: float | None
    is_admin: bool
    revoked_at: datetime | None
    created_at: datetime


class BillingConfigRequest(BaseModel):
    price_per_mtok_input: float
    price_per_mtok_output: float
    markup: float


class BillingConfigResponse(BaseModel):
    tenant_id: str
    price_per_mtok_input: float
    price_per_mtok_output: float
    markup: float


class WebhookConfigRequest(BaseModel):
    url: str
    secret: str


class WebhookConfigResponse(BaseModel):
    tenant_id: str
    url: str


class AdminUsageRow(BaseModel):
    tenant_id: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    runs: int
    priced_cost_usd: float | None
