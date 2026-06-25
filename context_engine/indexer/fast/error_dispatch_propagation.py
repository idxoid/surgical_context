"""Workspace-level ``error_dispatch`` propagation.

A symbol earns ``error_dispatch`` when it registers callables into a keyed
container whose keys are *exception types* — not because an upstream library
name says so. Two structural channels:

  1. **Exception-keyed registry writes** — axis ``keyed_write`` facts whose
     ``key_kind`` is ``Name`` and whose key resolves to a builtin exception
     base (``_EXCEPTION_BASES``) or an in-workspace class marked
     ``inherits_builtin_exception`` at link time. Method-level writes
     propagate to the owning class; writes on a class/function profile tag
     that symbol directly.

  2. **Inheritance** — a class whose structural ancestry (``DEPENDS_ON`` plus
     alias-resolved bases) reaches a class already carrying
     ``error_dispatch``.

The pass runs in pipeline stage 5.5 immediately after
``propagate_error_model_via_inheritance``. Classify-time proof for symbols
with direct ``keyed_write`` facts still works via
:meth:`context_engine.axis.graph_probe.Neo4jGraphContextProbe.is_error_model_type_name`.
"""

from __future__ import annotations

import json
from typing import Any

from context_engine.database.neo4j_client import Neo4jClient
from context_engine.indexer.fast.kind_propagation import (
    append_container_kind_match,
    apply_lance_kind_updates,
    owner_class_uid_for_methods,
    propagate_container_kind_via_inheritance,
    read_axis_evidence_rows,
    stage_kind_update,
)
from context_engine.indexer.fast.registry_class_inheritance import (
    _EXCEPTION_BASES,
    _read_lance_kinds,
)

BUILTIN_EXCEPTION_TYPE_NAMES: frozenset[str] = _EXCEPTION_BASES
_KIND = "error_dispatch"


def is_builtin_exception_type_name(name: str) -> bool:
    """True when ``name`` is a Python builtin exception root (language contract)."""
    return name in _EXCEPTION_BASES


def exception_name_keys_from_keyed_writes(
    facts: list[dict[str, Any]],
) -> list[str]:
    """Return ``Name``-kind keyed-write keys from serialized axis fact dicts."""
    keys: list[str] = []
    for fact in facts:
        if str(fact.get("axis") or "") != "dfg":
            continue
        if str(fact.get("bit") or "") != "keyed_write":
            continue
        payload = fact.get("payload") or {}
        if str(payload.get("key_kind") or "") != "Name":
            continue
        key = str(payload.get("key") or "").strip()
        if key:
            keys.append(key)
    return keys


def _resolve_exception_key_names(
    db: Neo4jClient,
    workspace_id: str,
    key_names: set[str],
) -> set[str]:
    """Subset of ``key_names`` that are exception types in this workspace."""
    resolved = {name for name in key_names if name in _EXCEPTION_BASES}
    remaining = sorted(key_names - resolved)
    if not remaining:
        return resolved
    with db.driver.session() as session:
        rows = session.run(
            """
            MATCH (:File {workspace_id: $ws})-[:CONTAINS]->(c:Symbol {kind: 'class'})
            WHERE c.name IN $names
              AND coalesce(c.inherits_builtin_exception, false) = true
            RETURN DISTINCT c.name AS name
            """,
            ws=workspace_id,
            names=remaining,
        ).data()
    resolved.update(str(r["name"]) for r in rows if r.get("name"))
    return resolved


def _collect_keyed_write_hits(
    evidence_rows: list[dict],
) -> tuple[list[tuple[str, list[str]]], set[str], list[str]]:
    keyed_hits: list[tuple[str, list[str]]] = []
    all_key_names: set[str] = set()
    method_uids: list[str] = []
    for row in evidence_rows:
        uid = str(row["uid"])
        try:
            facts = json.loads(row.get("axis_evidence_json") or "[]")
        except json.JSONDecodeError:
            continue
        keys = exception_name_keys_from_keyed_writes(facts)
        if not keys:
            continue
        keyed_hits.append((uid, keys))
        all_key_names.update(keys)
        if str(row.get("symbol_kind") or "") == "method":
            method_uids.append(uid)
    return keyed_hits, all_key_names, method_uids


