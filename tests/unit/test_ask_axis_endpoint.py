"""Integration test for the ``/ask/axis`` endpoint.

Calls the handler function directly so the test does not depend on the
HTTP layer (TestClient + the installed httpx version drop ``app=``).
The wiring under test is the endpoint function itself: that intent /
retrieval / context modules are reached with the right args, the
response object is well-formed, and the no-LLM contract holds.
"""

from __future__ import annotations

import pytest

from context_engine import main as context_engine_main
from context_engine.axis.context_builder import ContextBundle, ContextSymbol
from context_engine.axis.intent_classifier import IntentMatch
from context_engine.axis.role_retrieval import RoleCandidate


@pytest.fixture
def patch_axis_pipeline(monkeypatch):
    """Stub the three axis modules ``/ask/axis`` calls into."""

    def fake_classify(question, embed_fn, *, top_k, threshold):
        return [
            IntentMatch(
                role="routing_surface",
                similarity=0.7,
                description="routing description",
            ),
        ]

    # The endpoint runs the pipeline (``run_axis_retrieval``), which reaches
    # these stage functions through their source modules — so patch the
    # source modules, not ``context_engine_main``.
    import context_engine.axis.axis_ranking as _rank_mod
    import context_engine.axis.context_builder as _ctx_mod
    import context_engine.axis.intent_classifier as _intent_mod
    import context_engine.axis.role_retrieval as _retr_mod
    import context_engine.database.lancedb_client as _lance_mod

    monkeypatch.setattr(_intent_mod, "classify_intent", fake_classify)
    # The pipeline always applies intent-axis ranking; neutralise it so the
    # candidate scores assert against their pre-boost values (ranking has
    # its own unit coverage).
    monkeypatch.setattr(_rank_mod, "apply_intent_axis_boost", lambda raw, roles, **_k: dict(raw))

    candidate = RoleCandidate(
        uid="u:app",
        name="app",
        file_path="/repo/app.py",
        role="routing_surface",
        satisfying_contracts=("route_register_binding",),
        satisfying_kinds=("web_route_register",),
        contract_count=1,
        kind_count=1,
        vector_distance=0.5,
        score=0.8,
    )

    monkeypatch.setattr(
        _retr_mod,
        "scan_workspace_rows",
        lambda *a, **k: _retr_mod.WorkspaceScan(rows=[], vectors=None),
    )
    monkeypatch.setattr(
        _retr_mod,
        "find_symbols_by_roles",
        lambda ws, roles, **k: {r: [candidate] for r in roles},
    )
    monkeypatch.setattr(_retr_mod, "find_seeds_by_vector", lambda *a, **k: [])

    bundle = ContextBundle(
        role="routing_surface",
        seed=ContextSymbol(
            uid="u:app",
            name="app",
            file_path="/repo/app.py",
            role="routing_surface",
            distance_from_seed=0,
            expansion_step=None,
            code="app = FastAPI()",
        ),
        related=(
            ContextSymbol(
                uid="u:handler",
                name="handler",
                file_path="/repo/app.py",
                role="routing_surface",
                distance_from_seed=1,
                expansion_step="binding_structure_expansion",
                code="def handler(): ...",
            ),
        ),
    )

    monkeypatch.setattr(
        _ctx_mod,
        "build_context_for_candidates",
        lambda candidates, **kwargs: [bundle] if list(candidates) else [],
    )

    class _FakeLance:
        def _embed(self, texts):
            return [[0.0] * 4]

    monkeypatch.setattr(_lance_mod, "LanceDBClient", lambda **_: _FakeLance())

    class _NoopCtx:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(context_engine_main.route_services, "db_session", lambda **_: _NoopCtx())
    monkeypatch.setattr(
        context_engine_main.route_services, "_resolve_request_user", lambda *a, **k: "test-user"
    )
    monkeypatch.setattr(
        context_engine_main.route_services,
        "_resolve_workspace",
        lambda *a, **k: "test-workspace",
    )
    return candidate, bundle


def _request(**overrides) -> context_engine_main.AskAxisRequest:
    defaults = {
        "question": "how does routing work",
        "top_roles": 2,
        "per_role_limit": 3,
        "with_context": True,
        "context_seeds_per_role": 1,
        "context_per_seed": 4,
    }
    defaults.update(overrides)
    return context_engine_main.AskAxisRequest(**defaults)


def test_ask_axis_request_default_keeps_context_pool_uncapped() -> None:
    req = context_engine_main.AskAxisRequest(question="how does routing work")
    assert req.context_seeds_per_role is None
    assert req.intent_budget is True
    assert req.token_budget == 6000


def test_ask_axis_returns_well_formed_payload(patch_axis_pipeline):
    candidate, _bundle = patch_axis_pipeline
    resp = context_engine_main.ask_axis(_request())

    assert resp.question == "how does routing work"
    assert resp.workspace_id == "test-workspace"
    assert resp.user == "test-user"
    assert isinstance(resp.stage_warnings, list)
    assert len(resp.intent_matches) == 1
    assert resp.intent_matches[0].role == "routing_surface"
    assert list(resp.candidates_by_role) == ["routing_surface"]

    candidates_payload = resp.candidates_by_role["routing_surface"]
    assert len(candidates_payload) == 1
    assert candidates_payload[0].uid == candidate.uid
    assert candidates_payload[0].score == candidate.score
    assert candidates_payload[0].satisfying_contracts == ["route_register_binding"]

    assert len(resp.context_bundles) == 1
    bundle = resp.context_bundles[0]
    assert bundle.role == "routing_surface"
    assert bundle.seed.code == "app = FastAPI()"
    assert bundle.related[0].code == "def handler(): ..."
    assert bundle.related[0].distance_from_seed == 1


def test_ask_axis_skips_context_when_with_context_false(patch_axis_pipeline):
    resp = context_engine_main.ask_axis(_request(with_context=False))
    assert resp.candidates_by_role["routing_surface"]
    assert resp.context_bundles == []


def test_ask_axis_empty_intent_returns_empty_payload(monkeypatch, patch_axis_pipeline):
    """``classify_intent`` returning nothing must produce a well-shaped
    empty response — not a 500.
    """
    import context_engine.axis.intent_classifier as _intent_mod

    monkeypatch.setattr(_intent_mod, "classify_intent", lambda *a, **k: [])

    resp = context_engine_main.ask_axis(_request(intent_threshold=0.99))

    assert resp.intent_matches == []
    assert resp.candidates_by_role == {}
    assert resp.context_bundles == []
