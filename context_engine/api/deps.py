"""HTTP auth and workspace resolution helpers."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from context_engine.api.state import SidecarState
from context_engine.workspace import DEFAULT_WORKSPACE_ID, Workspace


def header_value(value: Any) -> str | None:
    """Normalize FastAPI Header defaults when route functions are called directly in tests."""
    return value if isinstance(value, str) and value.strip() else None


def canonical_user_id(value: str | None) -> str:
    """Canonicalize an explicit user id without creating/updating a user record."""
    return str(value or "").lower().strip()


def extract_bearer_token(authorization: Any = None) -> str | None:
    authorization_value = header_value(authorization)
    if not authorization_value:
        return None
    scheme, _, token = authorization_value.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    return token


def resolve_request_user(
    state: SidecarState,
    x_user_id: Any = None,
    authorization: Any = None,
    *,
    require_auth: bool | None = None,
    trust_client_user_header: bool | None = None,
) -> str:
    """Resolve the request user and optionally require a valid bearer token."""
    config = state.config
    require_auth = require_auth if require_auth is not None else config.auth_required
    trust_header = (
        trust_client_user_header
        if trust_client_user_header is not None
        else config.trust_client_user_header
    )
    token = extract_bearer_token(authorization)
    if token is not None:
        if not state.user_auth.verify_token(token):
            raise HTTPException(status_code=401, detail="Invalid or expired bearer token")
        return state.user_auth.get_user_from_token(token)

    if require_auth:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    if trust_header:
        return state.user_auth.identify_user(header_value(x_user_id))  # type: ignore[union-attr]

    return state.user_auth.identify_user(None)


def resolve_workspace_context(
    state: SidecarState,
    x_workspace: Any = None,
    authorization: Any = None,
    *,
    trust_client_workspace_header: bool | None = None,
) -> Workspace:
    token = extract_bearer_token(authorization)
    token_workspace: str | None = None
    if token is not None:
        if not state.user_auth.verify_token(token):
            raise HTTPException(status_code=401, detail="Invalid or expired bearer token")
        token_workspace = state.user_auth.get_workspace_from_token(token)

    header_workspace = header_value(x_workspace)
    if token_workspace:
        if header_workspace and header_workspace != token_workspace:
            raise HTTPException(
                status_code=403,
                detail="X-Workspace does not match bearer token workspace",
            )
        try:
            return state.workspace_resolver.from_header(token_workspace)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    config = state.config
    trust_workspace_header = (
        trust_client_workspace_header
        if trust_client_workspace_header is not None
        else config.trust_client_workspace_header
    )
    if trust_workspace_header:
        try:
            return state.workspace_resolver.from_header(header_workspace)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        return state.workspace_resolver.from_header(DEFAULT_WORKSPACE_ID)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def resolve_workspace(
    state: SidecarState,
    x_workspace: Any = None,
    authorization: Any = None,
) -> str:
    return resolve_workspace_context(state, x_workspace, authorization).id
