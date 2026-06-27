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

Gated by ``AXIS_INPROC_WALK`` (``graph_walk`` delegates here when active).

Default is ``auto``: use in-process walks when the workspace has a
materialized ``axis_adjacency`` Lance partition (indexed benchmarks and
production workspaces). Set ``AXIS_INPROC_WALK=0`` to force per-call
Neo4j traversals; ``1`` to force in-process even without Lance (one-time
Neo4j bulk load as fallback).
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field

from context_engine.axis.adjacency_bridges import load_external_maps
from context_engine.axis.edge_json import decode_edge_uid_map
from context_engine.axis.graph_walk import Direction, Neighbour
from context_engine.axis.test_file_filter import is_test_path


def _lance_adjacency_available(workspace_id: str) -> bool:
    """Fast catalog check — no full adjacency load."""
    try:
        import lancedb

        from context_engine.database.lance_workspace_tables import (
            workspace_partition_table_exists,
            workspace_partitioned_enabled,
        )
        from context_engine.database.lancedb_client import AXIS_ADJACENCY_TABLE, DB_PATH

        db = lancedb.connect(DB_PATH)
        if workspace_partitioned_enabled():
            return workspace_partition_table_exists(db, AXIS_ADJACENCY_TABLE, workspace_id)
        return AXIS_ADJACENCY_TABLE in db.table_names()
    except Exception:
        return False


def should_use(workspace_id: str) -> bool:
    """Whether graph walks for *workspace_id* should use in-process adjacency."""
    raw = os.environ.get("AXIS_INPROC_WALK", "auto").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return _lance_adjacency_available(workspace_id)


def enabled() -> bool:
    """Backward-compatible global check — prefer :func:`should_use`."""
    raw = os.environ.get("AXIS_INPROC_WALK", "auto").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return False


@dataclass
class _Adjacency:
    # uid -> edge_type -> set(neighbour uid)
    out: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    in_: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    # uid -> (name, file_path, kind) for workspace File-contained symbols
    meta: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    # file_path -> [class-kind uids in that file]
    file_classes: dict[str, list[str]] = field(default_factory=dict)
    # external bridge maps — see adjacency_bridges.load_external_maps
    sym_to_ext: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    ext_to_sym: dict[str, dict[str, set[str]]] = field(default_factory=dict)


_CACHE: dict[str, _Adjacency] = {}


def invalidate_adjacency(workspace_id: str | None = None) -> None:
    if workspace_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(workspace_id, None)


def _strip_uids_from_adjacency_buckets(
    mapping: dict[str, dict[str, set[str]]],
    uids: set[str],
) -> None:
    for source_uid, by_type in list(mapping.items()):
        if source_uid in uids:
            continue
        for edge_type in by_type.copy():
            remaining = set(by_type[edge_type]) - uids
            if remaining:
                by_type[edge_type] = remaining
            else:
                by_type.pop(edge_type, None)
        if not by_type:
            mapping.pop(source_uid, None)


def _drop_adjacency_uid_indices(adj: _Adjacency, uid: str) -> None:
    adj.out.pop(uid, None)
    adj.in_.pop(uid, None)
    meta = adj.meta.pop(uid, None)
    if not meta:
        return
    path = meta[1]
    if path not in adj.file_classes:
        return
    adj.file_classes[path] = [cu for cu in adj.file_classes[path] if cu != uid]
    if not adj.file_classes[path]:
        adj.file_classes.pop(path, None)


def invalidate_adjacency_uids(workspace_id: str, uids: set[str]) -> None:
    """Drop selected uids and their incident links from cached adjacency."""
    if not uids:
        return
    adj = _CACHE.get(workspace_id)
    if adj is None:
        return
    for mapping in (adj.out, adj.in_):
        _strip_uids_from_adjacency_buckets(mapping, uids)
    for uid in uids:
        _drop_adjacency_uid_indices(adj, uid)


def load_adjacency(db, workspace_id: str) -> _Adjacency:
    cached = _CACHE.get(workspace_id)
    if cached is not None:
        return cached
    lance_adj = _load_adjacency_from_lance(workspace_id, neo_db=db)
    if lance_adj is not None:
        _CACHE[workspace_id] = lance_adj
        return lance_adj
    adj = _load_adjacency_from_neo4j(db, workspace_id)
    _CACHE[workspace_id] = adj
    return adj


