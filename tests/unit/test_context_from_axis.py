"""Phase 1c: ``_context_from_axis`` — the axis retrieval provider.

Joins ``run_axis_retrieval`` to the Phase-1a ``axis_bundles_to_prompt_context``
adapter into one ``PromptContext | None`` provider, shaped like the existing
``_context_from_*`` seam. These tests pin the join (run the *real* adapter,
stub only the pipeline + Lance client) and the fall-through contract. The
adapter itself is also covered directly here since Phase 1a shipped it
test-free.
"""

from __future__ import annotations

import pytest

from context_engine import main as context_engine_main
from context_engine.axis.context_builder import ContextBundle, ContextSymbol
from context_engine.axis.intent_classifier import IntentMatch
from context_engine.axis.pipeline import AxisRetrievalResult
from context_engine.axis.prompt_provider import axis_bundles_to_prompt_context


def _sym(uid: str, name: str, *, depth: int, code: str) -> ContextSymbol:
    return ContextSymbol(
        uid=uid,
        name=name,
        file_path="/repo/app.py",
        role="routing_surface",
        distance_from_seed=depth,
        expansion_step=None if depth == 0 else "binding_structure_expansion",
        code=code,
    )


def _bundle() -> ContextBundle:
    return ContextBundle(
        role="routing_surface",
        seed=_sym("u:app", "app", depth=0, code="app = FastAPI()"),
        related=(_sym("u:handler", "handler", depth=1, code="def handler(): ..."),),
    )


def _result(bundles) -> AxisRetrievalResult:
    return AxisRetrievalResult(
        intent=[IntentMatch(role="routing_surface", similarity=0.7, description="d")],
        raw_by_role={"routing_surface": []},
        seed_files=["/repo/app.py"],
        candidates_for_context=[],
        bundles=bundles,
    )


def _patch_pipeline(monkeypatch, result):
    # ``_context_from_axis`` imports these at call time, so patch the source
    # modules (a bound import in main would not see the patch).
    import context_engine.axis.pipeline as _pipeline_mod
    import context_engine.database.lancedb_client as _lance_mod

    monkeypatch.setattr(_pipeline_mod, "run_axis_retrieval", lambda q, **k: result)
    monkeypatch.setattr(_lance_mod, "LanceDBClient", lambda **_: object())


def _axis_ids(base: str = "ws") -> dict[str, str]:
    return {"base_workspace_id": base, "index_workspace_id": f"{base}+axis_python_v1"}


def test_context_from_axis_builds_prompt_context(monkeypatch):
    _patch_pipeline(monkeypatch, _result([_bundle()]))

    ctx = context_engine_main._context_from_axis(
        "how does routing work",
        db=object(),
        trace_id="t1",
        **_axis_ids(),
    )

    assert ctx is not None
    assert ctx.mode == "surgical_full"
    assert ctx.intent == "routing_surface"
    assert ctx.workspace_id == "ws"
    assert ctx.trace_id == "t1"
    assert ctx.primary_source.symbol == "app"
    assert ctx.primary_source.file_path == "/repo/app.py"
    assert ctx.primary_source.code == "app = FastAPI()"
    assert any(s.symbol == "handler" for s in ctx.graph_context)
    assert ctx.intent_distribution == {"routing_surface": 0.7}
    assert ctx.intent_confidence == pytest.approx(0.7)
    assert ctx.intent_effective_mode == "architecture"
    assert ctx.intent_resolution["source"] == "axis_classifier"
    assert ctx.tier_tokens["code"] > 0
    assert ctx.tier_tokens["cross_refs"] > 0


def test_context_from_axis_returns_none_when_no_bundles(monkeypatch):
    # Empty pipeline -> adapter returns None -> provider falls through.
    _patch_pipeline(monkeypatch, _result([]))

    ctx = context_engine_main._context_from_axis(
        "how does routing work",
        db=object(),
        **_axis_ids(),
    )
    assert ctx is None


def test_context_from_axis_empty_intent_passes_blank(monkeypatch):
    result = _result([_bundle()])
    result.intent = []  # no classified intent
    _patch_pipeline(monkeypatch, result)

    ctx = context_engine_main._context_from_axis(
        "q",
        db=object(),
        **_axis_ids(),
    )
    assert ctx is not None
    assert ctx.intent == ""


