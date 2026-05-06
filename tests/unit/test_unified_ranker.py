from unittest.mock import MagicMock, patch

from sidecar.context.intent_classifier import Intent
from sidecar.context.mechanism_registry import determine_preloaded_mechanism
from sidecar.context.types import SubgraphNode
from sidecar.context.unified_ranker import (
    _NOISE_FACTOR,
    _TRACE_DEPENDS_RUNTIME_NAMES,
    Candidate,
    UnifiedRanker,
    VectorSearcher,
    compute_impact_noise_factor,
    compute_noise_factor,
)


def _make_db(*, allowed_paths=None, allowed_uids=None):
    session = MagicMock()

    def run(query, **params):
        if "RETURN f.path AS path" in query:
            return [{"path": path} for path in (allowed_paths or [])]
        if "RETURN DISTINCT s.uid AS uid" in query:
            return [{"uid": uid} for uid in (allowed_uids or [])]
        return []

    session.run.side_effect = run
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    driver.session.return_value.__exit__.return_value = None
    db = MagicMock()
    db.driver = driver
    return db


def _make_target_db(rows):
    session = MagicMock()

    def run(query, **params):
        if "RETURN s.uid AS uid" in query and "outgoing_edges" in query:
            return rows
        return []

    session.run.side_effect = run
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    driver.session.return_value.__exit__.return_value = None
    db = MagicMock()
    db.driver = driver
    return db


def _make_backfill_db(rows):
    session = MagicMock()

    def run(query, **params):
        if "WHERE s.name IN $names" in query and "ROLE_BACKFILL" not in query:
            return rows
        return []

    session.run.side_effect = run
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    driver.session.return_value.__exit__.return_value = None
    db = MagicMock()
    db.driver = driver
    return db


def _make_recovery_db(*, same_file_rows=None, imported_rows=None):
    session = MagicMock()

    def run(query, **params):
        if (
            "MATCH (f:File {workspace_id: $workspace_id, path: $file_path})-[c:CONTAINS]->(s:Symbol)"
            in query
        ):
            return same_file_rows or []
        if (
            "MATCH (f:File {workspace_id: $workspace_id, path: $file_path})-[:IMPORTS]->(dep:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)"
            in query
        ):
            return imported_rows or []
        return []

    session.run.side_effect = run
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    driver.session.return_value.__exit__.return_value = None
    db = MagicMock()
    db.driver = driver
    return db


class _FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)

    def single(self):
        return self.rows[0] if self.rows else None


class _FakeVector:
    def __init__(self, docs=None, symbols=None):
        self._docs = docs or []
        self._symbols = symbols or []

    def search(self, query, limit):
        return self._docs[:limit]

    def search_symbols(self, query, limit=30, threshold=1.0):
        return self._symbols[:limit]


def test_doc_candidates_filter_to_workspace_files():
    db = _make_db(allowed_paths=["/repo/docs/allowed.md"])
    vector = VectorSearcher(
        _FakeVector(
            docs=[
                {"id": "a", "file_path": "/repo/docs/allowed.md", "chunk": "allowed", "score": 0.9},
                {"id": "b", "file_path": "/other/docs/nope.md", "chunk": "nope", "score": 0.8},
            ]
        )
    )
    ranker = UnifiedRanker(db, vector, workspace_id="local/repo@main")

    candidates = ranker._doc_candidates("dependency injection", limit=10)

    assert [candidate.file_path for candidate in candidates] == ["/repo/docs/allowed.md"]


def test_symbol_candidates_filter_to_workspace_uids():
    db = _make_db(allowed_uids=["in-workspace"])
    vector = VectorSearcher(
        _FakeVector(
            symbols=[
                {
                    "uid": "in-workspace",
                    "name": "solve_dependencies",
                    "file_path": "/repo/a.py",
                    "score": 0.9,
                },
                {
                    "uid": "other-workspace",
                    "name": "solve_dependencies",
                    "file_path": "/other/a.py",
                    "score": 0.8,
                },
            ]
        )
    )
    ranker = UnifiedRanker(db, vector, workspace_id="local/repo@main")

    candidates = ranker._sym_vec_candidates("dependency injection", limit=10)

    assert [candidate.uid for candidate in candidates] == ["in-workspace"]


def test_duplicate_target_selection_prefers_behavioral_entrypoint():
    db = _make_target_db(
        [
            {
                "uid": "depends-class",
                "name": "Depends",
                "kind": "class",
                "qualified_name": "fastapi.params.Depends",
                "token_estimate": 24,
                "file_path": "/repo/fastapi/params.py",
                "file_hash": "a",
                "range": [746, 749],
                "outgoing_edges": 0,
                "incoming_edges": 0,
                "total_edges": 0,
            },
            {
                "uid": "depends-fn",
                "name": "Depends",
                "kind": "function",
                "qualified_name": "fastapi.param_functions.Depends",
                "token_estimate": 120,
                "file_path": "/repo/fastapi/param_functions.py",
                "file_hash": "b",
                "range": [2283, 2340],
                "outgoing_edges": 3,
                "incoming_edges": 2,
                "total_edges": 5,
            },
        ]
    )
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/fastapi@main")

    target, metadata = ranker.get_target(
        "Depends",
        query="How does dependency injection get resolved before the endpoint function is called?",
        intent=Intent.EXPLORATION,
        with_metadata=True,
    )

    assert target is not None
    assert target.uid == "depends-fn"
    assert target.file_path.endswith("param_functions.py")
    assert metadata["strategy"] == "duplicate_resolution"
    assert metadata["ambiguous"] is True
    assert metadata["selected_uid"] == "depends-fn"


