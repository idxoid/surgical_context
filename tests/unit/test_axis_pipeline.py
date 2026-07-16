"""Unit tests for the canonical axis pipeline (``run_axis_retrieval``).

The pipeline is the single read-side function the ``/ask/axis`` endpoint,
the QA benchmark, and the PromptContext provider all share. These tests
pin its *seam* — the layered ``AxisRetrievalResult`` — without a live
Neo4j/Lance: the stage functions are stubbed on their source modules
(which the pipeline reaches module-qualified) and the graph passes run
real against a bare ``object()`` db, which ``walk_neighbours`` degrades to
``[]`` on. The recall math that reads these layers lives in
``QA.axis_benchmark`` and is validated by the benchmark itself.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from context_engine.axis import pipeline as axis_pipeline
from context_engine.axis.context_builder import ContextBundle, ContextSymbol
from context_engine.axis.intent_classifier import IntentMatch
from context_engine.axis.role_retrieval import RoleCandidate, WorkspaceScan
from context_engine.observability.metrics import RequestTrace


def _cand(
    uid: str,
    path: str,
    *,
    score: float = 0.8,
    utility_score: float | None = None,
    query_similarity: float | None = None,
) -> RoleCandidate:
    return RoleCandidate(
        uid=uid,
        name=uid,
        file_path=path,
        role="routing_surface",
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=0.5,
        score=score,
        utility_score=utility_score,
        query_similarity=query_similarity,
    )


def _intent_budget_token_limit(render_budget) -> int | None:
    if render_budget is None:
        return None
    return render_budget.token_budget


def _intent_budget_render_mode(render_budget) -> str:
    if render_budget is None:
        return "full"
    return render_budget.render_mode


class _FakeLance:
    def _embed(self, texts):  # noqa: D401 - stub
        return [[0.0] * 4]


@pytest.fixture
def stub_stages(monkeypatch):
    """Stub intent / retrieval / context / ranking on their source modules.

    Three role candidates per intent role (so the per-role cap is
    observable); the graph pool passes are left real and return ``[]``
    against the bare-object db.
    """
    import context_engine.axis.axis_ranking as _rank_mod
    import context_engine.axis.context_builder as _ctx_mod
    import context_engine.axis.intent_classifier as _intent_mod
    import context_engine.axis.role_retrieval as _retr_mod

    monkeypatch.setattr(
        _intent_mod,
        "classify_intent",
        lambda q, embed, *, top_k, threshold: [
            IntentMatch(role="routing_surface", similarity=0.7, description="d"),
        ],
    )
    monkeypatch.setattr(
        _retr_mod,
        "scan_workspace_rows",
        lambda ws, **k: WorkspaceScan(rows=[], vectors=None),
    )
    monkeypatch.setattr(
        _retr_mod,
        "find_symbols_by_roles",
        lambda ws, roles, **k: {
            r: [_cand("a", "/x/a.py"), _cand("b", "/x/b.py"), _cand("c", "/x/c.py")] for r in roles
        },
    )
    monkeypatch.setattr(_retr_mod, "find_seeds_by_vector", lambda *a, **k: [])
    # Intent-axis ranking is identity here — covered by its own unit tests.
    monkeypatch.setattr(_rank_mod, "apply_intent_axis_boost", lambda raw, roles, **_k: dict(raw))

    def _fake_build(candidates, **kwargs):
        return [
            ContextBundle(
                role="routing_surface",
                seed=ContextSymbol(
                    uid=c.uid,
                    name=c.name,
                    file_path=c.file_path,
                    role=c.role,
                    distance_from_seed=0,
                    expansion_step=None,
                    code="x",
                ),
                related=(),
            )
            for c in candidates
        ]

    monkeypatch.setattr(_ctx_mod, "build_context_for_candidates", _fake_build)


def _run(**overrides):
    question = overrides.pop("question", "how does routing work")
    workspace_id = overrides.pop("workspace_id", "ws")
    db = overrides.pop("db", object())
    lance = overrides.pop("lance", _FakeLance())
    config = axis_pipeline.AxisRetrievalConfig(**overrides)
    return axis_pipeline.run_axis_retrieval(
        question,
        workspace_id=workspace_id,
        db=db,
        lance=lance,
        config=config,
    )


def test_result_layers_are_populated(stub_stages):
    result = _run()

    assert [m.role for m in result.intent] == ["routing_surface"]
    # role pool survives; the graph passes add empty pseudo-role keys.
    assert result.raw_by_role["routing_surface"]
    assert result.seed_files == ["/x/a.py", "/x/b.py", "/x/c.py"]
    # The default soft cap is seven, so this three-item pool is unchanged.
    assert [c.uid for c in result.candidates_for_context] == ["a", "b", "c"]
    assert [b.seed.uid for b in result.bundles] == ["a", "b", "c"]


def test_degraded_graph_walks_surface_stage_warnings(stub_stages):
    trace = RequestTrace(trace_id="trace-axis", endpoint="/ask/axis", workspace_id="ws")

    result = _run(trace=trace)

    warning_codes = {warning["code"] for warning in result.stage_warnings}
    assert "graph_walk_cypher_failed" in warning_codes
    assert trace.stage_warnings == result.stage_warnings


def test_with_context_false_skips_bundles(stub_stages):
    result = _run(with_context=False)

    assert [c.uid for c in result.candidates_for_context] == ["a", "b", "c"]
    assert result.bundles == []


def test_pregraph_lexical_span_probe_annotates_selected_pool(stub_stages, monkeypatch):
    trace = axis_pipeline.context_builder.LexicalSpanProbeTrace(
        candidate_count=3,
        bounded_candidates=3,
        payload_count=3,
        matched_symbols=1,
        span_count=1,
        covered_lines=4,
    )

    def _probe(candidates, **_kwargs):
        assert {candidate.uid for candidate in candidates} == {"a", "b", "c"}
        return {
            "b": axis_pipeline.context_builder.LexicalSpanEvidence(
                spans=((10, 13),),
                score=0.75,
                matched_terms=("routing",),
            )
        }, trace

    monkeypatch.setattr(
        axis_pipeline.context_builder,
        "probe_candidate_lexical_spans",
        _probe,
    )

    result = _run(pregraph_lexical_span_probe=True)

    by_uid = {candidate.uid: candidate for candidate in result.candidates_for_context}
    assert by_uid["b"].retrieval_spans == ((10, 13),)
    assert by_uid["b"].retrieval_channels == ("lexical_span",)
    assert by_uid["b"].lexical_span_score == 0.75
    assert result.lexical_span_probe_trace == trace


def test_pregraph_probe_candidates_round_robin_roles_and_keep_exact_extra():
    exact = replace(_cand("exact", "/x/exact.py"), exact_symbol_match=True)
    raw = {
        "role_a": [_cand("a", "/x/a.py"), _cand("b", "/x/b.py"), exact],
        "role_b": [_cand("a", "/x/a.py"), _cand("c", "/x/c.py"), _cand("d", "/x/d.py")],
    }

    selected = axis_pipeline._bounded_lexical_span_probe_candidates(
        raw,
        ["role_a", "role_b"],
        per_role_limit=2,
        max_symbols=4,
    )

    assert [candidate.uid for candidate in selected] == ["a", "c", "b", "exact"]


def test_lexical_span_utility_weight_is_additive_and_opt_in():
    plain = _cand("plain", "/x/plain.py", score=0.50, query_similarity=0.1)
    lexical = replace(
        _cand("lexical", "/x/lexical.py", score=0.45, query_similarity=0.1),
        lexical_span_score=1.0,
    )
    intent = [IntentMatch(role="routing_surface", similarity=0.8, description="d")]

    baseline, *_ = axis_pipeline._prepare_budgeted_candidates(
        [plain, lexical],
        intent,
        intent_budget=True,
        base_token_budget=6000,
        render_mode_override=None,
        anchor_path=None,
        anchor_symbol=None,
    )
    boosted, *_ = axis_pipeline._prepare_budgeted_candidates(
        [plain, lexical],
        intent,
        intent_budget=True,
        base_token_budget=6000,
        render_mode_override=None,
        anchor_path=None,
        anchor_symbol=None,
        lexical_span_utility_weight=0.10,
    )

    assert [candidate.uid for candidate in baseline] == ["plain", "lexical"]
    assert [candidate.uid for candidate in boosted] == ["lexical", "plain"]


def test_context_seeds_per_role_caps_the_pool(stub_stages):
    result = _run(context_seeds_per_role=1)

    # Cap applies to the context feed (and thus the bundles) but not to the
    # full ``raw_by_role`` pool the candidate response is built from.
    assert [c.uid for c in result.candidates_for_context] == ["a"]
    assert [b.seed.uid for b in result.bundles] == ["a"]
    assert len(result.raw_by_role["routing_surface"]) == 3
    assert result.seed_selection_trace is not None
    assert result.seed_selection_trace.per_role_soft_cap == 1


def test_hybrid_flags_preserve_shipped_off_order_and_enable_pregraph_sources(
    stub_stages, monkeypatch
):
    import context_engine.axis.intent_classifier as _intent_mod
    import context_engine.axis.role_lookahead as _lookahead_mod
    import context_engine.axis.role_retrieval as _retr_mod

    monkeypatch.setattr(
        _intent_mod,
        "classify_intent",
        lambda *a, **k: [
            IntentMatch(role="routing_surface", similarity=0.7, description="d"),
            IntentMatch(role="binding_surface", similarity=0.6, description="d"),
        ],
    )
    monkeypatch.setattr(
        _retr_mod,
        "find_seeds_by_vector",
        lambda *a, **k: [_cand("vector", "/x/vector.py")],
    )
    seen_at_lookahead: list[tuple[bool, bool]] = []

    def _lookahead(_roles, candidates, **_kwargs):
        seen_at_lookahead.append(("vector_seed" in candidates, "hybrid_seed" in candidates))
        return dict(candidates)

    monkeypatch.setattr(_lookahead_mod, "expand_candidates_via_neighbourhood", _lookahead)

    _run(
        with_context=False,
        lexical_retrieval=False,
        semantic_chunk_retrieval=False,
    )
    _run(with_context=False, lexical_retrieval=True, semantic_chunk_retrieval=False)

    assert seen_at_lookahead == [(False, False), (True, True)]


def test_vector_seed_connectivity_uses_symbol_index_seek_cypher():
    captured: dict[str, object] = {}

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def run(self, query, **params):
            captured["query"] = query
            captured["params"] = params
            return [{"uid": "vector", "c": 2}]

    class Driver:
        def session(self):
            return Session()

    class Db:
        driver = Driver()

    result = axis_pipeline._vector_seed_connectivity(  # noqa: SLF001
        Db(), ["vector"], ["structural"]
    )

    assert result == {"vector": 2}
    assert "MATCH (v:Symbol {uid: vu})" in str(captured["query"])
    assert captured["params"] == {"V": ["vector"], "O": ["structural"]}


def test_vector_seed_connectivity_gate_is_on_by_default_with_env_off_arm(stub_stages, monkeypatch):
    import context_engine.axis.role_retrieval as _retr_mod

    monkeypatch.setattr(
        _retr_mod,
        "find_seeds_by_vector",
        lambda *a, **k: [_cand("vector", "/x/vector.py")],
    )
    calls: list[int] = []
    monkeypatch.setattr(
        axis_pipeline,
        "_apply_vector_seed_connectivity_gate",
        lambda raw, *, db, min_conn: calls.append(min_conn),
    )

    monkeypatch.delenv("AXIS_VSEED_CONN_MIN", raising=False)
    _run(with_context=False)
    monkeypatch.setenv("AXIS_VSEED_CONN_MIN", "0")
    _run(with_context=False)

    assert calls == [1]


def test_anchor_symbol_pins_named_candidate_to_context_front(stub_stages):
    # A named symbol (without anchor_only) is a pinned SEED HINT: it moves to the
    # front but the full ranked pool still renders, so recall is not collapsed.
    result = _run(anchor_symbol="c")

    assert [c.uid for c in result.candidates_for_context][0] == "c"
    assert [b.seed.uid for b in result.bundles] == ["c", "a", "b"]


def test_anchor_symbol_unresolved_surfaces_stage_warning(stub_stages):
    # None of the pool candidates, the (empty) workspace scan, or the bare
    # `db` can resolve "does_not_exist" -> pinning silently falls through to
    # the unanchored pool. That fallthrough must not be silent to the caller.
    result = _run(anchor_symbol="does_not_exist")

    assert [c.uid for c in result.candidates_for_context] == ["a", "b", "c"]
    warning_codes = {warning["code"] for warning in result.stage_warnings}
    assert "anchor_symbol_unresolved" in warning_codes
    warning = next(w for w in result.stage_warnings if w["code"] == "anchor_symbol_unresolved")
    assert warning["details"]["anchor_symbol"] == "does_not_exist"


def test_anchor_only_expands_only_pinned_seed(stub_stages):
    # anchor_only opts into the CodeLens fast context: render just the anchor.
    result = _run(anchor_symbol="b", anchor_only=True)

    assert len(result.bundles) == 1
    assert result.bundles[0].seed.uid == "b"
    assert len(result.candidates_for_context) == 3


def test_anchor_symbol_uses_architecture_budget(stub_stages, monkeypatch):
    import context_engine.axis.context_builder as _ctx_mod

    captured: dict = {}

    def _capture(candidates, **kw):
        budget = kw.get("render_budget")
        captured["render_mode"] = budget.render_mode if budget else "full"
        captured["token_budget"] = budget.token_budget if budget else None
        return []

    monkeypatch.setattr(_ctx_mod, "build_context_for_candidates", _capture)

    _run(anchor_symbol="c", base_token_budget=4000)

    assert captured["render_mode"] == "hybrid"
    assert captured["token_budget"] == 8000


def test_anchor_only_fast_path_classifies_question_but_skips_pool_retrieval(
    stub_stages, monkeypatch
):
    import context_engine.axis.intent_classifier as _intent_mod

    intent_calls: list[str] = []

    def _classify(question, embed_fn, **kwargs):
        del embed_fn, kwargs
        intent_calls.append(question)
        return [IntentMatch(role="routing_surface", similarity=0.7, description="d")]

    monkeypatch.setattr(_intent_mod, "classify_intent", _classify)

    class _PinDB:
        def get_symbol_uid_by_name_in_file(self, name, path, workspace_id=""):
            del name, workspace_id
            return "u:walk"

        def get_file_path_for_symbol(self, uid, workspace_id=""):
            del uid, workspace_id
            return "/repo/context_engine/axis/graph_walk.py"

    result = axis_pipeline.run_axis_retrieval(
        "impact of walk_neighbours",
        workspace_id="ws",
        db=_PinDB(),
        lance=_FakeLance(),
        config=axis_pipeline.AxisRetrievalConfig(
            anchor_symbol="walk_neighbours",
            anchor_path="/repo/context_engine/axis/graph_walk.py",
            anchor_only=True,
        ),
    )

    assert intent_calls == ["impact of walk_neighbours"]
    assert [match.role for match in result.intent] == ["routing_surface"]
    assert len(result.bundles) == 1
    assert result.bundles[0].seed.uid == "u:walk"


def test_anchor_symbol_impact_uses_directional_impact_candidates(stub_stages, monkeypatch):
    import context_engine.axis.context_builder as _ctx_mod
    import context_engine.axis.impact_traversal as _impact_mod
    import context_engine.axis.intent_classifier as _intent_mod

    monkeypatch.setattr(
        _intent_mod,
        "classify_intent",
        lambda *_a, **_k: [IntentMatch(role="impact_analysis", similarity=0.9, description="d")],
    )

    caller = RoleCandidate(
        uid="u:caller",
        name="impact",
        file_path="/repo/context_engine/api/routes/impact.py",
        role="impact_analysis",
        satisfying_contracts=(),
        satisfying_kinds=("reverse_calls",),
        contract_count=0,
        kind_count=1,
        vector_distance=None,
        score=0.35,
        depth=1,
        edge_type="CALLS_*",
        utility_score=0.9,
    )
    impact_kwargs: dict = {}

    def _impact(candidates, **kwargs):
        impact_kwargs.update(kwargs)
        assert candidates[0].uid == "u:target"
        return [caller]

    monkeypatch.setattr(_impact_mod, "expand_impact_neighbourhood", _impact)

    captured: dict = {}

    def _build(candidates, **kwargs):
        candidates = list(candidates)
        captured["candidates"] = candidates
        captured.update(kwargs)
        bundles = [
            ContextBundle(
                role=candidate.role,
                seed=ContextSymbol(
                    uid=candidate.uid,
                    name=candidate.name,
                    file_path=candidate.file_path,
                    role=candidate.role,
                    distance_from_seed=candidate.depth or 0,
                    expansion_step=None,
                    code="x",
                ),
            )
            for candidate in candidates
        ]
        return list(reversed(bundles))

    monkeypatch.setattr(_ctx_mod, "build_context_for_candidates", _build)

    class _PinDB:
        def get_symbol_uid_by_name_in_file(self, *_a, **_k):
            return "u:target"

        def get_file_path_for_symbol(self, *_a, **_k):
            return "/repo/context_engine/api/routes/impact.py"

    result = axis_pipeline.run_axis_retrieval(
        "What should I check before changing _resolve_committed_uid?",
        workspace_id="ws",
        db=_PinDB(),
        lance=_FakeLance(),
        config=axis_pipeline.AxisRetrievalConfig(
            anchor_symbol="_resolve_committed_uid",
            anchor_path="/repo/context_engine/api/routes/impact.py",
            anchor_only=True,  # exercise the symbol-targeted fast path explicitly
        ),
    )

    assert [match.role for match in result.intent] == ["impact_analysis"]
    assert [candidate.uid for candidate in result.candidates_for_context] == [
        "u:target",
        "u:caller",
    ]
    assert [bundle.seed.uid for bundle in result.bundles] == ["u:target", "u:caller"]
    assert captured["traversal_mode"] is None
    assert captured["include_tests"] is True
    assert impact_kwargs["include_tests"] is True


def test_anchor_symbol_prefers_file_path_when_disambiguating(stub_stages, monkeypatch):
    import context_engine.axis.role_retrieval as _retr_mod

    def _dup(name: str, path: str) -> RoleCandidate:
        return RoleCandidate(
            uid=f"{name}:{path}",
            name=name,
            file_path=path,
            role="routing_surface",
            satisfying_contracts=(),
            satisfying_kinds=(),
            contract_count=0,
            kind_count=0,
            vector_distance=0.5,
            score=0.8,
        )

    monkeypatch.setattr(
        _retr_mod,
        "find_symbols_by_roles",
        lambda ws, roles, **k: {
            r: [
                _dup("Neighbour", "/repo/stale/run_demo.py"),
                _dup("Neighbour", "/repo/context_engine/axis/graph_walk.py"),
            ]
            for r in roles
        },
    )

    result = axis_pipeline.run_axis_retrieval(
        "trace call chain",
        workspace_id="ws",
        db=object(),
        lance=_FakeLance(),
        config=axis_pipeline.AxisRetrievalConfig(
            with_context=False,
            anchor_symbol="Neighbour",
            anchor_path="/repo/context_engine/axis/graph_walk.py",
        ),
    )

    assert result.candidates_for_context[0].file_path == "/repo/context_engine/axis/graph_walk.py"


def test_runs_without_a_tracer(stub_stages):
    # trace=None must select the null tracer, not raise.
    result = _run(trace=None)
    assert result.bundles


def test_intent_budget_can_be_disabled_for_ab(stub_stages, monkeypatch):
    import context_engine.axis.context_builder as _ctx_mod

    captured: dict = {}
    monkeypatch.setattr(
        _ctx_mod,
        "build_context_for_candidates",
        lambda candidates, **kw: (
            captured.update(
                token_budget=_intent_budget_token_limit(kw.get("render_budget")),
                render_mode=_intent_budget_render_mode(kw.get("render_budget")),
            )
            or []
        ),
    )
    result = _run(intent_budget=False)
    assert captured["token_budget"] is None
    assert captured["render_mode"] == "full"
    assert result.render_mode == "full"


def test_intent_budget_defaults_to_architecture_profile(stub_stages, monkeypatch):
    import context_engine.axis.context_builder as _ctx_mod

    captured: dict = {}

    def _capture(candidates, **kw):
        captured["n_seeds"] = len(list(candidates))
        budget = kw.get("render_budget")
        captured["token_budget"] = budget.token_budget if budget else None
        captured["render_mode"] = budget.render_mode if budget else "full"
        return []

    monkeypatch.setattr(_ctx_mod, "build_context_for_candidates", _capture)

    # stub intent is a plain role (routing_surface) -> architecture profile:
    # generous max_seeds (pool of 3 unaffected), hybrid render, token_budget = 4000*2.
    result = _run(base_token_budget=4000)
    assert captured["render_mode"] == "hybrid"
    assert captured["token_budget"] == 8000
    assert captured["n_seeds"] == 3
    assert result.render_mode == "hybrid"


@pytest.mark.parametrize(
    ("base_token_budget", "expected"),
    [(4_000, 0.0), (5_000, 0.05), (6_000, 0.05)],
)
def test_role_consensus_boost_is_gated_by_effective_profile_budget(
    base_token_budget,
    expected,
):
    config = axis_pipeline.AxisRetrievalConfig(
        base_token_budget=base_token_budget,
        role_consensus_score_boost=0.05,
        role_consensus_min_effective_tokens=10_000,
    )
    intent = [IntentMatch(role="routing_surface", similarity=0.7, description="d")]

    assert axis_pipeline._gated_role_consensus_score_boost(config, intent) == expected


def test_role_consensus_defaults_to_validated_boost():
    config = axis_pipeline.AxisRetrievalConfig(base_token_budget=5_000)
    intent = [IntentMatch(role="routing_surface", similarity=0.7, description="d")]

    assert config.role_consensus_score_boost == 0.05
    assert axis_pipeline._gated_role_consensus_score_boost(config, intent) == 0.05


def test_regular_retrieval_defaults_to_validated_all_positive_envelope():
    config = axis_pipeline.AxisRetrievalConfig()

    assert config.rank_decay_body_allocation is True
    assert config.token_credit_upgrade_min_utility_per_token == 0.00025
    assert config.pregraph_lexical_span_probe is True
    assert config.lexical_span_utility_weight == 0.15
    assert config.evidence_graph_fanout is True
    assert config.channel_consensus_score_boost == 0.0
    assert config.exact_symbol_score_boost == 0.0
    assert config.span_line_rerank is False


def test_role_consensus_boost_is_off_without_intent_budget():
    config = axis_pipeline.AxisRetrievalConfig(
        intent_budget=False,
        role_consensus_score_boost=0.05,
        role_consensus_min_effective_tokens=0,
    )
    intent = [IntentMatch(role="routing_surface", similarity=0.7, description="d")]

    assert axis_pipeline._gated_role_consensus_score_boost(config, intent) == 0.0


def test_role_consensus_boost_uses_impact_and_anchor_profiles():
    impact_intent = [IntentMatch(role="impact_analysis", similarity=0.7, description="d")]
    impact = axis_pipeline.AxisRetrievalConfig(
        base_token_budget=6_000,
        role_consensus_score_boost=0.05,
    )
    anchored = replace(impact, base_token_budget=5_000, anchor_symbol="target")

    assert axis_pipeline._gated_role_consensus_score_boost(impact, impact_intent) == 0.0
    assert axis_pipeline._gated_role_consensus_score_boost(anchored, impact_intent) == 0.05


@pytest.mark.parametrize(
    ("base_token_budget", "expected"),
    [(4_000, (0.0, 0.0)), (5_000, (0.04, 0.08))],
)
def test_channel_score_boosts_use_the_effective_budget_gate(
    base_token_budget,
    expected,
):
    config = axis_pipeline.AxisRetrievalConfig(
        base_token_budget=base_token_budget,
        channel_consensus_score_boost=0.04,
        exact_symbol_score_boost=0.08,
    )
    intent = [IntentMatch(role="routing_surface", similarity=0.7, description="d")]

    assert axis_pipeline._gated_channel_score_boosts(config, intent) == expected


def test_span_line_rerank_threads_batch_scorer_and_budget_knobs(stub_stages, monkeypatch):
    import context_engine.axis.context_builder as _ctx_mod

    class SpanLance:
        def _embed(self, texts):
            return [[1.0, 0.0] for _text in texts]

    captured: dict = {}

    def _capture(_candidates, **kwargs):
        budget = kwargs["render_budget"]
        captured["enabled"] = budget.span_line_rerank
        captured["max_symbols"] = budget.span_rank_max_symbols
        captured["max_candidates"] = budget.span_rank_max_candidates_per_symbol
        captured["max_body_lines"] = budget.span_rank_max_body_lines
        captured["query"] = kwargs["span_query_text"]
        captured["scores"] = kwargs["span_score_fn"](["alpha", "beta"])
        return []

    monkeypatch.setattr(_ctx_mod, "build_context_for_candidates", _capture)

    _run(
        lance=SpanLance(),
        question="where is alpha handled",
        span_line_rerank=True,
        span_rank_max_symbols=7,
        span_rank_max_candidates_per_symbol=11,
        span_rank_max_body_lines=5,
    )

    assert captured == {
        "enabled": True,
        "max_symbols": 7,
        "max_candidates": 11,
        "max_body_lines": 5,
        "query": "where is alpha handled",
        "scores": [1.0, 1.0],
    }


def test_intent_budget_walks_full_scope_no_passive_split(stub_stages, monkeypatch):
    import context_engine.axis.context_builder as _ctx_mod

    captured: dict = {}

    def _capture(active, *, passive=(), **kw):
        captured["active"] = [c.uid for c in active]
        captured["passive"] = [c.uid for c in passive]
        return []

    monkeypatch.setattr(_ctx_mod, "build_context_for_candidates", _capture)

    # The Token Credit System IS the budget: the whole ranked pool is active
    # (no walk cap, no active/passive split) and the packer trims downstream.
    result = _run()

    assert captured["active"] == ["a", "b", "c"]
    assert captured["passive"] == []
    assert [c.uid for c in result.candidates_for_context] == ["a", "b", "c"]


def test_intent_budget_threads_proximity_utility_to_context_builder(stub_stages, monkeypatch):
    import context_engine.axis.context_builder as _ctx_mod

    captured: dict = {}

    def _capture(active, *, utility_score_fn=None, **kw):
        captured["utility_score_fn"] = utility_score_fn
        return []

    monkeypatch.setattr(_ctx_mod, "build_context_for_candidates", _capture)

    _run(anchor_path="/x/open.py")

    utility_score_fn = captured["utility_score_fn"]
    assert utility_score_fn is not None
    assert utility_score_fn(_cand("near", "/x/near.py", score=0.5)) == pytest.approx(0.65)
    assert utility_score_fn(_cand("far", "/elsewhere/far.py", score=0.5)) == pytest.approx(0.5)
    assert utility_score_fn(
        _cand(
            "semantic",
            "/elsewhere/semantic.py",
            score=0.25,
            utility_score=0.9,
            query_similarity=0.7,
        )
    ) == pytest.approx(0.25)


def test_seed_files_use_doc_anchor_bridge_not_doc_anchor_owners(stub_stages, monkeypatch):
    import context_engine.axis.doc_anchor_bridge as _bridge_mod
    import context_engine.axis.role_retrieval as _retr_mod

    monkeypatch.setattr(
        _retr_mod,
        "find_seeds_by_doc_anchor",
        lambda *a, **k: [
            RoleCandidate(
                uid="iface-uid",
                name="CanActivate",
                file_path="/x/can-activate.interface.ts",
                role="doc_anchor",
                satisfying_contracts=(),
                satisfying_kinds=(),
                contract_count=0,
                kind_count=0,
                vector_distance=0.1,
                score=0.9,
            )
        ],
    )
    monkeypatch.setattr(
        _bridge_mod,
        "expand_doc_anchor_bridge",
        lambda seeds, **k: [
            RoleCandidate(
                uid="consumer-uid",
                name="GuardsConsumer",
                file_path="/x/guards-consumer.ts",
                role="doc_anchor_bridge",
                satisfying_contracts=(),
                satisfying_kinds=("reverse_uses_type",),
                contract_count=0,
                kind_count=1,
                vector_distance=None,
                score=0.35,
            )
        ],
    )

    result = _run()

    assert "/x/can-activate.interface.ts" not in result.seed_files
    assert "/x/guards-consumer.ts" in result.seed_files
    assert result.raw_by_role["doc_anchor_bridge"][0].uid == "consumer-uid"


def test_seed_files_use_http_endpoint_bridge_callers(stub_stages, monkeypatch):
    import context_engine.axis.http_endpoint_bridge as _http_bridge_mod
    import context_engine.axis.role_retrieval as _retr_mod

    monkeypatch.setattr(
        _retr_mod,
        "find_seeds_by_vector",
        lambda *a, **k: [
            RoleCandidate(
                uid="handler-uid",
                name="ask",
                file_path="/x/context_engine/main.py",
                role="vector_seed",
                satisfying_contracts=(),
                satisfying_kinds=(),
                contract_count=0,
                kind_count=0,
                vector_distance=0.1,
                score=0.9,
            )
        ],
    )
    monkeypatch.setattr(
        _http_bridge_mod,
        "expand_http_endpoint_bridge",
        lambda seeds, **k: [
            RoleCandidate(
                uid="provider-uid",
                name="handleAsk",
                file_path="/x/extension/src/providers/SurgicalContextViewProvider.ts",
                role="http_endpoint_bridge",
                satisfying_contracts=(),
                satisfying_kinds=("http_client_caller",),
                contract_count=0,
                kind_count=1,
                vector_distance=None,
                score=0.36,
            )
        ],
    )

    result = axis_pipeline.run_axis_retrieval(
        "How does the VS Code extension send an ask request?",
        workspace_id="ws",
        db=object(),
        lance=_FakeLance(),
    )

    assert "/x/extension/src/providers/SurgicalContextViewProvider.ts" in result.seed_files
    assert result.raw_by_role["http_endpoint_bridge"][0].uid == "provider-uid"
