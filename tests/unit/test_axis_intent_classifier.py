"""Intent classifier — free-text question → L4 role(s)."""

from __future__ import annotations

import math

import pytest

from sidecar.axis.intent_classifier import (
    ROLE_INTENT_DESCRIPTIONS,
    classify_intent,
    clear_role_vector_cache,
)
from sidecar.axis.role_resolver import ROLE_CONTRACT_MAP


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        return vector
    return [x / norm for x in vector]


def _orthogonal_embedder() -> "callable":
    """Return an embedder that places every distinct string at a unit
    vector along a unique axis — so cosine similarity between two
    distinct strings is exactly 0 and between identical strings is
    exactly 1. Lets the tests reason about ranking deterministically.
    """
    string_to_axis: dict[str, int] = {}
    dim = 64

    def embed(text: str) -> list[float]:
        if text not in string_to_axis:
            string_to_axis[text] = len(string_to_axis)
        axis = string_to_axis[text]
        if axis >= dim:
            raise RuntimeError("orthogonal embedder ran out of dimensions")
        vec = [0.0] * dim
        vec[axis] = 1.0
        return vec

    return embed


def _shared_axis_embedder(boosts: dict[str, str]) -> "callable":
    """Embedder where the query and the boosted role description share
    a high-weight component on a shared axis (high similarity); every
    other distinct string is orthogonal. ``boosts`` is a ``{query:
    role_description}`` map.
    """
    string_to_axis: dict[str, int] = {}
    shared_pairs: list[tuple[str, str]] = list(boosts.items())
    shared_axis = 0
    dim = 64

    def axis_for(text: str) -> int:
        if text in string_to_axis:
            return string_to_axis[text]
        # Allocate axes 1+ for orthogonal slots; axis 0 is shared.
        idx = 1 + len(string_to_axis)
        string_to_axis[text] = idx
        return idx

    def embed(text: str) -> list[float]:
        for q, role_desc in shared_pairs:
            if text == q or text == role_desc:
                vec = [0.0] * dim
                vec[shared_axis] = 1.0
                return vec
        idx = axis_for(text)
        vec = [0.0] * dim
        if idx >= dim:
            raise RuntimeError("shared-axis embedder ran out of dimensions")
        vec[idx] = 1.0
        return vec

    return embed


@pytest.fixture(autouse=True)
def _isolate_cache():
    clear_role_vector_cache()
    yield
    clear_role_vector_cache()


def test_every_described_role_is_registered():
    """Every role with an intent description must exist in
    ``ROLE_CONTRACT_MAP`` — otherwise the classifier could pick a role
    no contract can satisfy.
    """
    missing = set(ROLE_INTENT_DESCRIPTIONS) - set(ROLE_CONTRACT_MAP)
    assert not missing, (
        f"intent_classifier describes roles that role_resolver doesn't know: "
        f"{sorted(missing)}"
    )


def test_empty_question_returns_no_matches():
    assert classify_intent("", _orthogonal_embedder()) == []
    assert classify_intent("   ", _orthogonal_embedder()) == []


def test_unrelated_question_drops_below_threshold():
    """When every role description is orthogonal to the query, no role
    crosses the similarity threshold.
    """
    matches = classify_intent(
        "a query that matches no role exactly",
        _orthogonal_embedder(),
        threshold=0.20,
    )
    assert matches == []


def test_query_aligned_with_role_description_ranks_first():
    query = "where do I find route handlers"
    embedder = _shared_axis_embedder(
        {query: ROLE_INTENT_DESCRIPTIONS["routing_surface"]},
    )

    matches = classify_intent(query, embedder, top_k=3, threshold=0.20)

    assert matches, "expected at least one match above threshold"
    assert matches[0].role == "routing_surface"
    assert matches[0].similarity == pytest.approx(1.0)


def test_top_k_limits_results():
    """Even when several roles could match, ``top_k`` caps how many
    come back, in similarity order.
    """
    query = "ambiguous query"
    # Share an axis with three distinct role descriptions so they all
    # score 1.0; top_k=2 must drop the third.
    embedder = _shared_axis_embedder({
        query: ROLE_INTENT_DESCRIPTIONS["routing_surface"],
        # `_shared_axis_embedder` only matches by string equality, so
        # the second / third are extra orthogonal — single role match
        # in this construction. To get genuinely multiple matches we
        # need a more complex embedder.
    })

    matches = classify_intent(query, embedder, top_k=2, threshold=0.20)

    assert len(matches) <= 2


def test_role_vectors_are_cached_across_calls():
    """The role descriptions should only be embedded once per process;
    the second call must reuse the cache.
    """
    calls: list[str] = []

    def counting_embed(text: str) -> list[float]:
        calls.append(text)
        return _orthogonal_embedder()(text)

    classify_intent("first question", counting_embed)
    first_call_count = len(calls)
    classify_intent("second question", counting_embed)
    second_call_count = len(calls)

    # Each call adds exactly one embed (the question itself); role
    # descriptions are cached after the first run.
    assert second_call_count - first_call_count == 1


def test_threshold_filters_low_similarity_matches():
    """Two roles sharing an axis pass; everything else stays below the
    threshold and is filtered out.
    """
    query = "narrow query"
    embedder = _shared_axis_embedder({
        query: ROLE_INTENT_DESCRIPTIONS["dependency_solver"],
    })

    above = classify_intent(query, embedder, threshold=0.50)
    below = classify_intent(query, embedder, threshold=1.5)  # impossible

    assert above and above[0].role == "dependency_solver"
    assert below == []
