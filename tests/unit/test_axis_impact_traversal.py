"""Impact-traversal — blast-radius walk over CALLS / AFFECTS / API."""

from __future__ import annotations

from typing import Any

import pytest

from sidecar.axis.impact_traversal import expand_impact_neighbourhood
from sidecar.axis.role_retrieval import RoleCandidate


WORKSPACE = "qa_repo/test@axis"


class _Result:
    def __init__(self, records):
        self._records = list(records)

    def __iter__(self):
        return iter(self._records)


class _Session:
    """Fake Neo4j session — pops a canned record set per ``run`` call.

    The impact traversal issues four queries in fixed order:
      1) reverse CALLS
      2) forward AFFECTS
      3) structural reverse (EXTENDS_EXTERNAL / INHERITED_API)
      4) structural forward (HAS_API)
    """

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
    out = expand_impact_neighbourhood(
        [], db=_FakeDB(), workspace_id=WORKSPACE
    )
    assert out == []


def test_reverse_calls_pass_emits_callers():
    """The reverse-CALLS walk surfaces who calls each seed. For
    ``Flask.dispatch_request`` this is ``full_dispatch_request``;
    one record's enough to confirm the caller is tagged
    ``reverse_calls``."""
    db = _FakeDB(
        [
            [_record("u:caller", name="full_dispatch_request", path="/tmp/app.py")],
            [],  # forward AFFECTS
            [],  # structural reverse
            [],  # structural forward
        ]
    )

    out = expand_impact_neighbourhood(
        [_seed("u:dispatch")], db=db, workspace_id=WORKSPACE,
    )

    assert len(out) == 1
    assert out[0].uid == "u:caller"
    assert out[0].role == "impact_analysis"
    assert out[0].satisfying_kinds == ("reverse_calls",)


def test_forward_affects_pass_emits_impact_closure():
    db = _FakeDB(
        [
            [],
            [_record("u:downstream", path="/tmp/handlers.py")],
            [],
            [],
        ]
    )

    out = expand_impact_neighbourhood(
        [_seed("u:dispatch")], db=db, workspace_id=WORKSPACE,
    )

    assert [c.uid for c in out] == ["u:downstream"]
    assert out[0].satisfying_kinds == ("forward_affects",)


def test_structural_reverse_pass_emits_inheritors():
    db = _FakeDB(
        [
            [],
            [],
            [_record("u:subclass")],
            [],
        ]
    )

    out = expand_impact_neighbourhood(
        [_seed("u:base")], db=db, workspace_id=WORKSPACE,
    )

    assert [c.satisfying_kinds for c in out] == [("structural_inheritor",)]


def test_structural_forward_pass_emits_api_carriers():
    db = _FakeDB(
        [
            [],
            [],
            [],
            [_record("u:carrier")],
        ]
    )

    out = expand_impact_neighbourhood(
        [_seed("u:base")], db=db, workspace_id=WORKSPACE,
    )

    assert [c.satisfying_kinds for c in out] == [("structural_api_carrier",)]


def test_seeds_are_never_in_impact_pool():
    """A seed reached through its own AFFECTS edge must not appear in
    the impact pool — the pool is *new* symbols only, otherwise the
    caller would deduplicate twice."""
    db = _FakeDB(
        [
            [_record("u:dispatch")],  # seed is its own caller (loop)
            [_record("u:dispatch")],  # seed in its own affects closure
            [],
            [],
        ]
    )
    out = expand_impact_neighbourhood(
        [_seed("u:dispatch")], db=db, workspace_id=WORKSPACE,
    )
    assert out == []


def test_explicit_exclude_uids_skipped():
    """An external dedup list lets the consumer keep its own
    candidates out of the impact pool."""
    db = _FakeDB(
        [
            [_record("u:caller"), _record("u:other")],
            [],
            [],
            [],
        ]
    )
    out = expand_impact_neighbourhood(
        [_seed("u:dispatch")],
        db=db,
        workspace_id=WORKSPACE,
        exclude_uids=["u:caller"],
    )
    assert [c.uid for c in out] == ["u:other"]


def test_duplicate_uids_collapsed_to_first_tag():
    """If two passes reach the same uid, keep the *first* tag — the
    earlier pass usually carries the stronger structural signal
    (reverse_calls is the strongest, forward_affects is its fallback)."""
    db = _FakeDB(
        [
            [_record("u:dup", path="/tmp/a.py")],
            [_record("u:dup", path="/tmp/a.py")],  # same uid via AFFECTS
            [],
            [],
        ]
    )
    out = expand_impact_neighbourhood(
        [_seed("u:dispatch")], db=db, workspace_id=WORKSPACE,
    )
    assert len(out) == 1
    assert out[0].satisfying_kinds == ("reverse_calls",)


def test_max_impacted_caps_pool_size():
    """A wide AFFECTS closure must not drown the context bundle —
    the cap keeps the impact pool comparable to a vector pool."""
    rev = [_record(f"u:c{i}", path="/tmp/x.py") for i in range(50)]
    db = _FakeDB([rev, [], [], []])
    out = expand_impact_neighbourhood(
        [_seed("u:dispatch")],
        db=db,
        workspace_id=WORKSPACE,
        max_impacted=10,
    )
    assert len(out) == 10


# Cypher-injection defence now lives in the shared graph_walk core —
# see tests/unit/test_axis_graph_walk.py::test_safe_rel_pattern_rejects_injection.
