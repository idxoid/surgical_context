"""Shared graph-walk core for the axis expansion passes.

Five expansion passes (``role_lookahead``, ``cross_role_boost``,
``impact_traversal``, ``structural_neighbours``,
``inheritance_ancestors``) grew independently, each re-implementing the
same three things: a Cypher-injection-safe edge-pattern builder, a
workspace-scoped neighbour walk over a relationship whitelist, and a
file-bucketed dedup-and-cap. This module is the single home for that
shared mechanic.

What stays here:

  * ``_safe_rel_pattern`` — the one validated edge-pattern builder.
  * ``EdgeProfile`` — named relationship whitelists (PROXIMITY,
    REVERSE_CALL, AFFECTS, INHERITANCE, …) so a widening is made in
    one place and every pass sees it.
  * ``Neighbour`` — the flat row a walk returns (uid, name, file_path,
    depth, reach).
  * ``walk_neighbours`` — the parametric walk. ``direction`` and
    ``anchor`` cover the cases the passes need; ``exclude_tests``
    folds in the test-file fence; ``reach`` (distinct seeds reaching a
    neighbour) is returned so a caller can rank by structural
    centrality instead of Lance-scan order.
  * ``cap_by_file`` — the dedup + per-file + total cap shared by the
    file-level passes.

What deliberately does *not* live here: the per-pass *interpretation*
of neighbours (lookahead's kind→role injection, impact's
walk-tagging, inheritance's class anchoring choice). Those stay in
their modules — this core only finds and ranks nodes, it never
decides what a node *means*.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Edge profiles — named relationship whitelists.
# ---------------------------------------------------------------------------


class EdgeProfile:
    """Named relationship-type whitelists. Widen here, not in a pass.

    ``PROXIMITY`` is the broad "structurally adjacent" set used by
    lookahead and cross-role intersection — every call flavour plus
    the API/type/attr edges that connect proxies to their consumers.
    The narrower sets back the directional passes.
    """

    # Call edges that target an in-workspace Symbol. ``CALLS_EXTERNAL``
    # is excluded on purpose: its target is an ExternalSymbol.
    CALLS: tuple[str, ...] = (
        "CALLS",
        "CALLS_DIRECT",
        "CALLS_SCOPED",
        "CALLS_IMPORTED",
        "CALLS_DYNAMIC",
        "CALLS_INFERRED",
        "CALLS_GUESS",
    )

    # Broad structural adjacency. Kept identical to the historical
    # ``_PROXIMITY_RELS`` so lookahead / intersection see no change.
    # READS_ATTR / WRITES_ATTR / RESOLVES_ATTR are how proxy objects
    # (Flask current_app, Werkzeug LocalProxy) connect to consumers.
    PROXIMITY: tuple[str, ...] = (
        *CALLS,
        "CALLS_EXTERNAL",
        "HAS_API",
        "INHERITED_API",
        "HANDLES",
        "INSTANTIATES",
        "DEPENDS_ON",
        "DECORATED_BY",
        "USES_TYPE",
        "REFERENCES",
        "READS_ATTR",
        "WRITES_ATTR",
        "RESOLVES_ATTR",
    )

    # Reverse-CALLS for "who calls X". Same set as CALLS — direction is
    # chosen by the walk, not the profile.
    REVERSE_CALL: tuple[str, ...] = CALLS

    # The indexer's pre-computed dataflow/parameter/return impact
    # closure.
    AFFECTS: tuple[str, ...] = ("AFFECTS",)

    # Class inheritance — the indexer emits this between class symbols
    # (see ``registry_class_inheritance``).
    INHERITANCE: tuple[str, ...] = ("DEPENDS_ON",)

    # Structural dependents: who implements / inherits (reverse) and
    # what API surface a subtype carries through (forward).
    STRUCTURAL_REVERSE: tuple[str, ...] = ("EXTENDS_EXTERNAL", "INHERITED_API")
    STRUCTURAL_FORWARD: tuple[str, ...] = ("HAS_API",)

    # Deferred-binding edges: how a symbol is wired to what it defers to
    # (decorators, type/dependency injection, handler registration, the
    # API surface it carries). The ``deferred_binding_flow`` first hop —
    # mirrors the legacy ``query_plan._STRUCTURAL_BINDING_EDGE_TYPES`` so
    # the per-candidate ``AxisGraphTraversal`` can fold onto this core.
    BINDING: tuple[str, ...] = (
        "DECORATED_BY",
        "USES_TYPE",
        "INJECTS",
        "HANDLES",
        "REFERENCES",
        "HAS_API",
        "INHERITED_API",
    )


# Context-expansion traversal modes → ordered (edges, direction, max_hops)
# steps over the shared walk core. This replaces the
# ``query_plan.TraversalMode`` → ``GraphExpansionStep`` compilation that
# only ``AxisGraphTraversal`` consumed: same edge sets, same depths,
# directions mapped to the walk's vocabulary (out→forward, in→reverse,
# both→undirected). ``build_context_for_candidates`` runs these steps with
# one batched grouped walk each, instead of a per-candidate traversal.
#: Each step is ``(name, edges, direction, max_hops)``. The names match the
#: legacy ``query_plan`` step names so a hit's ``expansion_step`` label is
#: byte-identical after the fold.
_MODE_STEPS: dict[
    str, tuple[tuple[str, tuple[str, ...], "Direction", int], ...]
] = {
    "immediate_control_flow": (
        ("control_call_expansion", EdgeProfile.CALLS, "forward", 2),
    ),
    "deferred_binding_flow": (
        ("binding_structure_expansion", EdgeProfile.BINDING, "undirected", 1),
        ("deferred_runtime_dispatch", EdgeProfile.CALLS, "undirected", 2),
    ),
}


def steps_for_mode(
    mode: str,
) -> tuple[tuple[str, tuple[str, ...], "Direction", int], ...]:
    """Return the ordered ``(name, edges, direction, max_hops)`` expansion
    steps for a context-traversal mode."""
    try:
        return _MODE_STEPS[mode]
    except KeyError:
        raise ValueError(f"Unknown traversal mode: {mode}") from None


def _safe_rel_pattern(edge_types: Iterable[str]) -> str:
    """Concatenate edge types into a Cypher ``|``-pattern; reject
    anything that isn't an uppercase identifier so a malformed name
    can never smuggle a fragment into the query."""
    safe: list[str] = []
    pattern = re.compile(r"^[A-Z][A-Z0-9_]*$")
    for et in edge_types:
        if not pattern.match(et):
            raise ValueError(f"unsafe edge type: {et!r}")
        safe.append(et)
    return "|".join(safe)


def _safe_max_hops(max_hops: int) -> int:
    """Validate a hop bound before interpolating it into Cypher syntax."""
    if type(max_hops) is not int or max_hops < 1:
        raise ValueError("max_hops must be an integer >= 1")
    return max_hops


# ---------------------------------------------------------------------------
# Walk
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Neighbour:
    """One reached node. ``depth`` is the shortest hop count to any
    seed; ``reach`` is the number of distinct seeds that reach it
    (structural-centrality signal)."""

    uid: str
    name: str
    file_path: str
    depth: int
    reach: int


Direction = Literal["forward", "reverse", "undirected"]
Anchor = Literal["seed", "file_classes"]


def walk_neighbours(
    db,
    workspace_id: str,
    seed_uids: Sequence[str],
    *,
    edges: Iterable[str],
    direction: Direction = "undirected",
    max_hops: int = 2,
    anchor: Anchor = "seed",
    exclude_tests: bool = False,
    class_targets_only: bool = False,
    limit: int | None = None,
) -> list[Neighbour]:
    """Workspace-scoped neighbour walk over ``edges``.

    ``direction``:
      * ``forward``     — ``(seed)-[r]->(n)``  ("what seed reaches")
      * ``reverse``     — ``(n)-[r]->(seed)``  ("what reaches seed")
      * ``undirected``  — ``(seed)-[r]-(n)``

    ``anchor``:
      * ``seed``         — start the walk at each seed uid.
      * ``file_classes`` — start at every class symbol in each seed's
        file, and drop neighbours that live in the seed's own file.
        This is the inheritance case: most retrieval seeds are
        functions/methods, so the class anchor is what reaches the
        ancestor.

    ``exclude_tests`` applies the conventional test-path fence to the
    neighbour's file. ``class_targets_only`` restricts neighbours to
    ``kind = 'class'`` (inheritance ancestors).

    Returns one ``Neighbour`` per reached uid, ``depth`` = shortest hop
    count, ``reach`` = number of distinct seeds reaching it, ordered by
    depth then reach-descending. Empty list on any driver error —
    expansion is best-effort, never fatal to retrieval.
    """
    seeds = [u for u in seed_uids if u]
    if not seeds:
        return []
    rel = _safe_rel_pattern(edges)
    hops = _safe_max_hops(max_hops)
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be an integer >= 1")

    # Build the directional relationship fragment between the anchor
    # symbol ``s`` (or ``cls``) and the neighbour ``n``.
    start_var = "cls" if anchor == "file_classes" else "s"
    if direction == "forward":
        edge_frag = f"({start_var})-[r:{rel}*1..{hops}]->(n:Symbol)"
    elif direction == "reverse":
        edge_frag = f"(n:Symbol)-[r:{rel}*1..{hops}]->({start_var})"
    else:  # undirected
        edge_frag = f"({start_var})-[r:{rel}*1..{hops}]-(n:Symbol)"

    where_clauses: list[str] = [
        "all(rel IN r WHERE coalesce(rel.workspace_id, $workspace_id) = $workspace_id)"
    ]
    if class_targets_only:
        where_clauses.append("n.kind = 'class'")
    if exclude_tests:
        from sidecar.axis.test_file_filter import cypher_test_exclusion_clause

        where_clauses.append(cypher_test_exclusion_clause("fn"))

    if anchor == "file_classes":
        # Start at classes inside the seed's file; drop same-file
        # neighbours (already represented by the seed's file).
        where_clauses.append("fn.path <> seed_file.path")
        anchor_match = (
            "MATCH (seed_file:File {workspace_id: $workspace_id})"
            "-[:CONTAINS]->(s:Symbol {uid: su})\n"
            "    MATCH (seed_file)-[:CONTAINS]->(cls:Symbol)\n"
            "    WHERE cls.kind = 'class'"
        )
    else:
        anchor_match = (
            "MATCH (sf:File {workspace_id: $workspace_id})"
            "-[:CONTAINS]->(s:Symbol {uid: su})"
        )

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    limit_sql = "\n    LIMIT $limit" if limit is not None else ""

    cypher = f"""
    UNWIND $seed_uids AS su
    {anchor_match}
    MATCH {edge_frag}
    MATCH (fn:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(n)
    {where_sql}
    WITH n, fn, min(size(r)) AS depth, count(DISTINCT su) AS reach
    RETURN
        n.uid AS uid,
        coalesce(n.name, '') AS name,
        fn.path AS file_path,
        depth AS depth,
        reach AS reach
    ORDER BY depth ASC, reach DESC, uid ASC
    {limit_sql}
    """

    out: list[Neighbour] = []
    try:
        with db.driver.session() as session:
            for rec in session.run(
                cypher,
                seed_uids=list(seeds),
                workspace_id=workspace_id,
                limit=limit,
            ):
                uid = str(rec.get("uid") or "")
                if not uid:
                    continue
                out.append(
                    Neighbour(
                        uid=uid,
                        name=str(rec.get("name") or ""),
                        file_path=str(rec.get("file_path") or ""),
                        depth=int(rec.get("depth") or 0),
                        reach=int(rec.get("reach") or 0),
                    )
                )
    except Exception:
        return []
    return out


def walk_neighbours_grouped(
    db,
    workspace_id: str,
    seed_uids: Sequence[str],
    *,
    edges: Iterable[str],
    direction: Direction = "undirected",
    max_hops: int = 2,
    limit_per_seed: int | None = None,
) -> dict[str, list[Neighbour]]:
    """Per-seed neighbour walk — ONE batched Cypher over the whole seed
    list, returning ``{seed_uid: [neighbours]}`` instead of the flat,
    seed-merged list ``walk_neighbours`` produces.

    This is what lets the per-candidate context expansion fold onto the
    shared core: ``build_context_for_candidates`` needs each seed's OWN
    neighbourhood (so seeds don't bleed into each other and each gets its
    own ``max_per_seed`` cap), which the merged ``reach`` form cannot give.
    Mirrors ``AxisGraphTraversal.expand`` per step exactly — matches the
    seed by uid (no File anchor), keeps the shallowest ``depth`` per
    ``(seed, neighbour)`` via ``min(size(r))``, applies the same
    per-relationship workspace filter, and does NOT exclude the seed from
    its own neighbourhood. ``reach`` is set to 1 (per-seed, meaningless
    here). Empty dict on any driver error — expansion is best-effort.
    """
    seeds = [u for u in seed_uids if u]
    if not seeds:
        return {}
    rel = _safe_rel_pattern(edges)
    hops = _safe_max_hops(max_hops)
    if limit_per_seed is not None and (
        type(limit_per_seed) is not int or limit_per_seed < 1
    ):
        raise ValueError("limit_per_seed must be an integer >= 1")

    if direction == "forward":
        edge_frag = f"(s)-[r:{rel}*1..{hops}]->(n:Symbol)"
    elif direction == "reverse":
        edge_frag = f"(s)<-[r:{rel}*1..{hops}]-(n:Symbol)"
    else:  # undirected
        edge_frag = f"(s)-[r:{rel}*1..{hops}]-(n:Symbol)"

    if limit_per_seed is None:
        limit_sql = """
    RETURN
        su AS seed_uid,
        n.uid AS uid,
        coalesce(n.name, '') AS name,
        fn.path AS file_path,
        depth AS depth
    ORDER BY seed_uid ASC, depth ASC, uid ASC
    """
    else:
        limit_sql = """
    ORDER BY su ASC, depth ASC, n.uid ASC
    WITH su, collect({
        uid: n.uid,
        name: coalesce(n.name, ''),
        file_path: fn.path,
        depth: depth
    })[..$limit_per_seed] AS rows
    UNWIND rows AS row
    RETURN
        su AS seed_uid,
        row.uid AS uid,
        row.name AS name,
        row.file_path AS file_path,
        row.depth AS depth
    ORDER BY seed_uid ASC, depth ASC, uid ASC
    """

    cypher = f"""
    UNWIND $seed_uids AS su
    MATCH (s:Symbol {{uid: su}})
    MATCH {edge_frag}
    MATCH (fn:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(n)
    WHERE all(rel IN r WHERE coalesce(rel.workspace_id, $workspace_id) = $workspace_id)
    WITH su, n, fn, min(size(r)) AS depth
    {limit_sql}
    """

    grouped: dict[str, list[Neighbour]] = {}
    try:
        with db.driver.session() as session:
            for rec in session.run(
                cypher,
                seed_uids=list(seeds),
                workspace_id=workspace_id,
                limit_per_seed=limit_per_seed,
            ):
                su = str(rec.get("seed_uid") or "")
                uid = str(rec.get("uid") or "")
                if not su or not uid:
                    continue
                grouped.setdefault(su, []).append(
                    Neighbour(
                        uid=uid,
                        name=str(rec.get("name") or ""),
                        file_path=str(rec.get("file_path") or ""),
                        depth=int(rec.get("depth") or 0),
                        reach=1,
                    )
                )
    except Exception:
        return {}
    return grouped


# ---------------------------------------------------------------------------
# File-bucketed cap
# ---------------------------------------------------------------------------


def cap_by_file(
    neighbours: Iterable[Neighbour],
    *,
    seed_files: Iterable[str] = (),
    exclude_uids: Iterable[str] = (),
    max_per_file: int = 2,
    max_files: int = 5,
    max_total: int = 10,
) -> list[Neighbour]:
    """Dedup + per-file + new-file + total cap, shared by the
    file-level passes (structural-neighbour, inheritance-ancestor).

    Drops neighbours in ``seed_files`` (already represented by the
    seeds) and in ``exclude_uids``. Keeps at most ``max_per_file`` per
    file, at most ``max_files`` distinct files, at most ``max_total``
    overall. Input order is honoured — the caller pre-sorts (walk
    returns depth-then-reach order).
    """
    seed_file_set = set(seed_files)
    excluded = set(exclude_uids)
    out: list[Neighbour] = []
    files_picked: dict[str, int] = {}
    new_files: set[str] = set()
    seen: set[str] = set()
    for n in neighbours:
        if not n.uid or n.uid in excluded or n.uid in seen:
            continue
        if n.file_path in seed_file_set:
            continue
        if files_picked.get(n.file_path, 0) >= max_per_file:
            continue
        if n.file_path not in new_files and len(new_files) >= max_files:
            continue
        seen.add(n.uid)
        new_files.add(n.file_path)
        files_picked[n.file_path] = files_picked.get(n.file_path, 0) + 1
        out.append(n)
        if len(out) >= max_total:
            break
    return out


__all__ = [
    "EdgeProfile",
    "Neighbour",
    "cap_by_file",
    "walk_neighbours",
]