def _inherit_error_dispatch_only(
    db: Neo4jClient,
    lance,
    workspace_id: str,
    lance_kinds,
    seed_uids: set[str] | None = None,
) -> int:
    return propagate_container_kind_via_inheritance(
        db,
        lance,
        workspace_id,
        lance_kinds,
        seed_uids or set(),
        kind=_KIND,
        probe_prefix="inherited_error_dispatch_via",
    )


def _stage_exception_keyed_seeds(
    keyed_hits: list[tuple[str, list[str]]],
    *,
    exception_keys: set[str],
    method_owner: dict[str, str],
    lance_kinds,
    qn_by_uid: dict[str, str],
    update_map: dict[str, dict[str, Any]],
) -> set[str]:
    seed_uids: set[str] = set()
    for symbol_uid, keys in keyed_hits:
        matched = sorted({key for key in keys if key in exception_keys})
        if not matched:
            continue
        target_uid = method_owner.get(symbol_uid, symbol_uid)
        seed_uids.add(target_uid)
        self_data = lance_kinds.get(target_uid)
        if not self_data:
            continue
        new_kinds, new_json = append_container_kind_match(
            kind=_KIND,
            uid=target_uid,
            qualified_name=qn_by_uid.get(target_uid, ""),
            existing_kinds=list(self_data["container_kinds"]),
            existing_json=self_data["axis_container_kinds_json"],
            evidence_bits=[["dfg", "keyed_write"]],
            evidence_probes=(f"exception_keyed_registry:{','.join(matched[:5])}",),
            payload={"exception_keys": matched[:8]},
        )
        stage_kind_update(update_map, lance_kinds, target_uid, new_kinds, new_json)
    return seed_uids


def propagate_error_dispatch(
    db: Neo4jClient,
    lance,
    workspace_id: str,
) -> int:
    """Tag registry symbols whose keyed writes target exception types.

    Returns the number of Lance symbol rows updated (both seed tagging and
    inheritance propagation).
    """
    lance_kinds = _read_lance_kinds(lance, workspace_id)
    evidence_rows = read_axis_evidence_rows(lance, workspace_id)

    keyed_hits, all_key_names, method_uids = _collect_keyed_write_hits(evidence_rows)
    if not keyed_hits:
        return _inherit_error_dispatch_only(db, lance, workspace_id, lance_kinds)

    exception_keys = _resolve_exception_key_names(db, workspace_id, all_key_names)
    if not exception_keys:
        return _inherit_error_dispatch_only(db, lance, workspace_id, lance_kinds)

    method_owner = owner_class_uid_for_methods(db, workspace_id, method_uids)
    update_map: dict[str, dict[str, Any]] = {}
    qn_by_uid = {str(r["uid"]): str(r.get("qualified_name") or "") for r in evidence_rows}
    seed_uids = _stage_exception_keyed_seeds(
        keyed_hits,
        exception_keys=exception_keys,
        method_owner=method_owner,
        lance_kinds=lance_kinds,
        qn_by_uid=qn_by_uid,
        update_map=update_map,
    )

    updated = apply_lance_kind_updates(lance, workspace_id, update_map)
    inherited = _inherit_error_dispatch_only(
        db,
        lance,
        workspace_id,
        lance_kinds,
        seed_uids,
    )
    return updated + inherited


__all__ = [
    "BUILTIN_EXCEPTION_TYPE_NAMES",
    "exception_name_keys_from_keyed_writes",
    "is_builtin_exception_type_name",
    "propagate_error_dispatch",
]
