"""Upward inheritance walk — surface abstract bases of retrieved seeds.

celery_new_q04 named the failure mode: a concrete pool implementation
(``celery.concurrency.prefork.TaskPool``) carries rich axis bits —
processes, forks, queue calls, real CFG/DFG signal — so it wins
``dispatch_surface`` retrieval handily. Its abstract base
(``celery.concurrency.base.BasePool``) is by design *empty of
execution physics*: abstract methods, interface declarations, no body
code for the kind classifier to chew on. The vector retrieval drops
it, structural_neighbour walks ``AFFECTS`` and misses it too because
the base file has nothing to AFFECT.

The structural truth is that the indexer DOES encode inheritance —
as a ``DEPENDS_ON`` edge between class symbols (handled by
``sidecar.indexer.fast.registry_class_inheritance``). ``TaskPool ->
DEPENDS_ON -> BasePool`` exists in the graph; the retrieval pipeline
just never walks it upward.

This pass closes that gap. The structural invariant: when a concrete
implementation is in the retrieval pool, its abstract interface is
relevant to the same question. The walk is directed (only outgoing
``DEPENDS_ON``) and capped tightly — empty interface files do not
flood context, but their *one* representative is in.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from sidecar.axis.role_retrieval import RoleCandidate


_INHERITANCE_RELS: tuple[str, ...] = ("DEPENDS_ON",)


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


def expand_inheritance_ancestors(
    seed_candidates: Iterable[RoleCandidate],
    *,
    db,
    workspace_id: str,
    max_hops: int = 3,
    max_files: int = 4,
    max_per_file: int = 1,
    max_total: int = 6,
    base_score: float = 0.30,
    exclude_uids: Iterable[str] = (),
) -> list[RoleCandidate]:
    """Walk ``DEPENDS_ON`` outward from each seed, surface ancestor
    classes that live in files the seeds do not already cover.

    Returns one synthesised ``RoleCandidate`` per ancestor file (capped
    by ``max_per_file``). The pass writes into the
    ``structural_neighbour`` pseudo-role pool so downstream consumers
    iterate it under the same key as the other directionless
    structural passes — no extra role plumbing required.
    """
    seeds = list(seed_candidates)
    if not seeds:
        return []
    seed_uids = [c.uid for c in seeds]
    seed_files = {c.file_path for c in seeds if c.file_path}
    excluded = set(exclude_uids) | set(seed_uids)

    rel_pattern = _safe_rel_pattern(_INHERITANCE_RELS)
    # ``DEPENDS_ON`` between class symbols is the indexer's "this class
    # inherits from that class" edge — see
    # ``sidecar.indexer.fast.registry_class_inheritance`` for the
    # producer side. The query is structured around two facts:
    #
    #   1. Many seeds are *functions* (module-level helpers, methods
    #      pulled in by vector retrieval), not the class itself. The
    #      walk starts at every *class* inside each seed's file so
    #      ``_add_to_pool_map`` in ``concurrency/eventlet.py`` still
    #      reaches ``BasePool`` through eventlet's pool class.
    #
    #   2. Only ancestors that live in a *different* file matter —
    #      same-file ancestors are already represented by the seed's
    #      file in the pool.
    cypher = f"""
    UNWIND $seed_uids AS su
    MATCH (seed_file:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(s:Symbol {{uid: su}})
    MATCH (seed_file)-[:CONTAINS]->(cls:Symbol)
    WHERE cls.kind = 'class'
    MATCH (cls)-[r:{rel_pattern}*1..{max_hops}]->(a:Symbol)
    WHERE a.kind = 'class'
    MATCH (fa:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(a)
    WHERE fa.path <> seed_file.path
    RETURN DISTINCT
        a.uid AS uid,
        coalesce(a.name, '') AS name,
        fa.path AS file_path,
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
        # Skip ancestors that live in a seed's own file — the seed
        # already represents that file in the pool.
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
                satisfying_kinds=("inheritance_ancestor",),
                contract_count=0,
                kind_count=1,
                vector_distance=None,
                score=base_score,
            )
        )
        if len(out) >= max_total:
            break
    return out


__all__ = ["expand_inheritance_ancestors"]
