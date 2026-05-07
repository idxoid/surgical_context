"""Pass 1: derive a per-repository role taxonomy from call-graph topology.

This module is the universal replacement for the hand-curated role naming
that currently lives in three places: ``mechanism_registry``,
``repository_profile`` generic archetype plans, and ``unified_ranker._infer_role``.
A symbol's role comes from its position in the call graph — fan-in/out, depth
from public exports, cross-package edges, doc-anchor density, kind — never from
framework names or benchmark fixture strings.

Output:
- per-symbol cluster id
- a workspace-level ``RoleTaxonomy`` describing each cluster (centroid,
  member count, structural signature)
- a workspace-level ``RoleCatalog`` mapping unstable cluster ids to portable
  structural archetypes such as active entrypoint and runtime handle

Consumers (mechanism mining, ranker, repository_profile) can read the catalog
without hard-coding framework families. The current pass persists taxonomy/catalog
metadata but does not yet cut every query-time fallback over to these derived roles.
"""

from __future__ import annotations

import json
import math
import os
import random
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from sidecar.context.mechanism_registry import merge_preloaded_mechanisms_into_role_catalog

ROLE_TAXONOMY_SCHEMA_VERSION = 1
ROLE_CATALOG_SCHEMA_VERSION = 2  # v2: mechanism_required_roles + mechanism_role_backfill in JSON


_FEATURE_NAMES: tuple[str, ...] = (
    "log_fan_in",
    "log_fan_out",
    "fan_in_ratio",
    "depth_from_public",
    "leaf_score",
    "cross_package_in_ratio",
    "cross_package_out_ratio",
    "log_import_in",
    "has_documentation",
    "doc_anchor_density",
    "log_doc_definition_weight",
    "log_doc_reference_weight",
    "log_doc_example_weight",
    "is_class",
    "is_function",
)


@dataclass(frozen=True)
class SymbolRow:
    """Structural facts about one symbol, gathered from Neo4j.

    Decoupled from the database client so clustering stays pure and the
    same algorithm can run against synthetic graphs in unit tests.
    """

    uid: str
    kind: str
    fan_in: int
    fan_out: int
    cross_package_in: int
    cross_package_out: int
    depth_from_public: int
    doc_anchor_count: int
    import_in: int = 0
    doc_definition_weight: float = 0.0
    doc_reference_weight: float = 0.0
    doc_example_weight: float = 0.0


@dataclass(frozen=True)
class RoleCluster:
    cluster_id: int
    centroid: tuple[float, ...]
    member_count: int
    signature: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "cluster_id": self.cluster_id,
            "centroid": [round(v, 4) for v in self.centroid],
            "member_count": self.member_count,
            "signature": list(self.signature),
        }


@dataclass(frozen=True)
class RoleTaxonomy:
    feature_names: tuple[str, ...]
    clusters: tuple[RoleCluster, ...]
    silhouette: float
    chosen_k: int
    sample_size: int

    def to_dict(self) -> dict:
        return {
            "feature_names": list(self.feature_names),
            "clusters": [c.to_dict() for c in self.clusters],
            "silhouette": round(self.silhouette, 4),
            "chosen_k": self.chosen_k,
            "sample_size": self.sample_size,
        }


@dataclass(frozen=True)
class RoleArchetypeMatch:
    archetype: str
    cluster_id: int
    confidence: float
    evidence: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "archetype": self.archetype,
            "cluster_id": self.cluster_id,
            "confidence": round(self.confidence, 4),
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class RoleCatalog:
    """Repository-local mapping from structural clusters to portable role shapes."""

    archetypes: dict[str, tuple[RoleArchetypeMatch, ...]]
    role_to_archetypes: dict[str, tuple[str, ...]]
    schema_version: int = ROLE_CATALOG_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "archetypes": {
                name: [match.to_dict() for match in matches]
                for name, matches in sorted(self.archetypes.items())
            },
            "role_to_archetypes": {
                role: list(archetypes)
                for role, archetypes in sorted(self.role_to_archetypes.items())
            },
        }


