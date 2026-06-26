"""Overlay-aware code resolution for the axis context builder."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

from context_engine.axis.context_builder import ContextBundle, ContextSymbol

if TYPE_CHECKING:
    from context_engine.overlay import InMemoryOverlay


def _overlay_code_for_symbol(
    overlay: InMemoryOverlay,
    *,
    file_path: str,
    name: str,
    workspace_id: str,
    user_id: str,
) -> str | None:
    if not file_path or not overlay.has(file_path, workspace_id=workspace_id, user_id=user_id):
        return None
    symbols = overlay.get_symbols(file_path, workspace_id=workspace_id, user_id=user_id)
    span = symbols.get(name)
    if span is None:
        return None
    start, end = span
    return overlay.read_lines(file_path, start, end, workspace_id=workspace_id, user_id=user_id)


def _symbol_name_from_payload(payload: dict[str, str | None]) -> str:
    qualified = str(payload.get("qualified_name") or "")
    if qualified:
        return qualified.rsplit(".", 1)[-1]
    return str(payload.get("name") or "")


def merge_saved_overlay_payloads(
    payloads: dict[str, dict[str, str | None]],
    *,
    overlay: InMemoryOverlay | None,
    workspace_id: str,
    user_id: str,
) -> dict[str, dict[str, str | None]]:
    """Replace Lance code with saved (non-dirty) overlay bodies at fetch time."""
    if overlay is None or not payloads:
        return payloads
    out = dict(payloads)
    for uid, row in payloads.items():
        file_path = str(row.get("file_path") or "")
        if not file_path or overlay.is_dirty(file_path, workspace_id=workspace_id, user_id=user_id):
            continue
        name = _symbol_name_from_payload(row)
        if not name:
            continue
        code = _overlay_code_for_symbol(
            overlay,
            file_path=file_path,
            name=name,
            workspace_id=workspace_id,
            user_id=user_id,
        )
        if code is None:
            continue
        patched = dict(row)
        patched["code"] = code
        out[uid] = patched
    return out


def apply_dirty_overlay_to_bundles(
    bundles: list[ContextBundle],
    *,
    overlay: InMemoryOverlay | None,
    workspace_id: str,
    user_id: str,
) -> list[ContextBundle]:
    """Pre-budget pass: unsaved editor buffers win over Lance/committed code."""
    if overlay is None:
        return bundles

    def _patch(sym: ContextSymbol) -> ContextSymbol:
        if not sym.file_path or not overlay.is_dirty(
            sym.file_path, workspace_id=workspace_id, user_id=user_id
        ):
            return sym
        code = _overlay_code_for_symbol(
            overlay,
            file_path=sym.file_path,
            name=sym.name,
            workspace_id=workspace_id,
            user_id=user_id,
        )
        if code is None:
            return sym
        return cast(ContextSymbol, replace(sym, code=code))

    patched: list[ContextBundle] = []
    for bundle in bundles:
        seed = _patch(bundle.seed)
        related = tuple(_patch(sym) for sym in bundle.related)
        if seed == bundle.seed and related == bundle.related:
            patched.append(bundle)
        else:
            patched.append(replace(bundle, seed=seed, related=related))
    return patched


__all__ = [
    "apply_dirty_overlay_to_bundles",
    "merge_saved_overlay_payloads",
]
