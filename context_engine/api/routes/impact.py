"""Symbol impact analysis routes."""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, HTTPException, Query, Request

from context_engine.api.routes.deps import (
    AuthHeader,
    UserIdHeader,
    WorkspaceHeader,
    require_main,
)
from context_engine.api.schemas import IMPACT_DEPTH_MAX, IMPACT_DEPTH_MIN, ImpactResponse

router = APIRouter(tags=["impact"])

MaxDepthQuery = Annotated[
    int,
    Query(ge=IMPACT_DEPTH_MIN, le=IMPACT_DEPTH_MAX),
]
FilePathQuery = Annotated[str | None, Query()]


def _symbol_in_local_file(file_path: str, symbol: str) -> bool:
    """True when ``symbol`` is defined in the on-disk ``file_path``."""
    if not file_path or not symbol:
        return False
    try:
        from context_engine.parser.extractor import SymbolExtractor

        return any(meta.name == symbol for meta in SymbolExtractor().extract(file_path))
    except (OSError, ValueError):
        return False


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


def _normalize_requested_path(file_path: str | None) -> str | None:
    if isinstance(file_path, str) and file_path.strip():
        return file_path.strip()
    return None


def _resolve_symbol_uid_across_workspaces(
    db: Any,
    main: Any,
    symbol: str,
    base_workspace_id: str,
    requested_path: str | None,
) -> tuple[str | None, str]:
    from context_engine.index_profile import index_workspace_lookup_order

    index_workspace_id = main.effective_index_workspace_id(base_workspace_id)
    for candidate_ws in index_workspace_lookup_order(base_workspace_id):
        uid = _resolve_committed_uid(db, symbol, candidate_ws, requested_path)
        if uid:
            return uid, candidate_ws
    return None, index_workspace_id


def _safe_sandbox_path(
    main: Any,
    requested_path: str | None,
    *,
    base_workspace_id: str,
    db: Any,
) -> str | None:
    if not requested_path:
        return None
    try:
        return cast(
            "str | None", main._sandbox_path(requested_path, workspace_id=base_workspace_id, db=db)
        )
    except Exception:
        return None


def _overlay_symbol_names(
    overlay: Any,
    safe_path: str | None,
    *,
    base_workspace_id: str,
    user_id: str,
) -> set[str]:
    if not safe_path or overlay is None:
        return set()
    if not overlay.has(safe_path, workspace_id=base_workspace_id, user_id=user_id):
        return set()
    return set(overlay.get_symbols(safe_path, workspace_id=base_workspace_id, user_id=user_id))


def _is_overlay_anchored(
    safe_path: str | None,
    symbol: str,
    overlay_symbols: set[str],
) -> bool:
    if not safe_path:
        return False
    return symbol in overlay_symbols or _symbol_in_local_file(safe_path, symbol)


def _build_committed_surface(
    db: Any,
    *,
    symbol_uid: str,
    symbol: str,
    index_workspace_id: str,
    max_depth: int,
) -> tuple[str, list[dict[str, Any]], list[str], int]:
    from context_engine.axis.impact_surface import build_impact_surface

    symbol_file = db.get_file_path_for_symbol(symbol_uid, workspace_id=index_workspace_id)
    surface = build_impact_surface(
        db=db,
        symbol_uid=symbol_uid,
        symbol_name=symbol,
        file_path=symbol_file,
        workspace_id=index_workspace_id,
        max_depth=max_depth,
    )
    return (
        symbol_file,
        surface["affected_symbols"],
        surface["affected_files"],
        surface["max_depth"],
    )


def _dedupe_overlay_callers(
    committed_rows: list[dict[str, Any]],
    overlay_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    committed_keys = {(row.get("file_path"), row.get("name")) for row in committed_rows}
    return [row for row in overlay_rows if (row["file_path"], row["name"]) not in committed_keys]


def _collect_affected_files(
    committed_files: list[str],
    extra_rows: list[dict[str, Any]],
) -> list[str]:
    overlay_files = {row["file_path"] for row in extra_rows if row.get("file_path")}
    return sorted(set(committed_files) | overlay_files)


def _impact_result_uid(
    symbol_uid: str | None,
    *,
    base_workspace_id: str,
    symbol_file: str,
    symbol: str,
) -> str:
    if symbol_uid:
        return symbol_uid
    if symbol_file:
        return f"overlay::{base_workspace_id}::{symbol_file}::{symbol}"
    return ""


@router.get(
    "/impact",
    response_model=ImpactResponse,
    responses={
        404: {"description": "Symbol not found in the workspace or overlay"},
    },
)
def impact(
    symbol: str,
    max_depth: MaxDepthQuery = 3,
    file_path: FilePathQuery = None,
    x_user_id: UserIdHeader = None,
    authorization: AuthHeader = None,
    x_workspace: WorkspaceHeader = None,
    request: Request = None,
):
    """Return downstream dependents affected by a change to the given symbol.

    The committed dependents subgraph is the authoritative surface. When the
    symbol is not yet indexed, or callers were just typed and not saved, the
    response is augmented with degraded ``overlay_caller`` rows parsed from the
    dirty editor buffers and flagged ``degraded: true``.
    """
    from context_engine.axis.impact_surface import MAX_IMPACT_SURFACE_DEPTH
    from context_engine.axis.overlay_impact import build_overlay_impact_callers

    main = require_main(request)
    user_id = main._resolve_request_user(x_user_id, authorization)
    base_workspace_id = main._resolve_workspace(x_workspace, authorization)
    requested_path = _normalize_requested_path(file_path)

    with main.db_session(user_id=user_id) as db:
        symbol_uid, index_workspace_id = _resolve_symbol_uid_across_workspaces(
            db, main, symbol, base_workspace_id, requested_path
        )
        safe_path = _safe_sandbox_path(
            main, requested_path, base_workspace_id=base_workspace_id, db=db
        )
        overlay_symbols = _overlay_symbol_names(
            main.overlay, safe_path, base_workspace_id=base_workspace_id, user_id=user_id
        )
        overlay_anchored = _is_overlay_anchored(safe_path, symbol, overlay_symbols)

        if not symbol_uid and not overlay_anchored:
            hint = f" in {file_path}" if file_path else ""
            raise HTTPException(status_code=404, detail=f"Symbol '{symbol}' not found{hint}")

        symbol_file = ""
        committed_rows: list[dict[str, Any]] = []
        committed_files: list[str] = []
        walk_depth = max(IMPACT_DEPTH_MIN, min(int(max_depth), MAX_IMPACT_SURFACE_DEPTH))
        if symbol_uid:
            symbol_file, committed_rows, committed_files, walk_depth = _build_committed_surface(
                db,
                symbol_uid=symbol_uid,
                symbol=symbol,
                index_workspace_id=index_workspace_id,
                max_depth=max_depth,
            )
        elif overlay_anchored:
            symbol_file = safe_path or ""

        overlay_rows = build_overlay_impact_callers(
            main.overlay,
            symbol_name=symbol,
            workspace_id=base_workspace_id,
            user_id=user_id,
        )
        extra_rows = _dedupe_overlay_callers(committed_rows, overlay_rows)
        affected_symbols = committed_rows + extra_rows
        affected_files = _collect_affected_files(committed_files, extra_rows)
        degraded = not symbol_uid or bool(extra_rows)
        result_uid = _impact_result_uid(
            symbol_uid,
            base_workspace_id=base_workspace_id,
            symbol_file=symbol_file,
            symbol=symbol,
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
