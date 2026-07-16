"""Phase 1f echelon-2: signature trimming + token-budget packing.

Targets the pure helpers (``_code_signature``, ``_apply_render_and_budget``)
directly — the graph walk in ``build_context_for_candidates`` is covered by
the benchmark and the live gate.
"""

from __future__ import annotations

import pytest

from context_engine.axis.context_builder import (
    ContextBundle,
    ContextSymbol,
    TokenCreditTrace,
    _apply_render_and_budget,
    _apply_span_line_rerank,
    _bundle_token_count,
    _code_compact,
    _code_impact_surface,
    _code_signature,
    _dedupe_bundles_by_seed_uid,
    _initial_credit_render,
    _leader_pool_metrics,
    _leader_transaction_limit,
    _mad,
    _median,
    _noise_level_from_tail,
    _rank_decay_body_credit,
    _render_bundle,
    _span_candidate_oracle_recall,
    _TokenCreditCoverageState,
    _upgrade_exact_delta,
    _upgrade_semantic_value,
    probe_candidate_lexical_spans,
)
from context_engine.axis.role_retrieval import RoleCandidate
from context_engine.observability.metrics import estimate_text_tokens

# --- helpers ---------------------------------------------------------------


def _sym(
    uid: str,
    code: str,
    *,
    qualified_name: str = "",
    file_path: str = "/f.py",
) -> ContextSymbol:
    return ContextSymbol(
        uid=uid,
        name=uid,
        file_path=file_path,
        role="r",
        distance_from_seed=0,
        expansion_step=None,
        code=code,
        qualified_name=qualified_name,
    )


# --- _dedupe_bundles_by_seed_uid -------------------------------------------


def test_dedupe_bundles_by_seed_uid_keeps_highest_utility():
    low = ContextBundle(
        role="seeds",
        seed=_sym("same", "def low(): pass", file_path="/a.py"),
        related=(),
        utility_score=0.3,
    )
    high = ContextBundle(
        role="impact_analysis",
        seed=_sym("same", "def high(): pass", file_path="/a.py"),
        related=(),
        utility_score=0.9,
    )
    other = ContextBundle(
        role="seeds",
        seed=_sym("other", "def other(): pass", file_path="/b.py"),
        related=(),
        utility_score=0.5,
    )
    out = _dedupe_bundles_by_seed_uid([low, high, other])
    assert [b.seed.uid for b in out] == ["same", "other"]
    assert out[0].utility_score == pytest.approx(0.9)


def test_token_credit_dedupes_axis_duplicate_symbols_before_packing():
    dup_a = ContextBundle(
        role="seeds",
        seed=_sym("dup", "def dup(): pass", file_path="/a.py"),
        related=(),
        utility_score=0.95,
    )
    dup_b = ContextBundle(
        role="structural_neighbour",
        seed=_sym("dup", "def dup(): pass", file_path="/a.py"),
        related=(),
        utility_score=0.10,
    )
    unique = ContextBundle(
        role="seeds",
        seed=_sym("uniq", "def uniq(): pass", file_path="/b.py"),
        related=(),
        utility_score=0.50,
    )
    out = _apply_render_and_budget(
        [dup_a, dup_b, unique],
        token_budget=20,
        render_mode="full",
    )
    assert len(out) == 2
    assert {b.seed.uid for b in out} == {"dup", "uniq"}


# --- marginal uid ledger (first-wins pricing) --------------------------------


def test_marginal_purchase_and_exact_upgrade_delta_price_first_wins():
    state = _TokenCreditCoverageState(file_soft_cap=1000)
    shared_sig = "def shared(a, b):"
    shared_full = "def shared(a, b):\n    return a + b\n"
    first = ContextBundle(
        role="seeds",
        seed=_sym("a", "def a():", file_path="/a.py"),
        related=(_sym("shared", shared_sig, file_path="/s.py"),),
    )
    second = ContextBundle(
        role="seeds",
        seed=_sym("b", "def b():", file_path="/b.py"),
        related=(_sym("shared", shared_sig, file_path="/s.py"),),
    )

    tok = estimate_text_tokens
    assert state.marginal_purchase_cost(first) == tok("def a():") + tok(shared_sig)
    state.charge_purchase(first)
    # ``shared`` already prints from the first bundle — the second pays only
    # for its seed.
    assert state.marginal_purchase_cost(second) == tok("def b():")
    state.charge_purchase(second)

    selected: list[dict[str, object]] = [
        {"index": 0, "source": first, "rendered": first, "cost": 0},
        {"index": 1, "source": second, "rendered": second, "cost": 0},
    ]

    # Upgrading entry 1 re-renders ``shared`` richer, but entry 0's occurrence
    # wins the first-wins dedupe — only entry 1's own seed delta prints.
    second_up = ContextBundle(
        role="seeds",
        seed=_sym("b", "def b():\n    return 2\n", file_path="/b.py"),
        related=(_sym("shared", shared_full, file_path="/s.py"),),
    )
    assert _upgrade_exact_delta(selected, 1, second_up) == tok("def b():\n    return 2\n") - tok(
        "def b():"
    )
    selected[1]["rendered"] = second_up

    # The fold-drop case: entry 0's new render DROPS ``shared``, so entry 1's
    # richer occurrence surfaces into the prompt — the exact delta charges the
    # swap instead of crediting the dropped stub (the 2026-07 overshoot bug).
    first_up = ContextBundle(
        role="seeds",
        seed=_sym("a", "def a():", file_path="/a.py"),
        related=(),
    )
    assert _upgrade_exact_delta(selected, 0, first_up) == tok(shared_full) - tok(shared_sig)


def test_upgrade_semantic_credit_uses_only_new_first_wins_tokens():
    shared_sig = "def shared(value):"
    shared_full = "def shared(value):\n    return value + 1\n"
    low = ContextSymbol(
        uid="shared",
        name="shared",
        file_path="/s.py",
        role="r",
        distance_from_seed=1,
        expansion_step="binding",
        code=shared_sig,
        semantic_excess=0.10,
        tier_weight=1.0,
    )
    high = ContextSymbol(
        uid="shared",
        name="shared",
        file_path="/s.py",
        role="r",
        distance_from_seed=1,
        expansion_step="binding",
        code=shared_sig,
        semantic_excess=0.80,
        tier_weight=0.50,
    )
    first = ContextBundle(
        role="r",
        seed=_sym("first", "def first():", file_path="/a.py"),
        related=(low,),
    )
    second = ContextBundle(
        role="r",
        seed=_sym("second", "def second():", file_path="/b.py"),
        related=(high,),
    )
    selected: list[dict[str, object]] = [
        {"index": 0, "source": first, "rendered": first, "cost": 0},
        {"index": 1, "source": second, "rendered": second, "cost": 0},
    ]
    second_up = ContextBundle(
        role="r",
        seed=second.seed,
        related=(ContextSymbol(**{**high.to_dict(), "code": shared_full}),),
    )
    # Entry 0 owns ``shared`` in prompt order, so enriching entry 1 is free
    # both in tokens and in semantic utility.
    assert _upgrade_semantic_value(selected, 1, second_up) == pytest.approx(0.0)

    first_up = ContextBundle(
        role="r",
        seed=first.seed,
        related=(ContextSymbol(**{**high.to_dict(), "code": shared_full}),),
    )
    # The added body belongs to the high-relevance first owner: .80 × .50.
    assert _upgrade_semantic_value(selected, 0, first_up) == pytest.approx(0.40)


