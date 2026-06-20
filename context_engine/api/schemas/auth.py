"""Auth, admin, and audit response models."""

from typing import Any

from pydantic import BaseModel


class AuthTokenResponse(BaseModel):
    token: str
    user_id: str
    expires_in_hours: int


class UsersResponse(BaseModel):
    users: list[dict[str, Any]]


class CloudStatusResponse(BaseModel):
    cloud_enabled: bool
    using_aura: bool
    using_fallback: bool
    health: dict[str, Any]


class AuditActionsResponse(BaseModel):
    actions: list[dict[str, Any]]
    total: int
