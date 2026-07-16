from __future__ import annotations

from context_engine.axis.role_retrieval import RoleCandidate
from context_engine.axis.seed_selector import select_context_seeds


def _candidate(
    uid: str,
    *,
    role: str = "routing_surface",
    score: float = 0.5,
    path: str | None = None,
    channels: tuple[str, ...] = (),
    spans: tuple[tuple[int, int], ...] = (),
    exact: bool = False,
) -> RoleCandidate:
    return RoleCandidate(
        uid=uid,
        name=uid,
        file_path=path or f"/repo/{uid}.py",
        role=role,
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=None,
        score=score,
        retrieval_channels=channels,
        retrieval_spans=spans,
        exact_symbol_match=exact,
    )


def test_exact_symbol_is_reserved_ahead_of_higher_scored_tail() -> None:
    selected, trace = select_context_seeds(
        {
            "routing_surface": [
                _candidate("semantic", score=0.95, channels=("vector",)),
                _candidate("named", score=0.20, channels=("lexical",), exact=True),
            ]
        },
        ["routing_surface"],
        per_role_soft_cap=1,
    )

    assert [candidate.uid for candidate in selected] == ["named"]
    assert selected[0].selection_reasons == (
        "exact_symbol",
        "hard_reserve",
    )
    assert trace.reason_counts["exact_symbol"] == 1


def test_exact_already_inside_ranked_slice_is_not_misreported_as_rescue() -> None:
    selected, _trace = select_context_seeds(
        {
            "routing_surface": [
                _candidate("named", score=0.95, channels=("lexical",), exact=True),
                _candidate("tail", score=0.20),
            ]
        },
        ["routing_surface"],
        per_role_soft_cap=1,
    )

    assert [candidate.uid for candidate in selected] == ["named"]
    assert selected[0].selection_reasons == ("exact_symbol", "ranked_fill")


def test_span_owner_is_a_bounded_prior_not_a_hard_reserve() -> None:
    selected, _trace = select_context_seeds(
        {
            "dependency_solver": [
                _candidate("high", role="dependency_solver", score=0.9),
                _candidate(
                    "span",
                    role="dependency_solver",
                    score=0.1,
                    channels=("semantic_chunk",),
                    spans=((40, 55),),
                ),
            ]
        },
        ["dependency_solver"],
        per_role_soft_cap=1,
    )

    assert [candidate.uid for candidate in selected] == ["high"]


def test_ambiguous_exact_matches_have_a_bounded_hard_reserve() -> None:
    selected, trace = select_context_seeds(
        {
            "routing_surface": [
                _candidate("one", exact=True),
                _candidate("two", exact=True),
                _candidate("ordinary", score=1.0),
            ]
        },
        ["routing_surface"],
        per_role_soft_cap=1,
    )

    assert [candidate.uid for candidate in selected] == ["one"]
    assert trace.selected_candidates == 1
    assert trace.dropped_candidates == 2


def test_explicit_anchors_can_overflow_the_soft_cap() -> None:
    selected, _trace = select_context_seeds(
        {
            "routing_surface": [
                _candidate("one", role="anchor_symbol"),
                _candidate("two", role="anchor_symbol"),
                _candidate("ordinary", score=1.0),
            ]
        },
        ["routing_surface"],
        per_role_soft_cap=1,
    )

    assert {candidate.uid for candidate in selected} == {"one", "two"}


def test_multi_intent_role_consensus_is_retained_without_reordering_fill() -> None:
    shared_a = _candidate("shared", role="routing_surface", score=0.3)
    shared_b = _candidate("shared", role="binding_surface", score=0.4)
    selected, _trace = select_context_seeds(
        {
            "routing_surface": [
                _candidate("routing-only", role="routing_surface", score=0.95),
                shared_a,
            ],
            "binding_surface": [
                _candidate("binding-only", role="binding_surface", score=0.9),
                shared_b,
            ],
        },
        (role for role in ("routing_surface", "binding_surface")),
        per_role_soft_cap=2,
    )

    assert [candidate.uid for candidate in selected] == [
        "routing-only",
        "shared",
        "binding-only",
    ]
    assert selected[1].supporting_roles == ("routing_surface", "binding_surface")
    assert "multi_intent_role" in selected[1].selection_reasons
    assert "ranked_fill" in selected[1].selection_reasons


