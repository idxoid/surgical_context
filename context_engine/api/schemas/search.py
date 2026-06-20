"""Search request/response models."""

from typing import Any

from pydantic import BaseModel, Field

from context_engine.api.schemas.common import (
    SEARCH_LIMIT_MAX,
    SEARCH_LIMIT_MIN,
    TOKEN_BUDGET_MAX,
    TOKEN_BUDGET_MIN,
)


class SearchRequest(BaseModel):
    query: str
    limit: int = Field(default=5, ge=SEARCH_LIMIT_MIN, le=SEARCH_LIMIT_MAX)


class UnifiedSearchRequest(SearchRequest):
    symbol: str | None = None
    include_graph: bool = True
    token_budget: int = Field(default=2000, ge=TOKEN_BUDGET_MIN, le=TOKEN_BUDGET_MAX)


class SearchResponse(BaseModel):
    results: list[dict[str, Any]]


class UnifiedSearchResponse(BaseModel):
    trace_id: str
    workspace_id: str
    results: list[dict[str, Any]]
    total: int
    index_manifest_id: str | None = None
    index_manifest_schema_version: int | None = None
