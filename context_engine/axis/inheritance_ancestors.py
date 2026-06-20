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
``context_engine.indexer.fast.registry_class_inheritance``). ``TaskPool ->
DEPENDS_ON -> BasePool`` exists in the graph; the retrieval pipeline
just never walks it upward.

This pass closes that gap via the shared ``graph_walk`` core with a
``file_classes`` anchor: most retrieval seeds are functions/methods,
not the class itself, so the walk starts at every class in the seed's
file and follows outgoing ``DEPENDS_ON`` to ancestor classes in
*other* files. Capped tightly — empty interface files contribute one
representative each.
"""

from __future__ import annotations

from collections.abc import Iterable

from context_engine.axis.graph_walk import EdgeProfile, cap_by_file, walk_neighbours
from context_engine.axis.role_retrieval import RoleCandidate


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
    """Walk ``DEPENDS_ON`` outward from the classes in each seed's file,
    surface ancestor classes that live in different files.

    Returns one synthesised ``RoleCandidate`` per ancestor file (capped
    by ``max_per_file``), tagged ``structural_neighbour`` /
    ``inheritance_ancestor`` so downstream consumers iterate it under
    the same pool key as the other directionless structural passes.
    """
    seeds = list(seed_candidates)
    if not seeds:
        return []
    seed_uids = [c.uid for c in seeds]
    seed_files = {c.file_path for c in seeds if c.file_path}
    # The ``file_classes`` anchor already drops same-file neighbours
    # inside the walk (``fa.path <> seed_file.path``); passing
    # ``seed_files`` to the cap is defense-in-depth for the same
    # invariant.
    neighbours = walk_neighbours(
        db,
        workspace_id,
        seed_uids,
        edges=EdgeProfile.INHERITANCE,
        direction="forward",
        max_hops=max_hops,
        anchor="file_classes",
        class_targets_only=True,
    )
    capped = cap_by_file(
        neighbours,
        seed_files=seed_files,
        exclude_uids=set(exclude_uids) | set(seed_uids),
        max_per_file=max_per_file,
        max_files=max_files,
        max_total=max_total,
    )
    return [
        RoleCandidate(
            uid=n.uid,
            name=n.name,
            file_path=n.file_path,
            role="structural_neighbour",
            satisfying_contracts=(),
            satisfying_kinds=("inheritance_ancestor",),
            contract_count=0,
            kind_count=1,
            vector_distance=None,
            score=base_score,
        )
        for n in capped
    ]


__all__ = ["expand_inheritance_ancestors"]
