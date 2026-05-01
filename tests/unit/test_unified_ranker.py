from unittest.mock import MagicMock

from sidecar.context.intent_classifier import Intent
from sidecar.context.types import SubgraphNode
from sidecar.context.unified_ranker import (
    Candidate,
    UnifiedRanker,
    VectorSearcher,
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
        if "MATCH (f:File {workspace_id: $workspace_id, path: $file_path})-[c:CONTAINS]->(s:Symbol)" in query:
            return same_file_rows or []
        if "MATCH (f:File {workspace_id: $workspace_id, path: $file_path})-[:IMPORTS]->(dep:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)" in query:
            return imported_rows or []
        return []

    session.run.side_effect = run
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    driver.session.return_value.__exit__.return_value = None
    db = MagicMock()
    db.driver = driver
    return db


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
                {"uid": "in-workspace", "name": "solve_dependencies", "file_path": "/repo/a.py", "score": 0.9},
                {"uid": "other-workspace", "name": "solve_dependencies", "file_path": "/other/a.py", "score": 0.8},
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
    assert metadata["alternatives"][0]["role"] == "api_surface"


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


def test_dependency_injection_role_backfill_supplies_missing_roles():
    db = _make_backfill_db(
        [
            {
                "uid": "depends-class",
                "name": "Depends",
                "symbol_kind": "class",
                "token_estimate": 20,
                "qualified_name": "fastapi.params.Depends",
                "file_path": "/repo/fastapi/params.py",
                "file_hash": "a",
                "range": [746, 749],
                "inbound_edges": 1,
                "outbound_edges": 0,
            },
            {
                "uid": "dependant",
                "name": "Dependant",
                "symbol_kind": "class",
                "token_estimate": 80,
                "qualified_name": "fastapi.dependencies.models.Dependant",
                "file_path": "/repo/fastapi/dependencies/models.py",
                "file_hash": "b",
                "range": [32, 101],
                "inbound_edges": 5,
                "outbound_edges": 3,
            },
            {
                "uid": "get-dependant",
                "name": "get_dependant",
                "symbol_kind": "function",
                "token_estimate": 120,
                "qualified_name": "fastapi.dependencies.utils.get_dependant",
                "file_path": "/repo/fastapi/dependencies/utils.py",
                "file_hash": "c",
                "range": [286, 360],
                "inbound_edges": 4,
                "outbound_edges": 5,
            },
            {
                "uid": "solve-dependencies",
                "name": "solve_dependencies",
                "symbol_kind": "function",
                "token_estimate": 220,
                "qualified_name": "fastapi.dependencies.utils.solve_dependencies",
                "file_path": "/repo/fastapi/dependencies/utils.py",
                "file_hash": "d",
                "range": [598, 760],
                "inbound_edges": 7,
                "outbound_edges": 6,
            },
            {
                "uid": "get-request-handler",
                "name": "get_request_handler",
                "symbol_kind": "function",
                "token_estimate": 260,
                "qualified_name": "fastapi.routing.get_request_handler",
                "file_path": "/repo/fastapi/routing.py",
                "file_hash": "e",
                "range": [351, 580],
                "inbound_edges": 6,
                "outbound_edges": 4,
            },
        ]
    )
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/fastapi@main")
    target = SubgraphNode(
        uid="depends-fn",
        name="Depends",
        file_path="/repo/fastapi/param_functions.py",
        range=[2283, 2340],
        token_estimate=120,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="function",
    )

    ranker._graph_candidates = lambda *args, **kwargs: []
    ranker._doc_candidates = lambda *args, **kwargs: []
    ranker._sym_vec_candidates = lambda *args, **kwargs: []
    ranker._doc_bridge_candidates = lambda *args, **kwargs: []

    chosen, _, _, _, missing_roles = ranker.rank(
        target,
        "Depends How does dependency injection get resolved before the endpoint function is called?",
        Intent.EXPLORATION,
        4000,
    )

    chosen_names = {candidate.name for candidate in chosen}
    assert {"Depends", "Dependant", "get_dependant", "solve_dependencies", "get_request_handler"} <= chosen_names
    assert set(missing_roles) <= {"docs_or_concept"}


def test_pydantic_validation_flow_backfill_supplies_transition_steps():
    db = _make_backfill_db(
        [
            {
                "uid": "model-validate",
                "name": "model_validate",
                "symbol_kind": "function",
                "token_estimate": 96,
                "qualified_name": "pydantic.main.BaseModel.model_validate",
                "file_path": "/repo/pydantic/main.py",
                "file_hash": "a",
                "range": [694, 740],
                "inbound_edges": 3,
                "outbound_edges": 2,
            },
            {
                "uid": "basemodel-init",
                "name": "__init__",
                "symbol_kind": "function",
                "token_estimate": 120,
                "qualified_name": "pydantic.main.BaseModel.__init__",
                "file_path": "/repo/pydantic/main.py",
                "file_hash": "b",
                "range": [253, 270],
                "inbound_edges": 2,
                "outbound_edges": 3,
            },
            {
                "uid": "complete-model-class",
                "name": "complete_model_class",
                "symbol_kind": "function",
                "token_estimate": 220,
                "qualified_name": "pydantic._internal._model_construction.complete_model_class",
                "file_path": "/repo/pydantic/_internal/_model_construction.py",
                "file_hash": "c",
                "range": [606, 727],
                "inbound_edges": 6,
                "outbound_edges": 5,
            },
            {
                "uid": "pydantic-validator",
                "name": "__pydantic_validator__",
                "symbol_kind": "attribute",
                "token_estimate": 24,
                "qualified_name": "pydantic.main.BaseModel.__pydantic_validator__",
                "file_path": "/repo/pydantic/main.py",
                "file_hash": "d",
                "range": [120, 121],
                "inbound_edges": 5,
                "outbound_edges": 1,
            },
            {
                "uid": "schema-validator",
                "name": "SchemaValidator",
                "symbol_kind": "class",
                "token_estimate": 180,
                "qualified_name": "pydantic_core._pydantic_core.SchemaValidator",
                "file_path": "/repo/pydantic-core/python/pydantic_core/_pydantic_core.pyi",
                "file_hash": "e",
                "range": [67, 152],
                "inbound_edges": 7,
                "outbound_edges": 4,
            },
            {
                "uid": "validate-python",
                "name": "validate_python",
                "symbol_kind": "function",
                "token_estimate": 160,
                "qualified_name": "pydantic_core._pydantic_core.SchemaValidator.validate_python",
                "file_path": "/repo/pydantic-core/python/pydantic_core/_pydantic_core.pyi",
                "file_hash": "f",
                "range": [91, 131],
                "inbound_edges": 4,
                "outbound_edges": 2,
            },
        ]
    )
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/pydantic@main")
    target = SubgraphNode(
        uid="basemodel",
        name="BaseModel",
        file_path="/repo/pydantic/main.py",
        range=[111, 620],
        token_estimate=900,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="class",
        qualified_name="pydantic.main.BaseModel",
    )

    ranker._graph_candidates = lambda *args, **kwargs: []
    ranker._doc_candidates = lambda *args, **kwargs: []
    ranker._sym_vec_candidates = lambda *args, **kwargs: []
    ranker._doc_bridge_candidates = lambda *args, **kwargs: []

    chosen, _, _, _, missing_roles = ranker.rank(
        target,
        "How does BaseModel validation flow work in v2 from model construction to validated output?",
        Intent.EXPLORATION,
        4000,
    )

    chosen_names = {candidate.name for candidate in chosen}
    assert {"model_validate", "__init__", "complete_model_class", "__pydantic_validator__", "SchemaValidator", "validate_python"} <= chosen_names
    assert set(missing_roles) <= {"docs_or_concept"}


def test_request_body_role_backfill_maps_request_body_mechanism():
    db = _make_backfill_db(
        [
            {
                "uid": "get-body-field",
                "name": "get_body_field",
                "symbol_kind": "function",
                "token_estimate": 120,
                "qualified_name": "fastapi.dependencies.utils.get_body_field",
                "file_path": "/repo/fastapi/dependencies/utils.py",
                "file_hash": "a",
                "range": [1001, 1080],
                "inbound_edges": 4,
                "outbound_edges": 3,
            },
            {
                "uid": "solve-dependencies",
                "name": "solve_dependencies",
                "symbol_kind": "function",
                "token_estimate": 220,
                "qualified_name": "fastapi.dependencies.utils.solve_dependencies",
                "file_path": "/repo/fastapi/dependencies/utils.py",
                "file_hash": "b",
                "range": [598, 760],
                "inbound_edges": 7,
                "outbound_edges": 6,
            },
            {
                "uid": "get-request-handler",
                "name": "get_request_handler",
                "symbol_kind": "function",
                "token_estimate": 260,
                "qualified_name": "fastapi.routing.get_request_handler",
                "file_path": "/repo/fastapi/routing.py",
                "file_hash": "c",
                "range": [351, 580],
                "inbound_edges": 6,
                "outbound_edges": 4,
            },
        ]
    )
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/fastapi@main")
    target = SubgraphNode(
        uid="request-body",
        name="request_body_to_args",
        file_path="/repo/fastapi/dependencies/utils.py",
        range=[951, 1000],
        token_estimate=90,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="function",
    )

    ranker._graph_candidates = lambda *args, **kwargs: []
    ranker._doc_candidates = lambda *args, **kwargs: []
    ranker._sym_vec_candidates = lambda *args, **kwargs: []
    ranker._doc_bridge_candidates = lambda *args, **kwargs: []

    chosen, _, _, _, missing_roles = ranker.rank(
        target,
        "request_body_to_args How are request body models validated and then passed into endpoint parameters?",
        Intent.EXPLORATION,
        4000,
    )

    chosen_names = {candidate.name for candidate in chosen}
    assert {"get_body_field", "solve_dependencies", "get_request_handler"} <= chosen_names
    assert set(missing_roles) <= {"docs_or_concept"}


def test_request_body_role_backfill_boosts_weak_existing_role_candidates():
    db = _make_backfill_db(
        [
            {
                "uid": "get-body-field",
                "name": "get_body_field",
                "symbol_kind": "function",
                "token_estimate": 120,
                "qualified_name": "fastapi.dependencies.utils.get_body_field",
                "file_path": "/repo/fastapi/dependencies/utils.py",
                "file_hash": "a",
                "range": [1001, 1080],
                "inbound_edges": 4,
                "outbound_edges": 3,
            },
            {
                "uid": "solve-dependencies",
                "name": "solve_dependencies",
                "symbol_kind": "function",
                "token_estimate": 120,
                "qualified_name": "fastapi.dependencies.utils.solve_dependencies",
                "file_path": "/repo/fastapi/dependencies/utils.py",
                "file_hash": "b",
                "range": [598, 760],
                "inbound_edges": 7,
                "outbound_edges": 6,
            },
            {
                "uid": "get-request-handler",
                "name": "get_request_handler",
                "symbol_kind": "function",
                "token_estimate": 120,
                "qualified_name": "fastapi.routing.get_request_handler",
                "file_path": "/repo/fastapi/routing.py",
                "file_hash": "c",
                "range": [351, 580],
                "inbound_edges": 6,
                "outbound_edges": 4,
            },
        ]
    )
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/fastapi@main")
    target = SubgraphNode(
        uid="request-body",
        name="request_body_to_args",
        file_path="/repo/fastapi/dependencies/utils.py",
        range=[951, 1000],
        token_estimate=90,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="function",
    )

    ranker._graph_candidates = lambda *args, **kwargs: [
        Candidate(
            kind="symbol",
            uid="solve-dependencies",
            name="solve_dependencies",
            file_path="/repo/fastapi/dependencies/utils.py",
            token_cost=1104,
            graph_score=-0.2,
            relation="CALLS_SCOPED",
            direction="callee",
            depth=1,
        ),
        Candidate(
            kind="symbol",
            uid="get-request-handler",
            name="get_request_handler",
            file_path="/repo/fastapi/routing.py",
            token_cost=3032,
            graph_score=-3.3,
            relation="CALLS_GUESS",
            direction="callee",
            depth=2,
        ),
    ]
    ranker._doc_candidates = lambda *args, **kwargs: []
    ranker._sym_vec_candidates = lambda *args, **kwargs: [
        Candidate(
            kind="symbol",
            uid="get-body-field",
            name="get_body_field",
            file_path="/repo/fastapi/dependencies/utils.py",
            token_cost=0,
            semantic_score=0.9,
        )
    ]
    ranker._doc_bridge_candidates = lambda *args, **kwargs: []
    fill_token_costs = ranker._fill_token_costs

    def _fill_token_costs_with_realistic_body_field(pool):
        fill_token_costs(pool)
        for candidate in pool:
            if candidate.uid == "get-body-field":
                candidate.token_cost = 416

    ranker._fill_token_costs = _fill_token_costs_with_realistic_body_field

    chosen, _, _, _, missing_roles = ranker.rank(
        target,
        "request_body_to_args How are request body models validated and then passed into endpoint parameters?",
        Intent.EXPLORATION,
        4000,
    )

    chosen_by_name = {candidate.name: candidate for candidate in chosen}
    assert {"get_body_field", "solve_dependencies", "get_request_handler"} <= set(chosen_by_name)
    assert chosen_by_name["get_body_field"].token_cost <= 120
    assert chosen_by_name["solve_dependencies"].token_cost <= 120
    assert chosen_by_name["get_request_handler"].token_cost <= 120
    assert any(step.startswith("role-backfill:") for step in chosen_by_name["solve_dependencies"].provenance)
    assert set(missing_roles) <= {"docs_or_concept"}


def test_pydantic_basemodel_mechanism_uses_query_to_split_boundary_vs_validation():
    ranker = UnifiedRanker(_make_db(), VectorSearcher(_FakeVector()), workspace_id="local/pydantic@main")
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
        == "pydantic_validation_core_bridge"
    )
    assert (
        ranker._determine_mechanism(
            target,
            query="Which parts of Pydantic are pure Python wrappers and which parts rely on pydantic-core?",
        )
        == "pydantic_python_core_boundary"
    )


