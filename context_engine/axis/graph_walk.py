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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from context_engine.axis.stage_warnings import record_stage_warning

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
        "EVENT_SUB",
        "EVENT_PUB",
        "METADATA_BRIDGE",
        "HOOK_CONFIG",
        "HOOK_EXEC",
        "CALLS_ENDPOINT",
        "IMPLEMENTS_ENDPOINT",
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
        "EVENT_SUB",
        "EVENT_PUB",
        "METADATA_BRIDGE",
        "HOOK_CONFIG",
        "HOOK_EXEC",
        "CALLS_ENDPOINT",
        "IMPLEMENTS_ENDPOINT",
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
_MODE_STEPS: dict[str, tuple[tuple[str, tuple[str, ...], Direction, int], ...]] = {
    "immediate_control_flow": (("control_call_expansion", EdgeProfile.CALLS, "forward", 2),),
    "deferred_binding_flow": (
        ("binding_structure_expansion", EdgeProfile.BINDING, "undirected", 1),
        ("deferred_runtime_dispatch", EdgeProfile.CALLS, "undirected", 2),
    ),
}


def steps_for_mode(
    mode: str,
) -> tuple[tuple[str, tuple[str, ...], Direction, int], ...]:
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


def _walk_neighbours_edge_frag(
    direction: Direction,
    rel: str,
    hops: int,
    *,
    start_var: str,
) -> str:
    if direction == "forward":
        return f"({start_var})-[r:{rel}*1..{hops}]->(n:Symbol)"
    if direction == "reverse":
        return f"(n:Symbol)-[r:{rel}*1..{hops}]->({start_var})"
    return f"({start_var})-[r:{rel}*1..{hops}]-(n:Symbol)"


def _walk_neighbours_where_clauses(
    *,
    exclude_tests: bool,
    class_targets_only: bool,
    anchor: Anchor,
) -> list[str]:
    where_clauses: list[str] = [
        "all(rel IN r WHERE coalesce(rel.workspace_id, $workspace_id) = $workspace_id)"
    ]
    if class_targets_only:
        where_clauses.append("n.kind = 'class'")
    if exclude_tests:
        from context_engine.axis.test_file_filter import cypher_test_exclusion_clause

        where_clauses.append(cypher_test_exclusion_clause("fn"))
    if anchor == "file_classes":
        where_clauses.append("fn.path <> seed_file.path")
    return where_clauses


def _walk_neighbours_anchor_match(anchor: Anchor) -> str:
    if anchor == "file_classes":
        return (
            "MATCH (seed_file:File {workspace_id: $workspace_id})"
            "-[:CONTAINS]->(s:Symbol {uid: su})\n"
            "    MATCH (seed_file)-[:CONTAINS]->(cls:Symbol)\n"
            "    WHERE cls.kind = 'class'"
        )
    return "MATCH (sf:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol {uid: su})"