_ROLE_TO_ARCHETYPES: dict[str, tuple[str, ...]] = {
    "api_surface": ("passive_api_surface", "active_entrypoint"),
    "factory_surface": ("active_entrypoint", "orchestrator"),
    "config_surface": ("config_surface", "passive_api_surface"),
    "representation_surface": ("representation_surface", "passive_api_surface"),
    "runtime_surface": ("active_entrypoint", "runtime_handle", "executor"),
    "executor": ("executor", "runtime_handle"),
    "orchestrator": ("orchestrator", "active_entrypoint"),
    "schema_builder": ("orchestrator", "runtime_handle"),
    "binding_surface": ("active_entrypoint", "orchestrator"),
    "composition_surface": ("orchestrator", "active_entrypoint"),
    "integration_surface": ("orchestrator", "active_entrypoint"),
    "core_runtime": ("runtime_handle", "executor"),
    "validator_handle": ("runtime_handle",),
    "serializer_handle": ("runtime_handle",),
    "compat_bridge": ("passive_api_surface", "representation_surface"),
    "error_surface": ("representation_surface", "runtime_handle"),
    "impact_runtime": ("runtime_handle", "executor", "orchestrator"),
    "impact_public_api": ("passive_api_surface", "active_entrypoint"),
    "impact_test_surface": ("executor", "passive_api_surface"),
    "docs_or_concept": ("passive_api_surface",),
    "supporting_surface": ("executor", "representation_surface"),
}


_ARCHETYPE_TEMPLATES: dict[str, dict[str, float]] = {
    "active_entrypoint": {
        "log_fan_out": 1.0,
        "leaf_score": -1.0,
        "depth_from_public": -0.9,
        "is_function": 0.4,
    },
    "passive_api_surface": {
        "log_doc_definition_weight": 1.0,
        "log_doc_reference_weight": 0.7,
        "log_doc_example_weight": -0.4,
        "log_fan_in": 0.4,
        "has_documentation": 0.5,
    },
    "orchestrator": {
        "log_fan_out": 1.0,
        "leaf_score": -1.0,
        "cross_package_out_ratio": 0.6,
        "fan_in_ratio": -0.3,
    },
    "runtime_handle": {
        "log_fan_in": 1.0,
        "fan_in_ratio": 0.8,
        "cross_package_in_ratio": 0.8,
        "leaf_score": 0.3,
    },
    "representation_surface": {
        "is_class": 1.0,
        "leaf_score": 0.5,
        "is_function": -0.7,
    },
    "executor": {
        "leaf_score": 1.0,
        "depth_from_public": 0.5,
        "log_fan_in": 0.5,
        "has_documentation": -0.3,
    },
    "config_surface": {
        "log_doc_definition_weight": 0.9,
        "log_doc_reference_weight": 0.6,
        "fan_in_ratio": 0.5,
        "leaf_score": 0.3,
    },
}


def cluster_symbols(
    rows: Sequence[SymbolRow],
    *,
    k_min: int = 5,
    k_max: int = 8,
    max_iter: int = 50,
    seed: int = 0,
    max_silhouette_samples: int = 800,
) -> tuple[RoleTaxonomy, dict[str, int]]:
    """Cluster symbols by structural features.

    Returns ``(taxonomy, uid_to_cluster_id)``. For inputs smaller than
    ``k_min`` the result is a single trivial cluster — the silhouette
    metric is meaningless at that scale and a real role taxonomy needs
    enough symbols to differentiate.
    """
    if not rows:
        return _empty_taxonomy(), {}

    feature_vectors = [_features_for(row) for row in rows]
    standardized = _standardize(feature_vectors)

    if len(rows) < k_min:
        return _single_cluster_taxonomy(rows, standardized), {row.uid: 0 for row in rows}

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

    if not best_assignments:
        return _single_cluster_taxonomy(rows, standardized), {row.uid: 0 for row in rows}

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
            signature=_signature_for(best_centroids[cid], feature_means, feature_stds),
        )
        for cid in range(best_k)
    )

    taxonomy = RoleTaxonomy(
        feature_names=_FEATURE_NAMES,
        clusters=clusters,
        silhouette=best_score if best_score > -math.inf else 0.0,
        chosen_k=best_k,
        sample_size=len(rows),
    )
    uid_to_cluster = {row.uid: cid for row, cid in zip(rows, best_assignments, strict=True)}
    return taxonomy, uid_to_cluster