def test_token_credit_budget_pays_shared_related_symbol_once():
    # One fat related symbol shared by three bundles: gross accounting bills
    # it three times and evicts bundles that the deduped prompt would fit.
    shared = _sym(
        "shared", "def shared(alpha, beta, gamma, delta, epsilon, zeta):", file_path="/s.py"
    )
    bundles = [
        ContextBundle(
            role="seeds",
            seed=_sym(uid, f"def {uid}():", file_path=f"/{uid}.py"),
            related=(shared,),
            utility_score=score,
        )
        for uid, score in (("a", 0.9), ("b", 0.8), ("c", 0.7))
    ]
    shared_cost = estimate_text_tokens(shared.code or "")
    seed_cost = estimate_text_tokens("def a():")
    deduped_total = shared_cost + 3 * seed_cost
    gross_total = 3 * (shared_cost + seed_cost)
    budget = deduped_total + 2  # fits deduped, nowhere near gross
    assert gross_total > budget

    out = _apply_render_and_budget(bundles, token_budget=budget, render_mode="signature_only")

    assert len(out) == 3
    printed: set[str] = set()
    printed_tokens = 0
    for bundle in out:
        for sym in bundle.all_symbols():
            if sym.uid in printed:
                continue
            printed.add(sym.uid)
            printed_tokens += estimate_text_tokens(sym.code or "")
    assert printed_tokens <= budget


def test_cap_relaxation_buys_large_body_on_leftover_budget():
    # Two leaders → per-step cap = budget/2, below the big body's cost. The
    # capped sweep leaves the big bundle at signature; the relaxation sweep
    # must spend the leftover budget on its body.
    big_body = "def big():\n" + "    x = 1\n" * 80
    bundles = [
        ContextBundle(
            role="seeds",
            seed=_sym("big", big_body, file_path="/big.py"),
            related=(),
            utility_score=1.0,
        ),
        ContextBundle(
            role="seeds",
            seed=_sym("mid", "def mid():\n    return 2\n", file_path="/mid.py"),
            related=(),
            utility_score=1.0,
        ),
        ContextBundle(
            role="seeds",
            seed=_sym("w1", "def w1(): pass", file_path="/w1.py"),
            related=(),
            utility_score=0.2,
        ),
        ContextBundle(
            role="seeds",
            seed=_sym("w2", "def w2(): pass", file_path="/w2.py"),
            related=(),
            utility_score=0.2,
        ),
    ]
    budget = 300
    big_cost = estimate_text_tokens(big_body)
    assert _leader_transaction_limit(budget, leader_count=2) < big_cost

    out = _apply_render_and_budget(bundles, token_budget=budget, render_mode="full")

    big_rendered = next(b for b in out if b.seed.uid == "big")
    assert "x = 1" in (big_rendered.seed.code or "")
    assert estimate_text_tokens(big_rendered.seed.code or "") >= big_cost // 2


def test_render_ceiling_lifts_to_full_on_leftover_budget():
    # A ``hybrid`` profile used to cap the ladder at hybrid — related symbols
    # stayed signatures forever even with most of the budget unspent. The
    # profile mode shapes initial coverage; the budget is the only ceiling.
    # The related member shares the seed's file: cross-file members are
    # body-frozen by design (see the freeze test below).
    related = _sym("rel", "def rel():\n    return 42\n", file_path="/s.py")
    bundle = ContextBundle(
        role="seeds",
        seed=_sym("s", "def s():\n    return 1\n", file_path="/s.py"),
        related=(related,),
        utility_score=1.0,
    )

    out = _apply_render_and_budget([bundle], token_budget=500, render_mode="hybrid")

    rel_rendered = next(s for s in out[0].all_symbols() if s.uid == "rel")
    assert "return 42" in (rel_rendered.code or "")


def test_cross_file_member_bodies_freeze_at_signature():
    # A related member in a file no seed points at keeps only its signature
    # on every rung — the symbol (and its file) still reaches the bundle, so
    # file coverage survives, but its body can't win budget as noise. A
    # member in another BUNDLE's seed file is not cross-file and still lifts.
    stranger = _sym("stranger", "def stranger():\n    return 99\n", file_path="/elsewhere.py")
    neighbour = _sym("nb", "def nb():\n    return 7\n", file_path="/other_seed.py")
    bundles = [
        ContextBundle(
            role="seeds",
            seed=_sym("s", "def s():\n    return 1\n", file_path="/s.py"),
            related=(stranger, neighbour),
            utility_score=1.0,
        ),
        ContextBundle(
            role="seeds",
            seed=_sym("s2", "def s2():\n    return 2\n", file_path="/other_seed.py"),
            related=(),
            utility_score=0.9,
        ),
    ]

    out = _apply_render_and_budget(bundles, token_budget=500, render_mode="hybrid")

    symbols = {s.uid: s for b in out for s in b.all_symbols()}
    assert "return 99" not in (symbols["stranger"].code or "")
    assert "def stranger" in (symbols["stranger"].code or "")
    assert "return 7" in (symbols["nb"].code or "")


def test_third_wave_upgrades_below_floor_symbols_on_leftover_budget():
    # Leaders saturate cheaply and budget remains; the below-floor bundle's
    # body exceeds the per-step cap so the capped pass can't buy it and the
    # leaders-only relaxation excludes it. The third wave must deliver the
    # noise-floor contract: weak symbols enter on leftover budget.
    weak_body = "def weak():\n" + "    y = 2\n" * 80
    bundles = [
        ContextBundle(
            role="seeds",
            seed=_sym("l1", "def l1():\n    return 1\n", file_path="/l1.py"),
            related=(),
            utility_score=1.0,
        ),
        ContextBundle(
            role="seeds",
            seed=_sym("l2", "def l2():\n    return 2\n", file_path="/l2.py"),
            related=(),
            utility_score=1.0,
        ),
        ContextBundle(
            role="seeds",
            seed=_sym("weak", weak_body, file_path="/weak.py"),
            related=(),
            utility_score=0.2,
        ),
        ContextBundle(
            role="seeds",
            seed=_sym("w2", "def w2(): pass", file_path="/w2.py"),
            related=(),
            utility_score=0.2,
        ),
    ]
    budget = 300
    assert _leader_transaction_limit(budget, leader_count=2) < estimate_text_tokens(weak_body)

    out = _apply_render_and_budget(bundles, token_budget=budget, render_mode="full")

    weak = next(b for b in out if b.seed.uid == "weak")
    assert "y = 2" in (weak.seed.code or "")
    printed: set[str] = set()
    total = 0
    for bundle in out:
        for sym in bundle.all_symbols():
            if sym.uid in printed:
                continue
            printed.add(sym.uid)
            total += estimate_text_tokens(sym.code or "")
    assert total <= budget