def test_duplicate_target_selection_prefers_main_pydantic_entrypoint_for_model_dump():
    db = _make_target_db(
        [
            {
                "uid": "root-model-dump",
                "name": "model_dump",
                "kind": "function",
                "qualified_name": "pydantic.root_model.RootModel.model_dump",
                "token_estimate": 224,
                "file_path": "/repo/pydantic/root_model.py",
                "file_hash": "a",
                "range": [120, 180],
                "outgoing_edges": 1,
                "incoming_edges": 2,
                "total_edges": 3,
            },
            {
                "uid": "base-model-dump",
                "name": "model_dump",
                "kind": "function",
                "qualified_name": "pydantic.main.BaseModel.model_dump",
                "token_estimate": 520,
                "file_path": "/repo/pydantic/main.py",
                "file_hash": "b",
                "range": [420, 620],
                "outgoing_edges": 0,
                "incoming_edges": 0,
                "total_edges": 0,
            },
        ]
    )
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/pydantic@main")

    target, metadata = ranker.get_target(
        "model_dump",
        query="How does model_dump() get from high-level API call to actual serialization logic?",
        intent=Intent.EXPLORATION,
        with_metadata=True,
    )

    assert target is not None
    assert target.uid == "base-model-dump"
    assert target.file_path.endswith("main.py")
    assert metadata["strategy"] == "duplicate_resolution"
    assert metadata["selected_uid"] == "base-model-dump"


def test_pydantic_basemodel_uses_generic_mechanism_when_dispatch_stubbed():
    ranker = UnifiedRanker(
        _make_db(), VectorSearcher(_FakeVector()), workspace_id="local/pydantic@main"
    )
    target = SubgraphNode(
        uid="basemodel",
        name="BaseModel",
        file_path="/repo/pydantic/main.py",
        range=[100, 400],
        token_estimate=200,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="class",
    )

    assert (
        ranker._determine_mechanism(
            target,
            query="How does BaseModel validation flow work in v2 from model construction to validated output?",
        )
        == "generic"
    )
    assert (
        ranker._determine_mechanism(
            target,
            query="Which parts of Pydantic are pure Python wrappers and which parts rely on pydantic-core?",
        )
        == "generic"
    )


def test_module_target_fallback_resolves_package_without_symbol():
    session = MagicMock()
    module_path = "/repo/pydantic/v1/__init__.py"

    def run(query, **params):
        if "RETURN s.uid AS uid" in query:
            return _FakeResult([])
        if "RETURN f.path AS path" in query:
            return _FakeResult([{"path": module_path, "file_hash": "abc"}])
        return _FakeResult([])

    session.run.side_effect = run
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    driver.session.return_value.__exit__.return_value = None
    db = MagicMock()
    db.driver = driver

    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/pydantic@main")

    target, metadata = ranker.get_target(
        "v1",
        query="How is backward compatibility with Pydantic v1 exposed in the codebase?",
        intent=Intent.EXPLORATION,
        with_metadata=True,
    )

    assert target is not None
    assert target.name == "v1"
    assert target.kind == "module"
    assert target.file_path == module_path
    assert metadata["strategy"] == "module_fallback"


def test_capability_roles_add_generic_impact_runtime_and_test_surfaces():
    ranker = UnifiedRanker(
        _make_db(), VectorSearcher(_FakeVector()), workspace_id="local/pydantic@main"
    )

    runtime_symbol = Candidate(
        kind="symbol",
        uid="field",
        name="Field",
        file_path="/repo/pydantic/fields.py",
        token_cost=80,
    )
    test_symbol = Candidate(
        kind="symbol",
        uid="parent",
        name="Parent",
        file_path="/repo/tests/test_aliases.py",
        token_cost=60,
    )

    assert "impact_runtime" in ranker._roles_of(runtime_symbol)
    assert "impact_test_surface" in ranker._roles_of(test_symbol)


def test_role_backfill_reads_specs_from_catalog_overlay():
    from sidecar.context.mechanism_registry import ROLE_CATALOG_MECHANISM_BACKFILL_KEY

    db = _make_backfill_db(
        [
            {
                "uid": "validator-handle",
                "name": "__pydantic_validator__",
                "symbol_kind": "attribute",
                "token_estimate": 24,
                "qualified_name": "pydantic.main.BaseModel.__pydantic_validator__",
                "file_path": "/repo/pydantic/main.py",
                "file_hash": "a",
                "range": [200, 204],
                "inbound_edges": 3,
                "outbound_edges": 1,
            },
            {
                "uid": "schema-validator",
                "name": "SchemaValidator",
                "symbol_kind": "class",
                "token_estimate": 120,
                "qualified_name": "pydantic_core.SchemaValidator",
                "file_path": "/repo/pydantic-core/python/pydantic_core/_pydantic_core.pyi",
                "file_hash": "b",
                "range": [67, 160],
                "inbound_edges": 8,
                "outbound_edges": 5,
            },
            {
                "uid": "schema-serializer",
                "name": "SchemaSerializer",
                "symbol_kind": "class",
                "token_estimate": 120,
                "qualified_name": "pydantic_core.SchemaSerializer",
                "file_path": "/repo/pydantic-core/python/pydantic_core/_pydantic_core.pyi",
                "file_hash": "c",
                "range": [295, 420],
                "inbound_edges": 7,
                "outbound_edges": 4,
            },
        ]
    )
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/pydantic@main")
    ranker.role_catalog = {
        ROLE_CATALOG_MECHANISM_BACKFILL_KEY: {
            "pydantic_python_core_boundary": {
                "validator_handle": [
                    {
                        "name": "__pydantic_validator__",
                        "path_hint": "/repo/pydantic/main.py",
                        "priority": 1.0,
                    },
                ],
                "core_runtime": [
                    {
                        "name": "SchemaValidator",
                        "path_hint": "/repo/pydantic-core/python/pydantic_core/_pydantic_core.pyi",
                        "priority": 0.95,
                    },
                ],
                "serializer_handle": [
                    {
                        "name": "SchemaSerializer",
                        "path_hint": "/repo/pydantic-core/python/pydantic_core/_pydantic_core.pyi",
                        "priority": 1.0,
                    },
                ],
            },
        },
    }

    backfill = ranker._role_backfill_candidates(
        "pydantic_python_core_boundary",
        ["validator_handle", "serializer_handle", "core_runtime"],
        excluded_uids=set(),
    )

    by_uid = {candidate.uid: candidate for candidate in backfill}
    assert by_uid["validator-handle"].evidence_role == "validator_handle"
    assert by_uid["schema-validator"].evidence_role == "core_runtime"
    assert by_uid["schema-serializer"].evidence_role == "serializer_handle"