def _attach_external_maps(adj: _Adjacency, db, workspace_id: str) -> None:
    if adj.sym_to_ext and adj.ext_to_sym:
        return
    try:
        from context_engine.database.lancedb_client import LanceDBClient
        from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE

        loaded = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE).load_axis_adjacency_external(
            workspace_id
        )
        if loaded is not None:
            adj.sym_to_ext, adj.ext_to_sym = loaded
            return
    except Exception:
        pass
    try:
        with db.driver.session() as session:
            adj.sym_to_ext, adj.ext_to_sym = load_external_maps(session, workspace_id)
    except Exception:
        pass


def _ingest_neo4j_symbol_meta(rec, adj: _Adjacency) -> None:
    uid = str(rec.get("uid") or "")
    if not uid:
        return
    name = str(rec.get("name") or "")
    path = str(rec.get("path") or "")
    kind = str(rec.get("kind") or "")
    adj.meta[uid] = (name, path, kind)
    if kind == "class" and path:
        adj.file_classes.setdefault(path, []).append(uid)


def _ingest_neo4j_symbol_edge(
    rec,
    out_adj: dict[str, dict[str, set[str]]],
    in_adj: dict[str, dict[str, set[str]]],
) -> None:
    au = str(rec.get("au") or "")
    bu = str(rec.get("bu") or "")
    t = str(rec.get("t") or "")
    if not au or not bu or not t:
        return
    out_adj[au][t].add(bu)
    in_adj[bu][t].add(au)


def _load_neo4j_symbol_meta(session, workspace_id: str, adj: _Adjacency) -> None:
    for rec in session.run(
        """
        MATCH (sf:File {workspace_id: $ws})-[:CONTAINS]->(s:Symbol)
        RETURN s.uid AS uid, coalesce(s.name, '') AS name,
               sf.path AS path, coalesce(s.kind, '') AS kind
        """,
        ws=workspace_id,
    ):
        _ingest_neo4j_symbol_meta(rec, adj)


def _load_neo4j_symbol_edges(
    session,
    workspace_id: str,
    out_adj: dict[str, dict[str, set[str]]],
    in_adj: dict[str, dict[str, set[str]]],
) -> None:
    for rec in session.run(
        """
        MATCH (a:Symbol)-[r]->(b:Symbol)
        WHERE coalesce(r.workspace_id, $ws) = $ws
        RETURN a.uid AS au, b.uid AS bu, type(r) AS t
        """,
        ws=workspace_id,
    ):
        _ingest_neo4j_symbol_edge(rec, out_adj, in_adj)


def _load_adjacency_from_neo4j(db, workspace_id: str) -> _Adjacency:
    adj = _Adjacency()
    out_adj: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    in_adj: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    try:
        with db.driver.session() as session:
            _load_neo4j_symbol_meta(session, workspace_id, adj)
            _load_neo4j_symbol_edges(session, workspace_id, out_adj, in_adj)
            adj.sym_to_ext, adj.ext_to_sym = load_external_maps(session, workspace_id)
    except Exception:
        pass
    adj.out = {u: dict(d) for u, d in out_adj.items()}
    adj.in_ = {u: dict(d) for u, d in in_adj.items()}
    if not adj.sym_to_ext and not adj.ext_to_sym:
        _attach_external_maps(adj, db, workspace_id)
    return adj


def _load_adjacency_from_lance(workspace_id: str, *, neo_db=None) -> _Adjacency | None:
    try:
        import lancedb

        from context_engine.database.lance_workspace_tables import (
            workspace_partition_table_exists,
            workspace_partition_table_name,
            workspace_partitioned_enabled,
        )
        from context_engine.database.lancedb_client import AXIS_ADJACENCY_TABLE, DB_PATH

        lance_conn = lancedb.connect(DB_PATH)
        columns = [
            "uid",
            "name",
            "file_path",
            "kind",
            "out_edges_json",
            "in_edges_json",
        ]
        if workspace_partitioned_enabled():
            if workspace_partition_table_exists(lance_conn, AXIS_ADJACENCY_TABLE, workspace_id):
                table = lance_conn.open_table(
                    workspace_partition_table_name(AXIS_ADJACENCY_TABLE, workspace_id)
                )
                rows = table.search().limit(0).select(columns).to_list()
            else:
                try:
                    table = lance_conn.open_table(AXIS_ADJACENCY_TABLE)
                except Exception:
                    return None
                ws = workspace_id.replace("'", "''")
                rows = (
                    table.search()
                    .where(f"workspace_id = '{ws}'", prefilter=True)
                    .limit(0)
                    .select(columns)
                    .to_list()
                )
        else:
            if AXIS_ADJACENCY_TABLE not in lance_conn.table_names():
                return None
            table = lance_conn.open_table(AXIS_ADJACENCY_TABLE)
            ws = workspace_id.replace("'", "''")
            try:
                rows = (
                    table.search()
                    .where(f"workspace_id = '{ws}'", prefilter=True)
                    .limit(0)
                    .select(columns)
                    .to_list()
                )
            except Exception:
                df = table.to_pandas()
                rows = [
                    {key: row.get(key) for key in columns}
                    for _, row in df.iterrows()
                    if row.get("workspace_id") == workspace_id
                ]
    except Exception:
        return None
    if not rows:
        # Empty means "no materialization for this workspace"; let Neo4j answer.
        return None
    adj = _adjacency_from_lance_rows(rows)
    if neo_db is not None:
        _attach_external_maps(adj, neo_db, workspace_id)
    return adj


