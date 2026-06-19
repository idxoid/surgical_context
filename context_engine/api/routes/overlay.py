"""Overlay buffer routes."""

from __future__ import annotations

from fastapi import APIRouter, Header

from context_engine.api.routes.deps import require_main
from context_engine.api.schemas import ClearOverlayResponse, OverlayRequest, OverlayResponse

router = APIRouter(tags=["overlay"])


@router.post("/overlay", response_model=OverlayResponse)
def update_overlay(
    req: OverlayRequest,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    main = require_main()
    user_id = main._resolve_request_user(x_user_id, authorization)
    workspace_id = main._resolve_workspace(x_workspace, authorization)
    with main.db_session(user_id=user_id) as db:
        safe_path = main._sandbox_path(req.file_path, workspace_id=workspace_id, db=db)
    main.overlay.update(
        safe_path,
        req.content,
        workspace_id=workspace_id,
        user_id=user_id,
        dirty=req.dirty,
    )
    symbols = main.overlay.get_symbols(safe_path, workspace_id=workspace_id, user_id=user_id)
    return {"file_path": safe_path, "symbols": list(symbols.keys())}


@router.delete("/overlay", response_model=ClearOverlayResponse)
def clear_overlay(
    file_path: str,
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
):
    main = require_main()
    user_id = main._resolve_request_user(x_user_id, authorization)
    workspace_id = main._resolve_workspace(x_workspace, authorization)
    with main.db_session(user_id=user_id) as db:
        safe_path = main._sandbox_path(file_path, workspace_id=workspace_id, db=db)
    main.overlay.clear(safe_path, workspace_id=workspace_id, user_id=user_id)
    return {"cleared": safe_path}
