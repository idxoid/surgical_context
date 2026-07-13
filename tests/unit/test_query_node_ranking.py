from __future__ import annotations

import numpy as np
import pytest

from context_engine.axis.pipeline import AxisRetrievalConfig, _request_embedder
from context_engine.axis.query_node_ranking import (
    apply_query_node_similarity,
    semantic_noise_floor,
)
from context_engine.axis.role_retrieval import (
    RoleCandidate,
    WorkspaceScan,
    build_query_scoring_context,
)


def _candidate(
    uid: str,
    *,
    role: str = "structural_neighbour",
    score: float = 0.3,
    utility_score: float | None = None,
    query_similarity: float | None = None,
) -> RoleCandidate:
    return RoleCandidate(
        uid=uid,
        name=uid,
        file_path=f"/{uid}.py",
        role=role,
        satisfying_contracts=(),
        satisfying_kinds=("graph_fact",),
        contract_count=0,
        kind_count=1,
        vector_distance=None,
        score=score,
        utility_score=utility_score,
        query_similarity=query_similarity,
    )


def _scoring() -> object:
    scan = WorkspaceScan(
        rows=[{"uid": "near"}, {"uid": "far"}],
        vectors=np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
    )
    return build_query_scoring_context(scan, "question", lambda _text: [1.0, 0.0])


def test_graph_only_pool_candidates_are_softly_reranked_by_cosine():
    out = apply_query_node_similarity(
        {"structural_neighbour": [_candidate("far"), _candidate("near")]},
        _scoring(),
    )["structural_neighbour"]

    assert [candidate.uid for candidate in out] == ["near", "far"]
    assert out[0].query_similarity == pytest.approx(1.0)
    assert out[0].score == pytest.approx(0.5)
    assert out[1].query_similarity == pytest.approx(0.0)
    assert out[1].score == pytest.approx(0.3)


def test_existing_semantic_score_is_not_applied_twice():
    already_scored = _candidate("near", score=0.77, query_similarity=0.8)
    out = apply_query_node_similarity({"role": [already_scored]}, _scoring())["role"][0]

    assert out.score == pytest.approx(0.77)
    # The annotation is refreshed from the canonical node matrix.
    assert out.query_similarity == pytest.approx(1.0)


def test_impact_ranking_uses_utility_baseline_and_lower_semantic_weight():
    impact = _candidate(
        "near",
        role="impact_analysis",
        score=0.35,
        utility_score=0.8,
    )
    out = apply_query_node_similarity(
        {"impact_analysis": [impact]},
        _scoring(),
        semantic_weight=0.2,
        mode_semantic_weight=0.05,
    )["impact_analysis"][0]

    assert out.graph_score == pytest.approx(0.8)
    assert out.score == pytest.approx(0.85)
    assert out.utility_score == pytest.approx(0.8)


def test_candidate_missing_from_vector_scan_is_left_neutral():
    missing = _candidate("unindexed", score=0.42)
    out = apply_query_node_similarity({"role": [missing]}, _scoring())["role"][0]

    assert out == missing


def test_query_scoring_uses_best_body_or_signature_cosine_and_embeds_once():
    calls = 0

    def _embed(_text):
        nonlocal calls
        calls += 1
        return [1.0, 0.0]

    scan = WorkspaceScan(
        rows=[{"uid": "signature_match"}],
        vectors=np.asarray([[0.0, 1.0]], dtype=np.float32),
        signature_vectors=np.asarray([[1.0, 0.0]], dtype=np.float32),
    )
    scoring = build_query_scoring_context(scan, "question", _embed)

    assert scoring is not None
    assert calls == 1
    assert scoring.similarity_for("signature_match") == pytest.approx(1.0)
    assert scoring.distance_for("signature_match") == pytest.approx(0.0)


def test_request_embedder_memoises_identical_question_text():
    class _Lance:
        calls = 0

        def _embed(self, texts):
            self.calls += 1
            return [[float(self.calls)]]

    lance = _Lance()
    embed = _request_embedder(lance)

    assert embed("same") == [1.0]
    assert embed("same") == [1.0]
    assert embed("different") == [2.0]
    assert lance.calls == 2


def test_semantic_noise_floor_uses_weak_half_median_and_mad():
    floor = semantic_noise_floor([0.10, 0.20, 0.80, 0.90])

    assert floor == pytest.approx(0.15 + 1.4826 * 0.05)


def test_calibrated_blend_can_promote_similarity_above_its_noise_floor():
    candidates = [_candidate("far", score=0.8), _candidate("near", score=0.3)]
    out = apply_query_node_similarity(
        {"role": candidates},
        _scoring(),
        ordering_mode="calibrated_blend",
        blend_alpha=0.70,
    )["role"]

    assert [candidate.uid for candidate in out] == ["near", "far"]
    assert out[0].score > out[1].score


def test_rrf_can_promote_semantic_rank_without_mixing_raw_score_scales():
    candidates = [_candidate("far", score=0.8), _candidate("near", score=0.3)]
    out = apply_query_node_similarity(
        {"role": candidates},
        _scoring(),
        ordering_mode="rrf",
        rrf_weight=2.0,
    )["role"]

    assert [candidate.uid for candidate in out] == ["near", "far"]
    assert 0.0 <= out[1].score < out[0].score <= 1.0


def test_calibrated_blend_keeps_impact_mode_structurally_dominant():
    candidates = [
        _candidate("far", role="impact_analysis", score=0.35, utility_score=0.9),
        _candidate("near", role="impact_analysis", score=0.35, utility_score=0.4),
    ]
    out = apply_query_node_similarity(
        {"impact_analysis": candidates},
        _scoring(),
        ordering_mode="calibrated_blend",
        blend_alpha=0.70,
        mode_blend_alpha=0.10,
    )["impact_analysis"]

    assert [candidate.uid for candidate in out] == ["far", "near"]


def test_unknown_query_node_ordering_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown query-node ordering mode"):
        apply_query_node_similarity({"role": [_candidate("near")]}, _scoring(), ordering_mode="x")


def test_pipeline_defaults_to_calibrated_query_node_ordering():
    config = AxisRetrievalConfig()

    assert config.query_node_ordering_mode == "calibrated_blend"
    assert config.query_node_blend_alpha == pytest.approx(0.40)
    assert config.query_node_mode_blend_alpha == pytest.approx(0.10)


def test_semantic_noise_floor_counts_duplicate_uid_once(monkeypatch):
    import context_engine.axis.query_node_ranking as ranking

    captured: list[list[float]] = []
    real_floor = ranking.semantic_noise_floor

    def _capture(values):
        captured.append(list(values))
        return real_floor(values)

    monkeypatch.setattr(ranking, "semantic_noise_floor", _capture)
    duplicate = _candidate("near", score=0.4)
    ranking.apply_query_node_similarity(
        {"role_a": [duplicate], "role_b": [duplicate], "role_c": [_candidate("far")]},
        _scoring(),
        ordering_mode="calibrated_blend",
    )

    assert len(captured) == 1
    assert sorted(captured[0]) == pytest.approx([0.0, 1.0])
