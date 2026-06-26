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

import pytest

from context_engine.axis import pipeline as axis_pipeline
from context_engine.axis.context_builder import ContextBundle, ContextSymbol
from context_engine.axis.intent_classifier import IntentMatch
from context_engine.axis.role_retrieval import RoleCandidate, WorkspaceScan


def _cand(uid: str, path: str, *, score: float = 0.8) -> RoleCandidate:
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
    )


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
    kwargs = dict(
        question="how does routing work",
        workspace_id="ws",
        db=object(),  # walk_neighbours degrades to [] against a bare object
        lance=_FakeLance(),
    )
    kwargs.update(overrides)
    return axis_pipeline.run_axis_retrieval(**kwargs)


def test_result_layers_are_populated(stub_stages):
    result = _run()

    assert [m.role for m in result.intent] == ["routing_surface"]
    # role pool survives; the graph passes add empty pseudo-role keys.
    assert result.raw_by_role["routing_surface"]
    assert result.seed_files == ["/x/a.py", "/x/b.py", "/x/c.py"]
    # No per-role cap -> the whole role pool feeds context.
    assert [c.uid for c in result.candidates_for_context] == ["a", "b", "c"]
    assert [b.seed.uid for b in result.bundles] == ["a", "b", "c"]


def test_with_context_false_skips_bundles(stub_stages):
    result = _run(with_context=False)

    assert [c.uid for c in result.candidates_for_context] == ["a", "b", "c"]
    assert result.bundles == []


def test_context_seeds_per_role_caps_the_pool(stub_stages):
    result = _run(context_seeds_per_role=1)

    # Cap applies to the context feed (and thus the bundles) but not to the
    # full ``raw_by_role`` pool the candidate response is built from.
    assert [c.uid for c in result.candidates_for_context] == ["a"]
    assert [b.seed.uid for b in result.bundles] == ["a"]
    assert len(result.raw_by_role["routing_surface"]) == 3


def test_anchor_symbol_pins_named_candidate_to_context_front(stub_stages):
    result = _run(anchor_symbol="c")

    assert [c.uid for c in result.candidates_for_context][0] == "c"
    assert [b.seed.uid for b in result.bundles] == ["c"]


def test_anchor_symbol_expands_only_pinned_seed(stub_stages):
    result = _run(anchor_symbol="b")

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


def test_anchor_symbol_fast_path_classifies_question_but_skips_pool_retrieval(
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
        anchor_symbol="walk_neighbours",
        anchor_path="/repo/context_engine/axis/graph_walk.py",
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
        anchor_symbol="_resolve_committed_uid",
        anchor_path="/repo/context_engine/api/routes/impact.py",
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
        with_context=False,
        anchor_symbol="Neighbour",
        anchor_path="/repo/context_engine/axis/graph_walk.py",
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
                token_budget=(budget.token_budget if (budget := kw.get("render_budget")) else None),
                render_mode=(budget.render_mode if budget else "full"),
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
