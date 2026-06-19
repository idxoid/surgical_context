"""Shared API validation bounds and generic response models."""

from pydantic import BaseModel

SEARCH_LIMIT_MIN = 1
SEARCH_LIMIT_MAX = 50
TOKEN_BUDGET_MIN = 400
TOKEN_BUDGET_MAX = 32_000
IMPACT_DEPTH_MIN = 1
IMPACT_DEPTH_MAX = 4


class HealthResponse(BaseModel):
    status: str