def _build_walk_neighbours_cypher(
    *,
    edge_types: tuple[str, ...],
    direction: Direction,
    max_hops: int,
    anchor: Anchor,
    exclude_tests: bool,
    class_targets_only: bool,
    limit: int | None,
) -> str:
    rel = _safe_rel_pattern(edge_types)
    hops = _safe_max_hops(max_hops)
    start_var = "cls" if anchor == "file_classes" else "s"
    edge_frag = _walk_neighbours_edge_frag(direction, rel, hops, start_var=start_var)
    where_sql = "WHERE " + " AND ".join(
        _walk_neighbours_where_clauses(
            exclude_tests=exclude_tests,
            class_targets_only=class_targets_only,
            anchor=anchor,
        )
    )
    limit_sql = "\n    LIMIT $limit" if limit is not None else ""
    return f"""
    UNWIND $seed_uids AS su
    {_walk_neighbours_anchor_match(anchor)}
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


def _neighbours_from_cypher_records(records) -> list[Neighbour]:
    out: list[Neighbour] = []
    for rec in records:
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
    return out


def _run_walk_neighbours_cypher(
    db,
    cypher: str,
    seeds: list[str],
    workspace_id: str,
    limit: int | None,
) -> list[Neighbour]:
    try:
        with db.driver.session() as session:
            records = session.run(
                cypher,
                seed_uids=list(seeds),
                workspace_id=workspace_id,
                limit=limit,
            )
            return _neighbours_from_cypher_records(records)
    except Exception as exc:
        record_stage_warning(
            "graph_walk",
            "graph_walk_cypher_failed",
            "Graph neighbour walk failed; returning an empty neighbour set.",
            error=exc,
            details={
                "workspace_id": workspace_id,
                "seed_count": len(seeds),
                "limit": limit,
            },
        )
        return []


def _grouped_walk_edge_frag(direction: Direction, rel: str, hops: int) -> str:
    if direction == "forward":
        return f"(s)-[r:{rel}*1..{hops}]->(n:Symbol)"
    if direction == "reverse":
        return f"(s)<-[r:{rel}*1..{hops}]-(n:Symbol)"
    return f"(s)-[r:{rel}*1..{hops}]-(n:Symbol)"


def _grouped_walk_limit_sql(
    limit_per_seed: int | None,
    limit_per_seed_by_uid: Mapping[str, int] | None,
) -> str:
    if limit_per_seed is None and not limit_per_seed_by_uid:
        return """
    RETURN
        su AS seed_uid,
        n.uid AS uid,
        coalesce(n.name, '') AS name,
        fn.path AS file_path,
        depth AS depth
    ORDER BY seed_uid ASC, depth ASC, uid ASC
    """
    limit_expression = (
        "coalesce($limit_per_seed_by_uid[su], $limit_per_seed)"
        if limit_per_seed_by_uid
        else "$limit_per_seed"
    )
    return f"""
    WITH su, n, fn, depth, {limit_expression} AS seed_limit
    ORDER BY su ASC, depth ASC, n.uid ASC
    WITH su, collect({{
        uid: n.uid,
        name: coalesce(n.name, ''),
        file_path: fn.path,
        depth: depth
    }}) AS all_rows, seed_limit
    WITH su, all_rows[..seed_limit] AS rows
    UNWIND rows AS row
    RETURN
        su AS seed_uid,
        row.uid AS uid,
        row.name AS name,
        row.file_path AS file_path,
        row.depth AS depth
    ORDER BY seed_uid ASC, depth ASC, uid ASC
    """


def _build_walk_neighbours_grouped_cypher(
    *,
    edge_types: Iterable[str],
    direction: Direction,
    max_hops: int,
    limit_per_seed: int | None,
    limit_per_seed_by_uid: Mapping[str, int] | None = None,
) -> str:
    rel = _safe_rel_pattern(edge_types)
    hops = _safe_max_hops(max_hops)
    edge_frag = _grouped_walk_edge_frag(direction, rel, hops)
    return f"""
    UNWIND $seed_uids AS su
    MATCH (s:Symbol {{uid: su}})
    MATCH {edge_frag}
    MATCH (fn:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(n)
    WHERE all(rel IN r WHERE coalesce(rel.workspace_id, $workspace_id) = $workspace_id)
    WITH su, n, fn, min(size(r)) AS depth
    {_grouped_walk_limit_sql(limit_per_seed, limit_per_seed_by_uid)}
    """


def _grouped_neighbours_from_cypher_records(records) -> dict[str, list[Neighbour]]:
    grouped: dict[str, list[Neighbour]] = {}
    for rec in records:
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
    return grouped


def _run_walk_neighbours_grouped_cypher(
    db,
    cypher: str,
    seeds: list[str],
    workspace_id: str,
    limit_per_seed: int | None,
    limit_per_seed_by_uid: Mapping[str, int] | None = None,
) -> dict[str, list[Neighbour]]:
    try:
        with db.driver.session() as session:
            records = session.run(
                cypher,
                seed_uids=list(seeds),
                workspace_id=workspace_id,
                limit_per_seed=limit_per_seed,
                limit_per_seed_by_uid=dict(limit_per_seed_by_uid or {}),
            )
            return _grouped_neighbours_from_cypher_records(records)
    except Exception as exc:
        record_stage_warning(
            "graph_walk",
            "graph_walk_grouped_cypher_failed",
            "Grouped graph neighbour walk failed; returning empty per-seed neighbours.",
            error=exc,
            details={
                "workspace_id": workspace_id,
                "seed_count": len(seeds),
                "limit_per_seed": limit_per_seed,
                "variable_seed_limits": bool(limit_per_seed_by_uid),
            },
        )
        return {}


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
    from context_engine.axis import graph_walk_inproc

    edge_types = tuple(edges)
    if graph_walk_inproc.should_use(workspace_id):
        try:
            inproc_rows = graph_walk_inproc.walk_neighbours(
                db,
                workspace_id,
                seed_uids,
                edges=edge_types,
                direction=direction,
                max_hops=max_hops,
                anchor=anchor,
                exclude_tests=exclude_tests,
                class_targets_only=class_targets_only,
                limit=limit,
            )
        except Exception as exc:
            record_stage_warning(
                "graph_walk",
                "graph_walk_inproc_failed",
                "In-process graph walk failed; falling back to Neo4j traversal.",
                error=exc,
                details={"workspace_id": workspace_id, "seed_count": len(seed_uids)},
            )
            inproc_rows = []
        if inproc_rows:
            return inproc_rows
        # A materialized partition can lag Neo4j after an incremental edge
        # update.  Treating an empty snapshot walk as authoritative creates a
        # false "no impact" result until the next full materialization.  Empty
        # walks are cheap enough to verify against the source of truth; real
        # leaf nodes still return [] after the Cypher fallback.
    seeds = [u for u in seed_uids if u]
    if not seeds:
        return []
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be an integer >= 1")

    cypher = _build_walk_neighbours_cypher(
        edge_types=edge_types,
        direction=direction,
        max_hops=max_hops,
        anchor=anchor,
        exclude_tests=exclude_tests,
        class_targets_only=class_targets_only,
        limit=limit,
    )
    return _run_walk_neighbours_cypher(db, cypher, seeds, workspace_id, limit)


def call_fan_in(
    db,
    workspace_id: str,
    uids: Sequence[str],
    *,
    edges: Iterable[str] = EdgeProfile.CALLS,
    exclude_tests: bool = False,
) -> dict[str, int]:
    """Global in-degree over ``edges`` (distinct in-workspace callers) for
    each uid.

    A shared-utility hub (``warn``, ``maybe_list``, a logging helper) is
    called from everywhere, so reverse-walking from it to find "what is
    impacted" drags in the whole repo. The impact pass uses this to size
    each forward-closure node's fan-in *relative to the closure's own
    median* and keep hubs out of the test-reverse anchor set — a purely
    structural (call-graph topology) signal, no name matching.

    ``exclude_tests`` counts only PRODUCTION callers. A routing/API function
    exercised by a dozen unit tests (``route``, ``Router.prepare``) otherwise
    looks like a hub purely because the suite hammers it — its production
    fan-in is tiny. Excluding test callers separates "API the tests cover"
    from "god utility called everywhere in the code", so the gate stops
    clipping the very spine the impact walk needs.

    Best-effort: empty dict on any driver error.
    """
    from context_engine.axis import graph_walk_inproc

    targets = [u for u in uids if u]
    if not targets:
        return {}
    if graph_walk_inproc.should_use(workspace_id):
        try:
            return graph_walk_inproc.call_fan_in(
                db, workspace_id, targets, edges=edges, exclude_tests=exclude_tests
            )
        except Exception as exc:
            record_stage_warning(
                "graph_walk",
                "graph_walk_inproc_fan_in_failed",
                "In-process fan-in lookup failed; falling back to Neo4j.",
                error=exc,
                details={"workspace_id": workspace_id, "uid_count": len(targets)},
            )
    rel = _safe_rel_pattern(edges)
    where = ["coalesce(r.workspace_id, $workspace_id) = $workspace_id"]
    if exclude_tests:
        from context_engine.axis.test_file_filter import cypher_test_exclusion_clause

        where.append(cypher_test_exclusion_clause("cf"))
    cypher = f"""
    UNWIND $uids AS tu
    MATCH (caller:Symbol)-[r:{rel}]->(t:Symbol {{uid: tu}})
    MATCH (cf:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(caller)
    WHERE {" AND ".join(where)}
    RETURN tu AS uid, count(DISTINCT caller) AS fanin
    """
    out: dict[str, int] = {}
    try:
        with db.driver.session() as session:
            for rec in session.run(cypher, uids=targets, workspace_id=workspace_id):
                uid = str(rec.get("uid") or "")
                if uid:
                    out[uid] = int(rec.get("fanin") or 0)
    except Exception as exc:
        record_stage_warning(
            "graph_walk",
            "graph_walk_fan_in_cypher_failed",
            "Fan-in lookup failed; returning empty fan-in counts.",
            error=exc,
            details={"workspace_id": workspace_id, "uid_count": len(targets)},
        )
        return {}
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
    limit_per_seed_by_uid: Mapping[str, int] | None = None,
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
    here). ``limit_per_seed_by_uid`` overrides the scalar limit without
    splitting the batch into per-seed queries. Empty dict on any driver error
    — expansion is best-effort.
    """
    seeds = [u for u in seed_uids if u]
    if not seeds:
        return {}
    if limit_per_seed is not None and (type(limit_per_seed) is not int or limit_per_seed < 1):
        raise ValueError("limit_per_seed must be an integer >= 1")
    seed_limits = {
        str(uid): limit for uid, limit in (limit_per_seed_by_uid or {}).items() if str(uid)
    }
    if any(type(limit) is not int or limit < 1 for limit in seed_limits.values()):
        raise ValueError("limit_per_seed_by_uid values must be integers >= 1")
    if seed_limits and limit_per_seed is None:
        limit_per_seed = max(seed_limits.values())

    from context_engine.axis import graph_walk_inproc

    if graph_walk_inproc.should_use(workspace_id):
        try:
            return graph_walk_inproc.walk_neighbours_grouped(
                db,
                workspace_id,
                seeds,
                edges=edges,
                direction=direction,
                max_hops=max_hops,
                limit_per_seed=limit_per_seed,
                limit_per_seed_by_uid=seed_limits,
            )
        except Exception as exc:
            record_stage_warning(
                "graph_walk",
                "graph_walk_inproc_grouped_failed",
                "In-process grouped graph walk failed; falling back to Neo4j traversal.",
                error=exc,
                details={"workspace_id": workspace_id, "seed_count": len(seeds)},
            )

    cypher = _build_walk_neighbours_grouped_cypher(
        edge_types=edges,
        direction=direction,
        max_hops=max_hops,
        limit_per_seed=limit_per_seed,
        limit_per_seed_by_uid=seed_limits,
    )
    return _run_walk_neighbours_grouped_cypher(
        db,
        cypher,
        seeds,
        workspace_id,
        limit_per_seed,
        seed_limits,
    )


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