def test_pydantic_role_inference_normalizes_to_canonical_roles():
    ranker = UnifiedRanker(_make_db(), VectorSearcher(_FakeVector()), workspace_id="local/pydantic@main")

    assert ranker._infer_role(
        Candidate(
            kind="symbol",
            uid="validator",
            name="__pydantic_validator__",
            file_path="/repo/pydantic/main.py",
            token_cost=40,
        )
    ) == "validator_handle"
    assert ranker._infer_role(
        Candidate(
            kind="symbol",
            uid="generator",
            name="GenerateJsonSchema",
            file_path="/repo/pydantic/json_schema.py",
            token_cost=120,
        )
    ) == "schema_builder"
    assert ranker._infer_role(
        Candidate(
            kind="symbol",
            uid="runtime",
            name="SchemaValidator",
            file_path="/repo/pydantic-core/python/pydantic_core/_pydantic_core.pyi",
            token_cost=120,
        )
    ) == "core_runtime"


def test_capability_roles_infer_validator_and_serializer_support_without_dunder_symbols():
    ranker = UnifiedRanker(_make_db(), VectorSearcher(_FakeVector()), workspace_id="local/pydantic@main")

    schema_validator = Candidate(
        kind="symbol",
        uid="schema-validator",
        name="SchemaValidator",
        file_path="/repo/pydantic-core/python/pydantic_core/_pydantic_core.pyi",
        token_cost=120,
    )
    model_dump = Candidate(
        kind="symbol",
        uid="model-dump",
        name="model_dump",
        file_path="/repo/pydantic/main.py",
        token_cost=80,
    )

    assert set(ranker._roles_of(schema_validator)) >= {"core_runtime", "validator_handle"}
    assert set(ranker._roles_of(model_dump)) >= {"api_surface", "serializer_handle"}