def test_opt_in_role_consensus_boost_is_saturating_and_traceable() -> None:
    selected, trace = select_context_seeds(
        {
            "routing_surface": [_candidate("shared", score=0.60)],
            "binding_surface": [_candidate("shared", role="binding_surface", score=0.70)],
            "dispatch_surface": [_candidate("shared", role="dispatch_surface", score=0.80)],
            "metadata_surface": [_candidate("shared", role="metadata_surface", score=0.90)],
        },
        ["routing_surface", "binding_surface"],
        per_role_soft_cap=1,
        role_consensus_score_boost=0.05,
        role_consensus_max_extra_roles=2,
    )

    assert len(selected) == 1
    candidate = selected[0]
    # The first role's payload remains the stable base; three extra roles are
    # observed, but the experimental bonus saturates after two.
    assert abs(candidate.score - 0.70) < 1e-9
    assert abs(candidate.role_consensus_bonus - 0.10) < 1e-9
    assert "role_consensus_boost" in candidate.selection_reasons
    assert trace.reason_counts["role_consensus_boost"] == 1


def test_channel_consensus_collapses_correlated_lexical_evidence_and_saturates() -> None:
    selected, trace = select_context_seeds(
        {
            "hybrid_seed": [
                _candidate(
                    "shared",
                    role="hybrid_seed",
                    score=0.60,
                    channels=(
                        "lexical",
                        "lexical_span",
                        "vector",
                        "semantic_chunk",
                    ),
                    exact=True,
                )
            ]
        },
        ["routing_surface"],
        per_role_soft_cap=1,
        channel_consensus_score_boost=0.04,
        channel_consensus_max_extra_families=2,
        exact_symbol_score_boost=0.08,
    )

    candidate = selected[0]
    assert abs(candidate.score - 0.76) < 1e-9
    assert abs(candidate.channel_consensus_bonus - 0.08) < 1e-9
    assert abs(candidate.exact_symbol_bonus - 0.08) < 1e-9
    assert "channel_consensus_boost" in candidate.selection_reasons
    assert "exact_symbol_boost" in candidate.selection_reasons
    assert trace.reason_counts["channel_consensus_boost"] == 1
    assert trace.reason_counts["exact_symbol_boost"] == 1


def test_lexical_and_lexical_span_alone_are_one_channel_family() -> None:
    selected, _trace = select_context_seeds(
        {
            "hybrid_seed": [
                _candidate(
                    "lexical",
                    role="hybrid_seed",
                    score=0.60,
                    channels=("lexical", "lexical_span"),
                )
            ]
        },
        ["routing_surface"],
        per_role_soft_cap=1,
        channel_consensus_score_boost=0.04,
    )

    assert selected[0].score == 0.60
    assert selected[0].channel_consensus_bonus == 0.0


def test_non_intent_structural_cap_preserves_active_and_universal_channels() -> None:
    selected, trace = select_context_seeds(
        {
            "routing_surface": [
                _candidate(f"active-{index}", score=1.0 - index / 10) for index in range(3)
            ],
            "binding_surface": [
                _candidate(
                    f"non-intent-{index}",
                    role="binding_surface",
                    score=1.0 - index / 10,
                )
                for index in range(3)
            ],
            "vector_seed": [
                _candidate(
                    f"vector-{index}",
                    role="vector_seed",
                    score=1.0 - index / 10,
                )
                for index in range(3)
            ],
        },
        ["routing_surface"],
        per_role_soft_cap=3,
        non_intent_structural_role_soft_cap=1,
    )

    uids = {candidate.uid for candidate in selected}
    assert {f"active-{index}" for index in range(3)} <= uids
    assert {f"vector-{index}" for index in range(3)} <= uids
    assert "non-intent-0" in uids
    assert "non-intent-1" not in uids
    assert "non-intent-2" not in uids
    assert trace.reason_counts["non_intent_ranked_fill"] == 1