def build_role_catalog(taxonomy: RoleTaxonomy) -> RoleCatalog:
    """Auto-resolve structural clusters into portable role archetypes.

    Cluster ids are local to a re-index. The durable layer is this confidence
    mapping from archetype names to clusters by centroid shape.
    """
    archetypes: dict[str, tuple[RoleArchetypeMatch, ...]] = {}
    for archetype, template in _ARCHETYPE_TEMPLATES.items():
        scored: list[RoleArchetypeMatch] = []
        for cluster in taxonomy.clusters:
            confidence, evidence = _score_cluster_for_archetype(
                taxonomy.feature_names,
                cluster,
                template,
            )
            if confidence >= 0.35:
                scored.append(
                    RoleArchetypeMatch(
                        archetype=archetype,
                        cluster_id=cluster.cluster_id,
                        confidence=confidence,
                        evidence=evidence,
                    )
                )
        if not scored and taxonomy.clusters:
            cluster = taxonomy.clusters[0]
            confidence, evidence = _score_cluster_for_archetype(
                taxonomy.feature_names,
                cluster,
                template,
            )
            scored.append(
                RoleArchetypeMatch(
                    archetype=archetype,
                    cluster_id=cluster.cluster_id,
                    confidence=confidence,
                    evidence=evidence,
                )
            )
        archetypes[archetype] = tuple(
            sorted(scored, key=lambda match: match.confidence, reverse=True)[:3]
        )

    return RoleCatalog(
        archetypes=archetypes,
        role_to_archetypes=_ROLE_TO_ARCHETYPES,
    )


def resolve_role_clusters(
    catalog: RoleCatalog | dict,
    role: str,
    *,
    min_confidence: float = 0.35,
) -> list[dict]:
    """Resolve a canonical role to repo-local clusters.

    Preserves the archetype preference order from ``_ROLE_TO_ARCHETYPES``:
    the first archetype with at least one qualifying match owns the top
    slots, sorted by confidence within that archetype. Subsequent archetypes
    contribute fallbacks. Returned cluster ids are preferences, not hard
    filters — cluster ids can shift after re-index.
    """
    data = catalog.to_dict() if isinstance(catalog, RoleCatalog) else catalog
    archetype_names = (data.get("role_to_archetypes") or {}).get(role, [])
    archetypes = data.get("archetypes") or {}
    matches: list[dict] = []
    seen: set[int] = set()
    for archetype in archetype_names:
        local: list[dict] = []
        for match in archetypes.get(archetype, []):
            cluster_id = match.get("cluster_id")
            confidence = float(match.get("confidence", 0.0))
            if cluster_id in seen or confidence < min_confidence:
                continue
            seen.add(cluster_id)
            local.append(
                {
                    "cluster_id": cluster_id,
                    "confidence": confidence,
                    "archetype": archetype,
                    "evidence": list(match.get("evidence") or []),
                }
            )
        local.sort(key=lambda item: item["confidence"], reverse=True)
        matches.extend(local)
    return matches


def _score_cluster_for_archetype(
    feature_names: tuple[str, ...],
    cluster: RoleCluster,
    template: dict[str, float],
) -> tuple[float, tuple[str, ...]]:
    values = dict(zip(feature_names, cluster.centroid, strict=True))
    weighted = 0.0
    total = 0.0
    evidence: list[tuple[str, float]] = []
    for feature, weight in template.items():
        value = values.get(feature, 0.0)
        contribution = _positive(value) if weight >= 0 else _positive(-value)
        abs_weight = abs(weight)
        weighted += contribution * abs_weight
        total += abs_weight
        if contribution >= 0.25:
            evidence.append((f"{feature}:{'+' if weight >= 0 else '-'}", contribution))
    confidence = weighted / total if total else 0.0
    evidence.sort(key=lambda item: item[1], reverse=True)
    return round(confidence, 4), tuple(item[0] for item in evidence[:3])


def _positive(value: float) -> float:
    return max(0.0, min(1.0, value / 1.5))