def test_redux_style_symbols_use_generic_mechanism_when_dispatch_stubbed():
    ranker = UnifiedRanker(
        _make_db(), VectorSearcher(_FakeVector()), workspace_id="local/redux@main"
    )

    create_slice = SubgraphNode(
        uid="createSlice",
        name="createSlice",
        file_path="/repo/packages/toolkit/src/createSlice.ts",
        range=[854, 854],
        token_estimate=20,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="variable",
    )
    configure_store = SubgraphNode(
        uid="configureStore",
        name="configureStore",
        file_path="/repo/packages/toolkit/src/configureStore.ts",
        range=[121, 180],
        token_estimate=80,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="function",
    )
    create_async_thunk = SubgraphNode(
        uid="createAsyncThunk",
        name="createAsyncThunk",
        file_path="/repo/packages/toolkit/src/createAsyncThunk.ts",
        range=[520, 620],
        token_estimate=120,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="variable",
    )
    create_api = SubgraphNode(
        uid="createApi",
        name="createApi",
        file_path="/repo/packages/toolkit/src/query/core/index.ts",
        range=[4, 4],
        token_estimate=20,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="variable",
    )
    create_listener_middleware = SubgraphNode(
        uid="createListenerMiddleware",
        name="createListenerMiddleware",
        file_path="/repo/packages/toolkit/src/listenerMiddleware/index.ts",
        range=[330, 562],
        token_estimate=240,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="variable",
    )

    assert (
        ranker._determine_mechanism(
            create_slice,
            query="How does createSlice turn reducer definitions into action creators and the final reducer?",
        )
        == "generic"
    )
    assert (
        ranker._determine_mechanism(
            configure_store,
            query="How does configureStore assemble middleware, enhancers, and DevTools behavior?",
        )
        == "generic"
    )
    assert (
        ranker._determine_mechanism(
            create_async_thunk,
            query="How does createAsyncThunk generate pending / fulfilled / rejected action flow?",
        )
        == "generic"
    )
    assert (
        ranker._determine_mechanism(
            create_api,
            query="How does RTK Query define an API slice and connect generated endpoints into the store?",
        )
        == "generic"
    )
    assert (
        ranker._determine_mechanism(
            create_listener_middleware,
            query="How does listener middleware intercept actions and trigger side effects?",
        )
        == "generic"
    )
    assert (
        ranker._determine_mechanism(
            configure_store,
            query="In this monorepo, which packages are core runtime behavior and which are docs/examples/supporting surfaces?",
        )
        == "generic"
    )


def test_auto_strategy_profile_supplies_mechanism_and_roles_for_unknown_repo():
    db = _make_db()
    db.get_repository_profile.return_value = {
        "strategy_profile": {
            "selected_strategy": "middleware_pipeline_trace",
            "role_plan": ["api_surface", "composition_surface", "runtime_surface"],
            "mechanism_archetypes": [
                {
                    "type": "middleware_pipeline",
                    "strategy": "middleware_pipeline_trace",
                    "confidence": 0.78,
                    "role_plan": [
                        "api_surface",
                        "factory_surface",
                        "composition_surface",
                        "runtime_surface",
                    ],
                    "evidence": ["express", "Router(", "app.use"],
                }
            ],
        }
    }
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/express@main")
    target = SubgraphNode(
        uid="router",
        name="Router",
        file_path="/repo/lib/router/index.js",
        range=[1, 20],
        token_estimate=80,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="function",
    )

    mechanism = ranker._determine_mechanism(
        target,
        query="How does Express middleware execution call next handlers?",
    )

    assert mechanism == "auto:middleware_pipeline"
    assert ranker._get_required_roles(mechanism) == [
        "api_surface",
        "factory_surface",
        "composition_surface",
        "runtime_surface",
        "docs_or_concept",
    ]


def test_topic_focus_downranks_unrelated_redux_query_chain_candidates():
    ranker = UnifiedRanker(
        _make_db(), VectorSearcher(_FakeVector()), workspace_id="local/redux@main"
    )
    target = SubgraphNode(
        uid="configureStore",
        name="configureStore",
        file_path="/repo/packages/toolkit/src/configureStore.ts",
        range=[121, 180],
        token_estimate=80,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="function",
    )

    off_topic_query_candidate = Candidate(
        kind="symbol",
        uid="selectQueryEntry",
        name="selectQueryEntry",
        file_path="/repo/packages/toolkit/src/query/core/buildSelectors.ts",
        token_cost=80,
        depth=4,
    )
    off_topic_role_candidate = Candidate(
        kind="symbol",
        uid="hasPendingRequests",
        name="hasPendingRequests",
        file_path="/repo/packages/toolkit/src/query/core/buildMiddleware/invalidationByTags.ts",
        token_cost=80,
        depth=3,
        evidence_role="composition_surface",
    )
    focused_devtools_candidate = Candidate(
        kind="symbol",
        uid="composeWithDevTools",
        name="composeWithDevTools",
        file_path="/repo/packages/toolkit/src/devtoolsExtension.ts",
        token_cost=80,
        depth=2,
    )

    query = "How does configureStore assemble middleware, enhancers, and DevTools behavior?"
    required = ["api_surface", "composition_surface", "config_surface", "docs_or_concept"]
    assert (
        ranker._topic_focus_factor(
            off_topic_query_candidate,
            target,
            query=query,
            mechanism="runtime_configuration_pipeline",
            intent=Intent.EXPLORATION,
            required_roles=required,
        )
        < 1.0
    )
    assert (
        ranker._topic_focus_factor(
            off_topic_role_candidate,
            target,
            query=query,
            mechanism="runtime_configuration_pipeline",
            intent=Intent.EXPLORATION,
            required_roles=required,
        )
        < 1.0
    )
    off_topic_role_candidate.noise_factor = 0.15
    assert "composition_surface" not in ranker._selection_roles(
        off_topic_role_candidate,
        target,
        query=query,
        mechanism="runtime_configuration_pipeline",
        intent=Intent.EXPLORATION,
        required_roles=required,
    )
    assert (
        ranker._topic_focus_factor(
            focused_devtools_candidate,
            target,
            query=query,
            mechanism="runtime_configuration_pipeline",
            intent=Intent.EXPLORATION,
            required_roles=required,
        )
        == 1.0
    )


