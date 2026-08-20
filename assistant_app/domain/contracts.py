from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class SideEffect(StrEnum):
    NONE = "none"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class ConfirmationPolicy(StrEnum):
    NEVER = "never"
    POLICY = "policy"
    ALWAYS = "always"


class SourceReference(BaseModel):
    label: str
    uri: str | None = None
    observed_at: datetime | None = None


class ToolDefinition(BaseModel):
    name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    permissions: set[str] = Field(default_factory=set)
    side_effect: SideEffect = SideEffect.NONE
    confirmation: ConfirmationPolicy = ConfirmationPolicy.NEVER
    timeout_seconds: float = Field(default=10, gt=0, le=300)
    cache_ttl_seconds: int = Field(default=0, ge=0)


class ToolContext(BaseModel):
    user_id: str
    thread_id: str
    run_id: str
    granted_permissions: set[str] = Field(default_factory=set)


class ToolResult(BaseModel):
    status: str
    data: dict[str, Any] = Field(default_factory=dict)
    sources: list[SourceReference] = Field(default_factory=list)
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)
    trace_id: str | None = None


@runtime_checkable
class Tool(Protocol):
    definition: ToolDefinition

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult: ...


class ModelMessage(BaseModel):
    role: str
    content: str


class ModelRequest(BaseModel):
    messages: list[ModelMessage]
    tools: list[ToolDefinition] = Field(default_factory=list)
    temperature: float = Field(default=0.2, ge=0, le=2)


class ModelEvent(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class ModelGateway(Protocol):
    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...


class RetrievalHit(BaseModel):
    content: str
    score: float
    source: SourceReference
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class KnowledgeRetriever(Protocol):
    async def retrieve(
        self, query: str, *, user_id: str, limit: int = 10
    ) -> list[RetrievalHit]: ...


class TransportOffer(BaseModel):
    provider: str
    offer_id: str
    origin: str
    destination: str
    departure_at: datetime
    arrival_at: datetime
    price_amount: float | None = None
    currency: str | None = None
    deep_link: str | None = None
    observed_at: datetime
    expires_at: datetime | None = None


@runtime_checkable
class TransportSearchProvider(Protocol):
    async def search_offers(
        self,
        *,
        origin: str,
        destination: str,
        departure_date: str,
        passengers: int = 1,
        preferences: dict[str, Any] | None = None,
    ) -> list[TransportOffer]: ...
