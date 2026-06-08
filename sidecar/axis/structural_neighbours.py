"""File-level structural neighbour expansion via ``AFFECTS``.

Some answers sit in workspace files that have no role kind, no contract,
no shared external dependency with the retrieved candidates — but the
indexer's ``AFFECTS`` closure already proves they belong to the same
structural region. ``fastapi/concurrency.py``'s
``contextmanager_in_threadpool`` is the case the user named: its
``AFFECTS`` reach loops through ``routing.get_request_handler`` (which
calls ``routing.run_endpoint_function``), but no other retrieval pass
sees concurrency.py because:

  - it has no axis container_kind (only `_T` TypeVar and one
    ``@asynccontextmanager`` helper),
  - it shares no small-fan external with routing.py (``run_endpoint_function``
    consumes ``starlette.concurrency.run_in_threadpool`` directly;
    ``contextmanager_in_threadpool`` consumes ``anyio.*``),
  - and its CALLS edges to ``run_in_threadpool`` are aliased away by
    ``from starlette.concurrency import run_in_threadpool as run_in_threadpool``
    which the Python adapter does not trace.

This pass is a *file-level* bridge — it walks ``AFFECTS`` undirected
from each seed and returns one or two reached symbols from each
*previously-unseen* file, capped tightly. The pass is intentionally
broad (no kind filter) and precision-controlled by the caps:

  - ``max_hops`` (default 2) keeps the closure short — AFFECTS edges
    are dense at workspace scale,
  - ``max_files`` limits the number of *new* files reached,
  - ``max_per_file`` limits symbols per file,
  - the synthesised score (``base_score=0.25``) sits below lookahead's
    0.40 so vector candidates always rank above structural-neighbours.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from sidecar.axis.role_retrieval import RoleCandidate


_AFFECTS_RELS: tuple[str, ...] = ("AFFECTS",)


def _safe_rel_pattern(edge_types: Iterable[str]) -> str:
    """Same validation contract as the other axis modules — reject
    anything that is not an uppercase identifier."""
    safe: list[str] = []
    pattern = re.compile(r"^[A-Z][A-Z0-9_]*$")
    for et in edge_types:
        if not pattern.match(et):
            raise ValueError(f"unsafe edge type: {et!r}")
        safe.append(et)
    return "|".join(safe)


def expand_structural_neighbours(
    seed_candidates: Iterable[RoleCandidate],
    *,
    db,
    workspace_id: str,
    max_hops: int = 2,
    max_files: int = 5,
    max_per_file: int = 2,
    max_total: int = 10,
    base_score: float = 0.25,
    exclude_uids: Iterable[str] = (),
    include_tests: bool = False,
) -> list[RoleCandidate]:
    """Walk ``AFFECTS`` undirected K hops from each seed, return
    deduplicated symbols from *previously-unseen* files (relative to
    the seeds' own files), capped.

    The returned candidates carry the pseudo-role
    ``structural_neighbour`` and ``satisfying_kinds=("affects_bridge",)``
    so the consumer can tell file-level bridges apart from lookahead's
    kind-evidenced injections.
    """
    seeds = list(seed_candidates)
    if not seeds:
        return []
    seed_uids = [c.uid for c in seeds]
    seed_files = {c.file_path for c in seeds if c.file_path}
    excluded = set(exclude_uids) | set(seed_uids)

    rel_pattern = _safe_rel_pattern(_AFFECTS_RELS)
    # ``size(r)`` returns the relationship-list length for a
    # variable-length pattern; ``length(r)`` is for Path objects and
    # raises a type mismatch in Cypher 5.
    if include_tests:
        where_clause = ""
    else:
        from sidecar.axis.test_file_filter import cypher_test_exclusion_clause

        where_clause = "WHERE " + cypher_test_exclusion_clause("fn")
    cypher = f"""
    UNWIND $seed_uids AS su
    MATCH (f:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(s:Symbol {{uid: su}})
    MATCH (s)-[r:{rel_pattern}*1..{max_hops}]-(n:Symbol)
    MATCH (fn:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(n)
    {where_clause}
    RETURN DISTINCT
        n.uid AS uid,
        coalesce(n.name, '') AS name,
        fn.path AS file_path,
        size(r) AS depth
    ORDER BY depth ASC
    """
    rows: list[dict] = []
    try:
        with db.driver.session() as session:
            for rec in session.run(
                cypher, seed_uids=list(seed_uids), workspace_id=workspace_id,
            ):
                rows.append(
                    {
                        "uid": str(rec.get("uid") or ""),
                        "name": str(rec.get("name") or ""),
                        "file_path": str(rec.get("file_path") or ""),
                        "depth": int(rec.get("depth") or 0),
                    }
                )
    except Exception:
        return []

    out: list[RoleCandidate] = []
    files_picked: dict[str, int] = {}
    new_files: set[str] = set()
    seen_uids: set[str] = set()
    for row in rows:
        uid = row["uid"]
        if not uid or uid in excluded or uid in seen_uids:
            continue
        file_path = row["file_path"]
        # Skip symbols from the seeds' own files — those are already
        # represented by the seeds; the point of this pass is *new* files.
        if file_path in seed_files:
            continue
        if files_picked.get(file_path, 0) >= max_per_file:
            continue
        if file_path not in new_files and len(new_files) >= max_files:
            continue
        seen_uids.add(uid)
        new_files.add(file_path)
        files_picked[file_path] = files_picked.get(file_path, 0) + 1
        out.append(
            RoleCandidate(
                uid=uid,
                name=row["name"],
                file_path=file_path,
                role="structural_neighbour",
                satisfying_contracts=(),
                satisfying_kinds=("affects_bridge",),
                contract_count=0,
                kind_count=1,
                vector_distance=None,
                score=base_score,
            )
        )
        if len(out) >= max_total:
            break
    return out


__all__ = ["expand_structural_neighbours"]
