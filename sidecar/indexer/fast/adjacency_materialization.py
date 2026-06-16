"""Materialize graph-walk adjacency into LanceDB.

The in-process graph walk needs the workspace symbol set plus typed
Symbol-to-Symbol adjacency. Neo4j remains the source of truth during indexing;
this module snapshots that structure into Lance after all edge-producing phases
have settled.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any


def materialize_axis_adjacency(
    db: Any,
    lance: Any,
    workspace_id: str,
) -> int:
    """Replace one workspace's Lance adjacency snapshot.

    Returns the number of workspace-contained symbol rows written.
    """
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

    rows = []
    for uid in sorted(meta):
        row = dict(meta[uid])
        row["out_edges_json"] = _encode_edges(out_adj.get(uid, {}))
        row["in_edges_json"] = _encode_edges(in_adj.get(uid, {}))
        rows.append(row)

    replace = getattr(lance, "replace_axis_adjacency", None)
    if callable(replace):
        replace(rows, workspace_id=workspace_id)

    try:
        from sidecar.axis import graph_walk_inproc

        graph_walk_inproc.invalidate_adjacency(workspace_id)
    except Exception:
        pass
    return len(rows)


def _encode_edges(edges: dict[str, set[str]]) -> str:
    return json.dumps(
        {edge_type: sorted(uids) for edge_type, uids in sorted(edges.items()) if uids},
        sort_keys=True,
    )


__all__ = ["materialize_axis_adjacency"]