def _adjacency_from_lance_rows(rows: list[dict]) -> _Adjacency:
    adj = _Adjacency()
    out_adj: dict[str, dict[str, set[str]]] = {}
    in_adj: dict[str, dict[str, set[str]]] = {}

    for row in rows:
        uid = str(row.get("uid") or "")
        if not uid:
            continue
        name = str(row.get("name") or "")
        path = str(row.get("file_path") or "")
        kind = str(row.get("kind") or "")
        adj.meta[uid] = (name, path, kind)
        if kind == "class" and path:
            adj.file_classes.setdefault(path, []).append(uid)
        out_adj[uid] = decode_edge_uid_map(row.get("out_edges_json"))
        in_adj[uid] = decode_edge_uid_map(row.get("in_edges_json"))

    adj.out = out_adj
    adj.in_ = in_adj
    return adj


def call_fan_in(
    db, workspace_id: str, uids, *, edges, exclude_tests: bool = False
) -> dict[str, int]:
    """In-proc twin of ``graph_walk.call_fan_in`` — distinct in-workspace
    callers over ``edges`` per uid, read straight off the cached reverse
    adjacency. Callers are restricted to workspace symbols (``adj.meta``)
    to match the Neo4j File-CONTAINS scoping. ``exclude_tests`` drops callers
    that live in test files, so a function's production fan-in is not inflated
    by the suite that exercises it."""
    adj = load_adjacency(db, workspace_id)
    rels = frozenset(edges)
    meta = adj.meta

    def _keep(caller_uid: str) -> bool:
        m = meta.get(caller_uid)
        if m is None:
            return False
        return not (exclude_tests and is_test_path(m[1] or ""))

    out: dict[str, int] = {}
    for u in uids:
        if not u:
            continue
        by_type = adj.in_.get(u)
        callers: set[str] = set()
        if by_type:
            for t in rels:
                callers |= {c for c in by_type.get(t, frozenset()) if _keep(c)}
        out[u] = len(callers)
    return out


def _neighbours_for_edge_types(
    by_type: dict[str, set[str]] | None,
    rels: frozenset[str],
) -> set[str]:
    if not by_type:
        return set()
    res: set[str] = set()
    for edge_type in rels:
        res |= by_type.get(edge_type, frozenset())
    return res


def _collect_node_neighbours(
    adj: _Adjacency,
    uid: str,
    rels: frozenset[str],
    direction: Direction,
    *,
    fwd: bool,
    rev: bool,
) -> set[str]:
    res: set[str] = set()
    if fwd:
        res |= _neighbours_for_edge_types(adj.out.get(uid), rels)
    if rev:
        res |= _neighbours_for_edge_types(adj.in_.get(uid), rels)
    if uid in adj.meta and (fwd or direction == "undirected"):
        res |= _neighbours_for_edge_types(adj.sym_to_ext.get(uid, {}), rels)
    if uid not in adj.meta:
        res |= _neighbours_for_edge_types(adj.ext_to_sym.get(uid, {}), rels)
    return res