def test_rank_records_pruned_reasons_and_score_breakdown():
    ranker = UnifiedRanker(
        _make_db(), VectorSearcher(_FakeVector()), workspace_id="local/redux@main"
    )
    target = SubgraphNode(
        uid="configureStore",
        name="configureStore",
        file_path="/repo/packages/toolkit/src/configureStore.ts",
        range=[121, 180],
        token_estimate=80,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="function",
    )

    huge_role_filler = Candidate(
        kind="symbol",
        uid="getDefaultMiddleware",
        name="getDefaultMiddleware",
        file_path="/repo/packages/toolkit/src/getDefaultMiddleware.ts",
        token_cost=500,
        graph_score=0.9,
        semantic_score=0.8,
        relation="ROLE_BACKFILL",
        evidence_role="composition_surface",
        provenance=["role-backfill:composition_surface"],
    )
    noisy_test = Candidate(
        kind="symbol",
        uid="test_configure_store_noise",
        name="test_unrelated_listener_case",
        file_path="/repo/packages/toolkit/src/tests/configureStore.test.ts",
        token_cost=80,
        graph_score=0.7,
        semantic_score=0.7,
        relation="CALLS_DIRECT",
        provenance=["graph:CALLS_DIRECT"],
    )

    ranker._graph_candidates = lambda *a, **kw: [huge_role_filler, noisy_test]
    ranker._doc_candidates = lambda *a, **kw: []
    ranker._sym_vec_candidates = lambda *a, **kw: []
    ranker._doc_bridge_candidates = lambda *a, **kw: []

    _, budget_info, _, pruned, _ = ranker.rank(
        target,
        "configureStore assemble middleware and enhancers",
        Intent.EXPLORATION,
        budget=300,
    )

    by_uid = {item["uid"]: item for item in pruned}
    assert budget_info["pruned"] == len(pruned)
    assert by_uid["getDefaultMiddleware"]["reason"] == "over_budget"
    assert by_uid["test_configure_store_noise"]["reason"] == "noise_penalty"
    assert (
        by_uid["getDefaultMiddleware"]["scores"]["blended_score"]
        == by_uid["getDefaultMiddleware"]["blended_score"]
    )
    assert "noise_factor" in by_uid["test_configure_store_noise"]["scores"]


def test_low_signal_virtual_and_fixture_paths_are_noisy():
    assert (
        compute_noise_factor(
            "/repo/docs/virtual/matchers/index.ts",
            "requestThunk1",
            kind="symbol",
        )
        < 1.0
    )
    assert (
        compute_noise_factor(
            "/repo/packages/rtk-codemods/transforms/createSliceBuilder/__testfixtures__/basic.ts",
            "incrementAsync",
            kind="symbol",
        )
        < 1.0
    )


def test_editorial_docs_get_downranked_relative_to_mechanism_docs():
    assert (
        compute_noise_factor(
            "/repo/docs/usage/migrating-rtk-2.md",
            "migrating",
            kind="doc",
        )
        < 1.0
    )
    assert (
        compute_noise_factor(
            "/repo/docs/rtk-query/overview.md",
            "overview",
            kind="doc",
        )
        == 1.0
    )


def test_tests_get_softer_penalty_for_exploration_intent():
    from sidecar.context.intent_classifier import Intent

    # default (no intent) — hard penalty
    assert compute_noise_factor("/repo/tests/test_foo.py", "test_bar") == _NOISE_FACTOR

    # EXPLORATION — softer penalty so tests can supplement explain_behavior context
    assert (
        compute_noise_factor("/repo/tests/test_foo.py", "test_bar", intent=Intent.EXPLORATION)
        == 0.3
    )

    # IMPACT_ANALYSIS is handled at call-site (noise_factor=1.0 set directly), not here
    # but compute_noise_factor itself still applies the standard path for non-EXPLORATION
    assert (
        compute_noise_factor("/repo/tests/test_foo.py", "test_bar", intent=Intent.NAVIGATION)
        == _NOISE_FACTOR
    )

    # clean files unaffected regardless of intent
    assert compute_noise_factor("/repo/src/utils.py", "helper", intent=Intent.EXPLORATION) == 1.0


def test_impact_noise_keeps_only_topic_related_tests_unpenalized():
    query = (
        "If alias handling changes, what modules, docs, and tests are most likely to be affected?"
    )

    assert (
        compute_impact_noise_factor(
            "/repo/tests/test_aliases.py",
            "test_basic_alias",
            query=query,
            target_name="Field",
        )
        == 1.0
    )
    assert (
        compute_impact_noise_factor(
            "/repo/tests/test_computed_fields.py",
            "test_computed_field_alias",
            query=query,
            target_name="Field",
        )
        == 1.0
    )
    assert (
        compute_impact_noise_factor(
            "/repo/pydantic-core/tests/serializers/test_union.py",
            "test_union_serializer",
            query=query,
            target_name="Field",
        )
        == _NOISE_FACTOR
    )


