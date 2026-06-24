"""Degraded impact callers parsed from the live editor overlay.

Impact's real value is the committed dependents subgraph in Neo4j. A
brand-new symbol has no such graph, and an edited one may have callers that
were just typed and not yet indexed. This module recovers what it can from
the dirty editor buffers: symbols that *call* the target, parsed in-process.

It is additive and degraded by construction:
  * bounded to currently-open dirty buffers (not the whole repository);
  * name-based call resolution (homonym risk), no scope/import analysis;
  * never the complete blast radius.

Every row it produces carries ``degraded: True`` so the caller (and the UI)
treats it as a partial, local-only augmentation of the committed surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from context_engine.overlay import InMemoryOverlay


def _enclosing_symbol(
    symbols: dict[str, tuple[int, int]],
    line: int,
) -> tuple[str, tuple[int, int]] | None:
    """The innermost symbol whose span contains ``line`` (the caller)."""
    best: tuple[str, tuple[int, int]] | None = None
    for name, span in symbols.items():
        start, end = span
        if start <= line <= end:
            if best is None or (span[1] - span[0]) < (best[1][1] - best[1][0]):
                best = (name, span)
    return best


def _overlay_caller_row(
    name: str,
    file_path: str,
    start: int,
    end: int,
    workspace_id: str,
) -> dict[str, Any]:
    return {
        "uid": f"overlay::{workspace_id}::{file_path}::{name}",
        "name": name,
        "symbol": name,
        "file_path": file_path,
        "depth": 1,
        "edge_type": "CALLS_*",
        "kind": "overlay_caller",
        "role": "direct_consumer",
        "zone": "direct",
        "severity": "high",
        "utility_score": 0.5,
        "relevance_score": 0.5,
        "satisfying_kinds": ["overlay_caller"],
        "degraded": True,
        "start_line": start,
        "end_line": end,
    }


def build_overlay_impact_callers(
    overlay: InMemoryOverlay | None,
    *,
    symbol_name: str,
    workspace_id: str,
    user_id: str,
    max_items: int = 50,
) -> list[dict[str, Any]]:
    """Callers of ``symbol_name`` found in the dirty editor buffers.

    Scans each unsaved buffer for calls whose callee name matches the target,
    maps each call site to its enclosing symbol, and returns one degraded row
    per distinct (file, caller). Recursive self-calls are skipped.
    """
    if overlay is None or not symbol_name:
        return []

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for file_path in overlay.iter_dirty_files(workspace_id=workspace_id, user_id=user_id):
        try:
            calls = overlay.get_calls(file_path, workspace_id=workspace_id, user_id=user_id)
        except Exception:
            continue
        hit_lines = sorted(
            {
                int(call["call_site_line"])
                for call in calls
                if call.get("callee_name") == symbol_name and call.get("call_site_line")
            }
        )
        if not hit_lines:
            continue
        try:
            symbols = overlay.get_symbols(file_path, workspace_id=workspace_id, user_id=user_id)
        except Exception:
            continue
        for line in hit_lines:
            caller = _enclosing_symbol(symbols, line)
            if caller is None:
                continue
            name, (start, end) = caller
            if name == symbol_name:
                # Recursive call inside the target's own body — not a dependent.
                continue
            key = (file_path, name)
            if key in seen:
                continue
            seen.add(key)
            rows.append(_overlay_caller_row(name, file_path, start, end, workspace_id))
            if len(rows) >= max_items:
                return rows
    return rows


__all__ = ["build_overlay_impact_callers"]