def test_capability_roles_reduce_pydantic_handle_backfill_when_equivalent_symbols_exist():
    ranker = UnifiedRanker(_make_db(), VectorSearcher(_FakeVector()), workspace_id="local/pydantic@main")
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
    pool = [
        Candidate(
            kind="symbol",
            uid="model-validate",
            name="model_validate",
            file_path="/repo/pydantic/main.py",
            token_cost=90,
            semantic_score=0.9,
        ),
        Candidate(
            kind="symbol",
            uid="schema-validator",
            name="SchemaValidator",
            file_path="/repo/pydantic-core/python/pydantic_core/_pydantic_core.pyi",
            token_cost=120,
            graph_score=0.8,
        ),
    ]

    needed = ranker._roles_needing_backfill(
        target,
        pool,
        ["api_surface", "validator_handle", "core_runtime"],
    )

    assert "validator_handle" not in needed
    assert "core_runtime" not in needed


def test_capability_roles_add_generic_impact_runtime_and_test_surfaces():
    ranker = UnifiedRanker(_make_db(), VectorSearcher(_FakeVector()), workspace_id="local/pydantic@main")

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


def test_pydantic_backfill_preserves_explicit_role_overrides():
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

    backfill = ranker._role_backfill_candidates(
        "pydantic_python_core_boundary",
        ["validator_handle", "serializer_handle", "core_runtime"],
        excluded_uids=set(),
    )

    by_uid = {candidate.uid: candidate for candidate in backfill}
    assert by_uid["validator-handle"].evidence_role == "validator_handle"
    assert by_uid["schema-validator"].evidence_role == "core_runtime"
    assert by_uid["schema-serializer"].evidence_role == "serializer_handle"