def test_docs_deferred_until_code_breadth_met():
    """High-scoring docs should not crowd out low-scoring code candidates while
    coverage breadth (distinct code files) is below the deferral threshold.

    Regression: fastapi_q02 was burning ~2k tokens on tutorial docs while
    `Dependant`/`get_dependant` lost to the marginal-gain stop. The fix is to
    hold docs until ≥3 code files are seated, then replay them.
    """
    code_paths = [f"/repo/src/mod_{i}.py" for i in range(4)]
    code_uids = [f"sym-{i}" for i in range(4)]
    doc_paths = [f"/repo/docs/tutorial_{i}.md" for i in range(5)]
    db = _make_db(
        allowed_paths=code_paths + doc_paths + ["/repo/src/main.py"],
        allowed_uids=code_uids + ["primary"],
    )
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/test@main")

    target = SubgraphNode(
        uid="primary",
        name="primary_func",
        file_path="/repo/src/main.py",
        range=[1, 10],
        token_estimate=100,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="function",
    )

    # Build a pool: 5 high-scoring docs + 4 lower-scoring code symbols across
    # 4 distinct files. Without deferral, docs would be picked first by score
    # and consume budget before code breadth is established.
    # Code symbols with spread graph_scores (so min-max normalization keeps
    # them above zero) and a CALLS_DIRECT relation. Below docs but well above
    # the marginal-gain floor.
    code_candidates = [
        Candidate(
            uid=f"sym-{i}",
            name=f"helper_{i}",
            file_path=f"/repo/src/mod_{i}.py",
            kind="symbol",
            range=[1, 10],
            token_cost=80,
            graph_score=0.9 - 0.1 * i,
            semantic_score=0.7 - 0.05 * i,
            relation="CALLS_DIRECT",
            direction="out",
            depth=1,
            provenance=["graph"],
        )
        for i in range(4)
    ]
    # Docs with spread semantic scores so they would normally be picked first.
    doc_candidates = [
        Candidate(
            uid=f"doc-{i}",
            name=f"tutorial_{i}",
            file_path=f"/repo/docs/tutorial_{i}.md",
            kind="doc",
            range=[1, 10],
            token_cost=200,
            graph_score=0.0,
            semantic_score=0.99 - 0.01 * i,
            relation="DOC_BRIDGE",
            direction="related",
            depth=1,
            provenance=["doc"],
        )
        for i in range(5)
    ]

    ranker._graph_candidates = lambda *a, **kw: code_candidates
    ranker._doc_candidates = lambda *a, **kw: doc_candidates
    ranker._sym_vec_candidates = lambda *a, **kw: []
    ranker._doc_bridge_candidates = lambda *a, **kw: []

    chosen, _, _, _, _ = ranker.rank(
        target,
        "primary_func how does this work",
        Intent.EXPLORATION,
        budget=1500,
    )

    chosen_code_files = {c.file_path for c in chosen if c.kind != "doc"}
    chosen_doc_count = sum(1 for c in chosen if c.kind == "doc")

    # Code breadth is established before docs eat the budget.
    assert len(chosen_code_files) >= 3, (
        f"Expected ≥3 distinct code files seated before docs; got {chosen_code_files}"
    )
    # Docs may or may not fit after code, but they no longer dominate.
    assert chosen_doc_count <= len(chosen_code_files), (
        f"Docs ({chosen_doc_count}) outnumbered code files ({len(chosen_code_files)})"
    )


def test_trace_depends_runtime_symbol_rows_use_package_prefix():
    """Depends lives beside params without IMPORTS to dependencies/utils — seed by name."""
    solve_row = {
        "uid": "solve-u",
        "name": "solve_dependencies",
        "symbol_kind": "function",
        "token_estimate": 80,
        "qualified_name": "fastapi.dependencies.utils.solve_dependencies",
        "file_path": "fastapi/dependencies/utils.py",
        "file_hash": "",
        "range": [10, 20],
        "inbound_edges": 5,
        "outbound_edges": 2,
    }

    session = MagicMock()

    def run(query, **_kwargs):
        if "LIMIT 32" in query and "$pkg_prefix" in query:
            assert _kwargs.get("pkg_prefix") == "fastapi/"
            assert set(_kwargs["names"]) == set(_TRACE_DEPENDS_RUNTIME_NAMES)
            return [solve_row]
        return []

    session.run.side_effect = run
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    driver.session.return_value.__exit__.return_value = None
    db = MagicMock()
    db.driver = driver

    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="ws")
    target = SubgraphNode(
        uid="dep-u",
        name="Depends",
        file_path="/repo/fastapi/param_functions.py",
        range=[1, 5],
        token_estimate=40,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="function",
    )
    rows = ranker._trace_dependency_runtime_symbol_rows(target, excluded_uids=set())
    assert len(rows) == 1 and rows[0]["uid"] == "solve-u"


def test_trace_depends_runtime_symbol_rows_skipped_without_depends_name():
    session = MagicMock()
    session.run.side_effect = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("unexpected Neo4j query")
    )
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    driver.session.return_value.__exit__.return_value = None
    db = MagicMock()
    db.driver = driver
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="ws")
    target = SubgraphNode(
        uid="other-u",
        name="FastAPI",
        file_path="/repo/fastapi/applications.py",
        range=[1, 5],
        token_estimate=40,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="class",
    )
    assert ranker._trace_dependency_runtime_symbol_rows(target, excluded_uids=set()) == []


def test_structural_mechanism_dispatch_when_preloaded_rules_miss():
    """Pass 1 roles + catalog templates infer mechanism without name heuristics."""
    from sidecar.context.mechanism_registry import ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY

    db = _make_db()
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/test@main")
    ranker.role_catalog = {
        "schema_version": 2,
        ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY: {
            "fastapi_endpoint_execution": ["executor", "runtime_surface"],
        },
    }
    ranker._cluster_to_role = {0: "executor", 1: "runtime_surface"}
    ranker._derived_role_by_uid = {"target-u": 0, "n1": 1}
    target = SubgraphNode(
        uid="target-u",
        name="Router",
        file_path="/opaque/project/routing.py",
        range=[1, 10],
        token_estimate=100,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="function",
    )
    assert determine_preloaded_mechanism(target, "how does this work") == ""
    with patch.object(ranker, "_one_hop_connected_symbol_uids", return_value=["n1"]):
        assert ranker._determine_mechanism(target, "opaque query") == "fastapi_endpoint_execution"


