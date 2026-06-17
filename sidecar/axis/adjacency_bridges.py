"""External-node pass-through for in-process graph walks.

Neo4j variable-length walks can traverse a shared non-Symbol intermediate
(``ExternalPkg``, ``ExternalSymbol``, …). Maps are materialized into Lance at
index time and loaded with the workspace adjacency snapshot.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from sidecar.axis.graph_walk import EdgeProfile, _safe_rel_pattern


def serialize_external_maps(
    sym_to_ext: dict[str, dict[str, set[str]]],
    ext_to_sym: dict[str, dict[str, set[str]]],
) -> tuple[str, str]:
    """JSON blobs for Lance persistence."""
    sym_payload = {
        uid: {edge_type: sorted(neighbours) for edge_type, neighbours in sorted(by_type.items())}
        for uid, by_type in sorted(sym_to_ext.items())
        if by_type
    }
    ext_payload = {
        uid: {edge_type: sorted(neighbours) for edge_type, neighbours in sorted(by_type.items())}
        for uid, by_type in sorted(ext_to_sym.items())
        if by_type
    }
    return json.dumps(sym_payload, sort_keys=True), json.dumps(ext_payload, sort_keys=True)


def deserialize_external_maps(
    sym_to_ext_json: str,
    ext_to_sym_json: str,
) -> tuple[dict[str, dict[str, set[str]]], dict[str, dict[str, set[str]]]]:
    sym_to_ext: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    ext_to_sym: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    try:
        sym_raw = json.loads(sym_to_ext_json or "{}")
    except Exception:
        sym_raw = {}
    try:
        ext_raw = json.loads(ext_to_sym_json or "{}")
    except Exception:
        ext_raw = {}
    for uid, by_type in sym_raw.items():
        if not isinstance(by_type, dict):
            continue
        for edge_type, neighbours in by_type.items():
            sym_to_ext[str(uid)][str(edge_type)] = {str(n) for n in neighbours if n}
    for uid, by_type in ext_raw.items():
        if not isinstance(by_type, dict):
            continue
        for edge_type, neighbours in by_type.items():
            ext_to_sym[str(uid)][str(edge_type)] = {str(n) for n in neighbours if n}
    return sym_to_ext, ext_to_sym


def load_external_maps(
    session: Any,
    workspace_id: str,
    edge_types: tuple[str, ...] | None = None,
) -> tuple[dict[str, dict[str, set[str]]], dict[str, dict[str, set[str]]]]:
    """``sym_to_ext[symbol][type] -> {external_uid}`` and inverse ``ext_to_sym``."""
    types = edge_types or EdgeProfile.PROXIMITY
    rel = _safe_rel_pattern(types)
    sym_to_ext: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    ext_to_sym: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for rec in session.run(
        f"""
        MATCH (a:Symbol)-[r:{rel}]->(x)
        WHERE NOT x:Symbol
          AND coalesce(r.workspace_id, $ws) = $ws
        RETURN a.uid AS au, type(r) AS t, x.uid AS xu
        """,
        ws=workspace_id,
    ):
        au = str(rec.get("au") or "")
        xu = str(rec.get("xu") or "")
        edge_type = str(rec.get("t") or "")
        if not au or not xu or not edge_type:
            continue
        sym_to_ext[au][edge_type].add(xu)
        ext_to_sym[xu][edge_type].add(au)
    return sym_to_ext, ext_to_sym
