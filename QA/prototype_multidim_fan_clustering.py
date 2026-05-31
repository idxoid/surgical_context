#!/usr/bin/env python3
"""Prototype Pass-1 clustering with multidimensional weighted fan features.

Compares production (call-only count fan) vs prototype (Σ confidence per edge
family) on an already-indexed workspace — no re-index required.

Usage:
    python QA/prototype_multidim_fan_clustering.py --repo fastapi
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QA_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(QA_DIR))

from qa_benchmark import default_repo_checkout_path  # noqa: E402

from sidecar.context.ranker.signal_constants import NOISE_PATH_PATTERNS  # noqa: E402
from sidecar.database.neo4j_client import Neo4jClient  # noqa: E402
from sidecar.indexer.role_clustering import (  # noqa: E402
    RoleCluster,
    RoleTaxonomy,
    SymbolRow,
    _ARCHETYPE_TEMPLATES,
    _ROLE_TO_ARCHETYPES,
    _column_means,
    _column_stds,
    _kmeans,
    _score_cluster_for_archetype,
    _silhouette_score,
    _standardize,
    assemble_symbol_rows,
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
}

USES_TYPE_KIND_WEIGHT: dict[str, float] = {
    "param": 1.0,
    "annotation": 0.8,
    "return": 0.6,
    "isinstance": 0.5,
}

PROTOTYPE_FEATURE_NAMES: tuple[str, ...] = (
    "log_call_fan_in",
    "log_call_fan_out",
    "call_fan_in_ratio",
    "call_leaf_score",
    "log_type_fan_in",
    "log_type_fan_out",
    "log_api_fan_in",
    "log_api_fan_out",
    "log_inject_fan_in",
    "log_depend_fan_in",
    "log_handle_fan_in",
    "depth_from_public",
    "cross_package_call_in_ratio",
    "log_import_in",
    "has_documentation",
    "doc_anchor_density",
    "log_doc_definition_weight",
    "log_doc_reference_weight",
    "log_doc_example_weight",
    "is_class",
    "is_function",
)

# Archetype templates expressed in prototype feature space.
PROTOTYPE_ARCHETYPE_TEMPLATES: dict[str, dict[str, float]] = {
    "active_entrypoint": {
        "log_call_fan_out": 1.0,
        "call_leaf_score": -1.0,
        "depth_from_public": -0.9,
        "is_function": 0.4,
    },
    "passive_api_surface": {
        "log_doc_definition_weight": 1.0,
        "log_doc_reference_weight": 0.7,
        "log_doc_example_weight": -0.4,
        "log_call_fan_in": 0.4,
        "has_documentation": 0.5,
    },
    "orchestrator": {
        "log_call_fan_out": 1.0,
        "call_leaf_score": -1.0,
        "cross_package_call_in_ratio": 0.6,
        "call_fan_in_ratio": -0.3,
    },
    "runtime_handle": {
        "log_call_fan_in": 1.0,
        "call_fan_in_ratio": 0.8,
        "cross_package_call_in_ratio": 0.8,
        "call_leaf_score": 0.3,
    },
    "representation_surface": {
        "is_class": 1.0,
        "log_type_fan_in": 1.0,
        "call_leaf_score": 0.5,
        "depth_from_public": 0.4,
        "is_function": -0.7,
        "log_call_fan_in": -0.3,
    },
    "executor": {
        "call_leaf_score": 1.0,
        "log_call_fan_in": 1.0,
        "is_function": 0.6,
        "depth_from_public": 0.3,
        "log_type_fan_in": -0.6,
        "is_class": -0.5,
        "has_documentation": -0.3,
    },
    "config_surface": {
        "log_doc_definition_weight": 0.9,
        "log_doc_reference_weight": 0.6,
        "call_fan_in_ratio": 0.5,
        "call_leaf_score": 0.3,
    },
}

TARGET_SYMBOLS = (
    "run_endpoint_function",
    "request_body_to_args",
    "serialize_response",
    "get_request_handler",
    "solve_dependencies",
    "jsonable_encoder",
    "ENCODERS_BY_TYPE",
    "Default",
    "Param",
    "UJSONResponse",
)

@dataclass(frozen=True)
class WeightedFanRow:
    uid: str
    kind: str
    call_fan_in: float
    call_fan_out: float
    type_fan_in: float
    type_fan_out: float
    api_fan_in: float
    api_fan_out: float
    inject_fan_in: float
    depend_fan_in: float
    depend_fan_out: float
    handle_fan_in: float
    cross_package_call_in: float
    cross_package_call_out: float
    depth_from_public: int
    doc_anchor_count: int
    import_in: int
    doc_definition_weight: float
    doc_reference_weight: float
    doc_example_weight: float

    @property
    def call_dangling(self) -> bool:
        return self.call_fan_in == 0.0 and self.call_fan_out == 0.0

    @property
    def structurally_connected(self) -> bool:
        return any(
            v > 0.0
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
            )
        )

    @property
    def true_dangling(self) -> bool:
        return not self.structurally_connected


def exclude_true_dangling(rows: list[WeightedFanRow]) -> list[WeightedFanRow]:
    """Drop symbols with no position in any structural edge family."""
    return [row for row in rows if row.structurally_connected]


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


def _query_structural_edges(db, workspace_id: str) -> list[tuple[str, str, str, float]]:
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
        rows: list[tuple[str, str, str, float]] = []
        for record in result:
            caller = record["caller_uid"]
            callee = record["callee_uid"]
            if not caller or not callee or caller == callee:
                continue
            rel_type = record["rel_type"]
            conf = _edge_confidence(rel_type, record["confidence"], record["kind"] or "")
            rows.append((caller, callee, rel_type, conf))
        return rows


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


def assemble_weighted_rows(
    symbols: list[tuple[str, str, str]],
    edges: list[tuple[str, str, str, float]],
    doc_counts: dict[str, int],
    import_in_per_uid: dict[str, int],
    doc_signal_by_uid: dict[str, dict[str, float]],
) -> list[WeightedFanRow]:
    if not symbols:
        return []

    info: dict[str, dict] = {}
    for uid, kind, file_path in symbols:
        info[uid] = {
            "kind": kind or "",
            "package": os.path.dirname(file_path or ""),
        }

    call_out: dict[str, set[str]] = {uid: set() for uid in info}
    call_in: dict[str, set[str]] = {uid: set() for uid in info}

    call_fan_in: dict[str, float] = defaultdict(float)
    call_fan_out: dict[str, float] = defaultdict(float)
    type_fan_in: dict[str, float] = defaultdict(float)
    type_fan_out: dict[str, float] = defaultdict(float)
    api_fan_in: dict[str, float] = defaultdict(float)
    api_fan_out: dict[str, float] = defaultdict(float)
    inject_fan_in: dict[str, float] = defaultdict(float)
    depend_fan_in: dict[str, float] = defaultdict(float)
    depend_fan_out: dict[str, float] = defaultdict(float)
    handle_fan_in: dict[str, float] = defaultdict(float)

    for caller, callee, rel_type, conf in edges:
        if caller not in info or callee not in info:
            continue
        if rel_type in CALL_REL_TYPES:
            call_out[caller].add(callee)
            call_in[callee].add(caller)
            call_fan_out[caller] += conf
            call_fan_in[callee] += conf
        elif rel_type == "USES_TYPE":
            type_fan_out[caller] += conf
            type_fan_in[callee] += conf
        elif rel_type in {"HAS_API", "INHERITED_API"}:
            api_fan_out[caller] += conf
            api_fan_in[callee] += conf
        elif rel_type == "INJECTS":
            inject_fan_in[callee] += conf
        elif rel_type == "DEPENDS_ON":
            depend_fan_out[caller] += conf
            depend_fan_in[callee] += conf
        elif rel_type == "HANDLES":
            handle_fan_in[callee] += conf

    public_uids = {
        uid for uid in info if call_fan_in[uid] == 0.0 and call_out[uid]
    }
    depths = _bfs_depths(call_out, public_uids)
    if depths:
        unreachable_depth = max(depths.values()) + 1
    else:
        unreachable_depth = 0

    rows: list[WeightedFanRow] = []
    for uid, meta in info.items():
        callers = call_in[uid]
        callees = call_out[uid]
        my_pkg = meta["package"]
        cross_in = sum(1 for c in callers if info[c]["package"] != my_pkg)
        cross_out = sum(1 for c in callees if info[c]["package"] != my_pkg)
        doc_signal = doc_signal_by_uid.get(uid, {})
        rows.append(
            WeightedFanRow(
                uid=uid,
                kind=meta["kind"],
                call_fan_in=call_fan_in[uid],
                call_fan_out=call_fan_out[uid],
                type_fan_in=type_fan_in[uid],
                type_fan_out=type_fan_out[uid],
                api_fan_in=api_fan_in[uid],
                api_fan_out=api_fan_out[uid],
                inject_fan_in=inject_fan_in[uid],
                depend_fan_in=depend_fan_in[uid],
                depend_fan_out=depend_fan_out[uid],
                handle_fan_in=handle_fan_in[uid],
                cross_package_call_in=float(cross_in),
                cross_package_call_out=float(cross_out),
                depth_from_public=depths.get(uid, unreachable_depth),
                doc_anchor_count=int(doc_counts.get(uid, 0)),
                import_in=int(import_in_per_uid.get(uid, 0)),
                doc_definition_weight=float(doc_signal.get("definition", 0.0)),
                doc_reference_weight=float(doc_signal.get("reference", 0.0)),
                doc_example_weight=float(doc_signal.get("example", 0.0)),
            )
        )
    return rows


def _features_for_weighted(row: WeightedFanRow) -> tuple[float, ...]:
    call_in = max(0.0, row.call_fan_in)
    call_out = max(0.0, row.call_fan_out)
    call_total = max(1.0, call_in + call_out)
    doc_count = max(0, row.doc_anchor_count)
    is_class = 1.0 if row.kind in {"class", "interface"} else 0.0
    is_function = 1.0 if row.kind in {"function", "method"} else 0.0

    return (
        math.log1p(call_in),
        math.log1p(call_out),
        call_in / call_total,
        1.0 if call_out == 0.0 else 0.0,
        math.log1p(max(0.0, row.type_fan_in)),
        math.log1p(max(0.0, row.type_fan_out)),
        math.log1p(max(0.0, row.api_fan_in)),
        math.log1p(max(0.0, row.api_fan_out)),
        math.log1p(max(0.0, row.inject_fan_in)),
        math.log1p(max(0.0, row.depend_fan_in)),
        math.log1p(max(0.0, row.handle_fan_in)),
        float(max(0, row.depth_from_public)),
        row.cross_package_call_in / max(1.0, call_in),
        math.log1p(max(0, row.import_in)),
        1.0 if doc_count > 0 else 0.0,
        math.log1p(doc_count),
        math.log1p(max(0.0, row.doc_definition_weight)),
        math.log1p(max(0.0, row.doc_reference_weight)),
        math.log1p(max(0.0, row.doc_example_weight)),
        is_class,
        is_function,
    )


def _signature_for_features(
    feature_names: tuple[str, ...],
    centroid: list[float],
    feature_means: list[float],
    feature_stds: list[float],
) -> tuple[str, ...]:
    z_scores = [
        (name, (val - mean) / std)
        for name, val, mean, std in zip(
            feature_names,
            centroid,
            feature_means,
            feature_stds,
            strict=True,
        )
    ]
    z_scores.sort(key=lambda item: abs(item[1]), reverse=True)
    return tuple(f"{name}:{'+' if z >= 0 else '-'}" for name, z in z_scores[:3])


def cluster_weighted_rows(
    rows: list[WeightedFanRow],
    *,
    k_min: int = 5,
    k_max: int = 8,
    max_iter: int = 50,
    seed: int = 0,
    max_silhouette_samples: int = 800,
) -> tuple[RoleTaxonomy, dict[str, int]]:
    if not rows:
        return RoleTaxonomy(PROTOTYPE_FEATURE_NAMES, (), 0.0, 0, 0), {}

    feature_vectors = [_features_for_weighted(row) for row in rows]
    standardized = _standardize(feature_vectors)

    if len(rows) < k_min:
        return (
            RoleTaxonomy(
                PROTOTYPE_FEATURE_NAMES,
                (
                    RoleCluster(
                        0,
                        tuple(standardized[0]),
                        len(rows),
                        _signature_for_features(
                            PROTOTYPE_FEATURE_NAMES,
                            standardized[0],
                            _column_means(standardized),
                            _column_stds(standardized, _column_means(standardized)),
                        ),
                    ),
                ),
                0.0,
                1,
                len(rows),
            ),
            {row.uid: 0 for row in rows},
        )

    rng = random.Random(seed)
    best_k = k_min
    best_score = -math.inf
    best_assignments: list[int] = []
    best_centroids: list[list[float]] = []

    for k in range(k_min, k_max + 1):
        if k > len(rows):
            break
        centroids, assignments = _kmeans(standardized, k, max_iter=max_iter, rng=rng)
        score = _silhouette_score(
            standardized,
            assignments,
            k,
            max_samples=max_silhouette_samples,
            seed=seed + k,
        )
        if score > best_score:
            best_score = score
            best_k = k
            best_assignments = assignments
            best_centroids = centroids

    feature_means = _column_means(standardized)
    feature_stds = _column_stds(standardized, feature_means)
    member_counts = [0] * best_k
    for cid in best_assignments:
        member_counts[cid] += 1

    clusters = tuple(
        RoleCluster(
            cluster_id=cid,
            centroid=tuple(best_centroids[cid]),
            member_count=member_counts[cid],
            signature=_signature_for_features(
                PROTOTYPE_FEATURE_NAMES,
                best_centroids[cid],
                feature_means,
                feature_stds,
            ),
        )
        for cid in range(best_k)
    )
    taxonomy = RoleTaxonomy(
        feature_names=PROTOTYPE_FEATURE_NAMES,
        clusters=clusters,
        silhouette=best_score if best_score > -math.inf else 0.0,
        chosen_k=best_k,
        sample_size=len(rows),
    )
    uid_to_cluster = {row.uid: cid for row, cid in zip(rows, best_assignments, strict=True)}
    return taxonomy, uid_to_cluster


def build_prototype_catalog(taxonomy: RoleTaxonomy) -> dict:
    archetypes: dict[str, list[dict]] = {}
    for archetype, template in PROTOTYPE_ARCHETYPE_TEMPLATES.items():
        scored: list[tuple[float, dict]] = []
        for cluster in taxonomy.clusters:
            confidence, evidence = _score_cluster_for_archetype(
                taxonomy.feature_names,
                cluster,
                template,
            )
            if confidence >= 0.35:
                scored.append(
                    (
                        confidence,
                        {
                            "archetype": archetype,
                            "cluster_id": cluster.cluster_id,
                            "confidence": confidence,
                            "evidence": list(evidence),
                        },
                    )
                )
        scored.sort(key=lambda item: item[0], reverse=True)
        archetypes[archetype] = [item[1] for item in scored[:3]]
    return {
        "archetypes": archetypes,
        "role_to_archetypes": {role: list(arches) for role, arches in _ROLE_TO_ARCHETYPES.items()},
    }


def _build_cluster_to_role_map(catalog: dict) -> dict[int, str]:
    """Mirror production ``UnifiedRanker._build_cluster_to_role_map``."""
    cluster_claims: dict[int, list[tuple[str, float]]] = {}
    for role in catalog.get("role_to_archetypes") or []:
        matches = resolve_role_clusters(catalog, role)
        if not matches:
            continue
        top = matches[0]
        cluster_claims.setdefault(int(top["cluster_id"]), []).append(
            (role, float(top["confidence"]))
        )
    result: dict[int, str] = {}
    for cid, claims in cluster_claims.items():
        claims.sort(key=lambda item: item[1], reverse=True)
        result[cid] = claims[0][0]
    return result


def _symbol_is_call_leaf_worker(wf: WeightedFanRow) -> bool:
    """Leaf function with call fan-in and without type-dominated profile."""
    if wf.kind not in {"function", "method"}:
        return False
    if wf.call_fan_out > 0.0 or wf.call_fan_in <= 0.0:
        return False
    if wf.type_fan_in > max(2.0, wf.call_fan_in * 3.0):
        return False
    return True


def infer_symbol_roles(
    wf: WeightedFanRow | None,
    cluster_id: int,
    catalog: dict,
    cluster_to_role: dict[int, str],
) -> list[str]:
    """Cluster role + symbol-level structural bumps (prototype query-time layer)."""
    roles: list[str] = []
    primary = cluster_to_role.get(cluster_id)
    if primary:
        roles.append(primary)

    if wf and _symbol_is_call_leaf_worker(wf):
        roles.append("executor")

    # Production maps runtime_surface through active_entrypoint/runtime_handle too.
    for role in ("runtime_surface", "executor"):
        for match in resolve_role_clusters(catalog, role):
            if int(match["cluster_id"]) == cluster_id and role not in roles:
                roles.append(role)

    deduped: list[str] = []
    seen: set[str] = set()
    for role in roles:
        if role not in seen:
            seen.add(role)
            deduped.append(role)
    return deduped


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


def _print_executor_cluster(
    label: str,
    taxonomy: RoleTaxonomy,
    catalog: dict,
    uid_to_cluster: dict[str, int],
    names: dict[str, tuple[str, str]],
    weighted_by_uid: dict[str, WeightedFanRow] | None = None,
) -> None:
    exec_clusters = resolve_role_clusters(catalog, "executor")
    print(f"\n=== {label}: executor role → clusters ===")
    for match in exec_clusters[:5]:
        print(
            f"  cluster {match['cluster_id']}: conf={match['confidence']:.3f} "
            f"via {match['archetype']} evidence={match.get('evidence')}"
        )

    if not exec_clusters:
        print("  (no executor clusters)")
        return

    primary_cid = exec_clusters[0]["cluster_id"]
    members = [
        (uid, names[uid])
        for uid, cid in uid_to_cluster.items()
        if cid == primary_cid and uid in names
    ]
    print(f"\n=== {label}: primary executor cluster {primary_cid} ({len(members)} members) ===")
    scored: list[tuple[float, str, str, WeightedFanRow | None]] = []
    for uid, (name, path) in members:
        wf = weighted_by_uid.get(uid) if weighted_by_uid else None
        key = wf.call_fan_in if wf else 0.0
        scored.append((key, name, path, wf))
    for call_in, name, path, wf in sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)[:25]:
        extras = ""
        if wf:
            extras = (
                f" call_in={wf.call_fan_in:.1f} call_out={wf.call_fan_out:.1f} "
                f"type_in={wf.type_fan_in:.0f} api_in={wf.api_fan_in:.1f}"
            )
        prod = "/fastapi/fastapi/" in path
        flag = "" if prod else " [non-prod path]"
        print(f"  {call_in:5.1f} | {name:35s} | {Path(path).name}{extras}{flag}")


def _print_target_symbols(
    label: str,
    uid_to_cluster: dict[str, int],
    names: dict[str, tuple[str, str]],
    weighted_by_uid: dict[str, WeightedFanRow],
    catalog: dict,
    cluster_to_role: dict[int, str],
    *,
    pass1_uids: set[str] | None = None,
    qa_expected: dict[str, tuple[str, ...]] | None = None,
) -> None:
    print(f"\n=== {label}: target symbols ===")
    by_name: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for uid, (name, path) in names.items():
        if pass1_uids is not None and uid not in pass1_uids:
            continue
        if uid not in uid_to_cluster:
            continue
        by_name[name].append((uid, path, uid_to_cluster[uid]))

    for sym in TARGET_SYMBOLS:
        entries = by_name.get(sym, [])
        prod = [e for e in entries if "/fastapi/fastapi/" in e[1]]
        entries = prod or entries[:1]
        if not entries:
            candidates = [
                (uid, path)
                for uid, (name, path) in names.items()
                if name == sym and (pass1_uids is None or uid in pass1_uids)
            ]
            prod_c = [c for c in candidates if "/fastapi/fastapi/" in c[1]]
            candidates = prod_c or candidates[:1]
            if not candidates:
                print(f"  {sym:30s} NOT IN PASS-1")
                continue
            uid, path = candidates[0]
            wf = weighted_by_uid.get(uid)
            if wf and wf.true_dangling:
                print(f"  {sym:30s} EXCLUDED (true-dangling) | {Path(path).name}")
            else:
                print(f"  {sym:30s} NOT CLUSTERED | {Path(path).name}")
            continue
        for uid, path, cid in entries[:1]:
            wf = weighted_by_uid.get(uid)
            roles = infer_symbol_roles(wf, cid, catalog, cluster_to_role)
            detail = ""
            if wf:
                detail = (
                    f" call_in={wf.call_fan_in:.1f} call_out={wf.call_fan_out:.1f} "
                    f"type_in={wf.type_fan_in:.0f} leaf={wf.call_fan_out == 0.0}"
                )
            qa = ""
            if qa_expected and sym in qa_expected:
                missing = [r for r in qa_expected[sym] if r not in roles]
                qa = f" | qa_missing={missing or '-'}"
            print(
                f"  {sym:30s} cluster={cid} roles={roles}{detail}{qa} | {Path(path).name}"
            )


def run_prototype(workspace_id: str) -> None:
    from sidecar.indexer.role_clustering import (
        _query_doc_anchor_counts,
        _query_doc_anchor_signals,
        _query_file_import_in_counts,
    )

    db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    names = _load_symbol_names(db, workspace_id)

    print(f"workspace_id={workspace_id}")

    # Baseline (production Pass-1)
    baseline_rows = extract_symbol_rows(db, workspace_id)
    baseline_taxonomy, baseline_map = cluster_symbols(baseline_rows)
    baseline_catalog = build_role_catalog(baseline_taxonomy).to_dict()

    baseline_dangling = sum(1 for r in baseline_rows if r.fan_in == 0 and r.fan_out == 0)
    prod_dangling = sum(
        1
        for r in baseline_rows
        if r.fan_in == 0
        and r.fan_out == 0
        and r.uid in names
        and "/fastapi/fastapi/" in names[r.uid][1]
    )

    # Prototype
    symbols = _query_pass1_symbols(db, workspace_id)
    edges = _query_structural_edges(db, workspace_id)
    doc_counts = _query_doc_anchor_counts(db, workspace_id)
    doc_signals = _query_doc_anchor_signals(db, workspace_id)
    import_in = _query_file_import_in_counts(db, workspace_id)
    weighted_rows = assemble_weighted_rows(symbols, edges, doc_counts, import_in, doc_signals)
    weighted_by_uid = {r.uid: r for r in weighted_rows}
    pass1_uids = {r.uid for r in weighted_rows}

    proto_call_dangling = sum(1 for r in weighted_rows if r.call_dangling)
    proto_true_dangling = sum(1 for r in weighted_rows if r.true_dangling)
    proto_reconnected = proto_call_dangling - proto_true_dangling
    prod_true_dangling = sum(
        1
        for r in weighted_rows
        if r.true_dangling and r.uid in names and "/fastapi/fastapi/" in names[r.uid][1]
    )

    proto_taxonomy, proto_map = cluster_weighted_rows(weighted_rows)
    proto_catalog = build_prototype_catalog(proto_taxonomy)

    connected_rows = exclude_true_dangling(weighted_rows)
    filtered_taxonomy, filtered_map = cluster_weighted_rows(connected_rows)
    filtered_catalog = build_prototype_catalog(filtered_taxonomy)
    filtered_cluster_to_role = _build_cluster_to_role_map(filtered_catalog)
    baseline_cluster_to_role = _build_cluster_to_role_map(baseline_catalog)
    proto_cluster_to_role = _build_cluster_to_role_map(proto_catalog)

    qa_expected = {
        "run_endpoint_function": ("executor", "runtime_surface"),
    }

    print("\n=== cluster → primary role (no-dangling) ===")
    for cid in sorted(filtered_cluster_to_role):
        print(f"  cluster {cid}: {filtered_cluster_to_role[cid]}")

    edge_type_counts = Counter(rel for _, _, rel, _ in edges)

    print("\n=== Edge inventory (prototype projection) ===")
    for rel, count in edge_type_counts.most_common():
        print(f"  {rel}: {count}")

    print("\n=== Dangling comparison ===")
    print(f"  Baseline call-dangling (Pass-1):     {baseline_dangling}/{len(baseline_rows)} ({100*baseline_dangling/len(baseline_rows):.0f}%)")
    print(f"    of which fastapi/fastapi/:         {prod_dangling}")
    print(f"  Prototype call-dangling:             {proto_call_dangling}/{len(weighted_rows)} ({100*proto_call_dangling/len(weighted_rows):.0f}%)")
    print(f"  Prototype true-dangling (all dims=0): {proto_true_dangling}/{len(weighted_rows)} ({100*proto_true_dangling/len(weighted_rows):.0f}%)")
    print(f"    of which fastapi/fastapi/:         {prod_true_dangling}")
    print(f"  Reconnected via non-call edges:      {proto_reconnected}")
    print(f"  Clustering input after exclude:      {len(connected_rows)}/{len(weighted_rows)} ({100*len(connected_rows)/len(weighted_rows):.0f}%)")

    print("\n=== Taxonomy ===")
    print(
        f"  Baseline:           k={baseline_taxonomy.chosen_k} silhouette={baseline_taxonomy.silhouette:.4f} n={baseline_taxonomy.sample_size}"
    )
    print(
        f"  Prototype (all):    k={proto_taxonomy.chosen_k} silhouette={proto_taxonomy.silhouette:.4f} n={proto_taxonomy.sample_size}"
    )
    print(
        f"  Prototype (no-dang): k={filtered_taxonomy.chosen_k} silhouette={filtered_taxonomy.silhouette:.4f} n={filtered_taxonomy.sample_size}"
    )

    print("\n=== Cluster signatures (prototype, no-dangling) ===")
    for cluster in filtered_taxonomy.clusters:
        print(f"  cluster {cluster.cluster_id}: n={cluster.member_count} sig={cluster.signature}")

    _print_executor_cluster(
        "BASELINE",
        baseline_taxonomy,
        baseline_catalog,
        baseline_map,
        names,
    )
    _print_executor_cluster(
        "PROTOTYPE (all)",
        proto_taxonomy,
        proto_catalog,
        proto_map,
        names,
        weighted_by_uid,
    )
    _print_executor_cluster(
        "PROTOTYPE (no-dangling)",
        filtered_taxonomy,
        filtered_catalog,
        filtered_map,
        names,
        weighted_by_uid,
    )

    _print_target_symbols(
        "BASELINE",
        baseline_map,
        names,
        weighted_by_uid,
        baseline_catalog,
        baseline_cluster_to_role,
    )
    _print_target_symbols(
        "PROTOTYPE (all)",
        proto_map,
        names,
        weighted_by_uid,
        proto_catalog,
        proto_cluster_to_role,
    )
    _print_target_symbols(
        "PROTOTYPE (no-dangling)",
        filtered_map,
        names,
        weighted_by_uid,
        filtered_catalog,
        filtered_cluster_to_role,
        pass1_uids=pass1_uids,
        qa_expected=qa_expected,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Prototype multidimensional weighted fan clustering")
    parser.add_argument("--repo", default="fastapi", help="QA repo id (default: fastapi)")
    parser.add_argument("--workspace-id", default="", help="Override workspace id")
    args = parser.parse_args()

    if args.workspace_id:
        workspace_id = args.workspace_id
    else:
        project_path = default_repo_checkout_path(args.repo)
        workspace_id = WorkspaceResolver().from_project_path(str(project_path)).id

    run_prototype(workspace_id)


if __name__ == "__main__":
    main()