def test_required_roles_drop_unavailable_role_catalog_entries():
    db = _make_db()
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/test@main")
    ranker.role_catalog = {
        "schema_version": 2,
        "mechanism_required_roles": {
            "fastapi_endpoint_execution": [
                "api_surface",
                "factory_surface",
                "representation_surface",
                "runtime_surface",
            ]
        },
    }
    ranker._cluster_to_role = {0: "api_surface", 1: "runtime_surface"}
    ranker._derived_role_by_uid = {"u1": 0, "u2": 1, "u3": 0}

    required = ranker._get_required_roles("fastapi_endpoint_execution")

    assert "api_surface" in required
    assert "runtime_surface" in required
    assert "factory_surface" not in required
    assert "representation_surface" not in required
    assert "docs_or_concept" in required


def test_required_roles_use_target_local_supply_when_available():
    db = _make_db()
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/test@main")
    ranker.role_catalog = {
        "schema_version": 2,
        "mechanism_required_roles": {
            "fastapi_endpoint_execution": [
                "api_surface",
                "factory_surface",
                "representation_surface",
                "runtime_surface",
            ]
        },
    }
    ranker._cluster_to_role = {
        0: "api_surface",
        1: "runtime_surface",
        2: "factory_surface",
    }
    ranker._derived_role_by_uid = {
        "target-u": 0,
        "n1": 1,
        "remote-factory": 2,  # Exists globally but not in target neighborhood.
    }
    target = SubgraphNode(
        uid="target-u",
        name="FastAPI",
        file_path="/repo/fastapi/applications.py",
        range=[1, 10],
        token_estimate=100,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="class",
    )

    with patch.object(ranker, "_one_hop_connected_symbol_uids", return_value=["n1"]):
        required = ranker._get_required_roles("fastapi_endpoint_execution", target=target)

    assert "api_surface" in required
    assert "runtime_surface" in required
    assert "factory_surface" not in required
    assert "representation_surface" not in required
    assert "docs_or_concept" in required


def test_adaptive_generic_roles_follow_workspace_role_supply():
    db = _make_db()
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/test@main")
    ranker._cluster_to_role = {
        0: "api_surface",
        1: "runtime_surface",
        2: "runtime_surface",
        3: "composition_surface",
    }
    ranker._derived_role_by_uid = {
        "a": 0,
        "b": 1,
        "c": 2,
        "d": 3,
        "e": 3,
    }

    required = ranker._get_required_roles("generic")

    # Top supplied role first; returned plan is data-driven, not hardcoded.
    assert required[0] == "runtime_surface"
    assert "composition_surface" in required
    assert "api_surface" in required
    assert "docs_or_concept" in required


def test_openapi_symbols_use_generic_mechanism_without_bundled_dispatch():
    """Bundled FastAPI mechanism rules are stubbed; symbols use generic routing."""
    db = _make_db()
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/test@main")

    for name, file_path in [
        ("get_openapi", "/repo/fastapi/openapi/utils.py"),
        ("openapi", "/repo/fastapi/applications.py"),
        ("get_openapi_path", "/repo/fastapi/openapi/utils.py"),
        ("get_fields_from_routes", "/repo/fastapi/openapi/utils.py"),
    ]:
        target = SubgraphNode(
            uid=f"u-{name}",
            name=name,
            file_path=file_path,
            range=[1, 10],
            token_estimate=100,
            relation="target",
            direction="primary",
            depth=0,
            relevance_score=1.0,
            kind="function",
        )
        assert (
            ranker._determine_mechanism(target, "How does FastAPI generate OpenAPI?") == "generic"
        )

    required = ranker._get_required_roles("generic")
    assert "supporting_surface" in required
    assert "docs_or_concept" in required


def test_fastapi_serialization_symbol_uses_generic_mechanism_when_dispatch_stubbed():
    ranker = UnifiedRanker(
        _make_db(), VectorSearcher(_FakeVector()), workspace_id="local/test@main"
    )
    target = SubgraphNode(
        uid="serialize-response",
        name="serialize_response",
        file_path="/repo/fastapi/routing.py",
        range=[1, 80],
        token_estimate=180,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="function",
    )

    assert (
        ranker._determine_mechanism(
            target,
            "If I change response model serialization behavior, what parts of the framework and tests break?",
        )
        == "generic"
    )

    required = ranker._get_required_roles("generic")
    assert "supporting_surface" in required
    assert "docs_or_concept" in required


def test_role_filler_outranks_unrelated_high_score_docs():
    """Regression for fastapi_q05: a candidate that's the unique source of a
    missing required role must (a) be sorted above unrelated high-scoring docs
    and (b) bypass the low-gain floor that would otherwise drop it. `openapi`
    in fastapi/applications.py is large (256 tokens) with weak graph signal,
    yielding negative blended score, but it's the unique api_surface for
    openapi-generation and must seat regardless.
    """
    db = _make_db(
        allowed_paths=["/repo/src/openapi.py", "/repo/src/main.py"]
        + [f"/repo/docs/d{i}.md" for i in range(5)],
        allowed_uids=["primary", "openapi-uid"],
    )
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/test@main")
    ranker.strategy_profile = {
        "role_plan": ["api_surface", "runtime_surface"],
        "mechanism_archetypes": [],
    }
    target = SubgraphNode(
        uid="primary",
        name="get_openapi",
        file_path="/repo/src/main.py",
        range=[1, 10],
        token_estimate=100,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="function",
    )

    # The lone api_surface candidate — large, weak blended score on its own.
    api_filler = Candidate(
        uid="openapi-uid",
        name="openapi",
        file_path="/repo/src/openapi.py",
        kind="symbol",
        range=[1, 10],
        token_cost=256,
        graph_score=0.3,
        semantic_score=0.2,
        relation="CALLS_DIRECT",
        direction="out",
        depth=1,
        provenance=["graph"],
        evidence_role="api_surface",
    )
    # Five unrelated high-score docs that don't fill api_surface.
    docs = [
        Candidate(
            uid=f"doc-{i}",
            name=f"d{i}",
            file_path=f"/repo/docs/d{i}.md",
            kind="doc",
            range=[1, 10],
            token_cost=200,
            graph_score=0.6,
            semantic_score=0.95,
            relation="DOC_BRIDGE",
            direction="related",
            depth=1,
            provenance=["doc"],
        )
        for i in range(5)
    ]

    ranker._graph_candidates = lambda *a, **kw: [api_filler]
    ranker._doc_candidates = lambda *a, **kw: docs
    ranker._sym_vec_candidates = lambda *a, **kw: []
    ranker._doc_bridge_candidates = lambda *a, **kw: []

    chosen, _, _, _, missing = ranker.rank(
        target,
        "get_openapi how does FastAPI generate OpenAPI for registered routes?",
        Intent.EXPLORATION,
        budget=1500,
    )

    # The api_surface filler must be selected (otherwise the role stays missing).
    chosen_names = {c.name for c in chosen}
    assert "openapi" in chosen_names, f"api_surface filler not chosen; got {chosen_names}"
    assert "api_surface" not in missing


