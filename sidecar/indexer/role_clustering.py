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
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass

from sidecar.context.mechanism_registry import merge_preloaded_mechanisms_into_role_catalog
from sidecar.context.ranker.signal_constants import NOISE_PATH_PATTERNS

ROLE_TAXONOMY_SCHEMA_VERSION = 2
ROLE_CATALOG_SCHEMA_VERSION = 2  # v2: mechanism_required_roles + mechanism_role_backfill in JSON

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

DEFAULT_EDGE_CONFIDENCE: dict[str, float] = {
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


_FEATURE_NAMES: tuple[str, ...] = (
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
    "cross_package_call_out_ratio",
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
    call_fan_in: float = 0.0
    call_fan_out: float = 0.0
    type_fan_in: float = 0.0
    type_fan_out: float = 0.0
    api_fan_in: float = 0.0
    api_fan_out: float = 0.0
    inject_fan_in: float = 0.0
    depend_fan_in: float = 0.0
    depend_fan_out: float = 0.0
    handle_fan_in: float = 0.0

    def effective_call_fan_in(self) -> float:
        return self.call_fan_in if self.call_fan_in > 0.0 else float(self.fan_in)

    def effective_call_fan_out(self) -> float:
        return self.call_fan_out if self.call_fan_out > 0.0 else float(self.fan_out)

    @property
    def structurally_connected(self) -> bool:
        return any(
            v > 0.0
            for v in (
                self.effective_call_fan_in(),
                self.effective_call_fan_out(),
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
        "cross_package_call_out_ratio": 0.6,
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


def filter_clustering_rows(rows: Sequence[SymbolRow]) -> list[SymbolRow]:
    """Drop symbols with no position in any structural edge family."""
    return [row for row in rows if row.structurally_connected]


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
    call_in = max(0.0, row.effective_call_fan_in())
    call_out = max(0.0, row.effective_call_fan_out())
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
        row.cross_package_in / max(1.0, call_in),
        row.cross_package_out / max(1.0, call_out),
        math.log1p(max(0, row.import_in)),
        1.0 if doc_count > 0 else 0.0,
        math.log1p(doc_count),
        math.log1p(max(0.0, row.doc_definition_weight)),
        math.log1p(max(0.0, row.doc_reference_weight)),
        math.log1p(max(0.0, row.doc_example_weight)),
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


def _edge_confidence(rel_type: str, stored: float | None, kind: str = "") -> float:
    if rel_type == "USES_TYPE":
        return USES_TYPE_KIND_WEIGHT.get(kind or "", DEFAULT_EDGE_CONFIDENCE["USES_TYPE"])
    if stored is not None:
        return float(stored)
    return DEFAULT_EDGE_CONFIDENCE.get(rel_type, 1.0)


def _iter_structural_edges(
    edges: Sequence[tuple[str, ...]],
) -> list[tuple[str, str, str, float]]:
    normalized: list[tuple[str, str, str, float]] = []
    for edge in edges:
        if len(edge) == 2:
            normalized.append((edge[0], edge[1], "CALLS_DIRECT", 1.0))
        elif len(edge) >= 4:
            normalized.append((edge[0], edge[1], edge[2], float(edge[3])))
    return normalized


def assemble_symbol_rows(
    symbols: Sequence[tuple[str, str, str]],
    call_edges: Sequence[tuple[str, ...]],
    doc_counts: dict[str, int],
    import_in_per_uid: dict[str, int] | None = None,
    doc_signal_by_uid: dict[str, dict[str, float]] | None = None,
) -> list[SymbolRow]:
    """Combine raw graph extracts into ``SymbolRow``s with structural features.

    Pure function. Inputs are decoupled from Neo4j so the same logic runs
    against synthetic graphs in tests.

    - ``symbols``: ``(uid, kind, file_path)`` tuples. ``file_path`` may be
      empty; in that case the symbol's package is the empty string.
    - ``call_edges``: ``(caller_uid, callee_uid)`` legacy call edges, or
      ``(caller_uid, callee_uid, rel_type, confidence)`` structural edges.
      Edges referencing unknown uids are silently dropped.
    - ``doc_counts``: ``uid -> count`` of incoming ``COVERS`` edges.
    - ``doc_signal_by_uid``: optional weighted COVERS signals by anchor type.

    Package = directory portion of ``file_path``. ``cross_package_*`` counts
    call edges whose endpoints sit in different directories.
    ``depth_from_public`` is BFS distance from call-graph sources following
    outgoing CALLS-family edges only.
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

    for caller, callee, rel_type, conf in _iter_structural_edges(call_edges):
        if caller not in info or callee not in info or caller == callee:
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

    rows: list[SymbolRow] = []
    for uid, meta in info.items():
        callers = call_in[uid]
        callees = call_out[uid]
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
    edges = _query_structural_edges(db, workspace_id)
    doc_counts = _query_doc_anchor_counts(db, workspace_id)
    doc_signals = _query_doc_anchor_signals(db, workspace_id)
    import_in = _query_file_import_in_counts(db, workspace_id)
    return assemble_symbol_rows(symbols, edges, doc_counts, import_in, doc_signals)


def _query_symbols(db, workspace_id: str) -> list[tuple[str, str, str]]:
    # Pass 1 derives the PRODUCT role taxonomy. Test / example / benchmark code is
    # not part of the product's role structure — clustering it skews the centroids
    # (e.g. test data classes flood the "executor" leaf+fan_in cluster). Exclude it.
    # Those symbols still get their role structurally at query time (a /tests/ path
    # yields impact_test_surface in infer_supporting_roles), so impact analysis is
    # unaffected — only the clustering input is cleaned.
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


def _query_call_edges(db, workspace_id: str) -> list[tuple[str, str]]:
    """Legacy call-only edge query (tests / diagnostics)."""
    return [
        (caller, callee)
        for caller, callee, rel_type, _conf in _query_structural_edges(db, workspace_id)
        if rel_type in CALL_REL_TYPES
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
    structural_rows: Sequence[SymbolRow] | None = None,
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

        if structural_rows:
            profile_items = [
                {
                    "uid": row.uid,
                    "cid": uid_to_cluster.get(row.uid),
                    "call_fan_in": round(row.effective_call_fan_in(), 4),
                    "call_fan_out": round(row.effective_call_fan_out(), 4),
                    "type_fan_in": round(row.type_fan_in, 4),
                }
                for row in structural_rows
            ]
            for offset in range(0, len(profile_items), batch_size):
                batch = profile_items[offset : offset + batch_size]
                session.run(
                    """
                    UNWIND $items AS item
                    MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol {uid: item.uid})
                    SET s.call_fan_in = item.call_fan_in,
                        s.call_fan_out = item.call_fan_out,
                        s.type_fan_in = item.type_fan_in,
                        s.derived_role_id = item.cid
                    """,
                    items=batch,
                    workspace_id=workspace_id,
                )
        else:
            cluster_items = [{"uid": uid, "cid": cid} for uid, cid in uid_to_cluster.items()]
            for offset in range(0, len(cluster_items), batch_size):
                batch = cluster_items[offset : offset + batch_size]
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
    all_rows = extract_symbol_rows(db, workspace_id)
    cluster_rows = filter_clustering_rows(all_rows)
    taxonomy, uid_to_cluster = cluster_symbols(cluster_rows, seed=seed)
    persist_role_taxonomy(
        db,
        workspace_id,
        taxonomy,
        uid_to_cluster,
        structural_rows=all_rows,
    )
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
