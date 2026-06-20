"""Auth, cloud status, and audit routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, Request

from context_engine.api.routes.deps import require_main
from context_engine.api.schemas import (
    AuditActionsResponse,
    AuthTokenResponse,
    CloudStatusResponse,
    UsersResponse,
)
from context_engine.workspace import DEFAULT_WORKSPACE_ID

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


@router.post("/auth/token", response_model=AuthTokenResponse)
def auth_token(
    user_id: str = None,  # type: ignore
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
    request: Request | None = None,
):
    """Generate a signed token scoped to a workspace.

    Bootstrap endpoint for the VS Code extension. When AUTH_REQUIRED=true an
    existing bearer token is required, and callers may only mint a replacement
    token for themselves. X-User-Id is never trusted for identity; workspace
    scope is taken from X-Workspace (or DEFAULT_WORKSPACE_ID).
    """
    main = require_main(request)
    workspace_id = main._header_value(x_workspace) or DEFAULT_WORKSPACE_ID
    try:
        main.workspace_resolver.from_header(workspace_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if main.AUTH_REQUIRED:
        requester = main._resolve_request_user(x_user_id, authorization, require_auth=True)
        requested_user = main._canonical_user_id(user_id) or requester
        if requested_user != requester:
            raise HTTPException(status_code=403, detail="Cannot issue token for another user")
        token_user = requester
    else:
        requested = main._canonical_user_id(user_id)
        if main.TRUST_CLIENT_USER_HEADER:
            token_user = main.user_auth.identify_user(requested or main._header_value(x_user_id))
        else:
            token_user = main.user_auth.identify_user(requested or None)

    token = main.user_auth.generate_token(token_user, workspace_id=workspace_id)
    logger.info("Token issued for user=%s workspace=%s", token_user, workspace_id)
    return {"token": token, "user_id": token_user, "expires_in_hours": 24}


@router.get("/auth/users", response_model=UsersResponse)
def list_users(
    request: Request | None = None,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
):
    """List all active users (requires a valid bearer token)."""
    main = require_main(request)
    main._resolve_request_user(x_user_id, authorization, require_auth=True)
    return {"users": main.user_auth.list_users()}


@router.get("/status/cloud", response_model=CloudStatusResponse)
def cloud_status(
    request: Request | None = None,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
):
    """Get cloud (Aura) connection status."""
    main = require_main(request)
    user_id = main._resolve_request_user(x_user_id, authorization)
    with main.db_session(user_id=user_id) as db:
        health = db.health_check()
        return {
            "cloud_enabled": True,
            "using_aura": db.is_cloud(),
            "using_fallback": db.is_fallback(),
            "health": health,
        }


@router.get("/audit/actions", response_model=AuditActionsResponse)
def audit_actions(
    user_id: str = None,  # type: ignore
    limit: int = 100,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    request: Request | None = None,
):
    """Get recent audit log entries."""
    main = require_main(request)
    requester = main._resolve_request_user(x_user_id, authorization)
    requested_user = main._canonical_user_id(user_id)
    if requested_user and requested_user != requester:
        raise HTTPException(status_code=403, detail="Cannot read audit actions for another user")
    actions = main.audit_log.get_recent_actions(user_id=requester, limit=limit)
    return {"actions": actions, "total": len(actions)}
