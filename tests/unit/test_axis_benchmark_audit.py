"""Candidate-level audit helpers for ``QA.axis_benchmark``."""

from __future__ import annotations

from types import SimpleNamespace

from context_engine.axis.context_builder import ContextBundle, ContextSymbol
from context_engine.axis.role_retrieval import RoleCandidate
from QA.axis_benchmark import (
    _expected_file_layers,
    _populate_candidate_audit,
    _populate_recall_layers,
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


def _symbol(uid: str, path: str, role: str, *, depth: int = 0) -> ContextSymbol:
    return ContextSymbol(
        uid=uid,
        name=uid,
        qualified_name=f"pkg.{uid}",
        file_path=path,
        role=role,
        distance_from_seed=depth,
        expansion_step=role if depth else None,
        code="x",
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
