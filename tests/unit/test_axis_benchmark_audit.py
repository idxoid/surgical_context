"""Candidate-level audit helpers for ``QA.axis_benchmark``."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from context_engine.axis.context_builder import ContextBundle, ContextSymbol, RenderedOwner
from context_engine.axis.role_retrieval import RoleCandidate
from QA.axis_benchmark import (
    QuestionResult,
    _candidate_cohort_audit,
    _compute_span_owner_recall,
    _expected_file_layers,
    _gold_rank_audit,
    _lexical_span_score_audit,
    _populate_candidate_audit,
    _populate_recall_layers,
    _question_result_from_entry,
    _resolve_question_workspace,
    _split_rendered_tokens,
    summarise,
)


def _candidate(uid: str, path: str, role: str, *, score: float = 0.5) -> RoleCandidate:
    return RoleCandidate(
        uid=uid,
        name=uid,
        qualified_name=f"pkg.{uid}",
        file_path=path,
        role=role,
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=None,
        score=score,
    )


def _symbol(uid: str, path: str, role: str, *, depth: int = 0, code: str = "x") -> ContextSymbol:
    return ContextSymbol(
        uid=uid,
        name=uid,
        qualified_name=f"pkg.{uid}",
        file_path=path,
        role=role,
        distance_from_seed=depth,
        expansion_step=role if depth else None,
        code=code,
    )


def test_base_commit_question_resolves_to_exact_profile_workspace() -> None:
    entry = {
        "id": "django_django_11179",
        "repo": "django",
        "question": "Where should this regression be fixed?",
        "base_commit": "19fc6376ce67d01ca37a91ef2f55ef769f50513a",
        "expected_files": ["django/forms/models.py"],
        "expected_spans": [
            {"file_path": "django/forms/models.py", "start_line": 100, "end_line": 110}
        ],
    }

    result = _question_result_from_entry(entry)
    workspace_id = _resolve_question_workspace(
        entry,
        result,
        None,
        use_base_commit_workspace=True,
        commit_workspace_tenant="contextbench",
    )

    assert result.base_commit == entry["base_commit"]
    assert workspace_id == "contextbench/django@19fc6376ce67+axis_python_v1"


def test_explicit_question_workspace_wins_over_base_commit() -> None:
    entry = {
        "id": "task",
        "repo": "django",
        "question": "Where?",
        "base_commit": "a" * 40,
        "workspace_id": "custom/django@exact",
        "expected_files": ["django/forms/models.py"],
    }
    result = _question_result_from_entry(entry)

    workspace_id = _resolve_question_workspace(
        entry,
        result,
        None,
        use_base_commit_workspace=True,
        commit_workspace_tenant="contextbench",
    )

    assert workspace_id == "custom/django@exact+axis_python_v1"


def test_axis_benchmark_records_candidate_audit_and_expected_layers() -> None:
    from QA.axis_benchmark import QuestionResult

    result = QuestionResult(
        question_id="q1",
        repo="repo",
        workspace_id="ws",
        question="why is this noisy?",
        mechanism="debug",
        expected_files=["src/a.py", "src/b.py", "src/c.py", "src/e.py", "src/missing.py"],
    )
    retrieval = SimpleNamespace(
        seed_files=["/repo/src/a.py"],
        candidates_for_context=[
            _candidate("a", "/repo/src/a.py", "vector_seed", score=0.9),
            _candidate("b", "/repo/src/b.py", "structural_neighbour", score=0.4),
            _candidate("e", "/repo/src/e.py", "structural_neighbour", score=0.3),
        ],
        bundles=[
            ContextBundle(
                role="vector_seed",
                seed=_symbol("a", "/repo/src/a.py", "vector_seed"),
                related=(
                    _symbol("b", "/repo/src/b.py", "reverse_calls", depth=1),
                    _symbol("c", "/repo/src/c.py", "binding_structure_expansion", depth=1),
                ),
            )
        ],
    )

    _populate_recall_layers(result, retrieval)
    _populate_candidate_audit(result, retrieval)
    result.expected_file_layers = _expected_file_layers(result)

    assert result.seed_recall == 0.2
    assert result.pool_recall == 0.6
    assert result.file_recall == 0.6
    assert result.candidate_relation_histogram == {
        "structural_neighbour": 2,
        "vector_seed": 1,
    }
    assert result.bundle_relation_histogram == {
        "binding_structure_expansion": 1,
        "reverse_calls": 1,
        "vector_seed": 1,
    }
    assert [row["relation"] for row in result.top_candidates] == [
        "vector_seed",
        "structural_neighbour",
        "structural_neighbour",
    ]

    by_file = {row["expected_file"]: row for row in result.expected_file_layers}
    assert by_file["src/a.py"]["first_layer"] == "seed"
    assert by_file["src/b.py"]["first_layer"] == "pool"
    assert by_file["src/c.py"]["first_layer"] == "bundle"
    assert by_file["src/e.py"]["first_layer"] == "pool"
    assert by_file["src/e.py"]["lost_after"] == ["pool"]
    assert by_file["src/missing.py"]["first_layer"] == "missing"

    summary = summarise([result])
    assert summary["candidate_relation_totals"]["structural_neighbour"] == 2
    assert summary["bundle_relation_totals"]["reverse_calls"] == 1


def test_candidate_cohort_audit_splits_role_axis_intent_and_token_spend() -> None:
    gold = replace(
        _candidate("route", "/repo/src/routes.py", "routing_surface", score=0.9),
        name="add_route",
        satisfying_kinds=("keyed_register_callable",),
        supporting_roles=("routing_surface", "binding_surface"),
    )
    trace = replace(
        _candidate("trace", "/repo/src/trace.py", "trace_dependency", score=0.8),
        satisfying_kinds=("trace_callers",),
        supporting_roles=("trace_dependency",),
    )
    impact = replace(
        _candidate("impact", "/repo/src/impact.py", "impact_analysis", score=0.75),
        edge_type="CALLS_*",
        satisfying_kinds=("reverse_calls",),
        supporting_roles=("impact_analysis",),
    )
    vector = replace(
        _candidate("vector", "/repo/src/noise.py", "vector_seed", score=0.7),
        retrieval_channels=("vector",),
        supporting_roles=("vector_seed",),
    )
    budget_trace = SimpleNamespace(
        transactions=[
            SimpleNamespace(phase="coverage", uid="route", delta_tokens=20),
            SimpleNamespace(phase="coverage", uid="trace", delta_tokens=10),
            SimpleNamespace(phase="coverage", uid="impact", delta_tokens=5),
            SimpleNamespace(phase="upgrade_rank_decay", uid="route", delta_tokens=80),
        ]
    )

    audit = _candidate_cohort_audit(
        [gold, trace, impact, vector],
        expected_files=["src/routes.py"],
        expected_symbols=["add_route"],
        expected_spans=[],
        intent_matches=[("routing_surface", 0.8), ("trace_dependency", 0.7)],
        budget_trace=budget_trace,
    )

    assert audit["candidate_count"] == 4
    assert audit["multi_role_candidates"] == 1
    assert audit["by_role"]["routing_surface"]["symbol_gold"] == 1
    assert audit["by_role"]["binding_surface"]["symbol_gold"] == 1
    assert audit["by_role_signature"]["binding_surface+routing_surface"]["candidates"] == 1
    assert audit["by_role_intent_alignment"]["routing_surface|role+axis"]["candidates"] == 1
    assert audit["by_role"]["routing_surface"]["upgrade_tokens"] == 80
    assert audit["by_role"]["routing_surface"]["mean_exact_gold_rank"] == 1.0
    assert audit["by_role"]["routing_surface"]["exact_gold_top10_share"] == 1.0
    assert audit["by_axis"]["registry"]["candidates"] == 1
    assert audit["by_axis"]["control"]["candidates"] == 1
    assert audit["by_axis"]["axisless"]["candidates"] == 2
    assert audit["by_intent_alignment"]["role+axis"]["candidates"] == 1
    assert audit["by_intent_alignment"]["role_only"]["candidates"] == 1
    assert audit["by_intent_alignment"]["axis_only"]["candidates"] == 1
    assert audit["by_intent_alignment"]["none"]["candidates"] == 1
    assert audit["by_channel_signature"]["symbol_vector"]["candidates"] == 1
    assert audit["by_channel_signature"]["(none)"]["candidates"] == 3
    assert audit["by_channel_count"]["1"]["candidates"] == 1
    assert audit["by_channel_count"]["0"]["candidates"] == 3
    assert audit["by_exact_symbol_prior"]["non_exact"]["candidates"] == 4

    result = QuestionResult(
        question_id="cohorts",
        repo="repo",
        workspace_id="ws",
        question="where is routing?",
        mechanism="audit",
        expected_files=[],
        candidate_cohort_audit=audit,
    )
    aggregate = summarise([result])["candidate_cohorts"]
    assert aggregate["totals"]["candidates"] == 4
    assert aggregate["totals"]["exact_gold"] == 1
    assert aggregate["by_top_intent"]["routing_surface"]["candidates"] == 4
    assert aggregate["by_top_intent_role"]["routing_surface|vector_seed"]["candidates"] == 1


def test_axis_benchmark_records_precision_layers_and_token_split() -> None:
    from context_engine.observability.metrics import estimate_text_tokens
    from QA.axis_benchmark import QuestionResult

    result = QuestionResult(
        question_id="q2",
        repo="repo",
        workspace_id="ws",
        question="how noisy is the bundle?",
        mechanism="debug",
        expected_files=["src/a.py"],
    )
    noise = _symbol("n", "/repo/src/noise.py", "structural_neighbour", depth=1, code="noise " * 40)
    retrieval = SimpleNamespace(
        seed_files=["/repo/src/a.py", "/repo/src/noise.py"],
        candidates_for_context=[
            _candidate("a", "/repo/src/a.py", "vector_seed", score=0.9),
            _candidate("n", "/repo/src/noise.py", "structural_neighbour", score=0.4),
        ],
        bundles=[
            ContextBundle(
                role="vector_seed",
                seed=_symbol("a", "/repo/src/a.py", "vector_seed"),
                related=(noise,),
            )
        ],
    )

    _populate_recall_layers(result, retrieval)

    assert result.seed_recall == 1.0
    assert result.seed_precision == 0.5
    assert result.pool_precision == 0.5
    assert result.bundle_precision == 0.5

    expected_tokens, other_tokens = _split_rendered_tokens(retrieval.bundles, result.expected_files)
    assert expected_tokens == estimate_text_tokens("x")
    assert other_tokens == estimate_text_tokens("noise " * 40)

    result.expected_tokens = expected_tokens
    result.other_tokens = other_tokens
    result.rendered_tokens = expected_tokens + other_tokens
    result.token_precision = expected_tokens / result.rendered_tokens

    summary = summarise([result])
    assert summary["overall_mean_precision"] == 0.5
    assert summary["overall_seed_mean_precision"] == 0.5
    assert summary["overall_pool_mean_precision"] == 0.5
    assert summary["overall_mean_token_precision"] == result.token_precision
    assert summary["per_repo"]["repo"]["mean_precision"] == 0.5
    assert summary["per_repo"]["repo"]["mean_token_precision"] == result.token_precision
    row = summary["per_question"][0]
    assert row["bundle_precision"] == 0.5
    assert row["expected_tokens"] == expected_tokens
    assert row["other_tokens"] == other_tokens


def test_axis_benchmark_records_exact_symbol_and_span_recall() -> None:
    from QA.axis_benchmark import QuestionResult

    seed = RoleCandidate(
        uid="run-once",
        name="run_once",
        qualified_name="worker.AsyncPoller.run_once",
        file_path="/repo/worker.py",
        role="hybrid_seed",
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=0.2,
        score=0.9,
        retrieval_channels=("lexical", "semantic_chunk"),
        retrieval_spans=((105, 110),),
        exact_symbol_match=True,
    )
    rendered = ContextSymbol(
        uid=seed.uid,
        name=seed.name,
        qualified_name=seed.qualified_name,
        file_path=seed.file_path,
        role=seed.role,
        distance_from_seed=0,
        expansion_step=None,
        code="def run_once():\n    pass",
        start_line=100,
        end_line=110,
        rendered_spans=((104, 108),),
        retrieval_spans=seed.retrieval_spans,
    )
    result = QuestionResult(
        question_id="q-symbol-span",
        repo="repo",
        workspace_id="ws",
        question="How does `run_once` work?",
        mechanism="debug",
        expected_files=["worker.py"],
        expected_symbols=["run_once", "missing_symbol"],
        expected_spans=[
            {
                "symbol": "run_once",
                "file_path": "worker.py",
                "start_line": 105,
                "end_line": 109,
            }
        ],
    )
    retrieval = SimpleNamespace(
        seed_files=[seed.file_path],
        seed_candidates=[seed],
        candidates_for_context=[seed],
        bundles=[ContextBundle(role=seed.role, seed=rendered)],
    )

    _populate_recall_layers(result, retrieval)

    assert result.seed_symbol_recall == 0.5
    assert result.pool_symbol_recall == 0.5
    assert result.bundle_symbol_recall == 0.5
    assert result.seed_span_owner_recall == 1.0
    assert result.pool_span_owner_recall == 1.0
    assert result.bundle_span_owner_recall == 1.0
    assert result.seed_span_recall == 1.0
    assert result.pool_span_recall == 1.0
    assert result.bundle_span_recall == 0.8
    summary = summarise([result])
    assert summary["overall_seed_symbol_recall"] == 0.5
    assert summary["overall_seed_span_owner_recall"] == 1.0
    assert summary["overall_pool_span_owner_recall"] == 1.0
    assert summary["overall_bundle_span_owner_recall"] == 1.0
    assert summary["overall_pool_span_recall"] == 1.0
    assert summary["overall_bundle_span_recall"] == 0.8
    assert summary["per_repo"]["repo"]["bundle_span_owner_recall"] == 1.0
    assert summary["per_repo"]["repo"]["seed_symbol_recall"] == 0.5
    assert summary["per_repo"]["repo"]["pool_symbol_recall"] == 0.5
    assert summary["per_repo"]["repo"]["bundle_symbol_recall"] == 0.5


def test_span_owner_recall_requires_the_file_symbol_pair_and_deduplicates_ranges() -> None:
    expected = [
        {"file_path": "worker.py", "symbol": "run_once", "start_line": 10, "end_line": 12},
        {"file_path": "worker.py", "symbol": "run_once", "start_line": 20, "end_line": 22},
    ]
    wrong_pair = [
        _candidate("wrong-file", "/repo/other.py", "hybrid_seed"),
        _candidate("wrong-symbol", "/repo/worker.py", "hybrid_seed"),
    ]
    wrong_pair[0] = replace(
        wrong_pair[0],
        name="run_once",
        qualified_name="worker.run_once",
    )

    assert _compute_span_owner_recall(expected, wrong_pair) == 0.0

    correct = replace(
        wrong_pair[1],
        name="run_once",
        qualified_name="worker.run_once",
    )
    assert _compute_span_owner_recall(expected, [*wrong_pair, correct]) == 1.0


def test_span_owner_recall_matches_all_honest_owners_of_folded_render() -> None:
    folded = replace(
        _symbol("method", "/repo/worker.py", "hybrid_seed"),
        name="run_once",
        qualified_name="worker.Worker.run_once",
        represented_owners=(
            RenderedOwner(
                uid="class",
                name="Worker",
                qualified_name="worker.Worker",
                file_path="/repo/worker.py",
            ),
            RenderedOwner(
                uid="method",
                name="run_once",
                qualified_name="worker.Worker.run_once",
                file_path="/repo/worker.py",
            ),
        ),
    )
    expected = [
        {"file_path": "worker.py", "symbol": "Worker", "start_line": 1, "end_line": 20},
        {"file_path": "worker.py", "symbol": "run_once", "start_line": 5, "end_line": 9},
    ]

    assert _compute_span_owner_recall(expected, [folded]) == 1.0


def test_lexical_span_score_audit_compares_exact_owner_pair_with_pool() -> None:
    expected = [{"file_path": "worker.py", "symbol": "run_once", "start_line": 10, "end_line": 12}]
    gold = replace(
        _candidate("gold", "/repo/worker.py", "hybrid_seed"),
        name="run_once",
        qualified_name="worker.run_once",
        lexical_span_score=0.8,
    )
    wrong_file = replace(
        _candidate("wrong", "/repo/other.py", "hybrid_seed"),
        name="run_once",
        qualified_name="other.run_once",
        lexical_span_score=0.9,
    )
    other = replace(
        _candidate("other", "/repo/other.py", "hybrid_seed"),
        lexical_span_score=0.2,
    )

    audit = _lexical_span_score_audit(expected, [gold, wrong_file, other])

    assert audit["gold_owner_candidates"] == 1
    assert audit["scored_gold_owner_candidates"] == 1
    assert audit["auc"] == 0.5
    assert audit["gold_precision_at_owner_count"] == 0.0


def test_gold_rank_audit_tracks_complete_budget_and_prompt_funnel() -> None:
    from QA.axis_benchmark import QuestionResult

    expected_spans = [
        {"file_path": "worker.py", "symbol": "run_once", "start_line": 10, "end_line": 12},
        {"file_path": "missing.py", "symbol": "rescued", "start_line": 20, "end_line": 22},
    ]
    other = _candidate("other", "/repo/other.py", "hybrid_seed")
    gold = replace(
        _candidate("gold", "/repo/worker.py", "hybrid_seed"),
        name="run_once",
        qualified_name="worker.run_once",
        lexical_span_score=0.8,
    )
    rendered_other = _symbol("other", "/repo/other.py", "hybrid_seed")
    rendered_gold = replace(
        _symbol("gold", "/repo/worker.py", "hybrid_seed"),
        name="run_once",
        qualified_name="worker.run_once",
    )
    rendered_rescue = replace(
        _symbol("rescue", "/repo/missing.py", "binding_structure_expansion"),
        name="rescued",
        qualified_name="missing.rescued",
    )
    trace = SimpleNamespace(
        transactions=[
            SimpleNamespace(
                phase="upgrade_capped",
                uid="gold",
                delta_tokens=80,
                attribution=[
                    SimpleNamespace(
                        scope="seed",
                        evidence="retrieval_backed",
                        edge_type="SEED",
                        depth=0,
                        delta_tokens=80,
                    )
                ],
            ),
            SimpleNamespace(phase="coverage", uid="gold", delta_tokens=10),
            SimpleNamespace(phase="coverage", uid="other", delta_tokens=20),
            SimpleNamespace(
                phase="upgrade_leader_relaxed",
                uid="other",
                delta_tokens=5,
                attribution=[
                    SimpleNamespace(
                        scope="related",
                        evidence="graph_only",
                        edge_type="CALLS_*",
                        depth=1,
                        delta_tokens=5,
                    )
                ],
            ),
            SimpleNamespace(
                phase="upgrade_tail_relaxed",
                uid="rescued",
                delta_tokens=7,
                attribution=[
                    SimpleNamespace(
                        scope="related",
                        evidence="retrieval_backed",
                        edge_type="REFERENCES",
                        depth=2,
                        delta_tokens=7,
                    )
                ],
            ),
        ]
    )

    audit = _gold_rank_audit(
        expected_spans,
        ["run_once", "rescued"],
        [other, gold],
        [rendered_other, rendered_gold, rendered_rescue],
        trace,
    )

    by_owner = {row["symbol"]: row for row in audit["owners"]}
    assert by_owner["run_once"]["utility_rank"] == 2
    assert by_owner["run_once"]["coverage_rank"] == 1
    assert by_owner["run_once"]["final_rank"] == 2
    assert by_owner["rescued"]["utility_rank"] is None
    assert by_owner["rescued"]["final_rank"] == 3
    assert audit["owner_funnel"]["retrieval_missing_but_final"] == 1
    assert audit["owner_funnel"]["utility"]["recall_at"]["1"] == 0.0
    assert audit["owner_funnel"]["utility"]["recall_at"]["3"] == 0.5
    spend = audit["candidate_rank_spend"]
    assert spend["coverage_tokens"] == 30
    assert spend["upgrade_tokens"] == 92
    assert spend["by_rank"]["1"]["upgrade_tokens"] == 5
    assert spend["by_rank"]["2-3"]["upgrade_tokens"] == 80
    assert spend["by_rank"]["unranked"]["upgrade_tokens"] == 7
    assert spend["upgrade_tokens_at"]["1"] == 5
    assert spend["upgrade_tokens_at"]["3"] == 85
    assert spend["upgrade_attribution"]["scope"] == {"seed": 80, "related": 12}
    assert spend["upgrade_attribution"]["evidence"] == {
        "retrieval_backed": 87,
        "graph_only": 5,
    }
    assert spend["upgrade_attribution"]["depth"] == {"0": 80, "2": 7, "1": 5}

    result = QuestionResult(
        question_id="rank-spend",
        repo="repo",
        workspace_id="ws",
        question="where did the body tokens go?",
        mechanism="audit",
        expected_files=[],
        gold_rank_audit=audit,
    )
    aggregate = summarise([result])["candidate_rank_token_spend"]
    assert aggregate["upgrade_tokens"] == 92
    assert aggregate["by_rank"]["2-3"]["upgrade_tokens"] == 80
    assert aggregate["upgrade_share_at"]["3"] == 85 / 92
    assert aggregate["allocation_mode_counts"] == {"legacy": 1}
    assert aggregate["upgrade_attribution"]["scope_evidence"] == {
        "seed|retrieval_backed": 80,
        "related|retrieval_backed": 7,
        "related|graph_only": 5,
    }
