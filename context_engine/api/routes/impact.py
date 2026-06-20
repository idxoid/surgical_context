"""Symbol impact analysis routes."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Request

from context_engine.api.routes.deps import require_main
from context_engine.api.schemas import IMPACT_DEPTH_MAX, IMPACT_DEPTH_MIN, ImpactResponse

router = APIRouter(tags=["impact"])


@router.get("/impact", response_model=ImpactResponse)
def impact(
    symbol: str,
    max_depth: int = Query(default=3, ge=IMPACT_DEPTH_MIN, le=IMPACT_DEPTH_MAX),
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
    request: Request | None = None,
):
    """Return downstream dependents affected by a change to the given symbol."""
    main = require_main(request)
    user_id = main._resolve_request_user(x_user_id, authorization)
    base_workspace_id = main._resolve_workspace(x_workspace, authorization)
    index_workspace_id = main.effective_index_workspace_id(base_workspace_id)
    with main.db_session(user_id=user_id) as db:
        from context_engine.axis.impact_surface import build_impact_surface

        symbol_uid = db.get_symbol_uid_by_name(symbol, workspace_id=index_workspace_id)
        if not symbol_uid:
            raise HTTPException(status_code=404, detail=f"Symbol '{symbol}' not found")

        symbol_file = db.get_file_path_for_symbol(symbol_uid, workspace_id=index_workspace_id)
        surface = build_impact_surface(
            db=db,
            symbol_uid=symbol_uid,
            symbol_name=symbol,
            file_path=symbol_file,
            workspace_id=index_workspace_id,
            max_depth=max_depth,
        )
        affected_symbols = surface["affected_symbols"]
        affected_files = surface["affected_files"]

        return {
            "symbol": symbol,
            "symbol_uid": symbol_uid,
            "file_path": symbol_file,
            "affected_symbols": affected_symbols,
            "affected_files": affected_files,
            "affected_count": len(affected_symbols),
            "affected_file_count": len(affected_files),
            "max_depth": surface["max_depth"],
        }
