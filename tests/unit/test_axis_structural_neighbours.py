"""File-level structural-neighbour expansion via AFFECTS."""

from __future__ import annotations

from context_engine.axis.role_retrieval import RoleCandidate
from context_engine.axis.structural_neighbours import expand_structural_neighbours

WORKSPACE = "qa_repo/test@axis"


class _Result:
    def __init__(self, records):
        self._records = list(records)

    def __iter__(self):
        return iter(self._records)


class _Session:
    def __init__(self, records: list[dict]):
        self._records = list(records)
        self.runs: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query: str, **params):
        self.runs.append((query, dict(params)))
        return _Result(self._records)


class _Driver:
    def __init__(self, session):
        self._session = session

    def session(self):
        return self._session


class _FakeDB:
    def __init__(self, records: list[dict] | None = None):
        self._session = _Session(records or [])
        self.driver = _Driver(self._session)


def _seed(uid: str, *, file_path: str, role: str = "routing_surface") -> RoleCandidate:
    return RoleCandidate(
        uid=uid,
        name=uid.split(":")[-1],
        file_path=file_path,
        role=role,
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=None,
        score=0.5,
    )


def _row(uid: str, name: str, file_path: str, depth: int = 1) -> dict:
    return {
        "uid": uid,
        "name": name,
        "file_path": file_path,
        "depth": depth,
    }


def test_no_seeds_returns_empty():
    assert expand_structural_neighbours([], db=_FakeDB(), workspace_id=WORKSPACE) == []


def test_reaches_new_file_via_affects():
    """Concrete case the user named: seed in ``routing.py`` reaches
    ``concurrency.contextmanager_in_threadpool`` via an undirected
    AFFECTS walk; the symbol is from a *different* file, so it gets
    included."""
    seed = _seed("u:run", file_path="/repo/fastapi/routing.py")
    db = _FakeDB(
        [
            _row(
                "u:ctx",
                "contextmanager_in_threadpool",
                "/repo/fastapi/concurrency.py",
                depth=4,
            ),
        ]
    )
    out = expand_structural_neighbours(
        [seed],
        db=db,
        workspace_id=WORKSPACE,
    )
    assert len(out) == 1
    c = out[0]
    assert c.uid == "u:ctx"
    assert c.role == "structural_neighbour"
    assert c.satisfying_kinds == ("affects_bridge",)
    assert c.file_path == "/repo/fastapi/concurrency.py"


def test_skips_symbols_in_seed_files():
    """The point of this pass is *new* files. A neighbour that lives
    in a seed's own file is already represented by the seed."""
    seed = _seed("u:run", file_path="/repo/fastapi/routing.py")
    db = _FakeDB(
        [
            _row("u:also_in_routing", "n", "/repo/fastapi/routing.py", depth=1),
            _row("u:elsewhere", "n", "/repo/fastapi/encoders.py", depth=2),
        ]
    )
    out = expand_structural_neighbours(
        [seed],
        db=db,
        workspace_id=WORKSPACE,
    )
    assert [c.uid for c in out] == ["u:elsewhere"]


def test_max_files_caps_distinct_file_count():
    seed = _seed("u:run", file_path="/repo/a.py")
    rows = [_row(f"u:n{i}", f"n{i}", f"/repo/file_{i}.py", depth=i + 1) for i in range(8)]
    db = _FakeDB(rows)
    out = expand_structural_neighbours(
        [seed],
        db=db,
        workspace_id=WORKSPACE,
        max_files=3,
        max_per_file=1,
        max_total=20,
    )
    assert len({c.file_path for c in out}) == 3


def test_max_per_file_caps_symbols_per_file():
    seed = _seed("u:run", file_path="/repo/a.py")
    # Five symbols all reached in same neighbour file.
    rows = [_row(f"u:n{i}", f"n{i}", "/repo/shared.py", depth=i + 1) for i in range(5)]
    db = _FakeDB(rows)
    out = expand_structural_neighbours(
        [seed],
        db=db,
        workspace_id=WORKSPACE,
        max_per_file=2,
    )
    assert len(out) == 2


def test_max_total_caps_pool_size():
    seed = _seed("u:run", file_path="/repo/a.py")
    rows = [_row(f"u:n{i}", f"n{i}", f"/repo/file_{i}.py", depth=1) for i in range(20)]
    db = _FakeDB(rows)
    out = expand_structural_neighbours(
        [seed],
        db=db,
        workspace_id=WORKSPACE,
        max_files=50,
        max_per_file=1,
        max_total=5,
    )
    assert len(out) == 5


def test_exclude_uids_dropped():
    seed = _seed("u:run", file_path="/repo/a.py")
    db = _FakeDB(
        [
            _row("u:dup", "n", "/repo/b.py", depth=1),
            _row("u:other", "n2", "/repo/c.py", depth=1),
        ]
    )
    out = expand_structural_neighbours(
        [seed],
        db=db,
        workspace_id=WORKSPACE,
        exclude_uids=["u:dup"],
    )
    assert [c.uid for c in out] == ["u:other"]


# Cypher-injection defence now lives in the shared graph_walk core —
# see tests/unit/test_axis_graph_walk.py::test_safe_rel_pattern_rejects_injection.
