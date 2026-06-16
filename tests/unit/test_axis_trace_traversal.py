"""Trace-dependency traversal -- CALLS-only call-chain walk."""

from __future__ import annotations

from sidecar.axis.role_retrieval import RoleCandidate
from sidecar.axis.trace_traversal import expand_trace_neighbourhood

WORKSPACE = "qa_repo/test@axis"


class _Result:
    def __init__(self, records):
        self._records = list(records)

    def __iter__(self):
        return iter(self._records)


class _Session:
    """Fake Neo4j session -- trace traversal issues two queries:
    reverse CALLS, then forward CALLS."""

    def __init__(self, records_by_call: list[list[dict]]):
        self._records = list(records_by_call)
        self.runs: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query: str, **params):
        self.runs.append((query, dict(params)))
        records = self._records.pop(0) if self._records else []
        return _Result(records)


class _Driver:
    def __init__(self, session):
        self._session = session

    def session(self):
        return self._session


class _FakeDB:
    def __init__(self, records_by_call=None):
        self._session = _Session(records_by_call or [])
        self.driver = _Driver(self._session)


def _seed(uid: str, *, role: str = "dispatch_surface") -> RoleCandidate:
    return RoleCandidate(
        uid=uid,
        name=uid.split(":")[-1],
        file_path=f"/tmp/{uid}.py",
        role=role,
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=None,
        score=0.5,
    )


def _record(uid: str, *, name: str = "x", path: str = "/tmp/x.py") -> dict:
    return {"uid": uid, "name": name, "file_path": path}


def test_no_seeds_returns_empty():
    assert expand_trace_neighbourhood([], db=_FakeDB(), workspace_id=WORKSPACE) == []


def test_reverse_calls_emit_trace_callers():
    db = _FakeDB(
        [
            [_record("u:caller", name="caller")],
            [],
        ]
    )

    out = expand_trace_neighbourhood(
        [_seed("u:target")],
        db=db,
        workspace_id=WORKSPACE,
    )

    assert len(out) == 1
    assert out[0].uid == "u:caller"
    assert out[0].role == "trace_dependency"
    assert out[0].satisfying_kinds == ("trace_callers",)


def test_forward_calls_emit_trace_callees():
    db = _FakeDB(
        [
            [],
            [_record("u:callee", name="callee")],
        ]
    )

    out = expand_trace_neighbourhood(
        [_seed("u:target")],
        db=db,
        workspace_id=WORKSPACE,
    )

    assert len(out) == 1
    assert out[0].uid == "u:callee"
    assert out[0].role == "trace_dependency"
    assert out[0].satisfying_kinds == ("trace_callees",)


def test_trace_walks_only_calls_edges():
    db = _FakeDB([[], []])
    expand_trace_neighbourhood(
        [_seed("u:target")],
        db=db,
        workspace_id=WORKSPACE,
    )

    assert len(db._session.runs) == 2
    joined = "\n".join(query for query, _params in db._session.runs)
    assert "CALLS_DIRECT" in joined
    assert "CALLS_SCOPED" in joined
    assert "AFFECTS" not in joined
    assert "HAS_API" not in joined
    assert "INHERITED_API" not in joined


def test_duplicate_uids_keep_caller_tag_first():
    db = _FakeDB(
        [
            [_record("u:both")],
            [_record("u:both")],
        ]
    )

    out = expand_trace_neighbourhood(
        [_seed("u:target")],
        db=db,
        workspace_id=WORKSPACE,
    )

    assert len(out) == 1
    assert out[0].satisfying_kinds == ("trace_callers",)


def test_seed_and_excluded_uids_are_skipped():
    db = _FakeDB(
        [
            [_record("u:target"), _record("u:skip"), _record("u:keep")],
            [],
        ]
    )

    out = expand_trace_neighbourhood(
        [_seed("u:target")],
        db=db,
        workspace_id=WORKSPACE,
        exclude_uids=["u:skip"],
    )

    assert [c.uid for c in out] == ["u:keep"]


def test_max_traced_caps_pool_size():
    db = _FakeDB(
        [
            [_record(f"u:c{i}") for i in range(20)],
            [],
        ]
    )

    out = expand_trace_neighbourhood(
        [_seed("u:target")],
        db=db,
        workspace_id=WORKSPACE,
        max_traced=5,
    )

    assert len(out) == 5
