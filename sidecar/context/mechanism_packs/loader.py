"""Load YAML mechanism packs from env paths and optional bundled defaults.

Role names in YAML must match Pass 1 taxonomy strings (e.g. ``api_surface``), not numeric cluster ids.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Keep in sync with ``mechanism_registry`` / persisted ``role_catalog_json``.
ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY = "mechanism_required_roles"
ROLE_CATALOG_MECHANISM_BACKFILL_KEY = "mechanism_role_backfill"

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore


_BUNDLED_DEFAULT = Path(__file__).resolve().parent / "bundled" / "default.yaml"


def _parse_pack_paths() -> list[Path]:
    raw = os.getenv("MECHANISM_PACK_PATH", "").strip()
    paths: list[Path] = []
    if raw:
        for part in raw.split(os.pathsep):
            p = Path(part).expanduser()
            if p.is_file():
                paths.append(p)
    if _BUNDLED_DEFAULT.is_file():
        paths.append(_BUNDLED_DEFAULT)
    return paths


def _normalize_document(raw: Any) -> dict[str, Any]:
    """Ensure canonical keys and dict-shaped subtrees."""
    if raw is None:
        return {
            ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY: {},
            ROLE_CATALOG_MECHANISM_BACKFILL_KEY: {},
        }
    if not isinstance(raw, dict):
        return {
            ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY: {},
            ROLE_CATALOG_MECHANISM_BACKFILL_KEY: {},
        }
    req = raw.get(ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY, {})
    bf = raw.get(ROLE_CATALOG_MECHANISM_BACKFILL_KEY, {})
    if req is None or not isinstance(req, dict):
        req = {}
    if bf is None or not isinstance(bf, dict):
        bf = {}
    return {
        ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY: req,
        ROLE_CATALOG_MECHANISM_BACKFILL_KEY: bf,
    }


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if yaml is None:
        return _normalize_document(None)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return _normalize_document(None)
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        return _normalize_document(None)
    return _normalize_document(raw)


def _merge_required_roles(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, list[str]]:
    keys = set(base) | set(overlay)
    out: dict[str, list[str]] = {}
    for k in keys:
        a = base.get(k, [])
        b = overlay.get(k, [])
        if not isinstance(a, list):
            a = []
        if not isinstance(b, list):
            b = []
        merged: list[str] = []
        seen: set[str] = set()
        for item in list(a) + list(b):
            s = str(item).strip()
            if s and s not in seen:
                seen.add(s)
                merged.append(s)
        if merged:
            out[str(k)] = merged
    return out


def _merge_backfill(
    base: dict[str, Any],
    overlay: dict[str, Any],
) -> dict[str, dict[str, list[dict[str, str | float]]]]:
    keys = set(base) | set(overlay)
    out: dict[str, dict[str, list[dict[str, str | float]]]] = {}
    for mech in keys:
        br = base.get(mech, {})
        ov = overlay.get(mech, {})
        if not isinstance(br, dict):
            br = {}
        if not isinstance(ov, dict):
            ov = {}
        role_keys = set(br) | set(ov)
        merged_m: dict[str, list[dict[str, str | float]]] = {}
        for rk in role_keys:
            a = br.get(rk, [])
            b = ov.get(rk, [])
            if not isinstance(a, list):
                a = []
            if not isinstance(b, list):
                b = []
            rows: list[dict[str, str | float]] = []
            seen_keys: set[tuple[str, str]] = set()
            for seq in (a, b):
                for item in seq:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name", "")).strip()
                    ph = str(item.get("path_hint", "") or "")
                    key = (name, ph)
                    if not name or key in seen_keys:
                        continue
                    seen_keys.add(key)
                    row: dict[str, str | float] = {"name": name}
                    if ph:
                        row["path_hint"] = ph
                    if item.get("priority") is not None:
                        try:
                            row["priority"] = float(item["priority"])
                        except (TypeError, ValueError):
                            row["priority"] = 1.0
                    rows.append(row)
            if rows:
                merged_m[str(rk)] = rows
        if merged_m:
            out[str(mech)] = merged_m
    return out


def _merge_pack_documents(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    ra = a.get(ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY, {})
    rb = b.get(ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY, {})
    ba = a.get(ROLE_CATALOG_MECHANISM_BACKFILL_KEY, {})
    bb = b.get(ROLE_CATALOG_MECHANISM_BACKFILL_KEY, {})
    if not isinstance(ra, dict):
        ra = {}
    if not isinstance(rb, dict):
        rb = {}
    if not isinstance(ba, dict):
        ba = {}
    if not isinstance(bb, dict):
        bb = {}
    return {
        ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY: _merge_required_roles(ra, rb),
        ROLE_CATALOG_MECHANISM_BACKFILL_KEY: _merge_backfill(ba, bb),
    }


def load_pack_overlay_merged() -> dict[str, Any]:
    """Merge all configured YAML packs into one overlay (relative to builtins)."""
    paths = _parse_pack_paths()
    merged = _normalize_document(None)
    for path in paths:
        chunk = _load_yaml_file(path)
        merged = _merge_pack_documents(merged, chunk)
    return merged


def merge_into_base_extensions(base: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``base`` preloaded extensions with YAML packs (used by mechanism_registry)."""
    overlay = load_pack_overlay_merged()
    return _merge_pack_documents(base, overlay)
