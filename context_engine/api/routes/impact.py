"""Symbol impact analysis routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request

from context_engine.api.routes.deps import require_main
from context_engine.api.schemas import IMPACT_DEPTH_MAX, IMPACT_DEPTH_MIN, ImpactResponse

router = APIRouter(tags=["impact"])


def _resolve_committed_uid(
    db: Any,
    symbol: str,
    index_workspace_id: str,
    requested_path: str | None,
) -> str | None:
    resolve_uid = getattr(db, "resolve_impact_symbol_uid", None)
    if callable(resolve_uid):
        return resolve_uid(symbol, index_workspace_id, file_path=requested_path) or None
    symbol_uid = None
    if requested_path and hasattr(db, "get_symbol_uid_by_name_in_file"):
        symbol_uid = db.get_symbol_uid_by_name_in_file(
            symbol, requested_path, workspace_id=index_workspace_id
        )
    if not symbol_uid:
        symbol_uid = db.get_symbol_uid_by_name(symbol, workspace_id=index_workspace_id)
    return symbol_uid or None


@router.get("/impact", response_model=ImpactResponse)
def impact(
    symbol: str,
    max_depth: int = Query(default=3, ge=IMPACT_DEPTH_MIN, le=IMPACT_DEPTH_MAX),
    file_path: str | None = Query(default=None),
    x_user_id: str = Header(None),
    authorization: str = Header(None),
    x_workspace: str = Header(None),
    request: Request = None,
):
    """Return downstream dependents affected by a change to the given symbol.

    The committed dependents subgraph is the authoritative surface. When the
    symbol is not yet indexed, or callers were just typed and not saved, the
    response is augmented with degraded ``overlay_caller`` rows parsed from the
    dirty editor buffers and flagged ``degraded: true``.
    """
    from context_engine.axis.impact_surface import (
        MAX_IMPACT_SURFACE_DEPTH,
        build_impact_surface,
    )
    from context_engine.axis.overlay_impact import build_overlay_impact_callers

    main = require_main(request)
    user_id = main._resolve_request_user(x_user_id, authorization)
    base_workspace_id = main._resolve_workspace(x_workspace, authorization)
    index_workspace_id = main.effective_index_workspace_id(base_workspace_id)
    overlay = main.overlay
    requested_path = file_path.strip() if isinstance(file_path, str) and file_path.strip() else None

    with main.db_session(user_id=user_id) as db:
        symbol_uid = _resolve_committed_uid(db, symbol, index_workspace_id, requested_path)

        # Overlay buffers are keyed by the sandboxed path, matching /overlay.
        safe_path: str | None = None
        if requested_path:
            try:
                safe_path = main._sandbox_path(
                    requested_path, workspace_id=base_workspace_id, db=db
                )
            except Exception:
                safe_path = None

        overlay_anchored = (
            bool(safe_path)
            and overlay.has(safe_path, workspace_id=base_workspace_id, user_id=user_id)
            and symbol
            in overlay.get_symbols(safe_path, workspace_id=base_workspace_id, user_id=user_id)
        )

        if not symbol_uid and not overlay_anchored:
            hint = f" in {file_path}" if file_path else ""
            raise HTTPException(status_code=404, detail=f"Symbol '{symbol}' not found{hint}")

        symbol_file = ""
        committed_rows: list[dict[str, Any]] = []
        committed_files: list[str] = []
        walk_depth = max(IMPACT_DEPTH_MIN, min(int(max_depth), MAX_IMPACT_SURFACE_DEPTH))
        if symbol_uid:
            symbol_file = db.get_file_path_for_symbol(symbol_uid, workspace_id=index_workspace_id)
            surface = build_impact_surface(
                db=db,
                symbol_uid=symbol_uid,
                symbol_name=symbol,
                file_path=symbol_file,
                workspace_id=index_workspace_id,
                max_depth=max_depth,
            )
            committed_rows = surface["affected_symbols"]
            committed_files = surface["affected_files"]
            walk_depth = surface["max_depth"]
        elif overlay_anchored:
            symbol_file = safe_path or ""

        overlay_rows = build_overlay_impact_callers(
            overlay,
            symbol_name=symbol,
            workspace_id=base_workspace_id,
            user_id=user_id,
        )
        committed_keys = {(row.get("file_path"), row.get("name")) for row in committed_rows}
        extra_rows = [
            row for row in overlay_rows if (row["file_path"], row["name"]) not in committed_keys
        ]

        affected_symbols = committed_rows + extra_rows
        affected_files = sorted(
            set(committed_files) | {row["file_path"] for row in extra_rows if row.get("file_path")}
        )
        degraded = not symbol_uid or bool(extra_rows)
        result_uid = symbol_uid or (
            f"overlay::{base_workspace_id}::{symbol_file}::{symbol}" if symbol_file else ""
        )

        return {
            "symbol": symbol,
            "symbol_uid": result_uid,
            "file_path": symbol_file,
            "affected_symbols": affected_symbols,
            "affected_files": affected_files,
            "affected_count": len(affected_symbols),
            "affected_file_count": len(affected_files),
            "max_depth": walk_depth,
            "degraded": degraded,
        }