def _features_for(row: SymbolRow) -> tuple[float, ...]:
    fan_in = max(0, row.fan_in)
    fan_out = max(0, row.fan_out)
    total_io = max(1, fan_in + fan_out)
    log_fan_in = math.log1p(fan_in)
    log_fan_out = math.log1p(fan_out)
    fan_in_ratio = fan_in / total_io
    leaf_score = 1.0 if fan_out == 0 else 0.0
    cross_in_ratio = row.cross_package_in / max(1, fan_in)
    cross_out_ratio = row.cross_package_out / max(1, fan_out)
    log_import_in = math.log1p(max(0, row.import_in))
    doc_count = max(0, row.doc_anchor_count)
    has_documentation = 1.0 if doc_count > 0 else 0.0
    doc_density = math.log1p(doc_count)
    doc_definition = math.log1p(max(0.0, row.doc_definition_weight))
    doc_reference = math.log1p(max(0.0, row.doc_reference_weight))
    doc_example = math.log1p(max(0.0, row.doc_example_weight))
    is_class = 1.0 if row.kind in {"class", "interface"} else 0.0
    is_function = 1.0 if row.kind in {"function", "method"} else 0.0

    return (
        log_fan_in,
        log_fan_out,
        fan_in_ratio,
        float(max(0, row.depth_from_public)),
        leaf_score,
        cross_in_ratio,
        cross_out_ratio,
        log_import_in,
        has_documentation,
        doc_density,
        doc_definition,
        doc_reference,
        doc_example,
        is_class,
        is_function,
    )


def _standardize(vectors: list[tuple[float, ...]]) -> list[list[float]]:
    if not vectors:
        return []
    n = len(vectors)
    cols = list(zip(*vectors, strict=True))
    means = [sum(col) / n for col in cols]
    stds = [
        math.sqrt(sum((v - mean) ** 2 for v in col) / n) or 1.0
        for col, mean in zip(cols, means, strict=True)
    ]
    return [
        [(v - mean) / std for v, mean, std in zip(vec, means, stds, strict=True)] for vec in vectors
    ]


def _kmeans(
    vectors: list[list[float]],
    k: int,
    *,
    max_iter: int,
    rng: random.Random,
) -> tuple[list[list[float]], list[int]]:
    n = len(vectors)
    dim = len(vectors[0])
    init_indices = rng.sample(range(n), k)
    centroids = [list(vectors[i]) for i in init_indices]
    assignments = [0] * n

    for _ in range(max_iter):
        for i, vec in enumerate(vectors):
            best_dist = math.inf
            best_cid = 0
            for cid, centroid in enumerate(centroids):
                dist = sum((a - b) ** 2 for a, b in zip(vec, centroid, strict=True))
                if dist < best_dist:
                    best_dist = dist
                    best_cid = cid
            assignments[i] = best_cid

        new_centroids = [[0.0] * dim for _ in range(k)]
        counts = [0] * k
        for vec, cid in zip(vectors, assignments, strict=True):
            for d, val in enumerate(vec):
                new_centroids[cid][d] += val
            counts[cid] += 1
        for cid in range(k):
            if counts[cid] == 0:
                new_centroids[cid] = list(vectors[rng.randrange(n)])
            else:
                new_centroids[cid] = [v / counts[cid] for v in new_centroids[cid]]

        if _centroids_equal(centroids, new_centroids):
            centroids = new_centroids
            break
        centroids = new_centroids

    return centroids, assignments


def _centroids_equal(a: list[list[float]], b: list[list[float]], eps: float = 1e-9) -> bool:
    return all(
        all(abs(x - y) < eps for x, y in zip(va, vb, strict=True))
        for va, vb in zip(a, b, strict=True)
    )


def _silhouette_score(
    vectors: list[list[float]],
    assignments: list[int],
    k: int,
    *,
    max_samples: int | None = None,
    max_intra_cluster_samples: int = 64,
    max_inter_cluster_samples: int = 64,
    seed: int = 0,
) -> float:
    if k <= 1 or len(vectors) <= k:
        return -math.inf
    by_cluster: dict[int, list[int]] = {}
    for i, cid in enumerate(assignments):
        by_cluster.setdefault(cid, []).append(i)

    candidate_indices = list(range(len(vectors)))
    if max_samples and len(candidate_indices) > max_samples:
        candidate_indices = sorted(random.Random(seed).sample(candidate_indices, max_samples))

    rng = random.Random(seed + 7919)
    total = 0.0
    counted = 0
    for i in candidate_indices:
        vec = vectors[i]
        own = assignments[i]
        own_members = [j for j in by_cluster[own] if j != i]
        if not own_members:
            continue
        if max_intra_cluster_samples > 0 and len(own_members) > max_intra_cluster_samples:
            own_members = rng.sample(own_members, max_intra_cluster_samples)
        a = sum(_dist(vec, vectors[j]) for j in own_members) / len(own_members)
        b = math.inf
        for cid, members in by_cluster.items():
            if cid == own or not members:
                continue
            compare_members = members
            if max_inter_cluster_samples > 0 and len(compare_members) > max_inter_cluster_samples:
                compare_members = rng.sample(compare_members, max_inter_cluster_samples)
            mean_dist = sum(_dist(vec, vectors[j]) for j in compare_members) / len(compare_members)
            if mean_dist < b:
                b = mean_dist
        if b == math.inf:
            continue
        denom = max(a, b)
        if denom > 0:
            total += (b - a) / denom
            counted += 1
    return total / counted if counted else -math.inf


