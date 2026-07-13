"""Query↔node semantic reranking for the fully expanded axis pool.

Candidate generation remains structural and recall-oriented. This pass runs
after graph expansion/intersection, annotates every indexed candidate, and
offers three ordering strategies: the legacy graph-only boost, a robustly
calibrated utility/cosine blend, and rank-only RRF.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from statistics import median

from context_engine.axis.role_retrieval import QueryScoringContext, RoleCandidate

_MODE_ROLES = frozenset({"impact_analysis", "trace_dependency"})
_ORDERING_MODES = frozenset({"legacy_boost", "calibrated_blend", "rrf"})


def _mad(values: list[float]) -> float:
    if not values:
        return 0.0
    center = median(values)
    return median(abs(value - center) for value in values)


def semantic_noise_floor(similarities: list[float], *, k: float = 1.0) -> float:
    """Robust upper edge of the weak half of query↔node similarities."""
    if not similarities:
        return 0.0
    center = median(similarities)
    tail = [value for value in similarities if value <= center] or similarities
    return float(median(tail) + k * 1.4826 * _mad(tail))


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = min(len(ordered) - 1, max(0, round(quantile * (len(ordered) - 1))))
    return float(ordered[position])


def _normalise(values: list[float]) -> list[float]:
    if not values:
        return []
    low = min(values)
    span = max(values) - low
    if span <= 1e-12:
        return [1.0 for _ in values]
    return [(value - low) / span for value in values]


def _descending_ranks(values: list[float]) -> list[int]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index), reverse=True)
    ranks = [0] * len(values)
    for rank, index in enumerate(order, start=1):
        ranks[index] = rank
    return ranks


def _ordering_base(candidate: RoleCandidate) -> float:
    if candidate.role in _MODE_ROLES and candidate.utility_score is not None:
        return max(0.0, candidate.utility_score)
    return max(0.0, candidate.score)


def _rerank_candidate(
    candidate: RoleCandidate,
    scoring: QueryScoringContext,
    *,
    semantic_weight: float,
    mode_semantic_weight: float,
) -> RoleCandidate:
    similarity = scoring.similarity_for(candidate.uid)
    if similarity is None:
        return candidate

    distance = scoring.distance_for(candidate.uid)
    semantic_was_applied = candidate.query_similarity is not None
    graph_score = candidate.graph_score
    if graph_score is None:
        graph_score = (
            candidate.utility_score
            if candidate.role in _MODE_ROLES and candidate.utility_score is not None
            else candidate.score
        )

    score = candidate.score
    if not semantic_was_applied:
        weight = mode_semantic_weight if candidate.role in _MODE_ROLES else semantic_weight
        # Negative cosine is not evidence against a graph fact.  Keeping the
        # boost one-sided preserves structurally necessary but lexically remote
        # dependencies while still breaking constant-score graph ties.
        score = min(1.0, graph_score + weight * max(0.0, similarity))

    return replace(
        candidate,
        vector_distance=distance,
        query_similarity=similarity,
        graph_score=graph_score,
        score=score,
    )


def apply_query_node_similarity(
    raw_by_role: Mapping[str, list[RoleCandidate]],
    scoring: QueryScoringContext | None,
    *,
    semantic_weight: float = 0.20,
    mode_semantic_weight: float = 0.05,
    ordering_mode: str = "legacy_boost",
    blend_alpha: float = 0.40,
    mode_blend_alpha: float = 0.10,
    rrf_weight: float = 1.0,
    mode_rrf_weight: float = 0.25,
    rrf_k: int = 60,
) -> dict[str, list[RoleCandidate]]:
    """Annotate and softly rerank every indexed candidate in every pool."""
    if scoring is None:
        return {role: list(candidates) for role, candidates in raw_by_role.items()}
    if ordering_mode not in _ORDERING_MODES:
        raise ValueError(f"unknown query-node ordering mode: {ordering_mode!r}")

    annotated_by_role: dict[str, list[RoleCandidate]] = {}
    flat: list[RoleCandidate] = []
    for role, candidates in raw_by_role.items():
        annotated = [
            _rerank_candidate(
                candidate,
                scoring,
                semantic_weight=semantic_weight,
                mode_semantic_weight=mode_semantic_weight,
            )
            for candidate in candidates
        ]
        annotated_by_role[role] = annotated
        flat.extend(annotated)

    if ordering_mode != "legacy_boost" and flat:
        blend_alpha = min(1.0, max(0.0, blend_alpha))
        mode_blend_alpha = min(1.0, max(0.0, mode_blend_alpha))
        rrf_weight = max(0.0, rrf_weight)
        mode_rrf_weight = max(0.0, mode_rrf_weight)
        bases = [_ordering_base(candidate) for candidate in flat]
        base_norm = _normalise(bases)
        # One vote per symbol: the same uid can occur in several role pools,
        # but role multiplicity must not skew the query-specific noise model.
        similarity_by_uid = {
            candidate.uid: candidate.query_similarity
            for candidate in flat
            if candidate.query_similarity is not None
        }
        similarities = list(similarity_by_uid.values())
        floor = semantic_noise_floor(similarities)
        ceiling = _percentile(similarities, 0.95)
        semantic_span = max(1e-9, ceiling - floor)
        semantic_excess = [
            min(
                1.0,
                max(
                    0.0,
                    (
                        (
                            candidate.query_similarity
                            if candidate.query_similarity is not None
                            else -1.0
                        )
                        - floor
                    )
                    / semantic_span,
                ),
            )
            for candidate in flat
        ]

        if ordering_mode == "calibrated_blend":
            ordered_scores = [
                (1.0 - (mode_blend_alpha if candidate.role in _MODE_ROLES else blend_alpha))
                * base
                + (mode_blend_alpha if candidate.role in _MODE_ROLES else blend_alpha) * semantic
                for candidate, base, semantic in zip(
                    flat, base_norm, semantic_excess, strict=True
                )
            ]
        else:
            base_ranks = _descending_ranks(bases)
            semantic_ranks = _descending_ranks(
                [
                    candidate.query_similarity
                    if candidate.query_similarity is not None
                    else -1.0
                    for candidate in flat
                ]
            )
            raw_rrf = [
                1.0 / (max(1, rrf_k) + base_rank)
                + (mode_rrf_weight if candidate.role in _MODE_ROLES else rrf_weight)
                / (max(1, rrf_k) + semantic_rank)
                for candidate, base_rank, semantic_rank in zip(
                    flat, base_ranks, semantic_ranks, strict=True
                )
            ]
            ordered_scores = _normalise(raw_rrf)

        replacements = iter(ordered_scores)
        annotated_by_role = {
            role: [replace(candidate, score=next(replacements)) for candidate in candidates]
            for role, candidates in annotated_by_role.items()
        }

    out: dict[str, list[RoleCandidate]] = {}
    for role, reranked in annotated_by_role.items():
        reranked.sort(key=lambda candidate: (candidate.score, candidate.uid), reverse=True)
        out[role] = reranked
    return out


__all__ = ["apply_query_node_similarity", "semantic_noise_floor"]
