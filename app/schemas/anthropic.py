"""DTOs for the Anthropic Messages API shim (`app.api.compat.anthropic`)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class AnthropicTool(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any] | None = None
    type: str | None = None


class AnthropicMessage(BaseModel):
    role: str
    content: str | list[dict[str, Any]]


class MessagesRequest(BaseModel):
    model: str | None = None
    max_tokens: int | None = None
    messages: list[AnthropicMessage]
    system: str | list[dict[str, Any]] | None = None
    tools: list[AnthropicTool] | None = None
    tool_choice: dict[str, Any] | None = None
    stream: bool = False
    output_config: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class CountTokensRequest(BaseModel):
    model: str | None = None
    messages: list[AnthropicMessage]
    system: str | list[dict[str, Any]] | None = None
    tools: list[AnthropicTool] | None = None
