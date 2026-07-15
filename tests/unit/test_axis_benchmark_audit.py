"""Candidate-level audit helpers for ``QA.axis_benchmark``."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from context_engine.axis.context_builder import ContextBundle, ContextSymbol
from context_engine.axis.role_retrieval import RoleCandidate
from QA.axis_benchmark import (
    _compute_span_owner_recall,
    _expected_file_layers,
    _lexical_span_score_audit,
    _populate_candidate_audit,
    _populate_recall_layers,
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


def test_lexical_span_score_audit_compares_exact_owner_pair_with_pool() -> None:
    expected = [
        {"file_path": "worker.py", "symbol": "run_once", "start_line": 10, "end_line": 12}
    ]
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