def test_context_from_axis_queries_index_workspace(monkeypatch):
    seen: dict[str, str] = {}

    def fake_run(_question, **kwargs):
        seen["workspace_id"] = kwargs["workspace_id"]
        return _result([_bundle()])

    import context_engine.axis.pipeline as _pipeline_mod
    import context_engine.database.lancedb_client as _lance_mod

    monkeypatch.setattr(_pipeline_mod, "run_axis_retrieval", fake_run)
    monkeypatch.setattr(_lance_mod, "LanceDBClient", lambda **_: object())

    context_engine_main._context_from_axis(
        "q",
        base_workspace_id="local/repo@main",
        index_workspace_id="local/repo@main+axis_python_v1",
        db=object(),
    )
    assert seen["workspace_id"] == "local/repo@main+axis_python_v1"


# --- the Phase-1a adapter, directly (shipped test-free) -------------------


def test_adapter_promotes_top_seed_and_dedupes():
    ctx = axis_bundles_to_prompt_context(
        [_bundle(), _bundle()],  # duplicate uids across bundles
        question="q",
        workspace_id="ws",
    )
    assert ctx is not None
    assert ctx.primary_source.uid == "u:app"
    # u:app is primary; u:handler appears once; the second bundle's dup seed
    # and related are deduped by uid.
    uids = [s.uid for s in ctx.graph_context]
    assert uids.count("u:handler") == 1
    assert "u:app" not in uids


def test_adapter_returns_none_on_empty():
    assert axis_bundles_to_prompt_context([], question="q", workspace_id="ws") is None


def test_adapter_uses_expansion_step_not_anchor_role_for_related():
    anchor_bundle = ContextBundle(
        role="anchor_symbol",
        seed=ContextSymbol(
            uid="u:walk",
            name="walk_neighbours",
            file_path="/repo/graph_walk.py",
            role="anchor_symbol",
            distance_from_seed=0,
            expansion_step=None,
            code="def walk_neighbours(...): ...",
        ),
        related=(
            ContextSymbol(
                uid="u:safe_hops",
                name="_safe_max_hops",
                file_path="/repo/graph_walk.py",
                role="control_call_expansion",
                distance_from_seed=1,
                expansion_step="control_call_expansion",
                code="def _safe_max_hops(...): ...",
            ),
        ),
    )
    ctx = axis_bundles_to_prompt_context([anchor_bundle], question="q", workspace_id="ws")
    assert ctx is not None
    assert ctx.primary_source.symbol == "walk_neighbours"
    assert len(ctx.graph_context) == 1
    assert ctx.graph_context[0].symbol == "_safe_max_hops"
    assert ctx.graph_context[0].relation == "control_call_expansion"


def test_adapter_preserves_impact_relation_direction_depth_and_score():
    anchor = ContextBundle(
        role="impact_analysis",
        seed=ContextSymbol(
            uid="u:target",
            name="target",
            file_path="/repo/app.py",
            role="impact_analysis",
            distance_from_seed=0,
            expansion_step=None,
            code="def target(): ...",
            kind="target_seed",
            relevance_score=1.0,
            utility_score=1.0,
        ),
        utility_score=1.0,
    )
    caller = ContextBundle(
        role="impact_analysis",
        seed=ContextSymbol(
            uid="u:caller",
            name="caller",
            file_path="/repo/caller.py",
            role="impact_analysis",
            distance_from_seed=1,
            expansion_step=None,
            code="def caller(): target()",
            kind="reverse_calls",
            direction="caller",
            edge_type="CALLS_*",
            relevance_score=0.35,
            utility_score=0.9,
        ),
        utility_score=0.9,
    )

    ctx = axis_bundles_to_prompt_context([anchor, caller], question="impact", workspace_id="ws")

    assert ctx is not None
    [dep] = ctx.graph_context
    assert dep.relation == "reverse_calls"
    assert dep.direction == "caller"
    assert dep.edge_type == "CALLS_*"
    assert dep.depth == 1
    assert dep.relevance_score == pytest.approx(0.35)
    assert dep.blended_score == pytest.approx(0.9)
    serialized = ctx.to_dict()["graph_context"][0]
    assert serialized["edge_type"] == "CALLS_*"
    assert serialized["provenance"] == ["reverse_calls", "CALLS_*"]
