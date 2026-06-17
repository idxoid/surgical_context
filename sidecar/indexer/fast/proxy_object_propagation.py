"""Workspace-level ``proxy_object`` propagation.

Proxy kinds are earned from graph topology and delegated-attribute method
shapes — not from werkzeug qualified-name literals.

Channels:

  1. **Proxy-binding topology** — symbols the indexer already marks as
     ``proxy_binding`` or that emit ``PROXY_OF`` / ``RESOLVES_ATTR`` edges
     (classify-time via :meth:`graph_probe.Neo4jGraphContextProbe.has_proxy_object_topology`).

  2. **Delegated-attribute methods** — a method whose axis profile shows
     attribute read + value call + return output without keyed registry
     writes is a lazy-resolution body; the owning class is tagged.

  3. **Inheritance** — ``DEPENDS_ON`` descendants of an existing
     ``proxy_object`` carrier.
"""

from __future__ import annotations

import json
from typing import Any

from sidecar.database.neo4j_client import Neo4jClient
from sidecar.indexer.fast.registry_class_inheritance import (
    _query_class_inheritance_context,
    _read_lance_kinds,
)


def _owner_class_uid_for_methods(
    db: Neo4jClient,
    workspace_id: str,
    method_uids: list[str],
) -> dict[str, str]:
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


def method_facts_show_proxy_delegation(facts: list[dict[str, Any]]) -> bool:
    """True when serialized axis facts match a lazy attribute-delegation body."""
    bits: set[tuple[str, str]] = set()
    has_keyed_write = False
    for fact in facts:
        axis = str(fact.get("axis") or "")
        bit = str(fact.get("bit") or "")
        bits.add((axis, bit))
        if axis == "dfg" and bit == "keyed_write":
            has_keyed_write = True
    if has_keyed_write:
        return False
    return (
        ("dfg", "attr_read") in bits
        and ("cfg", "value_call") in bits
        and ("dfg", "return_output") in bits
    )


def _read_axis_evidence_rows(lance, workspace_id: str) -> list[dict[str, Any]]:
    scan = getattr(lance, "scan_symbols_workspace", None)
    columns = [
        "uid",
        "symbol_kind",
        "qualified_name",
        "axis_evidence_json",
    ]
    if callable(scan):
        return scan(workspace_id, columns=columns)
    table = lance.symbols_table(workspace_id)  # type: ignore[attr-defined]
    return [
        r
        for r in table.to_lance().to_table(columns=[*columns, "workspace_id"]).to_pylist()
        if r.get("workspace_id") == workspace_id and r.get("uid")
    ]


def _proxy_binding_uids(db: Neo4jClient, workspace_id: str) -> set[str]:
    with db.driver.session() as session:
        rows = session.run(
            """
            MATCH (:File {workspace_id: $ws})-[:CONTAINS]->(s:Symbol)
            WHERE s.kind = 'proxy_binding'
            RETURN collect(DISTINCT s.uid) AS uids
            """,
            ws=workspace_id,
        ).single()
    return {str(uid) for uid in ((rows and rows.get("uids")) or []) if uid}


def _append_kind_match(
    *,
    uid: str,
    qualified_name: str,
    existing_kinds: list[str],
    existing_json: str,
    evidence_probes: tuple[str, ...],
    payload: dict[str, object],
) -> tuple[list[str], str]:
    if "proxy_object" in existing_kinds:
        return existing_kinds, existing_json
    try:
        matches = json.loads(existing_json or "[]")
    except json.JSONDecodeError:
        matches = []
    matches.append(
        {
            "kind": "proxy_object",
            "symbol_uid": uid,
            "qualified_name": qualified_name,
            "evidence_bits": [],
            "evidence_probes": list(evidence_probes),
            "payload": payload,
        }
    )
    new_kinds = sorted(set(existing_kinds) | {"proxy_object"})
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