def _neighbours_fn(adj: _Adjacency, rels: frozenset[str], direction: Direction):
    # (rels, direction) are fixed for the whole walk, so neigh(u) is
    # deterministic — memoise it across the call. A multi-seed walk
    # (role_lookahead: 8 seeds; grouped: 128) otherwise recomputes the same
    # node's union once per seed BFS that reaches it.
    fwd = direction in ("forward", "undirected")
    rev = direction in ("reverse", "undirected")
    cache: dict[str, frozenset[str]] = {}

    def neigh(u: str) -> frozenset[str]:
        hit = cache.get(u)
        if hit is not None:
            return hit
        frozen = frozenset(_collect_node_neighbours(adj, u, rels, direction, fwd=fwd, rev=rev))
        cache[u] = frozen
        return frozen

    return neigh


def _inproc_walk_starts(
    adj: _Adjacency,
    seed_uid: str,
    anchor: str,
) -> tuple[list[str], str | None] | None:
    seed_meta = adj.meta.get(seed_uid)
    if seed_meta is None:
        return None
    if anchor == "file_classes":
        seed_file = seed_meta[1]
        starts = adj.file_classes.get(seed_file, [])
    else:
        seed_file = None
        starts = [seed_uid]
    if not starts:
        return None
    return starts, seed_file


def _bfs_distances_from_starts(
    neigh,
    starts: list[str],
    max_hops: int,
) -> dict[str, int]:
    dist: dict[str, int] = dict.fromkeys(starts, 0)
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
    return dist


def _merge_seed_distances(
    agg_depth: dict[str, int],
    agg_seeds: dict[str, set[str]],
    dist: dict[str, int],
    seed_uid: str,
    *,
    adj: _Adjacency,
    anchor: str,
    seed_file: str | None,
) -> None:
    for node_uid, depth in dist.items():
        if depth == 0:
            continue
        if node_uid not in adj.meta:
            continue
        if anchor == "file_classes" and adj.meta[node_uid][1] == seed_file:
            continue
        if node_uid not in agg_depth or depth < agg_depth[node_uid]:
            agg_depth[node_uid] = depth
        agg_seeds[node_uid].add(seed_uid)


def _neighbour_passes_filters(
    node_uid: str,
    meta: dict[str, tuple[str, str, str]],
    *,
    class_targets_only: bool,
    exclude_tests: bool,
) -> bool:
    _name, path, kind = meta[node_uid]
    if class_targets_only and kind != "class":
        return False
    return not (exclude_tests and is_test_path(path or ""))


def _neighbours_from_aggregation(
    agg_depth: dict[str, int],
    agg_seeds: dict[str, set[str]],
    meta: dict[str, tuple[str, str, str]],
    *,
    class_targets_only: bool,
    exclude_tests: bool,
    limit: int | None,
) -> list[Neighbour]:
    out: list[Neighbour] = []
    for node_uid, depth in agg_depth.items():
        if not _neighbour_passes_filters(
            node_uid,
            meta,
            class_targets_only=class_targets_only,
            exclude_tests=exclude_tests,
        ):
            continue
        name, path, _kind = meta[node_uid]
        out.append(
            Neighbour(
                uid=node_uid,
                name=name,
                file_path=path,
                depth=depth,
                reach=len(agg_seeds[node_uid]),
            )
        )
    out.sort(key=lambda n: (n.depth, -n.reach, n.uid))
    if limit is not None:
        return out[:limit]
    return out


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
    rels = frozenset(edges)
    neigh = _neighbours_fn(adj, rels, direction)
    seeds = [u for u in seed_uids if u]

    agg_depth: dict[str, int] = {}
    agg_seeds: dict[str, set[str]] = defaultdict(set)

    for seed_uid in seeds:
        start_info = _inproc_walk_starts(adj, seed_uid, anchor)
        if start_info is None:
            continue
        starts, seed_file = start_info
        dist = _bfs_distances_from_starts(neigh, starts, max_hops)
        _merge_seed_distances(
            agg_depth,
            agg_seeds,
            dist,
            seed_uid,
            adj=adj,
            anchor=anchor,
            seed_file=seed_file,
        )

    return _neighbours_from_aggregation(
        agg_depth,
        agg_seeds,
        adj.meta,
        class_targets_only=class_targets_only,
        exclude_tests=exclude_tests,
        limit=limit,
    )


def _grouped_neighbours_for_seed(
    dist: dict[str, int],
    meta: dict[str, tuple[str, str, str]],
    limit_per_seed: int | None,
) -> list[Neighbour]:
    rows: list[Neighbour] = []
    for node_uid, depth in dist.items():
        if depth == 0:
            continue
        node_meta = meta.get(node_uid)
        if node_meta is None:
            continue
        rows.append(
            Neighbour(
                uid=node_uid,
                name=node_meta[0],
                file_path=node_meta[1],
                depth=depth,
                reach=1,
            )
        )
    rows.sort(key=lambda n: (n.depth, n.uid))
    if limit_per_seed is not None:
        return rows[:limit_per_seed]
    return rows


