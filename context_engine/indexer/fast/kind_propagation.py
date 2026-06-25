"""Shared Lance/Neo4j helpers for container-kind propagation passes."""

from __future__ import annotations

import json
from typing import Any, cast

from context_engine.database.neo4j_client import Neo4jClient
from context_engine.indexer.fast.registry_class_inheritance import (
    _query_class_inheritance_context,
)


def owner_class_uid_for_methods(
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


def read_axis_evidence_rows(lance, workspace_id: str) -> list[dict[str, Any]]:
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


def append_container_kind_match(
    *,
    kind: str,
    uid: str,
    qualified_name: str,
    existing_kinds: list[str],
    existing_json: str,
    evidence_bits: list,
    evidence_probes: tuple[str, ...],
    payload: dict[str, object],
) -> tuple[list[str], str]:
    if kind in existing_kinds:
        return existing_kinds, existing_json
    try:
        matches = json.loads(existing_json or "[]")
    except json.JSONDecodeError:
        matches = []
    matches.append(
        {
            "kind": kind,
            "symbol_uid": uid,
            "qualified_name": qualified_name,
            "evidence_bits": evidence_bits,
            "evidence_probes": list(evidence_probes),
            "payload": payload,
        }
    )
    new_kinds = sorted(set(existing_kinds) | {kind})
    return new_kinds, json.dumps(matches, sort_keys=True)


def stage_kind_update(
    update_map: dict[str, dict[str, Any]],
    lance_kinds: dict[str, dict[str, Any]],
    uid: str,
    new_kinds: list[str],
    new_json: str,
) -> None:
    payload = {
        "container_kinds": new_kinds,
        "axis_container_kinds_json": new_json,
    }
    update_map[uid] = payload
    lance_kinds[uid] = payload


def apply_lance_kind_updates(
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


def propagate_container_kind_via_inheritance(
    db: Neo4jClient,
    lance,
    workspace_id: str,
    lance_kinds: dict[str, dict[str, Any]],
    seed_uids: set[str],
    *,
    kind: str,
    probe_prefix: str,
) -> int:
    rows = _query_class_inheritance_context(db, workspace_id)
    if not rows:
        return 0

    carrier_uids = {
        uid for uid, data in lance_kinds.items() if kind in data.get("container_kinds", [])
    } | seed_uids

    update_map: dict[str, dict[str, Any]] = {}
    for r in rows:
        uid = r["class_uid"]
        self_data = lance_kinds.get(uid)
        if not self_data or kind in self_data["container_kinds"]:
            continue
        ancestor_uids = set(r.get("ancestor_uids") or [])
        matched_anc = ancestor_uids & carrier_uids
        if not matched_anc:
            continue
        anc_uid = min(matched_anc)
        new_kinds, new_json = append_container_kind_match(
            kind=kind,
            uid=uid,
            qualified_name=str(r.get("class_qn") or ""),
            existing_kinds=list(self_data["container_kinds"]),
            existing_json=self_data["axis_container_kinds_json"],
            evidence_bits=[],
            evidence_probes=(f"{probe_prefix}:{anc_uid}",),
            payload={"via": "inheritance"},
        )
        update_map[uid] = {
            "container_kinds": new_kinds,
            "axis_container_kinds_json": new_json,
        }

    return apply_lance_kind_updates(lance, workspace_id, update_map)