def test_docs_not_deferred_for_impact_analysis():
    """IMPACT_ANALYSIS already weights docs heavily on purpose; the deferral
    pass must not interfere there."""
    doc_paths = [f"/repo/docs/spec_{i}.md" for i in range(3)]
    db = _make_db(allowed_paths=doc_paths + ["/repo/src/main.py"], allowed_uids=["primary"])
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/test@main")

    target = SubgraphNode(
        uid="primary",
        name="primary_func",
        file_path="/repo/src/main.py",
        range=[1, 10],
        token_estimate=100,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="function",
    )

    doc_candidates = [
        Candidate(
            uid=f"doc-{i}",
            name=f"spec_{i}",
            file_path=f"/repo/docs/spec_{i}.md",
            kind="doc",
            range=[1, 10],
            token_cost=200,
            graph_score=0.0,
            semantic_score=0.95,
            relation="DOC_BRIDGE",
            direction="related",
            depth=1,
            provenance=["doc"],
        )
        for i in range(3)
    ]

    ranker._graph_candidates = lambda *a, **kw: []
    ranker._doc_candidates = lambda *a, **kw: doc_candidates
    ranker._sym_vec_candidates = lambda *a, **kw: []
    ranker._doc_bridge_candidates = lambda *a, **kw: []

    chosen, _, _, _, _ = ranker.rank(
        target,
        "primary_func what breaks if I change this",
        Intent.IMPACT_ANALYSIS,
        budget=2000,
    )

    chosen_doc_count = sum(1 for c in chosen if c.kind == "doc")
    assert chosen_doc_count >= 1, "IMPACT_ANALYSIS should still admit docs without deferral"


def test_filesystem_import_recovery_resolves_relative_typescript_imports(tmp_path):
    repo = tmp_path / "repo" / "packages" / "toolkit" / "src"
    repo.mkdir(parents=True)
    create_slice = repo / "createSlice.ts"
    create_reducer = repo / "createReducer.ts"
    create_slice.write_text(
        "import { createReducer } from './createReducer'\n"
        "export const createSlice = buildCreateSlice()\n",
        encoding="utf-8",
    )
    create_reducer.write_text(
        "export function createReducer() {}\n",
        encoding="utf-8",
    )

    ranker = UnifiedRanker(
        _make_db(), VectorSearcher(_FakeVector()), workspace_id="local/redux@main"
    )

    resolved = ranker._resolve_filesystem_import_paths(str(create_slice))

    assert str(create_reducer.resolve()) in resolved


def test_recovery_candidate_adds_factory_surface_from_target_local_signal():
    ranker = UnifiedRanker(
        _make_db(), VectorSearcher(_FakeVector()), workspace_id="local/fastapi@main"
    )
    target = SubgraphNode(
        uid="target-fastapi",
        name="FastAPI",
        file_path="/repo/fastapi/applications.py",
        range=[1, 20],
        token_estimate=100,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="class",
    )
    row = {
        "uid": "add-api-route",
        "name": "add_api_route",
        "symbol_kind": "function",
        "token_estimate": 120,
        "qualified_name": "fastapi.applications.FastAPI.add_api_route",
        "file_path": "/repo/fastapi/applications.py",
        "file_hash": "h1",
        "range": [100, 180],
        "inbound_edges": 3,
        "outbound_edges": 6,
    }

    candidate = ranker._recovery_candidate_from_row(
        row,
        origin="same_file",
        scoped_roles={"factory_surface"},
        target=target,
    )

    assert candidate is not None
    assert candidate.evidence_role == "factory_surface"
    assert candidate.relation == "ROLE_BACKFILL"


def test_recovery_candidate_adds_representation_and_runtime_from_local_signal():
    ranker = UnifiedRanker(
        _make_db(), VectorSearcher(_FakeVector()), workspace_id="local/fastapi@main"
    )
    target = SubgraphNode(
        uid="target-fastapi",
        name="FastAPI",
        file_path="/repo/fastapi/routing.py",
        range=[1, 20],
        token_estimate=100,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="class",
    )
    row = {
        "uid": "serialize-response",
        "name": "serialize_response",
        "symbol_kind": "function",
        "token_estimate": 140,
        "qualified_name": "fastapi.routing.serialize_response",
        "file_path": "/repo/fastapi/routing.py",
        "file_hash": "h2",
        "range": [300, 410],
        "inbound_edges": 4,
        "outbound_edges": 3,
    }

    candidate = ranker._recovery_candidate_from_row(
        row,
        origin="same_file",
        scoped_roles={"representation_surface", "runtime_surface"},
        target=target,
    )

    assert candidate is not None
    roles = set([candidate.evidence_role, *candidate.supporting_roles])
    assert "representation_surface" in roles
    assert "runtime_surface" in roles or "impact_runtime" in roles


def test_target_concept_fallback_candidate_is_doc():
    ranker = UnifiedRanker(
        _make_db(), VectorSearcher(_FakeVector()), workspace_id="local/test@main"
    )
    target = SubgraphNode(
        uid="target-1",
        name="FastAPI",
        file_path="/repo/fastapi/applications.py",
        range=[1, 10],
        token_estimate=100,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="class",
    )

    candidate = ranker._target_concept_fallback_candidate(
        target,
        query="How does app startup wiring work?",
    )

    assert candidate is not None
    assert candidate.kind == "doc"
    assert candidate.token_cost <= 120
    assert "fallback:target-concept-note" in candidate.provenance


