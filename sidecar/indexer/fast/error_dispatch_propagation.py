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
:meth:`sidecar.axis.graph_probe.Neo4jGraphContextProbe.is_error_model_type_name`.
"""

from __future__ import annotations

import json
from typing import Any, cast

from sidecar.database.neo4j_client import Neo4jClient
from sidecar.indexer.fast.registry_class_inheritance import (
    _EXCEPTION_BASES,
    _query_class_inheritance_context,
    _read_lance_kinds,
)

BUILTIN_EXCEPTION_TYPE_NAMES: frozenset[str] = _EXCEPTION_BASES


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


def _owner_class_uid_for_methods(
    db: Neo4jClient,
    workspace_id: str,
    method_uids: list[str],
) -> dict[str, str]:
    """Map method uid → owning class uid (``HAS_API`` reverse)."""
    if not method_uids:
        return {}
    with db.driver.session() as session:
        rows = session.run(
            """
            UNWIND $uids AS mid
            MATCH (cls:Symbol {kind: 'class'})-[:HAS_API]->(m:Symbol {uid: mid})
            WHERE EXISTS((:File {workspace_id: $ws})-[:CONTAINS]->(cls))
            RETURN mid AS method_uid, cls.uid AS class_uid
            """,
            ws=workspace_id,
            uids=method_uids,
        ).data()
    return {
        str(r["method_uid"]): str(r["class_uid"])
        for r in rows
        if r.get("method_uid") and r.get("class_uid")
    }


def _read_axis_evidence_rows(lance, workspace_id: str) -> list[dict[str, Any]]:
    scan = getattr(lance, "scan_symbols_workspace", None)
    columns = [
        "uid",
        "symbol_kind",
        "qualified_name",
        "axis_evidence_json",
    ]
    if callable(scan):
        return cast(list[dict[str, Any]], scan(workspace_id, columns=columns))
    table = lance.symbols_table(workspace_id)  # type: ignore[attr-defined]
    return [
        r
        for r in table.to_lance().to_table(columns=[*columns, "workspace_id"]).to_pylist()
        if r.get("workspace_id") == workspace_id and r.get("uid")
    ]


def _append_kind_match(
    *,
    uid: str,
    qualified_name: str,
    existing_kinds: list[str],
    existing_json: str,
    evidence_probes: tuple[str, ...],
    payload: dict[str, object],
) -> tuple[list[str], str]:
    if "error_dispatch" in existing_kinds:
        return existing_kinds, existing_json
    try:
        matches = json.loads(existing_json or "[]")
    except json.JSONDecodeError:
        matches = []
    matches.append(
        {
            "kind": "error_dispatch",
            "symbol_uid": uid,
            "qualified_name": qualified_name,
            "evidence_bits": [["dfg", "keyed_write"]],
            "evidence_probes": list(evidence_probes),
            "payload": payload,
        }
    )
    new_kinds = sorted(set(existing_kinds) | {"error_dispatch"})
    return new_kinds, json.dumps(matches, sort_keys=True)


def _apply_lance_kind_updates(
    lance,
    workspace_id: str,
    update_map: dict[str, dict[str, Any]],
) -> int:
    if not update_map:
        return 0
    table = lance._sym_table  # noqa: SLF001
    existing_rows = [
        r
        for r in table.to_lance().to_table().to_pylist()
        if r.get("workspace_id") == workspace_id and r.get("uid") in update_map
    ]
    if not existing_rows:
        return 0
    for row in existing_rows:
        payload = update_map[row["uid"]]
        row["container_kinds"] = list(payload["container_kinds"])
        row["axis_container_kinds_json"] = payload["axis_container_kinds_json"]
    import pyarrow as pa

    arrow = pa.Table.from_pylist(existing_rows, schema=table.schema)
    quoted_ws = workspace_id.replace("'", "''")
    uid_in = ", ".join("'" + uid.replace("'", "''") + "'" for uid in update_map)
    table.delete(f"workspace_id = '{quoted_ws}' AND uid IN ({uid_in})")
    table.add(arrow)
    return len(existing_rows)


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
    evidence_rows = _read_axis_evidence_rows(lance, workspace_id)

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

    if not keyed_hits:
        return _propagate_error_dispatch_via_inheritance(
            db, lance, workspace_id, lance_kinds, set()
        )

    exception_keys = _resolve_exception_key_names(db, workspace_id, all_key_names)
    if not exception_keys:
        return _propagate_error_dispatch_via_inheritance(
            db, lance, workspace_id, lance_kinds, set()
        )

    method_owner = _owner_class_uid_for_methods(db, workspace_id, method_uids)
    seed_uids: set[str] = set()
    update_map: dict[str, dict[str, Any]] = {}

    qn_by_uid = {str(r["uid"]): str(r.get("qualified_name") or "") for r in evidence_rows}

    for symbol_uid, keys in keyed_hits:
        matched = sorted({k for k in keys if k in exception_keys})
        if not matched:
            continue
        target_uid = method_owner.get(symbol_uid, symbol_uid)
        seed_uids.add(target_uid)
        self_data = lance_kinds.get(target_uid)
        if not self_data:
            continue
        new_kinds, new_json = _append_kind_match(
            uid=target_uid,
            qualified_name=qn_by_uid.get(target_uid, ""),
            existing_kinds=list(self_data["container_kinds"]),
            existing_json=self_data["axis_container_kinds_json"],
            evidence_probes=(f"exception_keyed_registry:{','.join(matched[:5])}",),
            payload={"exception_keys": matched[:8]},
        )
        update_map[target_uid] = {
            "container_kinds": new_kinds,
            "axis_container_kinds_json": new_json,
        }
        lance_kinds[target_uid] = {
            "container_kinds": new_kinds,
            "axis_container_kinds_json": new_json,
        }

    updated = _apply_lance_kind_updates(lance, workspace_id, update_map)
    inherited = _propagate_error_dispatch_via_inheritance(
        db,
        lance,
        workspace_id,
        lance_kinds,
        seed_uids,
    )
    return updated + inherited


def _propagate_error_dispatch_via_inheritance(
    db: Neo4jClient,
    lance,
    workspace_id: str,
    lance_kinds: dict[str, dict[str, Any]],
    seed_uids: set[str],
) -> int:
    """Propagate ``error_dispatch`` down ``DEPENDS_ON`` from seeds + Lance."""
    rows = _query_class_inheritance_context(db, workspace_id)
    if not rows:
        return 0

    error_dispatch_uids = {
        uid
        for uid, data in lance_kinds.items()
        if "error_dispatch" in data.get("container_kinds", [])
    } | seed_uids

    update_map: dict[str, dict[str, Any]] = {}
    for r in rows:
        uid = r["class_uid"]
        self_data = lance_kinds.get(uid)
        if not self_data or "error_dispatch" in self_data["container_kinds"]:
            continue
        ancestor_uids = set(r.get("ancestor_uids") or [])
        matched_anc = ancestor_uids & error_dispatch_uids
        if not matched_anc:
            continue
        anc_uid = sorted(matched_anc)[0]
        new_kinds, new_json = _append_kind_match(
            uid=uid,
            qualified_name=str(r.get("class_qn") or ""),
            existing_kinds=list(self_data["container_kinds"]),
            existing_json=self_data["axis_container_kinds_json"],
            evidence_probes=(f"inherited_error_dispatch_via:{anc_uid}",),
            payload={"via": "inheritance"},
        )
        update_map[uid] = {
            "container_kinds": new_kinds,
            "axis_container_kinds_json": new_json,
        }

    return _apply_lance_kind_updates(lance, workspace_id, update_map)


__all__ = [
    "BUILTIN_EXCEPTION_TYPE_NAMES",
    "exception_name_keys_from_keyed_writes",
    "is_builtin_exception_type_name",
    "propagate_error_dispatch",
]
