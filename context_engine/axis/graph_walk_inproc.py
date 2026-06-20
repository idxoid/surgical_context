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

import json
import os
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field

from context_engine.axis.adjacency_bridges import load_external_maps
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


def invalidate_adjacency_uids(workspace_id: str, uids: set[str]) -> None:
    """Drop selected uids and their incident links from cached adjacency."""
    if not uids:
        return
    adj = _CACHE.get(workspace_id)
    if adj is None:
        return
    # Remove references from every cached adjacency bucket first.
    for mapping in (adj.out, adj.in_):
        for source_uid, by_type in list(mapping.items()):
            if source_uid in uids:
                continue
            for edge_type, neighbours in list(by_type.items()):
                remaining = set(neighbours) - uids
                if remaining:
                    by_type[edge_type] = remaining
                else:
                    by_type.pop(edge_type, None)
            if not by_type:
                mapping.pop(source_uid, None)
    # Remove target nodes themselves from all indices.
    for uid in uids:
        adj.out.pop(uid, None)
        adj.in_.pop(uid, None)
        meta = adj.meta.pop(uid, None)
        if meta:
            path = meta[1]
            if path in adj.file_classes:
                adj.file_classes[path] = [cu for cu in adj.file_classes[path] if cu != uid]
                if not adj.file_classes[path]:
                    adj.file_classes.pop(path, None)


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


def _load_adjacency_from_neo4j(db, workspace_id: str) -> _Adjacency:
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
        out_adj[uid] = _decode_edges(row.get("out_edges_json"))
        in_adj[uid] = _decode_edges(row.get("in_edges_json"))

    adj.out = out_adj
    adj.in_ = in_adj
    return adj


def _decode_edges(raw: object) -> dict[str, set[str]]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        payload = raw
    else:
        try:
            payload = json.loads(str(raw))
        except (TypeError, json.JSONDecodeError):
            return {}
    if not isinstance(payload, dict):
        return {}
    decoded: dict[str, set[str]] = {}
    for edge_type, uids in payload.items():
        if not isinstance(edge_type, str):
            continue
        if not isinstance(uids, list):
            continue
        decoded[edge_type] = {str(uid) for uid in uids if uid}
    return decoded


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
        res: set[str] = set()
        if fwd:
            d = adj.out.get(u)
            if d:
                for t in rels:
                    res |= d.get(t, frozenset())
        if rev:
            d = adj.in_.get(u)
            if d:
                for t in rels:
                    res |= d.get(t, frozenset())
        # Pass through shared external nodes (ExternalPkg, ExternalSymbol, …).
        if u in adj.meta and (fwd or direction == "undirected"):
            for t in rels:
                res |= adj.sym_to_ext.get(u, {}).get(t, frozenset())
        if u not in adj.meta:
            for t in rels:
                res |= adj.ext_to_sym.get(u, {}).get(t, frozenset())
        frozen = frozenset(res)
        cache[u] = frozen
        return frozen

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
    flat_secondary_uids: set[str] = set()
    role_by_uid: dict[str, set[str]] = {}
    for role, uids in secondary_role_uids.items():
        for uid in uids:
            flat_secondary_uids.add(uid)
            role_by_uid.setdefault(uid, set()).add(role)
    if not flat_secondary_uids:
        return {}

    adj = load_adjacency(db, workspace_id)
    meta = adj.meta
    neigh = _neighbours_fn(adj, edges, "undirected")
    hops = max(1, int(max_hops))

    out: dict[str, set[str]] = {}
    for pu in primary_uids:
        if pu not in meta:
            continue
        dist: dict[str, int] = {pu: 0}
        frontier = [pu]
        reached_secondary: set[str] = set()
        for hop in range(1, hops + 1):
            nxt: list[str] = []
            for u in frontier:
                for v in neigh(u):
                    if v in dist:
                        continue
                    if v not in meta:
                        continue
                    dist[v] = hop
                    nxt.append(v)
                    if v in flat_secondary_uids:
                        reached_secondary.add(v)
            if not nxt:
                break
            frontier = nxt
        if not reached_secondary:
            continue
        roles: set[str] = set()
        for uid in reached_secondary:
            roles |= role_by_uid.get(uid, set())
        if roles:
            out[pu] = roles
    return out