def test_generic_ts_js_mechanisms_cover_redux_style_queries():
    ranker = UnifiedRanker(_make_db(), VectorSearcher(_FakeVector()), workspace_id="local/redux@main")

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
        == "state_factory_pipeline"
    )
    assert (
        ranker._determine_mechanism(
            configure_store,
            query="How does configureStore assemble middleware, enhancers, and DevTools behavior?",
        )
        == "runtime_configuration_pipeline"
    )
    assert (
        ranker._determine_mechanism(
            create_async_thunk,
            query="How does createAsyncThunk generate pending / fulfilled / rejected action flow?",
        )
        == "async_lifecycle_pipeline"
    )
    assert (
        ranker._determine_mechanism(
            create_api,
            query="How does RTK Query define an API slice and connect generated endpoints into the store?",
        )
        == "api_store_integration_pipeline"
    )
    assert (
        ranker._determine_mechanism(
            create_listener_middleware,
            query="How does listener middleware intercept actions and trigger side effects?",
        )
        == "listener_orchestration_pipeline"
    )


def test_generic_ts_js_role_inference_covers_redux_style_symbols():
    ranker = UnifiedRanker(_make_db(), VectorSearcher(_FakeVector()), workspace_id="local/redux@main")

    assert ranker._infer_role(
        Candidate(
            kind="symbol",
            uid="createSlice",
            name="createSlice",
            file_path="/repo/packages/toolkit/src/createSlice.ts",
            token_cost=20,
        )
    ) == "api_surface"
    assert ranker._infer_role(
        Candidate(
            kind="symbol",
            uid="createAction",
            name="createAction",
            file_path="/repo/packages/toolkit/src/createAction.ts",
            token_cost=20,
        )
    ) == "factory_surface"
    assert ranker._infer_role(
        Candidate(
            kind="symbol",
            uid="composeWithDevTools",
            name="composeWithDevTools",
            file_path="/repo/packages/toolkit/src/devtoolsExtension.ts",
            token_cost=20,
        )
    ) == "composition_surface"
    assert ranker._infer_role(
        Candidate(
            kind="symbol",
            uid="requestThunk1",
            name="requestThunk1",
            file_path="/repo/packages/toolkit/src/createAsyncThunk.ts",
            token_cost=20,
        )
    ) == "executor"
    assert ranker._infer_role(
        Candidate(
            kind="symbol",
            uid="getEndpointDefinition",
            name="getEndpointDefinition",
            file_path="/repo/packages/toolkit/src/query/core/module.ts",
            token_cost=20,
        )
    ) == "representation_surface"
    assert ranker._infer_role(
        Candidate(
            kind="symbol",
            uid="dispatch",
            name="dispatch",
            file_path="/repo/packages/toolkit/src/query/core/module.ts",
            token_cost=20,
        )
    ) == "integration_surface"
    assert ranker._infer_role(
        Candidate(
            kind="symbol",
            uid="listener-api",
            name="createListenerMiddleware",
            file_path="/repo/packages/toolkit/src/listenerMiddleware/index.ts",
            token_cost=20,
        )
    ) == "api_surface"
    assert ranker._infer_role(
        Candidate(
            kind="symbol",
            uid="add-listener",
            name="addListener",
            file_path="/repo/packages/toolkit/src/listenerMiddleware/index.ts",
            token_cost=20,
        )
    ) == "orchestrator"