def test_non_intent_structural_cap_keeps_exact_reserve() -> None:
    selected, _trace = select_context_seeds(
        {
            "binding_surface": [
                _candidate("high", role="binding_surface", score=0.9),
                _candidate(
                    "exact",
                    role="binding_surface",
                    score=0.1,
                    exact=True,
                ),
            ],
        },
        ["routing_surface"],
        per_role_soft_cap=2,
        non_intent_structural_role_soft_cap=1,
    )

    assert [candidate.uid for candidate in selected] == ["exact"]
    assert "hard_reserve" in selected[0].selection_reasons


def test_duplicate_uid_merges_all_retrieval_evidence() -> None:
    selected, _trace = select_context_seeds(
        {
            "routing_surface": [
                _candidate("shared", score=0.7, channels=("vector",)),
            ],
            "hybrid_seed": [
                _candidate(
                    "shared",
                    role="hybrid_seed",
                    score=0.8,
                    channels=("lexical", "semantic_chunk"),
                    spans=((10, 20),),
                    exact=True,
                ),
            ],
        },
        ["routing_surface"],
        per_role_soft_cap=7,
    )

    assert len(selected) == 1
    candidate = selected[0]
    assert candidate.role == "routing_surface"
    # Cross-role best scores inform selection only; the first occurrence's
    # payload must remain stable for downstream Token Credit ordering.
    assert candidate.score == 0.7
    assert candidate.retrieval_channels == ("vector", "lexical", "semantic_chunk")
    assert candidate.retrieval_spans == ((10, 20),)
    assert candidate.exact_symbol_match is True
    assert candidate.supporting_roles == ("routing_surface", "hybrid_seed")


def test_dedup_happens_after_each_roles_soft_slice() -> None:
    selected, _trace = select_context_seeds(
        {
            "routing_surface": [
                _candidate("a", role="routing_surface", score=0.9),
                _candidate("shared", role="routing_surface", score=0.8),
            ],
            "binding_surface": [
                _candidate("b", role="binding_surface", score=0.9),
                _candidate("c", role="binding_surface", score=0.8),
                _candidate("shared", role="binding_surface", score=0.1),
            ],
        },
        ["routing_surface", "binding_surface"],
        per_role_soft_cap=2,
    )

    # ``shared`` is below binding's top-2 and therefore must not consume one of
    # that role's slots merely because routing selected it earlier.
    assert [candidate.uid for candidate in selected] == ["a", "shared", "b", "c"]


def test_exact_already_selected_elsewhere_does_not_consume_another_role_slot() -> None:
    selected, _trace = select_context_seeds(
        {
            "routing_surface": [
                _candidate("shared", role="routing_surface", exact=True),
            ],
            "binding_surface": [
                _candidate("b", role="binding_surface", score=0.9),
                _candidate("c", role="binding_surface", score=0.8),
                _candidate("shared", role="binding_surface", score=0.1, exact=True),
            ],
        },
        ["routing_surface", "binding_surface"],
        per_role_soft_cap=2,
    )

    assert [candidate.uid for candidate in selected] == ["shared", "b", "c"]


def test_ranked_fill_preserves_score_order_without_unvalidated_diversity() -> None:
    selected, _trace = select_context_seeds(
        {
            "routing_surface": [
                _candidate("first", score=0.90, path="/repo/shared.py"),
                _candidate("same-file", score=0.88, path="/repo/shared.py"),
                _candidate("new-file", score=0.87, path="/repo/other.py"),
            ]
        },
        ["routing_surface"],
        per_role_soft_cap=2,
    )

    assert [candidate.uid for candidate in selected] == ["first", "same-file"]


def test_none_preserves_uncapped_first_role_order_and_aggregates() -> None:
    selected, trace = select_context_seeds(
        {
            "routing_surface": [_candidate("a"), _candidate("shared")],
            "binding_surface": [
                _candidate("shared", role="binding_surface"),
                _candidate("b", role="binding_surface"),
            ],
        },
        ["routing_surface", "binding_surface"],
        per_role_soft_cap=None,
    )

    assert [candidate.uid for candidate in selected] == ["a", "shared", "b"]
    assert selected[1].supporting_roles == ("routing_surface", "binding_surface")
    assert trace.per_role_soft_cap is None
    assert trace.dropped_candidates == 0
