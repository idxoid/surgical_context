"""Upward inheritance walk — surface abstract bases via DEPENDS_ON."""

from __future__ import annotations

from sidecar.axis.inheritance_ancestors import expand_inheritance_ancestors
from sidecar.axis.role_retrieval import RoleCandidate

WORKSPACE = "qa_repo/test@axis"


class _Result:
    def __init__(self, records):
        self._records = list(records)

    def __iter__(self):
        return iter(self._records)


class _Session:
    def __init__(self, records):
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
    def __init__(self, records=None):
        self._session = _Session(records or [])
        self.driver = _Driver(self._session)


def _seed(uid: str, file_path: str) -> RoleCandidate:
    return RoleCandidate(
        uid=uid,
        name=uid.split(":")[-1],
        file_path=file_path,
        role="dispatch_surface",
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
    assert expand_inheritance_ancestors([], db=_FakeDB(), workspace_id=WORKSPACE) == []


def test_surfaces_ancestor_in_different_file():
    """The canonical case: ``concurrency/prefork.py:TaskPool`` walks
    ``DEPENDS_ON`` to ``concurrency/base.py:BasePool``; the ancestor
    lives in a different file, so the pass surfaces it."""
    seed = _seed("u:taskpool", "/repo/celery/concurrency/prefork.py")
    db = _FakeDB(
        [
            _row(
                "u:basepool",
                "BasePool",
                "/repo/celery/concurrency/base.py",
                depth=1,
            )
        ]
    )
    out = expand_inheritance_ancestors(
        [seed],
        db=db,
        workspace_id=WORKSPACE,
    )
    assert len(out) == 1
    c = out[0]
    assert c.uid == "u:basepool"
    assert c.file_path == "/repo/celery/concurrency/base.py"
    assert c.role == "structural_neighbour"
    assert c.satisfying_kinds == ("inheritance_ancestor",)


def test_skips_ancestor_in_seed_file():
    """An ancestor in the seed's own file is already represented by
    the seed — no need to re-add it under a different role tag."""
    seed = _seed("u:concrete", "/repo/celery/concurrency/prefork.py")
    db = _FakeDB(
        [
            _row(
                "u:also_in_prefork",
                "Helper",
                "/repo/celery/concurrency/prefork.py",
                depth=1,
            )
        ]
    )
    out = expand_inheritance_ancestors(
        [seed],
        db=db,
        workspace_id=WORKSPACE,
    )
    assert out == []


def test_max_files_caps_distinct_file_count():
    seed = _seed("u:concrete", "/repo/celery/concurrency/prefork.py")
    rows = [_row(f"u:base{i}", f"Base{i}", f"/repo/celery/base_{i}.py", depth=1) for i in range(8)]
    db = _FakeDB(rows)
    out = expand_inheritance_ancestors(
        [seed],
        db=db,
        workspace_id=WORKSPACE,
        max_files=3,
        max_total=10,
    )
    assert len({c.file_path for c in out}) == 3


def test_max_total_caps_pool_size():
    seed = _seed("u:concrete", "/repo/celery/concurrency/prefork.py")
    rows = [_row(f"u:base{i}", f"Base{i}", f"/repo/celery/base_{i}.py", depth=1) for i in range(20)]
    db = _FakeDB(rows)
    out = expand_inheritance_ancestors(
        [seed],
        db=db,
        workspace_id=WORKSPACE,
        max_files=50,
        max_total=4,
    )
    assert len(out) == 4


def test_exclude_uids_dropped():
    seed = _seed("u:concrete", "/repo/celery/concurrency/prefork.py")
    db = _FakeDB(
        [
            _row("u:dup", "Dup", "/repo/celery/concurrency/base.py", depth=1),
            _row("u:other", "Other", "/repo/celery/other/base.py", depth=2),
        ]
    )
    out = expand_inheritance_ancestors(
        [seed],
        db=db,
        workspace_id=WORKSPACE,
        exclude_uids=["u:dup"],
    )
    assert [c.uid for c in out] == ["u:other"]


# Cypher-injection defence now lives in the shared graph_walk core —
# see tests/unit/test_axis_graph_walk.py::test_safe_rel_pattern_rejects_injection.