def propagate_proxy_object(
    db: Neo4jClient,
    lance,
    workspace_id: str,
) -> int:
    """Tag proxy carriers and propagate the kind down inheritance."""
    lance_kinds = _read_lance_kinds(lance, workspace_id)
    evidence_rows = _read_axis_evidence_rows(lance, workspace_id)
    qn_by_uid = {str(r["uid"]): str(r.get("qualified_name") or "") for r in evidence_rows}

    seed_uids: set[str] = set(_proxy_binding_uids(db, workspace_id))
    update_map: dict[str, dict[str, Any]] = {}

    for binding_uid in seed_uids:
        self_data = lance_kinds.get(binding_uid)
        if not self_data:
            continue
        new_kinds, new_json = _append_kind_match(
            uid=binding_uid,
            qualified_name=qn_by_uid.get(binding_uid, ""),
            existing_kinds=list(self_data["container_kinds"]),
            existing_json=self_data["axis_container_kinds_json"],
            evidence_probes=("graph_context:proxy_binding",),
            payload={"via": "proxy_binding"},
        )
        update_map[binding_uid] = {
            "container_kinds": new_kinds,
            "axis_container_kinds_json": new_json,
        }
        lance_kinds[binding_uid] = {
            "container_kinds": new_kinds,
            "axis_container_kinds_json": new_json,
        }

    method_uids: list[str] = []
    for row in evidence_rows:
        if str(row.get("symbol_kind") or "") != "method":
            continue
        uid = str(row["uid"])
        try:
            facts = json.loads(row.get("axis_evidence_json") or "[]")
        except json.JSONDecodeError:
            continue
        if not method_facts_show_proxy_delegation(facts):
            continue
        method_uids.append(uid)

    method_owner = _owner_class_uid_for_methods(db, workspace_id, method_uids)
    for method_uid in method_uids:
        class_uid = method_owner.get(method_uid)
        if not class_uid:
            continue
        seed_uids.add(class_uid)
        self_data = lance_kinds.get(class_uid)
        if not self_data or class_uid in update_map:
            continue
        new_kinds, new_json = _append_kind_match(
            uid=class_uid,
            qualified_name=qn_by_uid.get(class_uid, ""),
            existing_kinds=list(self_data["container_kinds"]),
            existing_json=self_data["axis_container_kinds_json"],
            evidence_probes=(f"proxy_delegate_method:{method_uid}",),
            payload={"via": "delegated_attribute_method"},
        )
        update_map[class_uid] = {
            "container_kinds": new_kinds,
            "axis_container_kinds_json": new_json,
        }
        lance_kinds[class_uid] = {
            "container_kinds": new_kinds,
            "axis_container_kinds_json": new_json,
        }

    updated = _apply_lance_kind_updates(lance, workspace_id, update_map)
    inherited = _propagate_proxy_object_via_inheritance(
        db, lance, workspace_id, lance_kinds, seed_uids
    )
    return updated + inherited


def _propagate_proxy_object_via_inheritance(
    db: Neo4jClient,
    lance,
    workspace_id: str,
    lance_kinds: dict[str, dict[str, Any]],
    seed_uids: set[str],
) -> int:
    rows = _query_class_inheritance_context(db, workspace_id)
    if not rows:
        return 0

    proxy_uids = {
        uid
        for uid, data in lance_kinds.items()
        if "proxy_object" in data.get("container_kinds", [])
    } | seed_uids

    update_map: dict[str, dict[str, Any]] = {}
    for r in rows:
        uid = r["class_uid"]
        self_data = lance_kinds.get(uid)
        if not self_data or "proxy_object" in self_data["container_kinds"]:
            continue
        ancestor_uids = set(r.get("ancestor_uids") or [])
        matched_anc = ancestor_uids & proxy_uids
        if not matched_anc:
            continue
        anc_uid = sorted(matched_anc)[0]
        new_kinds, new_json = _append_kind_match(
            uid=uid,
            qualified_name=str(r.get("class_qn") or ""),
            existing_kinds=list(self_data["container_kinds"]),
            existing_json=self_data["axis_container_kinds_json"],
            evidence_probes=(f"inherited_proxy_object_via:{anc_uid}",),
            payload={"via": "inheritance"},
        )
        update_map[uid] = {
            "container_kinds": new_kinds,
            "axis_container_kinds_json": new_json,
        }

    return _apply_lance_kind_updates(lance, workspace_id, update_map)


__all__ = [
    "method_facts_show_proxy_delegation",
    "propagate_proxy_object",
]
