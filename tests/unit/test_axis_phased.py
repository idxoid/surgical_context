"""Phased reactive traversal — REGISTRY*→CONTROL FSM."""

from __future__ import annotations

import json

from context_engine.axis.axis_phased import expand_phased
from context_engine.axis.role_retrieval import RoleCandidate

WORKSPACE = "qa_repo/test@axis"


class _Result:
    def __init__(self, records):
        self._records = list(records)

    def __iter__(self):
        return iter(self._records)


class _Session:
    def __init__(self, records_by_call):
        self._records = list(records_by_call)
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query, **params):
        self.queries.append(query)
        return _Result(self._records.pop(0) if self._records else [])


class _Driver:
    def __init__(self, session):
        self._s = session

    def session(self):
        return self._s


class _FakeDB:
    def __init__(self, records_by_call=None):
        self.s = _Session(records_by_call or [])
        self.driver = _Driver(self.s)


class _LanceTable:
    def __init__(self, rows):
        self._rows = rows

    def to_lance(self):
        outer = self

        class _L:
            def to_table(self, columns=None):
                class _A:
                    def to_pylist(self_):
                        return list(outer._rows)

                return _A()

        return _L()


class _FakeLance:
    def __init__(self, rows):
        self._sym_table = _LanceTable(rows)


def _seed(uid, kinds=()):
    return RoleCandidate(
        uid=uid,
        name=uid.split(":")[-1],
        file_path=f"/r/{uid}.py",
        role="?",
        satisfying_contracts=(),
        satisfying_kinds=tuple(kinds),
        contract_count=0,
        kind_count=0,
        vector_distance=None,
        score=1.0,
    )


def _lance_kind_row(uid, kinds):
    return {
        "uid": uid,
        "axis_container_kinds_json": json.dumps([{"kind": k} for k in kinds]),
        "workspace_id": WORKSPACE,
    }


def _wrow(uid, name="n", path="/r/x.py", depth=1, reach=1):
    return {"uid": uid, "name": name, "file_path": path, "depth": depth, "reach": reach}


def test_no_seeds_empty():
    assert expand_phased([], db=_FakeDB(), lance=_FakeLance([]), workspace_id=WORKSPACE) == []


def test_two_phases_tagged():
    """A registry seed runs discovery (REGISTRY/STRUCTURAL) then
    execution (CONTROL); outputs carry phase tags."""
    seed = _seed("u:router")
    lance = _FakeLance([_lance_kind_row("u:router", ["web_route_register"])])
    # call 1 = discovery walk, call 2 = execution walk
    db = _FakeDB(
        [
            [_wrow("u:handler", "handler", "/r/routing.py")],
            [_wrow("u:logic", "logic", "/r/deps.py")],
        ]
    )
    out = expand_phased(
        [seed],
        db=db,
        lance=lance,
        workspace_id=WORKSPACE,
    )
    tags = {c.uid: c.satisfying_kinds[0] for c in out}
    assert tags["u:handler"] == "phase_discovery"
    assert tags["u:logic"] == "phase_execution"


def test_discovery_axis_is_reactive_to_kind():
    """A ``data_model`` seed has only STRUCTURAL nature — discovery
    must walk the structural edges, not registry ones. We assert the
    discovery Cypher carries a STRUCTURAL edge and not a REGISTRY-only
    one."""
    seed = _seed("u:model")
    lance = _FakeLance([_lance_kind_row("u:model", ["data_model"])])
    db = _FakeDB([[], []])
    expand_phased([seed], db=db, lance=lance, workspace_id=WORKSPACE)
    discovery_q = db.s.queries[0]
    assert "DEPENDS_ON" in discovery_q  # structural edge present
    assert "DECORATED_BY" not in discovery_q  # registry-only edge absent


def test_no_discovery_kind_falls_back_to_structural():
    """A seed whose kinds carry no REGISTRY/STRUCTURAL nature still gets
    a discovery channel (STRUCTURAL fallback) rather than collapsing."""
    seed = _seed("u:x")
    lance = _FakeLance([_lance_kind_row("u:x", ["middleware_chain"])])  # CONTROL-only
    db = _FakeDB([[], []])
    expand_phased([seed], db=db, lance=lance, workspace_id=WORKSPACE)
    assert "DEPENDS_ON" in db.s.queries[0]  # structural fallback used


def test_execution_walks_control_from_seed_and_frontier():
    """Execution must seed from the originals plus discovery frontier so
    it can fall past the entrypoint into called code."""
    seed = _seed("u:router")
    lance = _FakeLance([_lance_kind_row("u:router", ["web_route_register"])])
    db = _FakeDB(
        [
            [_wrow("u:handler")],  # discovery frontier
            [_wrow("u:called")],  # execution
        ]
    )
    expand_phased([seed], db=db, lance=lance, workspace_id=WORKSPACE)
    exec_q = db.s.queries[1]
    assert "CALLS" in exec_q  # control axis


def test_caps_respected_per_phase():
    seed = _seed("u:router")
    lance = _FakeLance([_lance_kind_row("u:router", ["web_route_register"])])
    db = _FakeDB(
        [
            [_wrow(f"u:d{i}", path=f"/r/d{i}.py") for i in range(20)],
            [_wrow(f"u:e{i}", path=f"/r/e{i}.py") for i in range(20)],
        ]
    )
    out = expand_phased(
        [seed],
        db=db,
        lance=lance,
        workspace_id=WORKSPACE,
        max_discovery=3,
        max_execution=4,
    )
    disc = [c for c in out if c.satisfying_kinds == ("phase_discovery",)]
    exe = [c for c in out if c.satisfying_kinds == ("phase_execution",)]
    assert len(disc) == 3 and len(exe) == 4


def test_seeds_excluded_from_output():
    seed = _seed("u:router")
    lance = _FakeLance([_lance_kind_row("u:router", ["web_route_register"])])
    db = _FakeDB(
        [
            [_wrow("u:router")],  # discovery returns the seed itself
            [_wrow("u:other")],
        ]
    )
    out = expand_phased([seed], db=db, lance=lance, workspace_id=WORKSPACE)
    assert "u:router" not in {c.uid for c in out}