def test_listener_middleware_api_carries_supporting_execution_roles():
    ranker = UnifiedRanker(_make_db(), VectorSearcher(_FakeVector()), workspace_id="local/redux@main")
    target = Candidate(
        kind="symbol",
        uid="listener-api",
        name="createListenerMiddleware",
        file_path="/repo/packages/toolkit/src/listenerMiddleware/index.ts",
        token_cost=120,
    )

    assert set(ranker._roles_of(target)) >= {"api_surface", "orchestrator", "executor"}


def test_editorial_docs_get_downranked_relative_to_mechanism_docs():
    assert compute_noise_factor(
        "/repo/docs/usage/migrating-rtk-2.md",
        "migrating",
        kind="doc",
    ) < 1.0
    assert compute_noise_factor(
        "/repo/docs/rtk-query/overview.md",
        "overview",
        kind="doc",
    ) == 1.0


def test_generic_role_recovery_uses_same_file_and_imported_symbols():
    db = _make_recovery_db(
        same_file_rows=[
            {
                "uid": "build-create-slice",
                "name": "buildCreateSlice",
                "symbol_kind": "function",
                "token_estimate": 220,
                "qualified_name": "redux.createSlice.buildCreateSlice",
                "file_path": "/repo/packages/toolkit/src/createSlice.ts",
                "file_hash": "a",
                "range": [568, 821],
                "inbound_edges": 2,
                "outbound_edges": 6,
            },
        ],
        imported_rows=[
            {
                "uid": "create-reducer",
                "name": "createReducer",
                "symbol_kind": "function",
                "token_estimate": 180,
                "qualified_name": "redux.createReducer.createReducer",
                "file_path": "/repo/packages/toolkit/src/createReducer.ts",
                "file_hash": "b",
                "range": [141, 224],
                "inbound_edges": 5,
                "outbound_edges": 4,
            },
        ],
    )
    ranker = UnifiedRanker(db, VectorSearcher(_FakeVector()), workspace_id="local/redux@main")
    target = SubgraphNode(
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

    candidates = ranker._generic_role_recovery_candidates(
        target,
        ["factory_surface", "composition_surface"],
        excluded_uids={target.uid},
    )

    by_name = {candidate.name: candidate for candidate in candidates}
    assert by_name["buildCreateSlice"].evidence_role in {"factory_surface", "composition_surface"}
    assert "composition_surface" in by_name["buildCreateSlice"].supporting_roles or by_name["buildCreateSlice"].evidence_role == "composition_surface"
    assert by_name["createReducer"].evidence_role == "composition_surface"


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

    ranker = UnifiedRanker(_make_db(), VectorSearcher(_FakeVector()), workspace_id="local/redux@main")

    resolved = ranker._resolve_filesystem_import_paths(str(create_slice))

    assert str(create_reducer.resolve()) in resolved
