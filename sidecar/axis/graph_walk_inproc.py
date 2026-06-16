"""In-process backend for the graph walk (Phase 1a of the axis-materialization).

Loads the workspace's adjacency ONCE (cached) and answers ``walk_neighbours`` /
``walk_neighbours_grouped`` from memory instead of a per-call Neo4j
variable-length traversal — the measured hot path. It is a *faithful* drop-in:
same File-CONTAINS workspace scoping, same ``depth`` (min hops) / ``reach``
(distinct seeds), same anchor / direction / class-target / test-fence / limit
semantics, same ORDER BY (depth ASC, reach DESC, uid ASC). So enabling it is
recall-neutral by construction.

Phase 1a sources the adjacency from Neo4j (one bulk load, cached). Phase 1b
will source it from a Lance per-axis materialization that rides ``prescanned``,
removing even this one-time load.

Gated by ``AXIS_INPROC_WALK`` (graph_walk delegates here when set).
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field

from sidecar.axis.graph_walk import Direction, Neighbour
from sidecar.axis.test_file_filter import is_test_path


def enabled() -> bool:
    return os.environ.get("AXIS_INPROC_WALK", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass
class _Adjacency:
    # uid -> edge_type -> set(neighbour uid)
    out: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    in_: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    # uid -> (name, file_path, kind) for workspace File-contained symbols
    meta: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    # file_path -> [class-kind uids in that file]
    file_classes: dict[str, list[str]] = field(default_factory=dict)


_CACHE: dict[str, _Adjacency] = {}


def load_adjacency(db, workspace_id: str) -> _Adjacency:
    cached = _CACHE.get(workspace_id)
    if cached is not None:
        return cached
    adj = _Adjacency()
    out_adj: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    in_adj: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    try:
        with db.driver.session() as session:
            # Meta = the workspace symbol set (matches the walk's
            # ``(fn:File {ws})-[:CONTAINS]->(n)`` requirement) + name/path/kind.
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
                name = str(rec.get("name") or "")
                path = str(rec.get("path") or "")
                kind = str(rec.get("kind") or "")
                adj.meta[uid] = (name, path, kind)
                if kind == "class" and path:
                    adj.file_classes.setdefault(path, []).append(uid)
            # Edges (every type) with the per-rel workspace filter.
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
                t = str(rec.get("t") or "")
                if not au or not bu or not t:
                    continue
                out_adj[au][t].add(bu)
                in_adj[bu][t].add(au)
    except Exception:
        pass
    adj.out = {u: dict(d) for u, d in out_adj.items()}
    adj.in_ = {u: dict(d) for u, d in in_adj.items()}
    _CACHE[workspace_id] = adj
    return adj


def _neighbours_fn(adj: _Adjacency, rels: frozenset[str], direction: Direction):
    def neigh(u: str) -> set[str]:
        res: set[str] = set()
        if direction in ("forward", "undirected"):
            d = adj.out.get(u)
            if d:
                for t in rels:
                    res |= d.get(t, frozenset())
        if direction in ("reverse", "undirected"):
            d = adj.in_.get(u)
            if d:
                for t in rels:
                    res |= d.get(t, frozenset())
        return res

    return neigh


def walk_neighbours(
    db,
    workspace_id: str,
    seed_uids,
    *,
    edges,
    direction: Direction = "undirected",
    max_hops: int = 2,
    anchor: str = "seed",
    exclude_tests: bool = False,
    class_targets_only: bool = False,
    limit: int | None = None,
) -> list[Neighbour]:
    adj = load_adjacency(db, workspace_id)
    meta = adj.meta
    rels = frozenset(edges)
    neigh = _neighbours_fn(adj, rels, direction)
    seeds = [u for u in seed_uids if u]

    agg_depth: dict[str, int] = {}
    agg_seeds: dict[str, set[str]] = defaultdict(set)

    for su in seeds:
        seed_meta = meta.get(su)
        if anchor == "file_classes":
            if seed_meta is None:
                continue
            seed_file = seed_meta[1]
            starts = adj.file_classes.get(seed_file, [])
        else:
            if seed_meta is None:
                continue
            seed_file = None
            starts = [su]
        if not starts:
            continue
        dist: dict[str, int] = {st: 0 for st in starts}
        frontier = list(starts)
        for hop in range(1, max_hops + 1):
            nxt: list[str] = []
            for u in frontier:
                for v in neigh(u):
                    if v in dist:
                        continue
                    dist[v] = hop
                    nxt.append(v)
            if not nxt:
                break
            frontier = nxt
        for v, d in dist.items():
            if d == 0:
                continue  # start node itself
            if v not in meta:
                continue
            if anchor == "file_classes" and meta[v][1] == seed_file:
                continue  # drop same-file (per this seed)
            if v not in agg_depth or d < agg_depth[v]:
                agg_depth[v] = d
            agg_seeds[v].add(su)

    out: list[Neighbour] = []
    for v, d in agg_depth.items():
        name, path, kind = meta[v]
        if class_targets_only and kind != "class":
            continue
        if exclude_tests and is_test_path(path or ""):
            continue
        out.append(Neighbour(uid=v, name=name, file_path=path, depth=d, reach=len(agg_seeds[v])))
    out.sort(key=lambda n: (n.depth, -n.reach, n.uid))
    if limit is not None:
        out = out[:limit]
    return out


def walk_neighbours_grouped(
    db,
    workspace_id: str,
    seed_uids,
    *,
    edges,
    direction: Direction = "undirected",
    max_hops: int = 2,
    limit_per_seed: int | None = None,
) -> dict[str, list[Neighbour]]:
    adj = load_adjacency(db, workspace_id)
    meta = adj.meta
    rels = frozenset(edges)
    neigh = _neighbours_fn(adj, rels, direction)
    seeds = [u for u in seed_uids if u]

    grouped: dict[str, list[Neighbour]] = {}
    for su in seeds:
        # grouped matches the seed by uid only (no File anchor); neighbours
        # must still be workspace File-contained (in meta).
        dist: dict[str, int] = {su: 0}
        frontier = [su]
        for hop in range(1, max_hops + 1):
            nxt: list[str] = []
            for u in frontier:
                for v in neigh(u):
                    if v in dist:
                        continue
                    dist[v] = hop
                    nxt.append(v)
            if not nxt:
                break
            frontier = nxt
        rows: list[Neighbour] = []
        for v, d in dist.items():
            if d == 0:
                continue
            m = meta.get(v)
            if m is None:
                continue
            rows.append(Neighbour(uid=v, name=m[0], file_path=m[1], depth=d, reach=1))
        rows.sort(key=lambda n: (n.depth, n.uid))
        if limit_per_seed is not None:
            rows = rows[:limit_per_seed]
        if rows:
            grouped[su] = rows
    return grouped
