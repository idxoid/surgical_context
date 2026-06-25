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

from context_engine.database.neo4j_client import Neo4jClient
from context_engine.indexer.fast.kind_propagation import (
    append_container_kind_match,
    apply_lance_kind_updates,
    owner_class_uid_for_methods,
    propagate_container_kind_via_inheritance,
    read_axis_evidence_rows,
    stage_kind_update,
)
from context_engine.indexer.fast.registry_class_inheritance import _read_lance_kinds

_KIND = "proxy_object"


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


def propagate_proxy_object(
    db: Neo4jClient,
    lance,
    workspace_id: str,
) -> int:
    """Tag proxy carriers and propagate the kind down inheritance."""
    lance_kinds = _read_lance_kinds(lance, workspace_id)
    evidence_rows = read_axis_evidence_rows(lance, workspace_id)
    qn_by_uid = {str(r["uid"]): str(r.get("qualified_name") or "") for r in evidence_rows}

    seed_uids: set[str] = set(_proxy_binding_uids(db, workspace_id))
    update_map: dict[str, dict[str, Any]] = {}

    for binding_uid in seed_uids:
        self_data = lance_kinds.get(binding_uid)
        if not self_data:
            continue
        new_kinds, new_json = append_container_kind_match(
            kind=_KIND,
            uid=binding_uid,
            qualified_name=qn_by_uid.get(binding_uid, ""),
            existing_kinds=list(self_data["container_kinds"]),
            existing_json=self_data["axis_container_kinds_json"],
            evidence_bits=[],
            evidence_probes=("graph_context:proxy_binding",),
            payload={"via": "proxy_binding"},
        )
        stage_kind_update(update_map, lance_kinds, binding_uid, new_kinds, new_json)

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

    method_owner = owner_class_uid_for_methods(db, workspace_id, method_uids)
    for method_uid in method_uids:
        class_uid = method_owner.get(method_uid)
        if not class_uid:
            continue
        seed_uids.add(class_uid)
        self_data = lance_kinds.get(class_uid)
        if not self_data or class_uid in update_map:
            continue
        new_kinds, new_json = append_container_kind_match(
            kind=_KIND,
            uid=class_uid,
            qualified_name=qn_by_uid.get(class_uid, ""),
            existing_kinds=list(self_data["container_kinds"]),
            existing_json=self_data["axis_container_kinds_json"],
            evidence_bits=[],
            evidence_probes=(f"proxy_delegate_method:{method_uid}",),
            payload={"via": "delegated_attribute_method"},
        )
        stage_kind_update(update_map, lance_kinds, class_uid, new_kinds, new_json)

    updated = apply_lance_kind_updates(lance, workspace_id, update_map)
    inherited = propagate_container_kind_via_inheritance(
        db,
        lance,
        workspace_id,
        lance_kinds,
        seed_uids,
        kind=_KIND,
        probe_prefix="inherited_proxy_object_via",
    )
    return updated + inherited


__all__ = [
    "method_facts_show_proxy_delegation",
    "propagate_proxy_object",
]