# --- leader noise (MAD tail) ------------------------------------------------


def test_median_and_mad():
    assert _median([1.0, 2.0, 3.0]) == pytest.approx(2.0)
    assert _mad([1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_leader_pool_metrics_one_strong_above_noise():
    from context_engine.axis.context_builder import _BundleStatic

    # 1 strong + 99 weak: the weak tail's marginality sits at 5, so the floor
    # is 5 and only the peak symbol clears it → count=1.
    static = [
        _BundleStatic(
            files=frozenset({f"/f{i}.py"}),
            steps=frozenset(),
            tier_weight=1.0,
            impact_mode=False,
            base_utility=0.05 if i else 1.0,
            structural_bridge=0.0,
        )
        for i in range(100)
    ]
    noise, count = _leader_pool_metrics(static)
    assert noise == pytest.approx(5.0)
    assert count == 1
    assert _leader_transaction_limit(10_000, leader_count=count) == 10_000


def test_leader_pool_metrics_counts_symbols_above_noise_floor():
    from context_engine.axis.context_builder import _BundleStatic

    static = [
        _BundleStatic(
            files=frozenset({"/a.py"}),
            steps=frozenset(),
            tier_weight=1.0,
            impact_mode=False,
            base_utility=1.0,
            structural_bridge=0.0,
        ),
        _BundleStatic(
            files=frozenset({"/b.py"}),
            steps=frozenset(),
            tier_weight=1.0,
            impact_mode=False,
            base_utility=0.8,
            structural_bridge=0.0,
        ),
        _BundleStatic(
            files=frozenset({"/c.py"}),
            steps=frozenset(),
            tier_weight=1.0,
            impact_mode=False,
            base_utility=0.05,
            structural_bridge=0.0,
        ),
    ]
    noise, count = _leader_pool_metrics(static)
    # tail (u <= median 0.8) → marginality [5, 80]: floor = 42.5 + 1.4826*37.5.
    assert noise == pytest.approx(42.5 + 1.4826 * 37.5)
    # Only the peak (100) clears the floor; 80 sits inside the noise spread.
    assert count == 1
    assert _leader_transaction_limit(10_000, leader_count=count) == int(10_000 / count)


def test_leader_noise_floor_drops_when_tail_weakens():
    from context_engine.axis.context_builder import _BundleStatic

    def _pool(weak: float) -> list[_BundleStatic]:
        return [
            _BundleStatic(
                files=frozenset({f"/f{i}.py"}),
                steps=frozenset(),
                tier_weight=1.0,
                impact_mode=False,
                base_utility=u,
                structural_bridge=0.0,
            )
            for i, u in enumerate((1.0, 0.95, weak, weak))
        ]

    # The floor must FALL as the tail weakens: signal stands out more against
    # weaker noise. The old distance-axis form inverted this response.
    noise_mid_tail, _ = _leader_pool_metrics(_pool(0.5))
    noise_weak_tail, _ = _leader_pool_metrics(_pool(0.1))
    assert noise_mid_tail == pytest.approx(50.0)
    assert noise_weak_tail == pytest.approx(10.0)
    assert noise_weak_tail < noise_mid_tail


def test_noise_level_from_tail_matches_robust_formula():
    tail = [90.0, 92.0, 94.0, 96.0]
    assert _noise_level_from_tail(tail) == pytest.approx(_median(tail) + 1.4826 * _mad(tail))


def test_leader_transaction_limit_single_symbol_gets_full_budget():
    assert _leader_transaction_limit(10_000, leader_count=1) == 10_000


def test_leader_transaction_limit_scales_with_leader_count():
    assert _leader_transaction_limit(10_000, leader_count=100) == 100
    assert _leader_transaction_limit(10_000, leader_count=50) == 200


# --- _code_signature -------------------------------------------------------


def test_signature_function_drops_body_keeps_decorators():
    code = "@cached\ndef foo(a, b):\n    x = a + b\n    return x\n"
    assert _code_signature(code) == "@cached\ndef foo(a, b):"


def test_signature_multiline_header_survives_to_the_colon():
    code = "def foo(\n    a: int,\n    b: int,\n) -> int:\n    return a + b\n"
    assert _code_signature(code) == "def foo(\n    a: int,\n    b: int,\n) -> int:"


def test_signature_class_header():
    assert _code_signature("class Foo(Base):\n    x = 1\n") == "class Foo(Base):"


def test_signature_non_callable_is_first_nonempty_line():
    assert _code_signature("\napp = Flask(__name__)\napp.run()\n") == "app = Flask(__name__)"


def test_signature_empty_in_empty_out():
    assert _code_signature("") == ""
    assert _code_signature(None) == ""


def test_code_compact_keeps_structure_calls_and_returns():
    code = '''def render(x):
    """Docstring goes away."""
    temp = prepare(x)
    noisy = "x" * 1000
    if temp:
        return finish(temp)
    debug_log("ignored enough to still keep call")
    return None
'''

    compact = _code_compact(code)

    assert '"""Docstring goes away."""' not in compact
    assert "def render(x):" in compact
    assert "temp = prepare(x)" in compact
    assert "if temp:" in compact
    assert "return finish(temp)" in compact
    assert "return None" in compact


def test_signature_render_tracks_only_the_lines_it_keeps():
    symbol = ContextSymbol(
        uid="target",
        name="target",
        file_path="/f.py",
        role="r",
        distance_from_seed=0,
        expansion_step=None,
        code="@cached\ndef target():\n    work()\n    return 1\n",
        start_line=40,
        end_line=43,
    )

    rendered = _render_bundle(
        ContextBundle(role="r", seed=symbol),
        "signature_only",
    ).seed

    assert rendered.code == "@cached\ndef target():"
    assert rendered.effective_rendered_spans() == ((40, 41),)


def test_compact_render_tracks_noncontiguous_source_intervals():
    symbol = ContextSymbol(
        uid="target",
        name="target",
        file_path="/f.py",
        role="r",
        distance_from_seed=0,
        expansion_step=None,
        code=(
            "def target(value):\n"
            '    """Not rendered."""\n'
            "    prepared = prepare(value)\n"
            "    if prepared:\n"
            "        pass\n"
            "    return finish(prepared)\n"
        ),
        start_line=100,
        end_line=105,
    )

    rendered = _render_bundle(
        ContextBundle(role="r", seed=symbol),
        "hybrid_compact",
        full_render_max_depth=0,
    ).seed

    assert "Not rendered" not in (rendered.code or "")
    assert "pass" not in (rendered.code or "")
    assert rendered.effective_rendered_spans() == ((100, 100), (102, 103), (105, 105))


def test_span_line_rerank_selects_answer_window_over_same_symbol_hard_negatives():
    body = [
        "def dispatch(request):",
        "    noisy_0 = load_metrics()",
        "    noisy_1 = load_headers()",
        "    noisy_2 = load_cookies()",
        "    noisy_3 = load_session()",
        "    noisy_4 = load_locale()",
        "    noisy_5 = load_theme()",
        "    noisy_6 = load_flags()",
        "    noisy_7 = load_tracing()",
        "    noisy_8 = load_debug()",
        "    match = resolve_url_pattern(request.path)",
        "    handler = bind_resolved_view(match)",
        "    return handler(request)",
        "    noisy_tail = unreachable_cleanup()",
    ]
    symbol = ContextSymbol(
        uid="dispatch",
        name="dispatch",
        file_path="/router.py",
        role="routing_surface",
        distance_from_seed=0,
        expansion_step=None,
        code="\n".join(body),
        start_line=100,
        end_line=113,
    )
    calls: list[list[str]] = []

    def score(texts: list[str]) -> list[float]:
        calls.append(list(texts))
        return [0.95 if "resolve_url_pattern" in text else 0.05 for text in texts]

    [rendered_bundle] = _apply_span_line_rerank(
        [ContextBundle(role="routing_surface", seed=symbol)],
        query_text="Where is the URL pattern resolved and bound to a view?",
        score_fn=score,
        max_candidates_per_symbol=12,
        max_body_lines=4,
    )
    rendered = rendered_bundle.seed

    assert len(calls) == 1
    assert "resolve_url_pattern" in (rendered.code or "")
    assert "bind_resolved_view" in (rendered.code or "")
    assert "noisy_0" not in (rendered.code or "")
    assert rendered.effective_rendered_spans() != ((100, 113),)
    assert sum(end - start + 1 for start, end in rendered.effective_rendered_spans()) <= 5


def test_span_line_rerank_scores_all_symbols_in_one_embedding_batch():
    first = _sym(
        "first",
        "def first():\n    noise = prepare()\n    return target_alpha()\n",
        file_path="/a.py",
    )
    second = _sym(
        "second",
        "def second():\n    noise = prepare()\n    return target_beta()\n",
        file_path="/b.py",
    )
    batches: list[list[str]] = []

    def score(texts: list[str]) -> list[float]:
        batches.append(list(texts))
        return [1.0 if "target_" in text else 0.0 for text in texts]

    rendered = _apply_span_line_rerank(
        [
            ContextBundle(role="r", seed=first),
            ContextBundle(role="r", seed=second),
        ],
        query_text="target result",
        score_fn=score,
        max_candidates_per_symbol=8,
        max_body_lines=2,
    )

    assert len(batches) == 1
    assert "target_alpha" in (rendered[0].seed.code or "")
    assert "target_beta" in (rendered[1].seed.code or "")


def test_span_line_rerank_treats_explicit_source_line_as_dominant_signal():
    lines = ["def update(instance):"] + [
        f"    noise_{index} = unrelated_{index}()" for index in range(1, 25)
    ]
    lines[20] = "    instance.pk = None"
    lines[21] = "    return deleted_count"
    symbol = ContextSymbol(
        uid="update",
        name="update",
        file_path="/deletion.py",
        role="r",
        distance_from_seed=0,
        expansion_step=None,
        code="\n".join(lines),
        start_line=100,
        end_line=124,
    )

    def misleading_semantic_score(texts: list[str]) -> list[float]:
        return [1.0 if "noise_2" in text else 0.0 for text in texts]

    [rendered] = _apply_span_line_rerank(
        [ContextBundle(role="r", seed=symbol)],
        query_text="The fix belongs in deletion.py:120-121.",
        score_fn=misleading_semantic_score,
        max_candidates_per_symbol=12,
        max_body_lines=4,
    )

    assert "instance.pk = None" in (rendered.seed.code or "")
    assert any(start <= 120 <= end for start, end in rendered.seed.effective_rendered_spans())


def test_span_candidate_oracle_recall_is_independent_of_window_ranking():
    lines = ["def update(instance):"] + [
        f"    noise_{index} = unrelated_{index}()" for index in range(1, 25)
    ]
    lines[20] = "    instance.pk = None"
    lines[21] = "    return deleted_count"
    symbol = ContextSymbol(
        uid="update",
        name="update",
        file_path="/deletion.py",
        role="r",
        distance_from_seed=0,
        expansion_step=None,
        code="\n".join(lines),
        start_line=100,
        end_line=124,
    )

    recall = _span_candidate_oracle_recall(
        symbol,
        query_text="The fix belongs in deletion.py:120-121.",
        gold_lines={120, 121},
        max_candidates=4,
    )

    assert recall == 1.0


def test_pregraph_lexical_span_probe_emits_only_query_matching_windows(monkeypatch):
    lines = ["def update(instance):"] + [
        f"    noise_{index} = unrelated_{index}()" for index in range(1, 25)
    ]
    lines[20] = "    instance.pk = None"
    lines[21] = "    deleted_count = 1"
    candidate = RoleCandidate(
        uid="update",
        name="update",
        qualified_name="deletion.update",
        file_path="/deletion.py",
        role="hybrid_seed",
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=None,
        score=0.9,
    )
    monkeypatch.setattr(
        "context_engine.axis.context_builder._fetch_symbol_payloads",
        lambda *_args, **_kwargs: {
            "update": {
                "code": "\n".join(lines),
                "start_line": 100,
                "end_line": 124,
            }
        },
    )

    evidence, trace = probe_candidate_lexical_spans(
        [candidate],
        workspace_id="ws",
        lance=object(),
        query_text="Where is deleted_count assigned?",
        max_windows_per_symbol=1,
        window_lines=6,
    )

    spans = evidence["update"].spans
    assert any(start <= 121 <= end for start, end in spans)
    assert sum(end - start + 1 for start, end in spans) <= 6
    assert evidence["update"].score > 0.0
    assert "deleted" in evidence["update"].matched_terms
    assert trace.matched_symbols == 1
    assert not trace.fetch_failed

    empty, empty_trace = probe_candidate_lexical_spans(
        [candidate],
        workspace_id="ws",
        lance=object(),
        query_text="completely absent terminology",
    )
    assert empty == {}
    assert empty_trace.matched_symbols == 0


# --- _apply_render_and_budget ---------------------------------------------


def _bundle(
    seed_uid: str, seed_code: str, related=(), *, file_path: str = "/f.py"
) -> ContextBundle:
    return ContextBundle(
        role="r",
        seed=_sym(seed_uid, seed_code, file_path=file_path),
        related=tuple(_sym(u, c, file_path=file_path) for u, c in related),
    )


def test_no_budget_no_render_is_passthrough():
    bundles = [_bundle("a", "x" * 40, [("b", "y" * 40)])]
    assert _apply_render_and_budget(bundles, token_budget=None, render_mode="full") == bundles


def test_token_credit_trace_records_exact_marginal_utility_per_token():
    bundles = [
        ContextBundle(
            role="r1",
            seed=_sym("a", "def a():\n    return 1\n", file_path="/a.py"),
            utility_score=0.9,
        ),
        ContextBundle(
            role="r2",
            seed=_sym("b", "def b():\n    return 2\n", file_path="/b.py"),
            utility_score=0.5,
        ),
    ]
    trace = TokenCreditTrace()

    out = _apply_render_and_budget(
        bundles,
        token_budget=100,
        render_mode="signature_only",
        credit_trace=trace,
    )

    assert len(out) == 2
    assert trace.transactions
    assert sum(transaction.phase == "coverage" for transaction in trace.transactions) == 2
    assert trace.used_tokens == sum(transaction.delta_tokens for transaction in trace.transactions)
    assert trace.cumulative_utility == pytest.approx(
        sum(transaction.delta_utility for transaction in trace.transactions)
    )
    for transaction in trace.transactions:
        assert transaction.utility_per_token == pytest.approx(
            transaction.delta_utility / max(1, transaction.delta_tokens)
        )
        assert transaction.effective_utility >= 0.001
        assert transaction.effective_utility_per_token == pytest.approx(
            transaction.effective_utility / max(1, transaction.delta_tokens)
        )
    assert trace.transactions[-1].cumulative_tokens == trace.used_tokens
    assert trace.transactions[-1].cumulative_utility == pytest.approx(trace.cumulative_utility)


def test_token_credit_trace_does_not_change_render_selection_and_records_upgrades():
    bundle = ContextBundle(
        role="r",
        seed=_sym("upgrade", "def upgrade():\n    return 1\n", file_path="/upgrade.py"),
        utility_score=1.0,
    )
    trace = TokenCreditTrace()

    with_trace = _apply_render_and_budget(
        [bundle], token_budget=100, render_mode="full", credit_trace=trace
    )
    without_trace = _apply_render_and_budget([bundle], token_budget=100, render_mode="full")

    assert with_trace == without_trace
    assert trace.transactions[0].phase == "coverage"
    assert any(transaction.phase.startswith("upgrade_") for transaction in trace.transactions)


def test_rank_decay_body_credit_is_front_loaded_and_bounded():
    credits = [
        _rank_decay_body_credit(
            12_000,
            candidate_rank=rank,
            head_share=0.15,
            decay_rate=0.75,
        )
        for rank in range(1, 40)
    ]

    assert credits[:5] == [1800, 1350, 1012, 759, 569]
    assert all(left >= right for left, right in zip(credits, credits[1:]))
    assert sum(credits) <= int(12_000 * 0.15 / (1.0 - 0.75))


def test_rank_decay_upgrades_head_body_without_relaxing_tail():
    head_code = "def head():\n" + "    head_value = 1\n" * 40
    tail_code = "def tail():\n" + "    tail_value = 2\n" * 40
    bundles = [
        ContextBundle(
            role="r",
            seed=_sym("head", head_code, file_path="/head.py"),
            utility_score=1.0,
        ),
        ContextBundle(
            role="r",
            seed=_sym("tail", tail_code, file_path="/tail.py"),
            utility_score=0.5,
        ),
    ]
    trace = TokenCreditTrace()

    out = _apply_render_and_budget(
        bundles,
        token_budget=1_000,
        render_mode="full",
        rank_decay_body_allocation=True,
        rank_decay_head_share=0.50,
        rank_decay_rate=0.0,
        credit_trace=trace,
    )

    rendered = {bundle.seed.uid: bundle.seed.code or "" for bundle in out}
    assert "head_value" in rendered["head"]
    assert "tail_value" not in rendered["tail"]
    assert trace.allocation_mode == "rank_decay"
    assert 0.0 < trace.coverage_share <= 0.65
    assert any(transaction.phase == "upgrade_rank_decay" for transaction in trace.transactions)
    assert not any(
        transaction.phase in {"upgrade_leader_relaxed", "upgrade_tail_relaxed"}
        for transaction in trace.transactions
    )


def test_rank_decay_falls_back_to_legacy_when_coverage_pressure_is_high():
    bundle = ContextBundle(
        role="r",
        seed=_sym(
            "head",
            "def head():\n" + "    value = 1\n" * 20,
            file_path="/head.py",
        ),
        utility_score=1.0,
    )
    trace = TokenCreditTrace()

    _apply_render_and_budget(
        [bundle],
        token_budget=500,
        render_mode="full",
        rank_decay_body_allocation=True,
        rank_decay_max_coverage_share=0.0,
        credit_trace=trace,
    )

    assert trace.coverage_share > 0.0
    assert trace.allocation_mode == "legacy"
    assert not any(transaction.phase == "upgrade_rank_decay" for transaction in trace.transactions)


def test_rank_decay_pressure_threshold_relaxes_from_8k_to_12k():
    bundle = ContextBundle(
        role="r",
        seed=_sym("head", "def head():\n    return 1\n", file_path="/head.py"),
        utility_score=1.0,
    )
    thresholds: list[float] = []

    for budget in (8_000, 10_000, 12_000):
        trace = TokenCreditTrace()
        _apply_render_and_budget(
            [bundle],
            token_budget=budget,
            render_mode="full",
            rank_decay_body_allocation=True,
            rank_decay_max_coverage_share=0.65,
            credit_trace=trace,
        )
        thresholds.append(trace.rank_decay_coverage_threshold)

    assert thresholds == pytest.approx([0.65, 0.825, 1.0])


def test_token_credit_density_cutoff_rejects_paid_nonpositive_tail_only_when_enabled():
    bundles = [
        ContextBundle(
            role="r",
            seed=_sym(f"u{i}", f"def u{i}():\n    return {i}\n", file_path="/same.py"),
            utility_score=0.0,
        )
        for i in range(8)
    ]
    trace = TokenCreditTrace()

    baseline = _apply_render_and_budget(
        bundles,
        token_budget=1_000,
        render_mode="signature_only",
    )
    cut = _apply_render_and_budget(
        bundles,
        token_budget=1_000,
        render_mode="signature_only",
        min_utility_per_token=0.0,
        credit_trace=trace,
    )

    assert len(cut) < len(baseline)
    assert cut
    assert trace.cutoff_density == pytest.approx(0.0)
    assert trace.cutoff_rejections > 0
    assert all(
        transaction.delta_utility > 0
        for transaction in trace.transactions
        if transaction.phase == "coverage" and transaction.delta_tokens > 0
    )


def test_upgrade_only_density_cutoff_preserves_coverage_transactions():
    bundles = [
        ContextBundle(
            role="r",
            seed=_sym(
                f"seed-{index}",
                f"def seed_{index}():\n" + f"    value_{index} = {index}\n" * 20,
                file_path=f"/seed-{index}.py",
            ),
            utility_score=1.0 - index * 0.1,
        )
        for index in range(3)
    ]
    control_trace = TokenCreditTrace()
    cutoff_trace = TokenCreditTrace()

    _apply_render_and_budget(
        bundles,
        token_budget=1_000,
        render_mode="full",
        rank_decay_body_allocation=True,
        credit_trace=control_trace,
    )
    _apply_render_and_budget(
        bundles,
        token_budget=1_000,
        render_mode="full",
        rank_decay_body_allocation=True,
        upgrade_min_utility_per_token=1_000.0,
        credit_trace=cutoff_trace,
    )

    control_coverage = [
        transaction.uid
        for transaction in control_trace.transactions
        if transaction.phase == "coverage"
    ]
    cutoff_coverage = [
        transaction.uid
        for transaction in cutoff_trace.transactions
        if transaction.phase == "coverage"
    ]
    assert cutoff_coverage == control_coverage
    assert cutoff_trace.cutoff_rejections > 0
    assert not any(
        transaction.phase.startswith("upgrade_") for transaction in cutoff_trace.transactions
    )


def test_token_credit_plateau_freeze_leaves_rejected_budget_unspent():
    bundles = [
        ContextBundle(
            role="r",
            seed=_sym(f"p{i}", f"def p{i}():\n    return {i}\n", file_path="/same.py"),
            utility_score=0.0,
        )
        for i in range(8)
    ]
    trace = TokenCreditTrace()

    _apply_render_and_budget(
        bundles,
        token_budget=1_000,
        render_mode="full",
        min_utility_per_token=0.0,
        freeze_at_utility_plateau=True,
        credit_trace=trace,
    )

    assert trace.cutoff_rejections > 0
    assert trace.spend_ceiling < trace.token_budget
    assert trace.used_tokens <= trace.spend_ceiling


def test_token_credit_plateau_freeze_can_reserve_budget_for_upgrades():
    bundles = [
        ContextBundle(
            role="r",
            seed=_sym(
                f"p{i}",
                f"def p{i}():\n    return {' + '.join(str(n) for n in range(40))}\n",
                file_path="/same.py",
            ),
            utility_score=0.0,
        )
        for i in range(8)
    ]
    frozen_trace = TokenCreditTrace()
    reserve_trace = TokenCreditTrace()

    _apply_render_and_budget(
        bundles,
        token_budget=1_000,
        render_mode="full",
        min_utility_per_token=0.0,
        freeze_at_utility_plateau=True,
        credit_trace=frozen_trace,
    )
    _apply_render_and_budget(
        bundles,
        token_budget=1_000,
        render_mode="full",
        min_utility_per_token=0.0,
        freeze_at_utility_plateau=True,
        plateau_upgrade_reserve_share=0.25,
        credit_trace=reserve_trace,
    )

    assert frozen_trace.cutoff_rejections > 0
    assert reserve_trace.cutoff_rejections == frozen_trace.cutoff_rejections
    assert reserve_trace.spend_ceiling == min(
        reserve_trace.token_budget,
        frozen_trace.spend_ceiling + 250,
    )
    assert reserve_trace.used_tokens <= reserve_trace.spend_ceiling


def test_impact_surface_collapses_multiline_signature_to_one_line():
    code = "def foo(\n    a: int,\n    b: int,\n) -> int:\n    return a + b\n"
    assert _code_impact_surface(code) == "def foo("


def test_impact_surface_falls_back_to_qualified_name():
    assert _code_impact_surface("", qualified_name="pkg.mod.fn", name="fn") == "pkg.mod.fn"


def test_impact_surface_render_mode_trims_bundle():
    bundles = [_bundle("a", "def foo(\n    x,\n) -> int:\n    return x\n")]
    out = _apply_render_and_budget(bundles, token_budget=None, render_mode="impact_surface")
    assert out[0].seed.code == "def foo("


def test_signature_only_trims_every_symbol():
    bundles = [_bundle("a", "def f(x):\n    return x\n", [("b", "class C:\n    pass\n")])]
    out = _apply_render_and_budget(bundles, token_budget=None, render_mode="signature_only")
    assert out[0].seed.code == "def f(x):"
    assert out[0].related[0].code == "class C:"


def test_hybrid_keeps_seed_full_and_signatures_the_rest():
    # Default full_render_max_depth=0: only the seed (depth 0) stays full;
    # every expanded neighbour (depth >= 1) collapses to a signature.
    def _at(uid, depth, code):
        return ContextSymbol(
            uid=uid,
            name=uid,
            file_path="/f.py",
            role="r",
            distance_from_seed=depth,
            expansion_step=None,
            code=code,
        )

    bundle = ContextBundle(
        role="r",
        seed=_at("s", 0, "def seed(x):\n    return x\n"),
        related=(
            _at("near", 1, "def near(y):\n    return y\n"),
            _at("far", 2, "def far(z):\n    return z\n"),
        ),
    )
    out = _apply_render_and_budget([bundle], token_budget=None, render_mode="hybrid")
    assert out[0].seed.code == "def seed(x):\n    return x\n"  # depth 0 full
    assert out[0].related[0].code == "def near(y):"  # depth 1 signature
    assert out[0].related[1].code == "def far(z):"  # depth 2 signature


def test_hybrid_compact_keeps_seed_compact_and_signatures_the_rest():
    bundle = ContextBundle(
        role="r",
        seed=_sym("s", "def seed(x):\n    y = build(x)\n    return y\n"),
        related=(
            ContextSymbol(
                uid="near",
                name="near",
                file_path="/f.py",
                role="r",
                distance_from_seed=1,
                expansion_step=None,
                code="def near(y):\n    return y\n",
            ),
        ),
    )

    [out] = _apply_render_and_budget(
        [bundle],
        token_budget=None,
        render_mode="hybrid_compact",
    )

    assert out.seed.code.rstrip() == "def seed(x):\n    y = build(x)\n    return y"
    assert out.related[0].code == "def near(y):"


def test_generous_budget_keeps_everything():
    bundles = [_bundle("a", "x" * 40, [("b", "y" * 40)]), _bundle("c", "z" * 40)]
    out = _apply_render_and_budget(bundles, token_budget=10_000, render_mode="full")
    # Ample budget -> the packer keeps every bundle (full render, related intact).
    # Output is in selection order, so compare as a set.
    assert {b.seed.uid for b in out} == {"a", "c"}
    a_bundle = next(b for b in out if b.seed.uid == "a")
    assert [r.uid for r in a_bundle.related] == ["b"]


def test_fold_groups_class_members_and_signatures_siblings():
    seed = _sym(
        "target",
        "    def target(self):\n        value = self.helper()\n        return value\n",
        qualified_name="pkg.mod.Service.target",
    )
    sibling = ContextSymbol(
        uid="helper",
        name="helper",
        file_path="/f.py",
        role="r",
        distance_from_seed=1,
        expansion_step="binding",
        code="    def helper(self):\n        return 1\n",
        qualified_name="pkg.mod.Service.helper",
    )
    bundle = ContextBundle(role="r", seed=seed, related=(sibling,))

    [out] = _apply_render_and_budget([bundle], token_budget=None, render_mode="fold")

    # Fold changes the render, not the source identity.  Keeping the member
    # owner aligned with its uid prevents first-wins dedupe from turning a
    # method uid into a class row and hiding the exact owner.
    assert out.seed.uid == "target"
    assert out.seed.name == "target"
    assert out.seed.qualified_name == "pkg.mod.Service.target"
    assert out.seed.expansion_step == "fold"
    assert {
        (owner.uid, owner.name, owner.qualified_name) for owner in out.seed.represented_owners
    } == {
        ("target", "target", "pkg.mod.Service.target"),
        ("helper", "helper", "pkg.mod.Service.helper"),
    }
    assert out.related == ()
    assert out.seed.code == (
        "class Service:\n"
        "    def target(self):\n"
        "        value = self.helper()\n"
        "        return value\n"
        "    def helper(self):"
    )


def test_fold_compact_groups_class_members_as_signatures_only():
    seed = _sym(
        "target",
        "    def target(self):\n        value = self.helper()\n        return value\n",
        qualified_name="pkg.mod.Service.target",
    )
    sibling = ContextSymbol(
        uid="helper",
        name="helper",
        file_path="/f.py",
        role="r",
        distance_from_seed=1,
        expansion_step="binding",
        code="    def helper(self):\n        return 1\n",
        qualified_name="pkg.mod.Service.helper",
    )
    bundle = ContextBundle(role="r", seed=seed, related=(sibling,))

    out = _render_bundle(bundle, "fold_compact")

    assert out.render_mode == "fold_compact"
    assert out.seed.code == ("class Service:\n    def target(self):\n    def helper(self):")


def test_fold_compact_unions_member_signature_spans_without_claiming_synthetic_header():
    seed = ContextSymbol(
        uid="target",
        name="target",
        file_path="/f.py",
        role="r",
        distance_from_seed=0,
        expansion_step=None,
        code="    def target(self):\n        return self.helper()\n",
        qualified_name="pkg.mod.Service.target",
        start_line=10,
        end_line=11,
    )
    sibling = ContextSymbol(
        uid="helper",
        name="helper",
        file_path="/f.py",
        role="r",
        distance_from_seed=1,
        expansion_step="binding",
        code="    def helper(self):\n        return 1\n",
        qualified_name="pkg.mod.Service.helper",
        start_line=20,
        end_line=21,
    )

    rendered = _render_bundle(
        ContextBundle(role="r", seed=seed, related=(sibling,)),
        "fold_compact",
    ).seed

    assert rendered.effective_rendered_spans() == ((10, 10), (20, 20))


def test_fold_leaves_ambiguous_single_method_alone():
    bundle = ContextBundle(
        role="r",
        seed=_sym(
            "target",
            "def target():\n    return 1\n",
            qualified_name="pkg.mod.target",
        ),
        related=(),
    )

    [out] = _apply_render_and_budget([bundle], token_budget=None, render_mode="fold")

    assert out == bundle


def test_impact_tiered_folds_core_class_and_signatures_the_rest():
    def _at(uid, code, *, path="/repo/celery/app/task.py", qn=""):
        return ContextSymbol(
            uid=uid,
            name=uid,
            file_path=path,
            role="impact_analysis",
            distance_from_seed=0 if uid == "m1" else 1,
            expansion_step=None,
            code=code,
            qualified_name=qn or uid,
        )

    bundle = ContextBundle(
        role="impact_analysis",
        seed=_at(
            "m1",
            "    def route(self, task):\n        pass\n",
            qn="celery.app.routes.Router.route",
        ),
        related=(
            _at(
                "m2",
                "    def lookup_route(self, name):\n        pass\n",
                qn="celery.app.routes.Router.lookup_route",
            ),
        ),
    )
    out = _apply_render_and_budget([bundle], token_budget=None, render_mode="impact_tiered")
    assert out[0].render_mode == "impact_tiered"
    assert "class Router:" in out[0].seed.code
    assert "def route(self, task):" in out[0].seed.code
    assert "def lookup_route(self, name):" in out[0].seed.code
    assert "pass" not in out[0].seed.code


def test_impact_tiered_related_tail_is_one_line_stub():
    def _at(uid, code, *, depth, path="/repo/celery/app/base.py", qn=""):
        return ContextSymbol(
            uid=uid,
            name=uid,
            file_path=path,
            role="impact_analysis",
            distance_from_seed=depth,
            expansion_step=None,
            code=code,
            qualified_name=qn or uid,
        )

    bundle = ContextBundle(
        role="impact_analysis",
        seed=_at(
            "s", "def send_task(self, name):\n    pass\n", depth=0, path="/repo/celery/app/base.py"
        ),
        related=(
            _at(
                "r",
                "def route(\n    self,\n    task,\n) -> str:\n    pass\n",
                depth=1,
                path="/repo/celery/app/routes.py",
            ),
        ),
    )
    out = _apply_render_and_budget([bundle], token_budget=None, render_mode="impact_tiered")
    assert out[0].seed.code == "def send_task(self, name):"
    assert out[0].related[0].code == "def route("


def test_impact_tiered_skips_fold_for_test_tier_class():
    def _at(uid, code, *, path, qn):
        return ContextSymbol(
            uid=uid,
            name=uid,
            file_path=path,
            role="impact_analysis",
            distance_from_seed=0,
            expansion_step=None,
            code=code,
            qualified_name=qn,
        )

    bundle = ContextBundle(
        role="impact_analysis",
        seed=_at(
            "m1",
            "    def test_a(self):\n        pass\n",
            path="/repo/t/unit/test_routes.py",
            qn="t.unit.test_routes.TestRoutes.test_a",
        ),
        related=(
            _at(
                "m2",
                "    def test_b(self):\n        pass\n",
                path="/repo/t/unit/test_routes.py",
                qn="t.unit.test_routes.TestRoutes.test_b",
            ),
        ),
    )
    out = _apply_render_and_budget([bundle], token_budget=None, render_mode="impact_tiered")
    assert out[0].render_mode == "impact_tiered"
    assert "class " not in (out[0].seed.code or "")
    assert "def test_a(self):" in out[0].seed.code
    assert out[0].seed.code.startswith("def test")
    assert len(out[0].seed.code.splitlines()) == 1


def test_token_credit_impact_tiered_climbs_on_leftover_budget():
    """Impact profile starts at impact_tiered coverage, but the profile mode
    is initial shape, not a hard ceiling — leftover budget buys the rich
    rungs (the old cap left budget unspendable while answer bodies sat one
    rung above)."""
    bundle = ContextBundle(
        role="impact_analysis",
        seed=_sym("x", "def x():\n    return 1\n"),
        related=(),
        utility_score=1.0,
    )
    [out] = _apply_render_and_budget(
        [bundle],
        token_budget=10_000,
        render_mode="impact_tiered",
    )
    assert "return 1" in (out.seed.code or "")


def test_token_credit_downgrades_oversized_full_candidate_to_signature():
    code = "def large():\n" + ("    x = 1\n" * 200)
    bundle = ContextBundle(
        role="r",
        seed=_sym("large", code, qualified_name="pkg.mod.large"),
        related=(),
        utility_score=1.0,
    )

    [out] = _apply_render_and_budget(
        [bundle],
        token_budget=100,
        render_mode="full",
    )

    assert out.render_mode != "full"
    assert _bundle_token_count(out) <= 100
    assert out.seed.code.startswith("def large():")


def test_token_credit_upgrades_passive_when_surplus_remains():
    bundle = ContextBundle(
        role="r",
        seed=_sym("passive", "def passive():\n    return 1\n"),
        related=(),
        utility_score=0.5,
        passive=True,
    )

    [out] = _apply_render_and_budget(
        [bundle],
        token_budget=100,
        render_mode="full",
    )

    assert out.render_mode == "full"
    assert out.seed.code == "def passive():\n    return 1\n"


def test_token_credit_coverage_prefers_new_file_over_duplicate_file():
    same_file_top = ContextBundle(
        role="r",
        seed=_sym("same_top", "def same_top():\n    return 1\n", file_path="/a.py"),
        related=(),
        utility_score=0.90,
    )
    same_file_second = ContextBundle(
        role="r",
        seed=_sym(
            "same_second",
            "def same_second():\n    return 2\n",
            file_path="/a.py",
        ),
        related=(),
        utility_score=0.89,
    )
    new_file = ContextBundle(
        role="r",
        seed=_sym("new_file", "def new_file():\n    return 3\n", file_path="/b.py"),
        related=(),
        utility_score=0.75,
    )

    out = _apply_render_and_budget(
        [same_file_top, same_file_second, new_file],
        token_budget=8,
        render_mode="full",
    )

    assert [b.seed.uid for b in out] == ["same_top", "new_file"]


def test_token_credit_coverage_demotes_example_tier_against_core():
    example = ContextBundle(
        role="r",
        seed=_sym(
            "example",
            "def example():\n    return 1\n",
            file_path="/repo/docs_src/tutorial001.py",
        ),
        related=(),
        utility_score=0.90,
    )
    core = ContextBundle(
        role="r",
        seed=_sym("core", "def core():\n    return 2\n", file_path="/repo/pkg/core.py"),
        related=(),
        utility_score=0.70,
    )

    out = _apply_render_and_budget(
        [example, core],
        token_budget=4,
        render_mode="full",
    )

    assert [b.seed.uid for b in out] == ["core"]


def test_token_credit_coverage_prefers_structural_bridge_related_file():
    local = ContextBundle(
        role="r",
        seed=_sym("local", "def local():\n    return 1\n", file_path="/repo/pkg/local.py"),
        related=(),
        utility_score=0.75,
    )
    bridge = ContextBundle(
        role="r",
        seed=_sym(
            "seed",
            "def seed():\n    return 2\n",
            file_path="/repo/pkg/topic.py",
        ),
        related=(
            ContextSymbol(
                uid="api",
                name="api",
                file_path="/repo/pkg/api.py",
                role="r",
                distance_from_seed=2,
                expansion_step="hook_transparency",
                code="def api():\n    return 3\n",
            ),
        ),
        utility_score=0.60,
    )

    out = _apply_render_and_budget(
        [local, bridge],
        token_budget=7,
        render_mode="full",
    )

    assert [b.seed.uid for b in out] == ["seed"]
    assert [sym.uid for sym in out[0].related] == ["api"]


def test_token_credit_coverage_prefers_runtime_dispatch_bridge_to_core_file():
    local = ContextBundle(
        role="r",
        seed=_sym("local", "def local():\n    return helper()\n", file_path="/repo/pkg/local.py"),
        related=(
            ContextSymbol(
                uid="helper",
                name="helper",
                file_path="/repo/pkg/local.py",
                role="r",
                distance_from_seed=1,
                expansion_step="binding_structure_expansion",
                code="def helper():\n    return 1\n",
            ),
        ),
        utility_score=0.64,
    )
    bridge = ContextBundle(
        role="r",
        seed=_sym(
            "seed",
            "def seed():\n    return dispatch(value)\n",
            file_path="/repo/pkg/base.py",
        ),
        related=(
            ContextSymbol(
                uid="runtime",
                name="runtime",
                file_path="/repo/pkg/runtime.py",
                role="r",
                distance_from_seed=1,
                expansion_step="deferred_runtime_dispatch",
                code="def runtime(value):\n    return value\n",
            ),
        ),
        utility_score=0.50,
    )

    out = _apply_render_and_budget(
        [local, bridge],
        token_budget=9,
        render_mode="full",
    )

    assert [b.seed.uid for b in out] == ["seed"]
    assert [sym.uid for sym in out[0].related] == ["runtime"]


def test_token_credit_starts_foldable_active_bundle_as_fold_coverage():
    fold_bundle = ContextBundle(
        role="r",
        seed=_sym(
            "b_target",
            "    def target(self):\n        return 1\n",
            qualified_name="pkg.mod.Service.target",
        ),
        related=(
            ContextSymbol(
                uid="b_helper",
                name="helper",
                file_path="/f.py",
                role="r",
                distance_from_seed=1,
                expansion_step="binding",
                code="    def helper(self):\n        return 2\n",
                qualified_name="pkg.mod.Service.helper",
            ),
        ),
        utility_score=0.50,
    )
    rendered, cost = _initial_credit_render(
        fold_bundle,
        transaction_limit=10_000,
        full_render_max_depth=0,
    )

    assert cost == _bundle_token_count(rendered)
    assert rendered.render_mode == "fold_compact"
    assert rendered.seed.code == ("class Service:\n    def target(self):\n    def helper(self):")
