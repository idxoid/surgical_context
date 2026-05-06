"""Mechanism profiles — structural / workspace-driven path only.

Framework-specific dispatch tables for FastAPI, Pydantic, and Redux Toolkit were
removed (stubbed). Named mechanisms and role plans now come from persisted
``role_catalog_json`` (``mechanism_required_roles`` / ``mechanism_role_backfill``)
or future mining — not from bundled hardcoded rules.

``determine_preloaded_mechanism`` is intentionally inert (always ``""``).
``pick_mechanism_by_role_overlap`` scores only mechanisms listed in the catalog
(or any future built-ins added back without framework literals).

See ``docs/spec_indexer.md`` Pass 1 / catalog merge.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from sidecar.context.role_taxonomy import normalize_roles
from sidecar.context.types import SubgraphNode

ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY = "mechanism_required_roles"
ROLE_CATALOG_MECHANISM_BACKFILL_KEY = "mechanism_role_backfill"

_REQUIRED_ROLES: dict[str, tuple[str, ...]] = {}

_ROLE_BACKFILL_SPECS: dict[str, dict[str, list[dict[str, str | float]]]] = {}


def _roles_from_role_catalog_override(
    mechanism: str,
    role_catalog: Mapping[str, Any] | None,
) -> list[str] | None:
    """Return normalized roles if ``role_catalog`` overrides this mechanism."""
    if not role_catalog:
        return None
    raw = role_catalog.get(ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY)
    if not isinstance(raw, dict):
        return None
    entry = raw.get(mechanism)
    if entry is None:
        return None
    if not isinstance(entry, (list, tuple)):
        return None
    return normalize_roles([str(x) for x in entry])


def _coerce_backfill_spec(spec: Mapping[str, Any]) -> dict[str, str | float] | None:
    name = spec.get("name")
    if name is None or str(name).strip() == "":
        return None
    row: dict[str, str | float] = {"name": str(name)}
    if spec.get("path_hint") is not None:
        row["path_hint"] = str(spec["path_hint"])
    if spec.get("priority") is not None:
        try:
            row["priority"] = float(spec["priority"])
        except (TypeError, ValueError):
            row["priority"] = 1.0
    return row


def _backfill_from_role_catalog_override(
    mechanism: str,
    role_catalog: Mapping[str, Any] | None,
) -> dict[str, list[dict[str, str | float]]] | None:
    """Return parsed backfill specs when ``role_catalog`` defines this mechanism."""
    if not role_catalog:
        return None
    raw = role_catalog.get(ROLE_CATALOG_MECHANISM_BACKFILL_KEY)
    if not isinstance(raw, dict):
        return None
    entry = raw.get(mechanism)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        return None
    result: dict[str, list[dict[str, str | float]]] = {}
    for role, specs in entry.items():
        role_key = str(role)
        if not isinstance(specs, list):
            continue
        parsed: list[dict[str, str | float]] = []
        for item in specs:
            if isinstance(item, Mapping):
                coerced = _coerce_backfill_spec(item)
                if coerced is not None:
                    parsed.append(coerced)
        if parsed:
            result[role_key] = parsed
    return result if result else None


def required_roles_for_mechanism(
    mechanism: str,
    *,
    role_catalog: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return roles for a mechanism: workspace overlay first, then built-in table."""
    overridden = _roles_from_role_catalog_override(mechanism, role_catalog)
    if overridden is not None:
        return overridden
    return normalize_roles(_REQUIRED_ROLES.get(mechanism, ()))


def determine_preloaded_mechanism(target: SubgraphNode, query: str = "") -> str:
    """Return the best preloaded mechanism for a target, if one matches.

    Stubbed: no bundled name/query rules; always ``""``.
    """
    _ = (target, query)
    return ""


def role_backfill_specs_for_mechanism(
    mechanism: str,
    *,
    role_catalog: Mapping[str, Any] | None = None,
) -> dict[str, list[dict[str, str | float]]]:
    """Return role→spec lists: workspace overlay first, then built-in table."""
    overridden = _backfill_from_role_catalog_override(mechanism, role_catalog)
    if overridden is not None:
        return overridden
    return _ROLE_BACKFILL_SPECS.get(mechanism, {})


def known_mechanisms(*, role_catalog: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    """Mechanism ids from built-in table plus optional catalog overlays."""
    keys: set[str] = set(_REQUIRED_ROLES.keys())
    if role_catalog:
        raw = role_catalog.get(ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY)
        if isinstance(raw, dict):
            keys.update(str(k) for k in raw if isinstance(k, str) and k)
        raw_bf = role_catalog.get(ROLE_CATALOG_MECHANISM_BACKFILL_KEY)
        if isinstance(raw_bf, dict):
            keys.update(str(k) for k in raw_bf if isinstance(k, str) and k)
    return tuple(sorted(keys))


def preloaded_mechanism_catalog_extensions() -> dict[str, Any]:
    """JSON-friendly mechanism profiles for ``role_catalog_json`` (empty by default)."""
    return {
        ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY: {},
        ROLE_CATALOG_MECHANISM_BACKFILL_KEY: {},
    }


def merge_preloaded_mechanisms_into_role_catalog(catalog_dict: dict[str, Any]) -> dict[str, Any]:
    """Return ``catalog_dict`` plus preloaded mechanism keys (shallow copy)."""
    merged = dict(catalog_dict)
    merged.update(preloaded_mechanism_catalog_extensions())
    return merged


def pick_mechanism_by_role_overlap(
    observed_roles: Collection[str],
    *,
    target_role: str = "",
    role_catalog: Mapping[str, Any] | None = None,
    min_score: float = 0.42,
    target_bonus: float = 0.1,
) -> str:
    """Pick a named mechanism by overlap between observed Pass 1 roles and templates."""
    obs = {r for r in observed_roles if r}
    if len(obs) < 2:
        return ""

    best_mech = ""
    best_score = 0.0
    second_best = 0.0

    for mech in known_mechanisms(role_catalog=role_catalog):
        req = [
            r
            for r in required_roles_for_mechanism(mech, role_catalog=role_catalog)
            if r != "docs_or_concept"
        ]
        if not req:
            continue
        req_set = set(req)
        overlap_ratio = len(obs & req_set) / len(req_set)
        score = overlap_ratio
        if target_role and target_role in req_set:
            score = min(1.0, score + target_bonus)
        if score > best_score:
            second_best = best_score
            best_score = score
            best_mech = mech
        elif score > second_best:
            second_best = score

    if best_score < min_score:
        return ""
    if second_best > 0 and best_score - second_best < 0.035:
        return ""
    return best_mech
