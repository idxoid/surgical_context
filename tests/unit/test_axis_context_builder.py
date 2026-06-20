"""Context builder — RoleCandidate → ContextBundle expansion."""

from __future__ import annotations

from typing import Any

from context_engine.axis.context_builder import build_context_for_candidates
from context_engine.axis.role_retrieval import RoleCandidate

WORKSPACE = "qa_repo/test@axis"


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
    def __init__(self, session_records: list[list[dict]]):
        self._session = _Session(session_records)
        self.driver = _Driver(self._session)


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
        file_path=f"/tmp/{name}.py",
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
                    file_path="/tmp/app.py",
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
                    file_path="/tmp/x.py",
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
                    file_path="/tmp/x.py",
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
            file_path="/tmp/x.py",
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


def test_each_candidate_expands_independently():
    cand_a = _make_candidate("u:A", "A")
    cand_b = _make_candidate("u:B", "B")
    db = _FakeDB(
        [
            # binding step — ONE batched grouped walk over all seeds; each
            # seed's neighbour is grouped back by seed_uid.
            [
                _hit_record("u:A", "u:Anbr", "Anbr", "/tmp/a.py", "binding_structure_expansion", 1),
                _hit_record("u:B", "u:Bnbr", "Bnbr", "/tmp/b.py", "binding_structure_expansion", 1),
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
                    "/tmp/service.py",
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
        render_mode="fold",
    )

    assert bundle.seed.name == "Service"
    assert bundle.seed.qualified_name == "pkg.mod.Service"
    assert bundle.related == ()
    assert "def target(self):" in (bundle.seed.code or "")
    assert "return self.helper()" in (bundle.seed.code or "")
    assert "def helper(self):" in (bundle.seed.code or "")
    assert "return 1" not in (bundle.seed.code or "")
