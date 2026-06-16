"""Materialize graph-walk adjacency into LanceDB.

The in-process graph walk needs the workspace symbol set plus typed
Symbol-to-Symbol adjacency. Neo4j remains the source of truth during indexing;
this module snapshots that structure into Lance after all edge-producing phases
have settled.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Any

_AXIS_ADJACENCY_SUBSET_MAX_RATIO = float(
    os.getenv("LANCEDB_AXIS_ADJACENCY_PARTIAL_RESET_MAX_RATIO", "0.25")
)


def materialize_axis_adjacency(
    db: Any,
    lance: Any,
    workspace_id: str,
) -> int:
    """Replace one workspace's Lance adjacency snapshot.

    Returns the number of workspace-contained symbol rows written.
    """
    meta, out_adj, in_adj = _fetch_workspace_adjacency(db, workspace_id)
    rows = _build_adjacency_rows(meta, out_adj, in_adj)

    replace = getattr(lance, "replace_axis_adjacency", None)
    if callable(replace):
        replace(rows, workspace_id=workspace_id)

    _invalidate_adjacency_cache(workspace_id)
    return len(rows)


def materialize_axis_adjacency_subset(
    db: Any,
    lance: Any,
    workspace_id: str,
    seed_uids: set[str] | list[str],
) -> int:
    """Refresh only the adjacency rows for symbols incident to ``seed_uids``.

    Seeds are expanded to every symbol that shares a workspace edge with a
    seed (one hop). Rows are upserted in Lance; if the closure covers more
    than ``LANCEDB_AXIS_ADJACENCY_PARTIAL_RESET_MAX_RATIO`` of the existing
    snapshot, falls back to a full workspace materialization.
    """
    seeds = {str(uid) for uid in seed_uids if uid}
    if not seeds:
        return 0

    count_rows = getattr(lance, "count_axis_adjacency_workspace", None)
    existing = int(count_rows(workspace_id)) if callable(count_rows) else 0

    meta, out_adj, in_adj, closure = _fetch_adjacency_closure(db, workspace_id, seeds)
    if existing > 0 and len(closure) / existing > _AXIS_ADJACENCY_SUBSET_MAX_RATIO:
        return materialize_axis_adjacency(db, lance, workspace_id)

    rows = _build_adjacency_rows(meta, out_adj, in_adj, uids=closure)
    upsert = getattr(lance, "upsert_axis_adjacency_rows", None)
    if callable(upsert):
        upsert(rows, workspace_id=workspace_id)

    _invalidate_adjacency_cache(workspace_id)
    return len(rows)


def _fetch_workspace_adjacency(
    db: Any,
    workspace_id: str,
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, set[str]]], dict[str, dict[str, set[str]]]]:
    meta: dict[str, dict[str, str]] = {}
    out_adj: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    in_adj: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    with db.driver.session() as session:
        for rec in session.run(
            """
            MATCH (sf:File {workspace_id: $ws})-[:CONTAINS]->(s:Symbol)
            RETURN s.uid AS uid, coalesce(s.name, '') AS name,
                   sf.path AS path, coalesce(s.kind, '') AS kind
            """,
            ws=workspace_id,
        ):
            uid = str(rec.get("uid") or "")
            if not uid:
                continue
            meta[uid] = {
                "workspace_id": workspace_id,
                "uid": uid,
                "name": str(rec.get("name") or ""),
                "file_path": str(rec.get("path") or ""),
                "kind": str(rec.get("kind") or ""),
            }

        for rec in session.run(
            """
            MATCH (a:Symbol)-[r]->(b:Symbol)
            WHERE coalesce(r.workspace_id, $ws) = $ws
            RETURN a.uid AS au, b.uid AS bu, type(r) AS t
            """,
            ws=workspace_id,
        ):
            au = str(rec.get("au") or "")
            bu = str(rec.get("bu") or "")
            edge_type = str(rec.get("t") or "")
            if not au or not bu or not edge_type:
                continue
            out_adj[au][edge_type].add(bu)
            in_adj[bu][edge_type].add(au)

    return meta, out_adj, in_adj


def _fetch_adjacency_closure(
    db: Any,
    workspace_id: str,
    seed_uids: set[str],
) -> tuple[
    dict[str, dict[str, str]],
    dict[str, dict[str, set[str]]],
    dict[str, dict[str, set[str]]],
    set[str],
]:
    """Load meta + incident edges for symbols touching ``seed_uids``."""
    meta: dict[str, dict[str, str]] = {}
    out_adj: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    in_adj: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    closure: set[str] = set(seed_uids)

    with db.driver.session() as session:
        for rec in session.run(
            """
            MATCH (a:Symbol)-[r]->(b:Symbol)
            WHERE coalesce(r.workspace_id, $ws) = $ws
              AND (a.uid IN $seeds OR b.uid IN $seeds)
            RETURN a.uid AS au, b.uid AS bu, type(r) AS t
            """,
            ws=workspace_id,
            seeds=sorted(seed_uids),
        ):
            au = str(rec.get("au") or "")
            bu = str(rec.get("bu") or "")
            edge_type = str(rec.get("t") or "")
            if not au or not bu or not edge_type:
                continue
            closure.add(au)
            closure.add(bu)
            out_adj[au][edge_type].add(bu)
            in_adj[bu][edge_type].add(au)

        for rec in session.run(
            """
            MATCH (sf:File {workspace_id: $ws})-[:CONTAINS]->(s:Symbol)
            WHERE s.uid IN $uids
            RETURN s.uid AS uid, coalesce(s.name, '') AS name,
                   sf.path AS path, coalesce(s.kind, '') AS kind
            """,
            ws=workspace_id,
            uids=sorted(closure),
        ):
            uid = str(rec.get("uid") or "")
            if not uid:
                continue
            meta[uid] = {
                "workspace_id": workspace_id,
                "uid": uid,
                "name": str(rec.get("name") or ""),
                "file_path": str(rec.get("path") or ""),
                "kind": str(rec.get("kind") or ""),
            }

    return meta, out_adj, in_adj, closure


def _build_adjacency_rows(
    meta: dict[str, dict[str, str]],
    out_adj: dict[str, dict[str, set[str]]],
    in_adj: dict[str, dict[str, set[str]]],
    *,
    uids: set[str] | None = None,
) -> list[dict]:
    target_uids = sorted(uids if uids is not None else meta)
    rows: list[dict] = []
    for uid in target_uids:
        row_meta = meta.get(uid)
        if row_meta is None:
            continue
        row = dict(row_meta)
        row["out_edges_json"] = _encode_edges(out_adj.get(uid, {}))
        row["in_edges_json"] = _encode_edges(in_adj.get(uid, {}))
        rows.append(row)
    return rows


def _encode_edges(edges: dict[str, set[str]]) -> str:
    return json.dumps(
        {edge_type: sorted(uids) for edge_type, uids in sorted(edges.items()) if uids},
        sort_keys=True,
    )


def _invalidate_adjacency_cache(workspace_id: str) -> None:
    try:
        from sidecar.axis import graph_walk_inproc

        graph_walk_inproc.invalidate_adjacency(workspace_id)
    except Exception:
        pass


__all__ = ["materialize_axis_adjacency", "materialize_axis_adjacency_subset"]
