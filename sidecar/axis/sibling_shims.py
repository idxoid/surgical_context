"""Sibling-shim discovery — surface re-export modules adjacent to seeds.

Some workspace files exist solely to re-export symbols from an external
package: ``fastapi/websockets.py``::

    from starlette.websockets import WebSocket as WebSocket
    from starlette.websockets import WebSocketDisconnect as WebSocketDisconnect
    from starlette.websockets import WebSocketState as WebSocketState

and ``fastapi/middleware/__init__.py``::

    from starlette.middleware import Middleware as Middleware

These shims have **no axis bits**, no container_kinds, no
AFFECTS/CALLS/USES_TYPE edges — the indexer's "names resolving outside
the workspace produce no edge" policy keeps them clean. They are
*invisible* to every retrieval and traversal pass.

The structural truth that connects these files to retrieved seeds is
package co-location: the shim sits in the same directory as a file that
did get retrieved. Until the indexer materializes a first-class
``RE_EXPORTS`` edge, this pass is deliberately topology-only: it does
not look at the user's question, symbol names, or file stems to decide
which shim is more relevant.

This pass is the file-system-topology mirror of ``structural_neighbours``:
where AFFECTS-bridges reach across the workspace via dataflow, this
one looks one directory level around each retrieved seed and includes
sibling Python files whose ``axis_container_kinds_json`` is empty —
the marker of a re-export shim that has nothing for the kind
classifier to chew on.

Caps are tight by design (``max_shims=4``) so a package with many
small modules cannot drown the candidate pool.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from sidecar.axis.role_retrieval import RoleCandidate


def _flat_kinds(raw):
    """Same flattener as ``role_lookahead`` — kind names from either a
    list of dicts (current indexer) or a list of strings (older one)."""
    if not raw:
        return set()
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return set()
    out = set()
    for item in parsed:
        if isinstance(item, dict):
            name = item.get("kind") or item.get("name")
            if name:
                out.add(str(name))
        elif item is not None:
            out.add(str(item))
    return out


def _parent_dir(path: str) -> str:
    norm = path.replace("\\", "/")
    if "/" not in norm:
        return ""
    return norm.rsplit("/", 1)[0]


def expand_sibling_shims(
    seed_candidates: Iterable[RoleCandidate],
    *,
    lance,
    workspace_id: str,
    max_shims: int = 4,
    base_score: float = 0.30,
    exclude_uids: Iterable[str] = (),
    prescanned=None,
) -> list[RoleCandidate]:
    """For each seed's containing directory, surface sibling ``.py``
    files in the same workspace whose ``axis_container_kinds_json`` is
    empty — the structural marker of a re-export shim.

    Returns synthesised ``RoleCandidate``s tagged with the
    ``structural_neighbour`` role (consumers already iterate that
    pool through the auto-promoted-role machinery, so no new pseudo-
    role is needed) and ``satisfying_kinds=("sibling_shim",)``.
    """
    seeds = list(seed_candidates)
    if not seeds:
        return []
    excluded = set(exclude_uids) | {c.uid for c in seeds}
    seed_dirs: set[str] = set()
    for c in seeds:
        if c.file_path:
            seed_dirs.add(_parent_dir(c.file_path))
    seed_dirs.discard("")
    if not seed_dirs:
        return []

    # Prefer the shared workspace scan (already workspace-filtered, with
    # ``_kinds`` pre-parsed) over a fresh full-table scan. The fallback
    # path keeps standalone callers (and tests without a shared scan)
    # working.
    if prescanned is not None:
        rows = prescanned.rows
        use_parsed = True
    else:
        sym_table = getattr(lance, "_sym_table", None)
        if sym_table is None:
            return []
        rows = sym_table.to_lance().to_table(
            columns=[
                "uid",
                "name",
                "file_path",
                "axis_container_kinds_json",
                "workspace_id",
            ]
        ).to_pylist()
        use_parsed = False

    # Collect every workspace row whose file lives directly inside one
    # of the seed directories AND whose container_kinds set is empty.
    # An empty kinds set is the structural fingerprint of a re-export
    # shim — the kind classifier walked the body and found nothing
    # because the body is ``from X import Y as Y`` only.
    by_file: dict[str, list[dict]] = {}
    for row in rows:
        if not use_parsed and row.get("workspace_id") != workspace_id:
            continue
        path = str(row.get("file_path") or "")
        if not path or not path.endswith(".py"):
            continue
        if _parent_dir(path) not in seed_dirs:
            continue
        kinds = (
            row.get("_kinds")
            if use_parsed
            else _flat_kinds(row.get("axis_container_kinds_json"))
        )
        if kinds:
            continue
        by_file.setdefault(path, []).append(row)

    # Build one candidate per shim file: pick the module-level row
    # (first uid that hasn't been excluded).
    candidates_by_file: list[tuple[str, dict]] = []
    for path, file_rows in by_file.items():
        chosen = None
        for row in file_rows:
            uid = str(row.get("uid") or "")
            if uid in excluded:
                continue
            chosen = row
            break
        if chosen is None:
            continue
        candidates_by_file.append((path, chosen))

    shims: list[RoleCandidate] = []
    for path, chosen in candidates_by_file:
        if len(shims) >= max_shims:
            break
        uid = str(chosen.get("uid") or "")
        if not uid:
            continue
        shims.append(
            RoleCandidate(
                uid=uid,
                name=str(chosen.get("name") or ""),
                file_path=path,
                role="structural_neighbour",
                satisfying_contracts=(),
                satisfying_kinds=("sibling_shim",),
                contract_count=0,
                kind_count=1,
                vector_distance=None,
                score=base_score,
            )
        )
    return shims


__all__ = ["expand_sibling_shims"]
