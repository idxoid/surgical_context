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

from sidecar.axis import pipeline as axis_pipeline
from sidecar.axis.context_builder import ContextBundle, ContextSymbol
from sidecar.axis.intent_classifier import IntentMatch
from sidecar.axis.role_retrieval import RoleCandidate, WorkspaceScan


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
    import sidecar.axis.axis_ranking as _rank_mod
    import sidecar.axis.context_builder as _ctx_mod
    import sidecar.axis.intent_classifier as _intent_mod
    import sidecar.axis.role_retrieval as _retr_mod

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
        lambda ws: WorkspaceScan(rows=[], vectors=None),
    )
    monkeypatch.setattr(
        _retr_mod,
        "find_symbols_by_roles",
        lambda ws, roles, **k: {
            r: [_cand("a", "/x/a.py"), _cand("b", "/x/b.py"), _cand("c", "/x/c.py")]
            for r in roles
        },
    )
    monkeypatch.setattr(_retr_mod, "find_seeds_by_vector", lambda *a, **k: [])
    # Intent-axis ranking is identity here — covered by its own unit tests.
    monkeypatch.setattr(
        _rank_mod, "apply_intent_axis_boost", lambda raw, roles, **_k: dict(raw)
    )

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


def test_runs_without_a_tracer(stub_stages):
    # trace=None must select the null tracer, not raise.
    result = _run(trace=None)
    assert result.bundles


def test_intent_budget_off_leaves_build_context_unbudgeted(stub_stages, monkeypatch):
    import sidecar.axis.context_builder as _ctx_mod

    captured: dict = {}
    monkeypatch.setattr(
        _ctx_mod,
        "build_context_for_candidates",
        lambda candidates, **kw: captured.update(
            token_budget=kw.get("token_budget"), render_mode=kw.get("render_mode")
        )
        or [],
    )
    result = _run()  # intent_budget defaults False -> benchmark behaviour
    assert captured["token_budget"] is None
    assert captured["render_mode"] == "full"
    assert result.render_mode == "full"


def test_intent_budget_on_applies_architecture_profile(stub_stages, monkeypatch):
    import sidecar.axis.context_builder as _ctx_mod

    captured: dict = {}

    def _capture(candidates, **kw):
        captured["n_seeds"] = len(list(candidates))
        captured["token_budget"] = kw.get("token_budget")
        captured["render_mode"] = kw.get("render_mode")
        return []

    monkeypatch.setattr(_ctx_mod, "build_context_for_candidates", _capture)

    # stub intent is a plain role (routing_surface) -> architecture profile:
    # generous max_seeds (pool of 3 unaffected), hybrid render, token_budget = 4000*2.
    result = _run(intent_budget=True, base_token_budget=4000)
    assert captured["render_mode"] == "hybrid"
    assert captured["token_budget"] == 8000
    assert captured["n_seeds"] == 3
    assert result.render_mode == "hybrid"


def test_intent_budget_splits_active_and_passive(stub_stages, monkeypatch):
    import sidecar.axis.context_builder as _ctx_mod

    captured: dict = {}

    def _capture(active, *, passive=(), **kw):
        captured["active"] = [c.uid for c in active]
        captured["passive"] = [c.uid for c in passive]
        return []

    monkeypatch.setattr(_ctx_mod, "build_context_for_candidates", _capture)

    # pool of 3 equal-score seeds; walk cap 2 -> top-2 active, 1 passive.
    result = _run(intent_budget=True, max_walk_seeds_override=2)
    assert captured["active"] == ["a", "b"]
    assert captured["passive"] == ["c"]
    # the full pool is preserved (pool recall unaffected — the cap is only on
    # the walk, not the candidate set).
    assert [c.uid for c in result.candidates_for_context] == ["a", "b", "c"]
