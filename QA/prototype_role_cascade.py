#!/usr/bin/env python3
"""Prototype discriminator-first L1/L2 role cascade on indexed benchmark repos.

Standalone companion to QA/prototype_multidim_fan_clustering.py (k-means comparison).
Does not modify the multidim prototype — loads the same Neo4j workspace projection
with extended edges (HANDLES out, DECORATED_BY, USES_TYPE kind, PROXY_OF) and runs
the cascade from QA/role_cascade.py.

Usage:
    python QA/prototype_role_cascade.py --repo fastapi
    python QA/prototype_role_cascade.py --repos fastapi,flask
    python QA/prototype_role_cascade.py --workspace-id <uuid>

See docs/role_clustering_architecture.md (D1–D5) and docs/role_catalog.md.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QA_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(QA_DIR))

from qa_benchmark import default_repo_checkout_path  # noqa: E402
from role_cascade import (  # noqa: E402
    L1_BUCKETS,
    SymbolRoleAssignment,
    assign_all,
    detect_present_roles,
    role_catalog_roles,
)

from sidecar.context.ranker.signal_constants import NOISE_PATH_PATTERNS  # noqa: E402
from sidecar.database.neo4j_client import Neo4jClient  # noqa: E402
from sidecar.indexer.role_clustering import (  # noqa: E402
    build_role_catalog,
    cluster_symbols,
    extract_symbol_rows,
    resolve_role_clusters,
)
from sidecar.workspace import WorkspaceResolver  # noqa: E402

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

CALL_REL_TYPES = (
    "CALLS",
    "CALLS_DIRECT",
    "CALLS_SCOPED",
    "CALLS_IMPORTED",
    "CALLS_DYNAMIC",
    "CALLS_INFERRED",
    "CALLS_GUESS",
)

STRUCTURAL_REL_TYPES = (
    *CALL_REL_TYPES,
    "DEPENDS_ON",
    "HAS_API",
    "INHERITED_API",
    "USES_TYPE",
    "INJECTS",
    "HANDLES",
    "DECORATED_BY",
    "PROXY_OF",
    "INSTANTIATES",
)

DEFAULT_CONFIDENCE: dict[str, float] = {
    "CALLS_DIRECT": 1.0,
    "CALLS_SCOPED": 0.9,
    "CALLS_IMPORTED": 0.85,
    "CALLS_DYNAMIC": 0.7,
    "CALLS_INFERRED": 0.7,
    "CALLS_GUESS": 0.4,
    "CALLS": 0.85,
    "DEPENDS_ON": 0.9,
    "HAS_API": 0.95,
    "INHERITED_API": 0.9,
    "INJECTS": 0.85,
    "HANDLES": 1.0,
    "USES_TYPE": 1.0,
    "DECORATED_BY": 1.0,
    "PROXY_OF": 1.0,
    "INSTANTIATES": 1.0,
}

USES_TYPE_KIND_WEIGHT: dict[str, float] = {
    "param": 1.0,
    "annotation": 0.8,
    "return": 0.6,
    "isinstance": 0.5,
}

_EPS = 0.05

TARGET_SYMBOLS = (
    "run_endpoint_function",
    "solve_dependencies",
    "get_dependant",
    "add_api_route",
    "jsonable_encoder",
    "Default",
    "Param",
    "FastAPI",
    "APIRoute",
)

# Optional QA hints: any listed role may appear in primary+supporting.
QA_EXPECTED: dict[str, tuple[str, ...]] = {
    "run_endpoint_function": ("executor", "runtime_surface"),
    "solve_dependencies": ("orchestrator", "dependency_solver"),
    "add_api_route": ("factory_surface", "registration_step"),
    "Param": ("config_surface",),
    "APIRoute": ("representation_surface",),
    "FastAPI": ("api_surface",),
}


@dataclass(frozen=True)
class CascadeFanRow:
    uid: str
    kind: str
    call_fan_in: float
    call_fan_out: float
    type_fan_in: float
    type_fan_out: float
    type_fan_in_param: float
    type_fan_in_isinstance: float
    type_fan_in_return: float
    type_fan_out_return: float
    api_fan_in: float
    api_fan_out: float
    inject_fan_in: float
    depend_fan_in: float
    depend_fan_out: float
    handle_fan_in: float
    handle_fan_out: float
    decorated_in: float
    decorated_out: float
    construct_fan_out: float
    cross_package_call_in: float
    cross_package_call_out: float
    depth_from_public: int
    doc_anchor_count: int
    import_in: int
    reexport_in: int
    doc_definition_weight: float
    doc_reference_weight: float
    doc_example_weight: float
    is_proxy_binding: bool = False

    @property
    def is_class(self) -> bool:
        return self.kind in {"class", "interface"}

    @property
    def is_function(self) -> bool:
        return self.kind in {"function", "method"}

    @property
    def has_documentation(self) -> bool:
        return self.doc_anchor_count > 0 or self.doc_definition_weight > 0

    @property
    def call_leaf(self) -> bool:
        return self.call_fan_out <= _EPS

    @property
    def zero_in_degree(self) -> bool:
        return all(
            v <= _EPS
            for v in (
                self.call_fan_in,
                self.type_fan_in,
                self.api_fan_in,
                self.inject_fan_in,
                self.depend_fan_in,
                self.handle_fan_in,
                self.decorated_in,
            )
        )

    @property
    def structurally_connected(self) -> bool:
        return any(
            v > _EPS
            for v in (
                self.call_fan_in,
                self.call_fan_out,
                self.type_fan_in,
                self.type_fan_out,
                self.api_fan_in,
                self.api_fan_out,
                self.inject_fan_in,
                self.depend_fan_in,
                self.depend_fan_out,
                self.handle_fan_in,
                self.handle_fan_out,
                self.decorated_in,
                self.decorated_out,
            )
        )


def _edge_confidence(rel_type: str, stored: float | None, kind: str = "") -> float:
    if rel_type == "USES_TYPE":
        return USES_TYPE_KIND_WEIGHT.get(kind or "", DEFAULT_CONFIDENCE["USES_TYPE"])
    if stored is not None:
        return float(stored)
    return DEFAULT_CONFIDENCE.get(rel_type, 1.0)


def _query_pass1_symbols(db, workspace_id: str) -> list[tuple[str, str, str]]:
    with db.driver.session() as session:
        result = session.run(
            """
            MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
            WHERE NOT any(noise IN $noise_patterns WHERE f.path CONTAINS noise)
            RETURN s.uid AS uid,
                   coalesce(s.kind, '') AS kind,
                   coalesce(f.path, '') AS file_path
            """,
            workspace_id=workspace_id,
            noise_patterns=list(NOISE_PATH_PATTERNS),
        )
        return [(r["uid"], r["kind"], r["file_path"]) for r in result if r["uid"]]


def _query_structural_edges(
    db, workspace_id: str
) -> list[tuple[str, str, str, float, str]]:
    rel_union = "|".join(STRUCTURAL_REL_TYPES)
    with db.driver.session() as session:
        result = session.run(
            f"""
            MATCH (caller:Symbol)-[r:{rel_union}]->(callee:Symbol)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
            RETURN caller.uid AS caller_uid,
                   callee.uid AS callee_uid,
                   type(r) AS rel_type,
                   r.confidence AS confidence,
                   coalesce(r.kind, '') AS kind
            """,
            workspace_id=workspace_id,
        )
        rows: list[tuple[str, str, str, float, str]] = []
        for record in result:
            caller = record["caller_uid"]
            callee = record["callee_uid"]
            if not caller or not callee or caller == callee:
                continue
            rel_type = record["rel_type"]
            conf = _edge_confidence(rel_type, record["confidence"], record["kind"] or "")
            rows.append((caller, callee, rel_type, conf, record["kind"] or ""))
        return rows


def _query_reexport_in_counts(db, workspace_id: str) -> dict[str, int]:
    """Per-symbol RE_EXPORTS in-degree: how many package __init__ files surface it.

    File->Symbol edge (an __init__ has no Symbol of its own), so it is pulled as a
    standalone per-symbol feature, like import_in — not part of the Symbol->Symbol
    fan aggregation.
    """
    with db.driver.session() as session:
        result = session.run(
            """
            MATCH (:File {workspace_id: $workspace_id})-[r:RE_EXPORTS]->(sym:Symbol)
            RETURN sym.uid AS uid, count(r) AS c
            """,
            workspace_id=workspace_id,
        )
        return {r["uid"]: int(r["c"]) for r in result if r.get("uid")}


def _query_proxy_binding_uids(db, workspace_id: str) -> set[str]:
    with db.driver.session() as session:
        result = session.run(
            """
            MATCH (p:Symbol {workspace_id: $workspace_id, kind: 'proxy_binding'})
            RETURN p.uid AS uid
            """,
            workspace_id=workspace_id,
        )
        return {r["uid"] for r in result if r.get("uid")}


def _bfs_depths(out_edges: dict[str, set[str]], sources: set[str]) -> dict[str, int]:
    if not sources:
        return {}
    depths: dict[str, int] = {src: 0 for src in sources}
    queue: deque[str] = deque(sources)
    while queue:
        u = queue.popleft()
        for v in out_edges.get(u, ()):
            if v not in depths:
                depths[v] = depths[u] + 1
                queue.append(v)
    return depths


def assemble_cascade_rows(
    symbols: list[tuple[str, str, str]],
    edges: list[tuple[str, str, str, float, str]],
    doc_counts: dict[str, int],
    import_in_per_uid: dict[str, int],
    doc_signal_by_uid: dict[str, dict[str, float]],
    proxy_uids: set[str],
    reexport_in_per_uid: dict[str, int] | None = None,
) -> list[CascadeFanRow]:
    reexport_in_per_uid = reexport_in_per_uid or {}
    if not symbols:
        return []

    info: dict[str, dict] = {}
    for uid, kind, file_path in symbols:
        info[uid] = {"kind": kind or "", "package": os.path.dirname(file_path or "")}

    call_out: dict[str, set[str]] = {uid: set() for uid in info}
    call_in: dict[str, set[str]] = {uid: set() for uid in info}

    call_fan_in: dict[str, float] = defaultdict(float)
    call_fan_out: dict[str, float] = defaultdict(float)
    type_fan_in: dict[str, float] = defaultdict(float)
    type_fan_out: dict[str, float] = defaultdict(float)
    type_fan_in_param: dict[str, float] = defaultdict(float)
    type_fan_in_isinstance: dict[str, float] = defaultdict(float)
    type_fan_in_return: dict[str, float] = defaultdict(float)
    type_fan_out_return: dict[str, float] = defaultdict(float)
    api_fan_in: dict[str, float] = defaultdict(float)
    api_fan_out: dict[str, float] = defaultdict(float)
    inject_fan_in: dict[str, float] = defaultdict(float)
    depend_fan_in: dict[str, float] = defaultdict(float)
    depend_fan_out: dict[str, float] = defaultdict(float)
    handle_fan_in: dict[str, float] = defaultdict(float)
    handle_fan_out: dict[str, float] = defaultdict(float)
    decorated_in: dict[str, float] = defaultdict(float)
    decorated_out: dict[str, float] = defaultdict(float)
    construct_fan_out: dict[str, float] = defaultdict(float)

    for caller, callee, rel_type, conf, kind in edges:
        caller_in = caller in info
        callee_in = callee in info
        # F13: credit a Pass-1 endpoint even when the other end is a test/docs_src
        # consumer outside the clustered set — that is where a framework's public
        # surface gets its real in-degree / consumption signal. Symbols are still
        # clustered/assigned test-free; only the edge-derived features see the full
        # graph. Each side is credited only if it is itself in the Pass-1 set.
        if not caller_in and not callee_in:
            continue
        if rel_type in CALL_REL_TYPES:
            if caller_in:
                call_out[caller].add(callee)
                call_fan_out[caller] += conf
            if callee_in:
                call_in[callee].add(caller)
                call_fan_in[callee] += conf
        elif rel_type == "USES_TYPE":
            if caller_in:
                type_fan_out[caller] += conf
                if kind == "return":
                    type_fan_out_return[caller] += conf
            if callee_in:
                type_fan_in[callee] += conf
                if kind in {"param", "annotation"}:
                    type_fan_in_param[callee] += conf
                elif kind == "isinstance":
                    type_fan_in_isinstance[callee] += conf
                elif kind == "return":
                    type_fan_in_return[callee] += conf
        elif rel_type in {"HAS_API", "INHERITED_API"}:
            if caller_in:
                api_fan_out[caller] += conf
            if callee_in:
                api_fan_in[callee] += conf
        elif rel_type == "INJECTS":
            if callee_in:
                inject_fan_in[callee] += conf
        elif rel_type == "DEPENDS_ON":
            if caller_in:
                depend_fan_out[caller] += conf
            if callee_in:
                depend_fan_in[callee] += conf
        elif rel_type == "HANDLES":
            if caller_in:
                handle_fan_out[caller] += conf
            if callee_in:
                handle_fan_in[callee] += conf
        elif rel_type == "DECORATED_BY":
            if caller_in:
                decorated_out[caller] += conf
            if callee_in:
                decorated_in[callee] += conf
        elif rel_type == "INSTANTIATES":
            if caller_in:
                construct_fan_out[caller] += conf

    public_uids = {uid for uid in info if call_fan_in[uid] <= _EPS and call_out[uid]}
    depths = _bfs_depths(call_out, public_uids)
    unreachable_depth = max(depths.values()) + 1 if depths else 0

    rows: list[CascadeFanRow] = []
    for uid, meta in info.items():
        callers = call_in[uid]
        callees = call_out[uid]
        my_pkg = meta["package"]
        cross_in = sum(1 for c in callers if c in info and info[c]["package"] != my_pkg)
        cross_out = sum(1 for c in callees if c in info and info[c]["package"] != my_pkg)
        doc_signal = doc_signal_by_uid.get(uid, {})
        rows.append(
            CascadeFanRow(
                uid=uid,
                kind=meta["kind"],
                call_fan_in=call_fan_in[uid],
                call_fan_out=call_fan_out[uid],
                type_fan_in=type_fan_in[uid],
                type_fan_out=type_fan_out[uid],
                type_fan_in_param=type_fan_in_param[uid],
                type_fan_in_isinstance=type_fan_in_isinstance[uid],
                type_fan_in_return=type_fan_in_return[uid],
                type_fan_out_return=type_fan_out_return[uid],
                api_fan_in=api_fan_in[uid],
                api_fan_out=api_fan_out[uid],
                inject_fan_in=inject_fan_in[uid],
                depend_fan_in=depend_fan_in[uid],
                depend_fan_out=depend_fan_out[uid],
                handle_fan_in=handle_fan_in[uid],
                handle_fan_out=handle_fan_out[uid],
                decorated_in=decorated_in[uid],
                decorated_out=decorated_out[uid],
                construct_fan_out=construct_fan_out[uid],
                cross_package_call_in=float(cross_in),
                cross_package_call_out=float(cross_out),
                depth_from_public=depths.get(uid, unreachable_depth),
                doc_anchor_count=int(doc_counts.get(uid, 0)),
                import_in=int(import_in_per_uid.get(uid, 0)),
                reexport_in=int(reexport_in_per_uid.get(uid, 0)),
                doc_definition_weight=float(doc_signal.get("definition", 0.0)),
                doc_reference_weight=float(doc_signal.get("reference", 0.0)),
                doc_example_weight=float(doc_signal.get("example", 0.0)),
                is_proxy_binding=uid in proxy_uids,
            )
        )
    return rows


def _load_symbol_names(db, workspace_id: str) -> dict[str, tuple[str, str]]:
    with db.driver.session() as session:
        rows = session.run(
            """
            MATCH (f:File {workspace_id: $ws})-[:CONTAINS]->(s:Symbol)
            RETURN s.uid AS uid, s.name AS name, f.path AS path
            """,
            ws=workspace_id,
        )
        return {r["uid"]: (r["name"], r["path"]) for r in rows if r["uid"]}


def _roles_list(asn: SymbolRoleAssignment) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for role in (asn.primary, *asn.supporting):
        if role not in seen:
            seen.add(role)
            out.append(role)
    return out


def _kmeans_primary_roles(
    db, workspace_id: str
) -> tuple[dict[str, str], dict[str, list[str]], set[str]]:
    """Production Pass-1: cluster id → primary role + phantom roles in catalog."""
    rows = extract_symbol_rows(db, workspace_id)
    taxonomy, uid_to_cluster = cluster_symbols(rows)
    catalog = build_role_catalog(taxonomy).to_dict()

    cluster_to_role: dict[int, str] = {}
    cluster_claims: dict[int, list[tuple[str, float]]] = defaultdict(list)
    for role in catalog.get("role_to_archetypes") or {}:
        matches = resolve_role_clusters(catalog, role)
        if not matches:
            continue
        top = matches[0]
        cluster_claims[int(top["cluster_id"])].append((role, float(top["confidence"])))
    for cid, claims in cluster_claims.items():
        claims.sort(key=lambda item: item[1], reverse=True)
        cluster_to_role[cid] = claims[0][0]

    uid_primary: dict[str, str] = {}
    for uid, cid in uid_to_cluster.items():
        uid_primary[uid] = cluster_to_role.get(cid, "")

    catalog_roles_with_match: set[str] = set()
    for role in catalog.get("role_to_archetypes") or {}:
        if resolve_role_clusters(catalog, role, min_confidence=0.35):
            catalog_roles_with_match.add(role)

    return uid_primary, catalog, catalog_roles_with_match


def _print_l1_distribution(assignments: dict[str, SymbolRoleAssignment]) -> None:
    counts = Counter(a.l1 for a in assignments.values())
    print("\n=== CASCADE: L1 distribution ===")
    for bucket in L1_BUCKETS:
        if counts.get(bucket, 0):
            print(f"  {bucket:16s} {counts[bucket]}")


def _print_present_roles(present: dict[str, int], label: str) -> None:
    print(f"\n=== {label}: present roles (presence gate) ===")
    if not present:
        print("  (none)")
        return
    for role, count in present.items():
        print(f"  {role:24s} {count:5d}")


def _print_phantom_comparison(
    cascade_present: dict[str, int],
    kmeans_catalog_roles: set[str],
    all_cascade_roles: tuple[str, ...],
) -> None:
    cascade_set = set(cascade_present)
    kmeans_only = sorted(kmeans_catalog_roles - cascade_set)
    cascade_only = sorted(cascade_set - kmeans_catalog_roles)
    print("\n=== PHANTOM / ABSENT role comparison (cascade vs k-means catalog) ===")
    print(f"  k-means catalog roles (matched): {len(kmeans_catalog_roles)}")
    print(f"  cascade present roles:           {len(cascade_set)}")
    if kmeans_only:
        print(f"  in k-means catalog only (possible phantoms): {kmeans_only[:15]}")
        if len(kmeans_only) > 15:
            print(f"    ... +{len(kmeans_only) - 15} more")
    if cascade_only:
        print(f"  in cascade only: {cascade_only[:15]}")
    never_seen = sorted(set(all_cascade_roles) - cascade_set - kmeans_catalog_roles)
    if never_seen:
        print(f"  absent in both: {never_seen[:12]}")


def _print_target_symbols(
    assignments: dict[str, SymbolRoleAssignment],
    names: dict[str, tuple[str, str]],
    *,
    prod_path_hint: str = "",
) -> None:
    print("\n=== CASCADE: target symbols ===")
    by_name: dict[str, list[tuple[str, str, SymbolRoleAssignment]]] = defaultdict(list)
    for uid, asn in assignments.items():
        if uid not in names:
            continue
        name, path = names[uid]
        by_name[name].append((uid, path, asn))

    for sym in TARGET_SYMBOLS:
        entries = by_name.get(sym, [])
        if prod_path_hint:
            preferred = [e for e in entries if prod_path_hint in e[1]]
            entries = preferred or entries
        if not entries:
            print(f"  {sym:30s} NOT FOUND")
            continue
        _uid, path, asn = entries[0]
        roles = _roles_list(asn)
        expected = QA_EXPECTED.get(sym, ())
        missing = [r for r in expected if r not in roles] if expected else []
        qa = f" | qa_missing={missing}" if missing else (" | qa_ok" if expected else "")
        print(
            f"  {sym:30s} L1={asn.l1:14s} primary={asn.primary:22s} "
            f"supporting={list(asn.supporting)}{qa} | {Path(path).name}"
        )


def _print_multi_label_samples(
    assignments: dict[str, SymbolRoleAssignment],
    names: dict[str, tuple[str, str]],
    *,
    limit: int = 12,
) -> None:
    multi = [
        (uid, names[uid], asn)
        for uid, asn in assignments.items()
        if uid in names and asn.supporting
    ]
    multi.sort(key=lambda item: (-len(item[2].supporting), item[1][0]))
    print(f"\n=== CASCADE: multi-label samples (top {limit}) ===")
    for uid, (name, path), asn in multi[:limit]:
        print(
            f"  {name:30s} L1={asn.l1:14s} roles={_roles_list(asn)} | {Path(path).name}"
        )


def run_cascade_for_workspace(workspace_id: str, *, repo_label: str = "") -> None:
    from sidecar.indexer.role_clustering import (
        _query_doc_anchor_counts,
        _query_doc_anchor_signals,
        _query_file_import_in_counts,
    )

    db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    label = repo_label or workspace_id
    names = _load_symbol_names(db, workspace_id)

    print(f"\n{'=' * 72}")
    print(f"REPO / WORKSPACE: {label}")
    print(f"workspace_id={workspace_id}")

    symbols = _query_pass1_symbols(db, workspace_id)
    edges = _query_structural_edges(db, workspace_id)
    proxy_uids = _query_proxy_binding_uids(db, workspace_id)
    doc_counts = _query_doc_anchor_counts(db, workspace_id)
    doc_signals = _query_doc_anchor_signals(db, workspace_id)
    import_in = _query_file_import_in_counts(db, workspace_id)
    reexport_in = _query_reexport_in_counts(db, workspace_id)

    all_rows = assemble_cascade_rows(
        symbols, edges, doc_counts, import_in, doc_signals, proxy_uids, reexport_in
    )
    connected = [r for r in all_rows if r.structurally_connected]

    edge_counts = Counter(rel for _, _, rel, _, _ in edges)
    print("\n=== Edge inventory ===")
    for rel, count in edge_counts.most_common():
        print(f"  {rel}: {count}")
    print(
        f"\n  symbols={len(all_rows)} connected={len(connected)} "
        f"proxy_bindings={len(proxy_uids)}"
    )

    assignments = assign_all(connected)
    present = detect_present_roles(assignments)

    _print_l1_distribution(assignments)
    _print_present_roles(present, "CASCADE")

    # k-means baseline for phantom comparison
    try:
        _uid_primary, _catalog, kmeans_roles = _kmeans_primary_roles(db, workspace_id)
        _print_phantom_comparison(present, kmeans_roles, role_catalog_roles())
        print(f"\n=== K-MEANS baseline (production Pass-1) ===")
        print(f"  chosen_k from last run — see prototype_multidim_fan_clustering.py")
        print(f"  catalog roles with cluster match: {len(kmeans_roles)}")
    except Exception as exc:
        print(f"\n  (k-means baseline skipped: {exc})")

    prod_hint = ""
    if repo_label == "fastapi":
        prod_hint = "/fastapi/fastapi/"
    elif repo_label == "flask":
        prod_hint = "/flask/flask/"
    _print_target_symbols(assignments, names, prod_path_hint=prod_hint)
    _print_multi_label_samples(assignments, names)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prototype L1/L2 discriminator-first role cascade"
    )
    parser.add_argument("--repo", default="", help="Single QA repo id (e.g. fastapi)")
    parser.add_argument(
        "--repos",
        default="",
        help="Comma-separated repo ids (e.g. fastapi,flask). Overrides --repo.",
    )
    parser.add_argument("--workspace-id", default="", help="Override workspace uuid")
    args = parser.parse_args()

    if args.workspace_id:
        run_cascade_for_workspace(args.workspace_id)
        return

    repo_ids: list[str] = []
    if args.repos:
        repo_ids = [r.strip() for r in args.repos.split(",") if r.strip()]
    elif args.repo:
        repo_ids = [args.repo]
    else:
        repo_ids = ["fastapi"]

    resolver = WorkspaceResolver()
    for repo in repo_ids:
        project_path = default_repo_checkout_path(repo)
        ws = resolver.from_project_path(str(project_path)).id
        run_cascade_for_workspace(ws, repo_label=repo)


if __name__ == "__main__":
    main()