def test_trace_dependency_marginal_gain_rewards_new_file_coverage():
    ranker = UnifiedRanker(
        _make_db(), VectorSearcher(_FakeVector()), workspace_id="local/test@main"
    )
    target = SubgraphNode(
        uid="target",
        name="Depends",
        file_path="/repo/fastapi/params.py",
        range=[1, 20],
        token_estimate=100,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="class",
    )
    chosen = [
        Candidate(
            kind="symbol",
            uid="c0",
            name="helper0",
            file_path="/repo/fastapi/params.py",
            token_cost=80,
            graph_score=0.6,
            semantic_score=0.4,
            relation="CALLS_DIRECT",
            depth=1,
        )
    ]
    same_file = Candidate(
        kind="symbol",
        uid="same",
        name="dep_same",
        file_path="/repo/fastapi/params.py",
        token_cost=80,
        graph_score=0.6,
        semantic_score=0.4,
        relation="CALLS_DIRECT",
        depth=1,
    )
    new_file = Candidate(
        kind="symbol",
        uid="new",
        name="dep_new",
        file_path="/repo/fastapi/dependencies/utils.py",
        token_cost=80,
        graph_score=0.6,
        semantic_score=0.4,
        relation="DEPENDS_ON",
        depth=1,
    )

    gain_same = ranker._calculate_marginal_gain(
        same_file,
        chosen,
        target,
        mechanism="trace_dependency",
        required_roles=["api_surface"],
    )
    gain_new = ranker._calculate_marginal_gain(
        new_file,
        chosen,
        target,
        mechanism="trace_dependency",
        required_roles=["api_surface"],
    )

    assert gain_new > gain_same


def test_trace_dependency_gain_mode_from_di_question_even_when_mechanism_generic():
    ranker = UnifiedRanker(
        _make_db(), VectorSearcher(_FakeVector()), workspace_id="local/test@main"
    )
    target = SubgraphNode(
        uid="target",
        name="Depends",
        file_path="/repo/fastapi/param_functions.py",
        range=[1, 20],
        token_estimate=100,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="class",
    )
    chosen: list = []
    di_candidate = Candidate(
        kind="symbol",
        uid="solve_dependencies",
        name="solve_dependencies",
        file_path="/repo/fastapi/dependencies/utils.py",
        token_cost=80,
        graph_score=0.55,
        semantic_score=0.35,
        relation="CALLS_DIRECT",
        depth=2,
    )
    q_di = "How does dependency injection get resolved before the endpoint function is called?"
    gain_di_query = ranker._calculate_marginal_gain(
        di_candidate,
        chosen,
        target,
        mechanism="generic",
        query=q_di,
        required_roles=["orchestrator"],
    )
    gain_plain = ranker._calculate_marginal_gain(
        di_candidate,
        chosen,
        target,
        mechanism="generic",
        query="Where is the middleware registered?",
        required_roles=["orchestrator"],
    )
    assert gain_di_query > gain_plain


def test_resolve_intra_repo_package_import_paths(tmp_path):
    """Absolute ``from pkg.sub.mod import …`` maps to sibling package files on disk."""
    inner = tmp_path / "fastapi" / "fastapi"
    deps = inner / "dependencies"
    deps.mkdir(parents=True)
    utils_py = deps / "utils.py"
    utils_py.write_text("def solve_dependencies():\n    pass\n", encoding="utf-8")
    param = inner / "param_functions.py"
    param.write_text(
        "from fastapi.dependencies.utils import solve_dependencies\n",
        encoding="utf-8",
    )
    ranker = UnifiedRanker(_make_db(), VectorSearcher(_FakeVector()), workspace_id="local/x@main")
    paths = ranker._resolve_intra_repo_package_import_paths(str(param))
    assert str(utils_py.resolve()) in paths


def test_trace_dependency_import_anchor_candidates():
    """Universal import-module anchors: same rows as ``_imported_symbol_rows``, no FastAPI names in ranker."""
    utils_row = {
        "uid": "uid-solve",
        "name": "solve_dependencies",
        "symbol_kind": "function",
        "token_estimate": 400,
        "qualified_name": "solve_dependencies",
        "file_path": "/repos/fastapi/fastapi/dependencies/utils.py",
        "file_hash": "h1",
        "range": [1, 80],
        "inbound_edges": 3,
        "outbound_edges": 6,
    }
    models_row = {
        "uid": "uid-get-dep",
        "name": "get_dependant",
        "symbol_kind": "function",
        "token_estimate": 300,
        "qualified_name": "get_dependant",
        "file_path": "/repos/fastapi/fastapi/dependencies/models.py",
        "file_hash": "h2",
        "range": [1, 60],
        "inbound_edges": 8,
        "outbound_edges": 4,
    }

    ranker = UnifiedRanker(_make_db(), VectorSearcher(_FakeVector()), workspace_id="local/app@main")
    target = SubgraphNode(
        uid="depends-target",
        name="Depends",
        file_path="/repos/fastapi/fastapi/param_functions.py",
        range=[1, 40],
        token_estimate=50,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="class",
    )
    with patch.object(ranker, "_imported_symbol_rows", return_value=[utils_row, models_row]):
        anchors = ranker._trace_dependency_import_anchor_candidates(
            target,
            query="How does dependency injection get resolved?",
            mechanism="generic",
            required_roles=["dependency_solver"],
            excluded_uids=set(),
            pool=[],
        )
    paths = {a.file_path for a in anchors}
    assert any(p.endswith("dependencies/utils.py") for p in paths)
    assert any(p.endswith("dependencies/models.py") for p in paths)
    assert {a.name for a in anchors} >= {"solve_dependencies", "get_dependant"}
    assert any("recovery:import-module-trace" in (a.provenance or []) for a in anchors)
