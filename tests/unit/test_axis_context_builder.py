"""Context builder — RoleCandidate → ContextBundle expansion."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from context_engine.axis.context_builder import (
    ContextRenderBudget,
    TokenCreditTrace,
    _evidence_graph_fanout_limit,
    _Hit,
    _nearest_expansion_hits,
    build_context_for_candidates,
)
from context_engine.axis.role_retrieval import RoleCandidate
from tests.unit.axis_helpers import axis_test_file_path

WORKSPACE = "qa_repo/test@axis"


class _FakeQueryScoring:
    def __init__(self, similarities: dict[str, float]):
        self._similarities = similarities

    def similarity_for(self, uid: str) -> float | None:
        return self._similarities.get(uid)


class _Result:
    def __init__(self, records: list[dict]):
        self._records = list(records)

    def __iter__(self):
        return iter(self._records)


class _Session:
    def __init__(self, records_by_query: list[list[dict]]):
        self._records_by_query = list(records_by_query)
        self.runs: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query: str, **params: Any):
        self.runs.append((query, dict(params)))
        records = self._records_by_query.pop(0) if self._records_by_query else []
        return _Result(records)


class _Driver:
    def __init__(self, session: _Session):
        self._session = session

    def session(self):
        return self._session


class _FakeDB:
    def __init__(
        self,
        session_records: list[list[dict]],
        *,
        spans: dict[str, dict[str, int | str]] | None = None,
    ):
        self._session = _Session(session_records)
        self.driver = _Driver(self._session)
        self._spans = spans or {}
        self.span_calls: list[tuple[list[str], str]] = []

    def get_symbol_spans_by_uids(
        self,
        uids: list[str],
        *,
        workspace_id: str,
    ) -> dict[str, dict[str, int | str]]:
        self.span_calls.append((list(uids), workspace_id))
        return {uid: self._spans[uid] for uid in uids if uid in self._spans}


class _FakeLanceTable:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def to_lance(self):
        outer = self

        class _Lance:
            def to_table(self, columns=None):
                class _Arrow:
                    def to_pylist(self_inner):
                        return list(outer._rows)

                return _Arrow()

        return _Lance()


class _FakeLance:
    def __init__(self, rows: list[dict[str, Any]]):
        self._sym_table = _FakeLanceTable(rows)


def _make_candidate(
    uid: str,
    name: str,
    role: str = "binding_surface",
    score: float = 1.0,
    qualified_name: str = "",
) -> RoleCandidate:
    return RoleCandidate(
        uid=uid,
        name=name,
        qualified_name=qualified_name,
        file_path=axis_test_file_path(name),
        role=role,
        satisfying_contracts=("registry_binding_inferred",),
        satisfying_kinds=(),
        contract_count=1,
        kind_count=0,
        vector_distance=None,
        score=score,
    )


def _lance_row(uid: str, code: str, qualified_name: str = "") -> dict[str, Any]:
    return {
        "uid": uid,
        "code": code,
        "qualified_name": qualified_name,
        "workspace_id": WORKSPACE,
    }


def _hit_record(
    seed_uid: str,
    uid: str,
    name: str,
    file_path: str,
    step: str,
    depth: int,
) -> dict:
    return {
        "seed_uid": seed_uid,
        "uid": uid,
        "name": name,
        "qualified_name": name,
        "file_path": file_path,
        "step": step,
        "depth": depth,
    }


def test_empty_candidates_returns_empty():
    db = _FakeDB([])
    lance = _FakeLance([])
    assert build_context_for_candidates([], workspace_id=WORKSPACE, db=db, lance=lance) == []


def test_semantic_expansion_can_select_relevant_depth_two_over_depth_one_noise():
    hits = [
        _Hit("bridge", "a_bridge", "/bridge.py", 1, "calls"),
        _Hit("noise", "b_noise", "/noise.py", 1, "calls"),
        _Hit("relevant", "z_relevant", "/relevant.py", 2, "calls"),
    ]
    scoring = _FakeQueryScoring({"bridge": 0.10, "noise": 0.11, "relevant": 0.80})

    selected = _nearest_expansion_hits(
        hits,
        include_tests=True,
        max_per_seed=2,
        query_scoring=scoring,  # type: ignore[arg-type]
        structural_reserve=1,
    )

    assert [hit.uid for hit in selected] == ["bridge", "relevant"]


def test_semantic_expansion_falls_back_to_depth_order_without_scores():
    hits = [
        _Hit("deep", "a_deep", "/deep.py", 2, "calls"),
        _Hit("direct", "z_direct", "/direct.py", 1, "calls"),
    ]

    selected = _nearest_expansion_hits(
        hits,
        include_tests=True,
        max_per_seed=1,
        query_scoring=_FakeQueryScoring({}),  # type: ignore[arg-type]
    )

    assert [hit.uid for hit in selected] == ["direct"]


def test_semantic_expansion_reuses_intent_inverted_test_tier_weight():
    hits = [
        _Hit("floor1", "floor1", "/core/floor1.py", 1, "calls"),
        _Hit("floor2", "floor2", "/core/floor2.py", 1, "calls"),
        _Hit("floor3", "floor3", "/core/floor3.py", 1, "calls"),
        _Hit("core", "core", "/core/answer.py", 1, "calls"),
        _Hit("test", "test", "/tests/test_answer.py", 1, "calls"),
    ]
    scoring = _FakeQueryScoring(
        {"floor1": 0.10, "floor2": 0.11, "floor3": 0.12, "core": 0.70, "test": 0.80}
    )

    normal = _nearest_expansion_hits(
        hits,
        include_tests=True,
        max_per_seed=1,
        query_scoring=scoring,  # type: ignore[arg-type]
        structural_reserve=0,
        impact_mode=False,
    )
    impact = _nearest_expansion_hits(
        hits,
        include_tests=True,
        max_per_seed=1,
        query_scoring=scoring,  # type: ignore[arg-type]
        structural_reserve=0,
        impact_mode=True,
    )

    assert [hit.uid for hit in normal] == ["core"]
    assert [hit.uid for hit in impact] == ["test"]


def test_related_symbols_keep_request_local_semantic_annotations():
    candidate = _make_candidate("u:seed", "seed")
    db = _FakeDB(
        [
            [
                _hit_record(
                    "u:seed",
                    "u:test",
                    "test_helper",
                    "/tests/test_helper.py",
                    "binding_structure_expansion",
                    1,
                )
            ],
            [],
        ]
    )
    lance = _FakeLance(
        [
            _lance_row("u:seed", "def seed(): pass"),
            _lance_row("u:test", "def test_helper(): pass"),
        ]
    )

    [bundle] = build_context_for_candidates(
        [candidate],
        workspace_id=WORKSPACE,
        db=db,
        lance=lance,
        include_tests=True,
        query_scoring=_FakeQueryScoring({"u:test": 0.8}),  # type: ignore[arg-type]
    )

    related = bundle.related[0]
    assert related.query_similarity == pytest.approx(0.8)
    assert related.tier_weight == pytest.approx(0.15)
    assert related.structural_weight == pytest.approx(1.0)
    assert related.utility_score == pytest.approx(0.15)


def test_seed_carries_code_and_zero_depth():
    candidate = _make_candidate("u:seed", "registry")
    # Two queries are issued — one per expansion step in
    # ``deferred_binding_flow`` (binding_structure_expansion +
    # deferred_runtime_dispatch). Both return nothing for this test.
    db = _FakeDB([[], []])
    lance = _FakeLance([_lance_row("u:seed", "code-of-seed")])

    bundles = build_context_for_candidates([candidate], workspace_id=WORKSPACE, db=db, lance=lance)

    assert len(bundles) == 1
    bundle = bundles[0]
    assert bundle.role == "binding_surface"
    assert bundle.seed.uid == "u:seed"
    assert bundle.seed.code == "code-of-seed"
    assert bundle.seed.distance_from_seed == 0
    assert bundle.seed.expansion_step is None
    assert bundle.related == ()


def test_symbol_ranges_thread_from_graph_spans():
    candidate = _make_candidate("u:seed", "registry")
    helper_path = axis_test_file_path("helper")
    db = _FakeDB(
        [
            [
                _hit_record(
                    "u:seed",
                    "u:helper",
                    "helper",
                    helper_path,
                    "binding_structure_expansion",
                    1,
                )
            ],
            [],
        ],
        spans={
            "u:seed": {
                "name": "registry",
                "file_path": candidate.file_path,
                "start_line": 12,
                "end_line": 14,
            },
            "u:helper": {
                "name": "helper",
                "file_path": helper_path,
                "start_line": 37,
                "end_line": 40,
            },
        },
    )
    lance = _FakeLance(
        [
            _lance_row("u:seed", "code-of-seed"),
            _lance_row("u:helper", "code-of-helper"),
        ]
    )

    [bundle] = build_context_for_candidates([candidate], workspace_id=WORKSPACE, db=db, lance=lance)

    assert bundle.seed.start_line == 12
    assert bundle.seed.end_line == 14
    assert bundle.related[0].start_line == 37
    assert bundle.related[0].end_line == 40
    assert bundle.to_dict()["related"][0]["start_line"] == 37


def test_flat_impact_candidate_preserves_directional_metadata_and_utility():
    candidate = replace(
        _make_candidate("u:caller", "caller", role="impact_analysis", score=0.35),
        satisfying_contracts=(),
        satisfying_kinds=("reverse_calls",),
        contract_count=0,
        kind_count=1,
        depth=2,
        edge_type="CALLS_*",
        utility_score=0.87,
    )
    db = _FakeDB([])
    lance = _FakeLance([_lance_row("u:caller", "def caller(): target()")])

    [bundle] = build_context_for_candidates(
        [candidate],
        workspace_id=WORKSPACE,
        db=db,
        lance=lance,
        traversal_mode=None,
    )

    assert db._session.runs == []
    assert bundle.utility_score == pytest.approx(0.87)
    assert bundle.seed.kind == "reverse_calls"
    assert bundle.seed.direction == "caller"
    assert bundle.seed.edge_type == "CALLS_*"
    assert bundle.seed.distance_from_seed == 2


def test_expanded_related_symbols_carry_step_and_depth():
    candidate = _make_candidate("u:app", "app", role="routing_surface")
    db = _FakeDB(
        [
            # Step 1: binding-structure expansion returns one HANDLES hit.
            [
                _hit_record(
                    seed_uid="u:app",
                    uid="u:handler",
                    name="handler",
                    file_path=axis_test_file_path("app"),
                    step="binding_structure_expansion",
                    depth=1,
                )
            ],
            # Step 2: runtime dispatch — empty.
            [],
        ]
    )
    lance = _FakeLance(
        [
            _lance_row("u:app", "app = Flask(__name__)"),
            _lance_row("u:handler", "def handler(): return 'ok'"),
        ]
    )

    [bundle] = build_context_for_candidates([candidate], workspace_id=WORKSPACE, db=db, lance=lance)

    assert len(bundle.related) == 1
    related = bundle.related[0]
    assert related.uid == "u:handler"
    assert related.distance_from_seed == 1
    assert related.expansion_step == "binding_structure_expansion"
    assert related.code == "def handler(): return 'ok'"


def test_duplicate_hits_across_expansion_steps_collapse_to_shallowest():
    """Same neighbour can be reached through both expansion steps
    (binding-structure first, then runtime-dispatch). The context
    builder must keep the shallowest occurrence and report only one
    related symbol per uid.
    """
    candidate = _make_candidate("u:seed", "seed")
    db = _FakeDB(
        [
            # Step 1: binding-structure expansion finds neighbour at depth 1.
            [
                _hit_record(
                    seed_uid="u:seed",
                    uid="u:n",
                    name="neighbour",
                    file_path=axis_test_file_path("x"),
                    step="binding_structure_expansion",
                    depth=1,
                ),
            ],
            # Step 2: runtime dispatch also reaches it, deeper (depth 2).
            [
                _hit_record(
                    seed_uid="u:seed",
                    uid="u:n",
                    name="neighbour",
                    file_path=axis_test_file_path("x"),
                    step="deferred_runtime_dispatch",
                    depth=2,
                ),
            ],
        ]
    )
    lance = _FakeLance(
        [
            _lance_row("u:seed", "seed code"),
            _lance_row("u:n", "neighbour code"),
        ]
    )

    [bundle] = build_context_for_candidates([candidate], workspace_id=WORKSPACE, db=db, lance=lance)

    assert len(bundle.related) == 1
    assert bundle.related[0].distance_from_seed == 1
    assert bundle.related[0].expansion_step == "binding_structure_expansion"


def test_max_per_seed_caps_related_count():
    candidate = _make_candidate("u:seed", "seed")
    hits = [
        _hit_record(
            seed_uid="u:seed",
            uid=f"u:n{i}",
            name=f"neighbour{i}",
            file_path=axis_test_file_path("x"),
            step="binding_structure_expansion",
            depth=1,
        )
        for i in range(10)
    ]
    db = _FakeDB([hits, []])
    lance = _FakeLance(
        [_lance_row("u:seed", "seed")] + [_lance_row(f"u:n{i}", f"code{i}") for i in range(10)]
    )

    [bundle] = build_context_for_candidates(
        [candidate],
        workspace_id=WORKSPACE,
        db=db,
        lance=lance,
        max_per_seed=4,
    )

    assert len(bundle.related) == 4


def test_evidence_graph_fanout_policy_is_reduce_only():
    weak = _make_candidate("u:weak", "weak")
    supported = replace(weak, retrieval_channels=("vector",))
    consensus = replace(weak, retrieval_channels=("vector", "lexical"))
    exact = replace(weak, exact_symbol_match=True)
    anchor = replace(weak, role="anchor_symbol", supporting_roles=("binding_surface",))

    assert _evidence_graph_fanout_limit(
        weak, rank=1, max_per_seed=4, min_per_seed=2, protected_head=5
    ) == (4, "ranked_head")
    assert _evidence_graph_fanout_limit(
        weak, rank=6, max_per_seed=4, min_per_seed=2, protected_head=5
    ) == (2, "weak_tail")
    assert _evidence_graph_fanout_limit(
        supported, rank=6, max_per_seed=4, min_per_seed=2, protected_head=5
    ) == (4, "supported_tail")
    assert _evidence_graph_fanout_limit(
        weak, rank=6, max_per_seed=6, min_per_seed=2, protected_head=5
    ) == (3, "weak_tail")
    assert _evidence_graph_fanout_limit(
        consensus, rank=6, max_per_seed=4, min_per_seed=2, protected_head=5
    ) == (4, "strong_consensus")
    assert _evidence_graph_fanout_limit(
        exact, rank=6, max_per_seed=4, min_per_seed=2, protected_head=5
    ) == (4, "protected")
    assert _evidence_graph_fanout_limit(
        anchor, rank=6, max_per_seed=4, min_per_seed=2, protected_head=5
    ) == (4, "protected")


def test_evidence_graph_fanout_reduces_only_weak_ranked_tail():
    candidates = [_make_candidate(f"u:s{i}", f"seed{i}") for i in range(1, 9)]
    candidates[6] = replace(candidates[6], retrieval_channels=("vector",))
    candidates[7] = replace(candidates[7], retrieval_channels=("vector", "lexical"))
    hits = [
        _hit_record(
            seed_uid=candidate.uid,
            uid=f"{candidate.uid}:n{index}",
            name=f"neighbour{index}",
            file_path=axis_test_file_path(f"n{index}"),
            step="binding_structure_expansion",
            depth=1,
        )
        for candidate in candidates
        for index in range(4)
    ]
    db = _FakeDB([hits, []])
    trace = TokenCreditTrace()
    lance = _FakeLance(
        [_lance_row(candidate.uid, f"{candidate.name} code") for candidate in candidates]
        + [
            _lance_row(f"{candidate.uid}:n{index}", f"related {index}")
            for candidate in candidates
            for index in range(4)
        ]
    )

    bundles = build_context_for_candidates(
        candidates,
        workspace_id=WORKSPACE,
        db=db,
        lance=lance,
        max_per_seed=4,
        evidence_graph_fanout=True,
        evidence_graph_fanout_min=2,
        evidence_graph_fanout_protected_head=5,
        credit_trace=trace,
    )

    assert [len(bundle.related) for bundle in bundles] == [4, 4, 4, 4, 4, 2, 4, 4]
    assert len(db._session.runs) == 2
    for _, params in db._session.runs:
        assert params["limit_per_seed"] == 16
        assert params["limit_per_seed_by_uid"]["u:s6"] == 8
        assert params["limit_per_seed_by_uid"]["u:s7"] == 16
    assert trace.graph_fanout_tier_counts == {
        "ranked_head": 5,
        "weak_tail": 1,
        "supported_tail": 1,
        "strong_consensus": 1,
    }
    assert trace.graph_fanout_limit_counts == {4: 7, 2: 1}


def test_each_candidate_expands_independently():
    cand_a = _make_candidate("u:A", "A")
    cand_b = _make_candidate("u:B", "B")
    db = _FakeDB(
        [
            # binding step — ONE batched grouped walk over all seeds; each
            # seed's neighbour is grouped back by seed_uid.
            [
                _hit_record(
                    "u:A",
                    "u:Anbr",
                    "Anbr",
                    axis_test_file_path("a"),
                    "binding_structure_expansion",
                    1,
                ),
                _hit_record(
                    "u:B",
                    "u:Bnbr",
                    "Bnbr",
                    axis_test_file_path("b"),
                    "binding_structure_expansion",
                    1,
                ),
            ],
            # runtime step — batched, empty
            [],
        ]
    )
    lance = _FakeLance(
        [
            _lance_row("u:A", "A-code"),
            _lance_row("u:Anbr", "Anbr-code"),
            _lance_row("u:B", "B-code"),
            _lance_row("u:Bnbr", "Bnbr-code"),
        ]
    )

    bundles = build_context_for_candidates(
        [cand_a, cand_b], workspace_id=WORKSPACE, db=db, lance=lance
    )

    assert [b.seed.uid for b in bundles] == ["u:A", "u:B"]
    assert bundles[0].related[0].uid == "u:Anbr"
    assert bundles[1].related[0].uid == "u:Bnbr"


def test_to_dict_round_trip_keeps_all_fields():
    candidate = _make_candidate("u:seed", "seed", role="routing_surface")
    db = _FakeDB([[], []])
    lance = _FakeLance([_lance_row("u:seed", "code")])

    [bundle] = build_context_for_candidates([candidate], workspace_id=WORKSPACE, db=db, lance=lance)

    d = bundle.to_dict()
    assert d["role"] == "routing_surface"
    assert d["seed"]["uid"] == "u:seed"
    assert d["seed"]["distance_from_seed"] == 0
    assert d["seed"]["code"] == "code"
    assert d["seed"]["qualified_name"] == ""
    assert d["related"] == []


def test_build_context_threads_qualified_name_for_fold_render():
    candidate = _make_candidate(
        "u:target",
        "target",
        role="binding_surface",
        qualified_name="pkg.mod.Service.target",
    )
    db = _FakeDB(
        [
            [
                _hit_record(
                    "u:target",
                    "u:helper",
                    "helper",
                    axis_test_file_path("service"),
                    "binding_structure_expansion",
                    1,
                )
            ],
            [],
        ]
    )
    lance = _FakeLance(
        [
            _lance_row(
                "u:target",
                "    def target(self):\n        return self.helper()\n",
                "pkg.mod.Service.target",
            ),
            _lance_row(
                "u:helper",
                "    def helper(self):\n        return 1\n",
                "pkg.mod.Service.helper",
            ),
        ]
    )

    [bundle] = build_context_for_candidates(
        [candidate],
        workspace_id=WORKSPACE,
        db=db,
        lance=lance,
        render_budget=ContextRenderBudget(render_mode="fold"),
    )

    assert bundle.seed.name == "target"
    assert bundle.seed.qualified_name == "pkg.mod.Service.target"
    assert {(owner.uid, owner.qualified_name) for owner in bundle.seed.represented_owners} == {
        ("u:target", "pkg.mod.Service.target"),
        ("u:helper", "pkg.mod.Service.helper"),
    }
    assert bundle.related == ()
    assert "def target(self):" in (bundle.seed.code or "")
    assert "return self.helper()" in (bundle.seed.code or "")
    assert "def helper(self):" in (bundle.seed.code or "")
    assert "return 1" not in (bundle.seed.code or "")