def _grouped_neighbours_by_seed(
    seeds: list[str],
    *,
    neigh,
    meta: dict[str, tuple[str, str, str]],
    max_hops: int,
    limit_per_seed: int | None,
) -> dict[str, list[Neighbour]]:
    grouped: dict[str, list[Neighbour]] = {}
    for seed_uid in seeds:
        dist = _bfs_distances_from_starts(neigh, [seed_uid], max_hops)
        rows = _grouped_neighbours_for_seed(dist, meta, limit_per_seed)
        if rows:
            grouped[seed_uid] = rows
    return grouped


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
    neigh = _neighbours_fn(adj, frozenset(edges), direction)
    return _grouped_neighbours_by_seed(
        [u for u in seed_uids if u],
        neigh=neigh,
        meta=adj.meta,
        max_hops=max_hops,
        limit_per_seed=limit_per_seed,
    )


def _index_roles_by_secondary_uid(
    secondary_role_uids: Mapping[str, set[str]],
) -> tuple[set[str], dict[str, set[str]]] | None:
    flat_secondary_uids: set[str] = set()
    role_by_uid: dict[str, set[str]] = {}
    for role, uids in secondary_role_uids.items():
        for uid in uids:
            flat_secondary_uids.add(uid)
            role_by_uid.setdefault(uid, set()).add(role)
    if not flat_secondary_uids:
        return None
    return flat_secondary_uids, role_by_uid


def _bfs_expand_frontier_for_secondary(
    neigh,
    meta: dict[str, tuple[str, str, str]],
    frontier: list[str],
    dist: dict[str, int],
    hop: int,
    flat_secondary_uids: set[str],
    reached_secondary: set[str],
) -> list[str]:
    nxt: list[str] = []
    for u in frontier:
        for v in neigh(u):
            if v in dist or v not in meta:
                continue
            dist[v] = hop
            nxt.append(v)
            if v in flat_secondary_uids:
                reached_secondary.add(v)
    return nxt


def _bfs_reached_secondary_uids(
    neigh,
    meta: dict[str, tuple[str, str, str]],
    primary_uid: str,
    flat_secondary_uids: set[str],
    max_hops: int,
) -> set[str]:
    if primary_uid not in meta:
        return set()
    dist: dict[str, int] = {primary_uid: 0}
    frontier = [primary_uid]
    reached_secondary: set[str] = set()
    for hop in range(1, max_hops + 1):
        frontier = _bfs_expand_frontier_for_secondary(
            neigh,
            meta,
            frontier,
            dist,
            hop,
            flat_secondary_uids,
            reached_secondary,
        )
        if not frontier:
            break
    return reached_secondary


def _roles_for_reached_uids(
    reached_secondary: set[str],
    role_by_uid: dict[str, set[str]],
) -> set[str]:
    roles: set[str] = set()
    for uid in reached_secondary:
        roles |= role_by_uid.get(uid, set())
    return roles


def query_proximity_roles(
    db,
    workspace_id: str,
    primary_uids: list[str],
    secondary_role_uids: Mapping[str, set[str]],
    *,
    edges: frozenset[str],
    max_hops: int,
) -> dict[str, set[str]]:
    """For each primary uid, secondary role names reachable within ``max_hops``.

    In-process mirror of ``cross_role_boost._query_proximity_roles`` Cypher —
    filters the walk to the flat secondary-uid set and groups by primary.
    """
    if not primary_uids or not secondary_role_uids:
        return {}
    indexed = _index_roles_by_secondary_uid(secondary_role_uids)
    if indexed is None:
        return {}
    flat_secondary_uids, role_by_uid = indexed

    adj = load_adjacency(db, workspace_id)
    neigh = _neighbours_fn(adj, edges, "undirected")
    hops = max(1, int(max_hops))

    out: dict[str, set[str]] = {}
    for primary_uid in primary_uids:
        reached = _bfs_reached_secondary_uids(
            neigh,
            adj.meta,
            primary_uid,
            flat_secondary_uids,
            hops,
        )
        roles = _roles_for_reached_uids(reached, role_by_uid)
        if roles:
            out[primary_uid] = roles
    return out
