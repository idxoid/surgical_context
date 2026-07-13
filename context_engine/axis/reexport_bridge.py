"""Re-export transparency bridge from the query anchor symbol.

A *re-export shim* is a pure-reexport-tier module whose only job is to publish
an upstream (external) symbol into the workspace's own public namespace, e.g.
``fastapi/websockets.py``::

    from starlette.websockets import WebSocket as WebSocket

The shim carries no handling logic and no in-workspace edges, so the ranked
structural pool never reaches it — yet it is the file that *names* the public
type a caller imports. When the query anchors on that very type (``WebSocket``),
the shim is the "where does this public name come from" answer and belongs in
the bundle alongside the concrete implementation.

The mechanism is structural, not a name pattern:

- **tier gate** — the file is classified ``reexport`` (pure re-export body,
  ``is_pure_reexport_source``); a normal module that merely imports the symbol
  is not a shim.
- **public-re-export gate** — the ``IMPORTS_EXTERNAL_SYMBOL`` edge's
  ``local_alias`` equals the external symbol name (the redundant ``import X as
  X`` is Python's explicit PEP 484 re-export marker), so a private internal
  import does not qualify.
- **anchor gate** — the external symbol name matches the query's anchor symbol
  (the caller's declared focus, the same query→symbol matching vector seeding
  does), so the bridge only fires for the type actually asked about.
- **corroboration** — the external symbol is imported by at least one *other*
  workspace file, proving the re-exported type is genuinely consumed in-tree
  rather than a dangling shim.

No repo, framework, or symbol-name literal lives here; the bridge is driven
entirely by (file tier, edge alias, anchor, in-workspace consumption).
"""

from __future__ import annotations

from context_engine.axis.role_retrieval import RoleCandidate, WorkspaceScan

_REEXPORT_BRIDGE_SCORE = 0.35


def expand_reexport_bridge(
    anchor_symbol: str | None,
    *,
    db,
    workspace_id: str,
    prescanned: WorkspaceScan | None = None,
    max_total: int = 5,
) -> list[RoleCandidate]:
    """Surface pure-reexport shims that publicly re-export the anchor symbol."""
    name = (anchor_symbol or "").strip()
    if not name:
        return []

    query = """
    MATCH (shim:File {workspace_id: $workspace_id})
          -[r:IMPORTS_EXTERNAL_SYMBOL]->(e:ExternalSymbol {name: $anchor})
    WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
      AND r.local_alias = e.name
    // The re-exported type is actually consumed somewhere else in-workspace.
    MATCH (other:File {workspace_id: $workspace_id})-[:IMPORTS_EXTERNAL_SYMBOL]->(e)
    WHERE other <> shim
    WITH DISTINCT shim
    MATCH (shim)-[:CONTAINS]->(sym:Symbol)
    RETURN shim.path AS file_path, sym.uid AS uid, sym.name AS name,
           sym.qualified_name AS qualified_name
    ORDER BY shim.path, sym.uid
    """
    try:
        with db.driver.session() as session:
            rows = list(session.run(query, workspace_id=workspace_id, anchor=name))
    except Exception:
        return []

    rows_by_uid = prescanned.rows_by_uid if prescanned is not None else {}
    out: list[RoleCandidate] = []
    seen: set[str] = set()
    for row in rows:
        uid = str(row.get("uid") or "")
        if not uid or uid in seen:
            continue
        # Tier gate: only pure re-export shims qualify. The module symbol row
        # carries the file tier stamped at index time.
        owner_row = rows_by_uid.get(uid) or {}
        if str(owner_row.get("file_tier") or "") != "reexport":
            continue
        seen.add(uid)
        out.append(
            RoleCandidate(
                uid=uid,
                name=str(row.get("name") or ""),
                qualified_name=str(row.get("qualified_name") or ""),
                file_path=str(row.get("file_path") or ""),
                role="reexport_bridge",
                satisfying_contracts=(),
                satisfying_kinds=("public_reexport",),
                contract_count=0,
                kind_count=1,
                vector_distance=None,
                score=_REEXPORT_BRIDGE_SCORE,
                depth=1,
                edge_type="IMPORTS_EXTERNAL_SYMBOL",
            )
        )
        if len(out) >= max_total:
            break
    return out


__all__ = ["expand_reexport_bridge"]