def _dist(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b, strict=True)))


def _column_means(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    n = len(vectors)
    cols = list(zip(*vectors, strict=True))
    return [sum(col) / n for col in cols]


def _column_stds(vectors: list[list[float]], means: list[float]) -> list[float]:
    if not vectors:
        return []
    n = len(vectors)
    cols = list(zip(*vectors, strict=True))
    return [
        math.sqrt(sum((v - mean) ** 2 for v in col) / n) or 1.0
        for col, mean in zip(cols, means, strict=True)
    ]


def _signature_for(
    centroid: list[float],
    feature_means: list[float],
    feature_stds: list[float],
) -> tuple[str, ...]:
    z_scores = [
        (name, (val - mean) / std)
        for name, val, mean, std in zip(
            _FEATURE_NAMES,
            centroid,
            feature_means,
            feature_stds,
            strict=True,
        )
    ]
    z_scores.sort(key=lambda item: abs(item[1]), reverse=True)
    return tuple(f"{name}:{'+' if z >= 0 else '-'}" for name, z in z_scores[:3])


def assemble_symbol_rows(
    symbols: Sequence[tuple[str, str, str]],
    call_edges: Sequence[tuple[str, str]],
    doc_counts: dict[str, int],
    import_in_per_uid: dict[str, int] | None = None,
    doc_signal_by_uid: dict[str, dict[str, float]] | None = None,
) -> list[SymbolRow]:
    """Combine raw graph extracts into ``SymbolRow``s with structural features.

    Pure function. Inputs are decoupled from Neo4j so the same logic runs
    against synthetic graphs in tests.

    - ``symbols``: ``(uid, kind, file_path)`` tuples. ``file_path`` may be
      empty; in that case the symbol's package is the empty string.
    - ``call_edges``: ``(caller_uid, callee_uid)`` for any CALLS-style edge.
      Edges referencing unknown uids are silently dropped.
    - ``doc_counts``: ``uid -> count`` of incoming ``COVERS`` edges.
    - ``doc_signal_by_uid``: optional weighted COVERS signals by anchor type.

    Package = directory portion of ``file_path``. ``cross_package_*`` counts
    edges whose endpoints sit in different directories. ``depth_from_public``
    is BFS distance from any cross-package-imported symbol following outgoing
    CALLS; nodes never reached get ``max_observed_depth + 1`` so the feature
    stays bounded for the standardizer.
    """
    if not symbols:
        return []

    import_in_per_uid = import_in_per_uid or {}
    doc_signal_by_uid = doc_signal_by_uid or {}
    info: dict[str, dict] = {}
    for uid, kind, file_path in symbols:
        info[uid] = {
            "uid": uid,
            "kind": kind or "",
            "package": os.path.dirname(file_path or ""),
        }

    out_edges: dict[str, set[str]] = {uid: set() for uid in info}
    in_edges: dict[str, set[str]] = {uid: set() for uid in info}
    for caller, callee in call_edges:
        if caller in info and callee in info and caller != callee:
            out_edges[caller].add(callee)
            in_edges[callee].add(caller)

    # Public = graph sources that *originate work* (no callers but have
    # callees). Pure topology, no name patterns. In any indexed graph, the
    # symbols where work originates are natural depth-0 entry points: user
    # code if it's in the graph, otherwise a library's externally-callable
    # methods (nothing internal calls them). Isolated dangling nodes (no
    # callers AND no callees) are left out — they're not real entry points,
    # and treating them as public would collapse genuinely unreachable
    # symbols into depth 0 alongside the actual entries.
    public_uids = {uid for uid, callers in in_edges.items() if not callers and out_edges[uid]}

    depths = _bfs_depths(out_edges, public_uids)
    if depths:
        max_depth = max(depths.values())
        unreachable_depth = max_depth + 1
    else:
        unreachable_depth = 0

    rows: list[SymbolRow] = []
    for uid, meta in info.items():
        callers = in_edges[uid]
        callees = out_edges[uid]
        my_pkg = meta["package"]
        cross_in = sum(1 for c in callers if info[c]["package"] != my_pkg)
        cross_out = sum(1 for c in callees if info[c]["package"] != my_pkg)
        doc_signal = doc_signal_by_uid.get(uid, {})
        rows.append(
            SymbolRow(
                uid=uid,
                kind=meta["kind"],
                fan_in=len(callers),
                fan_out=len(callees),
                cross_package_in=cross_in,
                cross_package_out=cross_out,
                depth_from_public=depths.get(uid, unreachable_depth),
                doc_anchor_count=int(doc_counts.get(uid, 0)),
                import_in=int(import_in_per_uid.get(uid, 0)),
                doc_definition_weight=float(doc_signal.get("definition", 0.0)),
                doc_reference_weight=float(doc_signal.get("reference", 0.0)),
                doc_example_weight=float(doc_signal.get("example", 0.0)),
            )
        )
    return rows


def _bfs_depths(
    out_edges: dict[str, set[str]],
    sources: set[str],
) -> dict[str, int]:
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


def extract_symbol_rows(db, workspace_id: str) -> list[SymbolRow]:
    """Read every symbol's structural facts from Neo4j and assemble them."""
    symbols = _query_symbols(db, workspace_id)
    edges = _query_call_edges(db, workspace_id)
    doc_counts = _query_doc_anchor_counts(db, workspace_id)
    doc_signals = _query_doc_anchor_signals(db, workspace_id)
    import_in = _query_file_import_in_counts(db, workspace_id)
    return assemble_symbol_rows(symbols, edges, doc_counts, import_in, doc_signals)


def _query_symbols(db, workspace_id: str) -> list[tuple[str, str, str]]:
    with db.driver.session() as session:
        result = session.run(
            """
            MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
            RETURN s.uid AS uid,
                   coalesce(s.kind, '') AS kind,
                   coalesce(f.path, '') AS file_path
            """,
            workspace_id=workspace_id,
        )
        return [(r["uid"], r["kind"], r["file_path"]) for r in result if r["uid"]]


def _query_call_edges(db, workspace_id: str) -> list[tuple[str, str]]:
    with db.driver.session() as session:
        result = session.run(
            """
            MATCH (caller:Symbol)-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS]->(callee:Symbol)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
            RETURN caller.uid AS caller_uid, callee.uid AS callee_uid
            """,
            workspace_id=workspace_id,
        )
        return [
            (r["caller_uid"], r["callee_uid"])
            for r in result
            if r["caller_uid"] and r["callee_uid"]
        ]


def _query_file_import_in_counts(db, workspace_id: str) -> dict[str, int]:
    """Count distinct files that import each symbol's container file.

    Applied to every symbol in the file as a popularity-of-container signal.
    Sparse for re-exported APIs (e.g. ``from fastapi import FastAPI`` does
    not generate an edge to ``fastapi/applications.py`` because the import
    resolver targets ``fastapi/__init__.py``); informative where direct
    file imports exist.
    """
    with db.driver.session() as session:
        result = session.run(
            """
            MATCH (target:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
            OPTIONAL MATCH (importer:File)-[r:IMPORTS]->(target)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
            WITH s, count(DISTINCT importer) AS imp_in
            RETURN s.uid AS uid, imp_in
            """,
            workspace_id=workspace_id,
        )
        return {r["uid"]: int(r["imp_in"]) for r in result if r["uid"]}


def _query_doc_anchor_counts(db, workspace_id: str) -> dict[str, int]:
    with db.driver.session() as session:
        result = session.run(
            """
            MATCH (a:DocAnchor)-[r:COVERS]->(s:Symbol)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
            RETURN s.uid AS uid, count(r) AS doc_count
            """,
            workspace_id=workspace_id,
        )
        return {r["uid"]: int(r["doc_count"]) for r in result if r["uid"]}


def _query_doc_anchor_signals(db, workspace_id: str) -> dict[str, dict[str, float]]:
    with db.driver.session() as session:
        result = session.run(
            """
            MATCH (a:DocAnchor)-[r:COVERS]->(s:Symbol)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
            WITH s.uid AS uid,
                 coalesce(r.anchor_type, 'reference') AS anchor_type,
                 coalesce(r.confidence, 0.6) * coalesce(r.primary_bias, 0.6) AS weight
            RETURN uid,
                   sum(CASE WHEN anchor_type IN ['definition', 'warning', 'deprecated'] THEN weight ELSE 0.0 END) AS definition_weight,
                   sum(CASE WHEN anchor_type = 'reference' THEN weight ELSE 0.0 END) AS reference_weight,
                   sum(CASE WHEN anchor_type = 'example' THEN weight ELSE 0.0 END) AS example_weight
            """,
            workspace_id=workspace_id,
        )
        return {
            r["uid"]: {
                "definition": float(r["definition_weight"] or 0.0),
                "reference": float(r["reference_weight"] or 0.0),
                "example": float(r["example_weight"] or 0.0),
            }
            for r in result
            if r["uid"]
        }


def persist_role_taxonomy(
    db,
    workspace_id: str,
    taxonomy: RoleTaxonomy,
    uid_to_cluster: dict[str, int],
    *,
    batch_size: int = 1000,
) -> None:
    """Save the taxonomy on the Workspace and cluster ids on each Symbol."""
    payload = json.dumps(taxonomy.to_dict(), sort_keys=True)
    catalog_dict = build_role_catalog(taxonomy).to_dict()
    catalog_dict = merge_preloaded_mechanisms_into_role_catalog(catalog_dict)
    catalog_payload = json.dumps(catalog_dict, sort_keys=True)
    with db.driver.session() as session:
        session.run(
            """
            MERGE (w:Workspace {id: $workspace_id})
            SET w.role_taxonomy_json = $payload,
                w.role_taxonomy_schema_version = $schema_version,
                w.role_catalog_json = $catalog_payload,
                w.role_catalog_schema_version = $catalog_schema_version,
                w.role_taxonomy_updated_at = timestamp()
            """,
            workspace_id=workspace_id,
            payload=payload,
            catalog_payload=catalog_payload,
            schema_version=ROLE_TAXONOMY_SCHEMA_VERSION,
            catalog_schema_version=ROLE_CATALOG_SCHEMA_VERSION,
        )

        items = [{"uid": uid, "cid": cid} for uid, cid in uid_to_cluster.items()]
        for offset in range(0, len(items), batch_size):
            batch = items[offset : offset + batch_size]
            session.run(
                """
                UNWIND $items AS item
                MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol {uid: item.uid})
                SET s.derived_role_id = item.cid
                """,
                items=batch,
                workspace_id=workspace_id,
            )


def get_role_taxonomy(db, workspace_id: str) -> dict | None:
    """Load the persisted taxonomy from the Workspace, if any."""
    with db.driver.session() as session:
        row = session.run(
            """
            MATCH (w:Workspace {id: $workspace_id})
            RETURN w.role_taxonomy_json AS payload
            """,
            workspace_id=workspace_id,
        ).single()
    if not row or not row["payload"]:
        return None
    try:
        data = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def get_role_catalog(db, workspace_id: str) -> dict | None:
    """Load the persisted role catalog from the Workspace, if any."""
    with db.driver.session() as session:
        row = session.run(
            """
            MATCH (w:Workspace {id: $workspace_id})
            RETURN w.role_catalog_json AS payload
            """,
            workspace_id=workspace_id,
        ).single()
    if not row or not row["payload"]:
        return None
    try:
        data = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def derive_and_persist_role_taxonomy(
    db,
    workspace_id: str,
    *,
    seed: int = 0,
) -> RoleTaxonomy:
    """Run Pass 1 end-to-end: extract → cluster → persist. Returns the taxonomy."""
    rows = extract_symbol_rows(db, workspace_id)
    taxonomy, uid_to_cluster = cluster_symbols(rows, seed=seed)
    persist_role_taxonomy(db, workspace_id, taxonomy, uid_to_cluster)
    return taxonomy


def _empty_taxonomy() -> RoleTaxonomy:
    return RoleTaxonomy(
        feature_names=_FEATURE_NAMES,
        clusters=(),
        silhouette=0.0,
        chosen_k=0,
        sample_size=0,
    )


def _single_cluster_taxonomy(
    rows: Sequence[SymbolRow],
    standardized: list[list[float]],
) -> RoleTaxonomy:
    centroid = tuple(_column_means(standardized))
    return RoleTaxonomy(
        feature_names=_FEATURE_NAMES,
        clusters=(
            RoleCluster(
                cluster_id=0,
                centroid=centroid,
                member_count=len(rows),
                signature=("trivial",),
            ),
        ),
        silhouette=0.0,
        chosen_k=1,
        sample_size=len(rows),
    )
