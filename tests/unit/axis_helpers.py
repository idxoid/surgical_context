"""Shared Neo4j/Lance fakes and factories for axis unit tests."""

from __future__ import annotations

import json
from typing import Any

from context_engine.axis.role_retrieval import RoleCandidate

AXIS_TEST_WORKSPACE = "qa_repo/test@axis"
AXIS_TEST_FILE_ROOT = "qa_repo/test_project"
BAD_MAX_HOPS = [0, -1, 1.5, "2", True]


def axis_test_file_path(name: str) -> str:
    """Synthetic repo-relative path for axis unit tests (never written to disk)."""
    return f"{AXIS_TEST_FILE_ROOT}/{name}.py"


class Neo4jResult:
    def __init__(self, records):
        self._records = list(records)

    def __iter__(self):
        return iter(self._records)


class Neo4jSession:
    def __init__(self, records, *, queued: bool = False):
        self._queued = queued
        self._records = list(records)
        self.runs: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query: str, **params):
        self.runs.append((query, dict(params)))
        if self._queued:
            records = self._records.pop(0) if self._records else []
        else:
            records = self._records
        return Neo4jResult(records)


class Neo4jDriver:
    def __init__(self, session: Neo4jSession):
        self._session = session

    def session(self):
        return self._session


class FakeNeo4jDB:
    def __init__(self, records=None, *, queued: bool = False):
        self._session = Neo4jSession(records or [], queued=queued)
        self.session_obj = self._session
        self.driver = Neo4jDriver(self._session)


def make_role_candidate(
    uid: str,
    *,
    name: str | None = None,
    file_path: str | None = None,
    role: str = "dispatch_surface",
    score: float = 0.5,
    satisfying_kinds: tuple[str, ...] = (),
    kind_count: int = 0,
) -> RoleCandidate:
    return RoleCandidate(
        uid=uid,
        name=name or uid.split(":")[-1],
        file_path=file_path or axis_test_file_path(uid.split(":")[-1]),
        role=role,
        satisfying_contracts=(),
        satisfying_kinds=satisfying_kinds,
        contract_count=0,
        kind_count=kind_count,
        vector_distance=None,
        score=score,
    )


def graph_row(
    uid: str,
    name: str,
    file_path: str,
    *,
    depth: int = 1,
    reach: int = 1,
) -> dict:
    row = {
        "uid": uid,
        "name": name,
        "file_path": file_path,
        "depth": depth,
    }
    if reach != 1:
        row["reach"] = reach
    return row


def walk_rows(uids: list[str], *, reach: int = 1, depth: int = 1) -> list[dict]:
    return [
        {
            "uid": u,
            "name": u.split(":")[-1],
            "file_path": axis_test_file_path(u.split(":")[-1]),
            "depth": depth,
            "reach": reach,
        }
        for u in uids
    ]


class FakeLanceTable:
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


def lance_kind_row(uid: str, *, kinds: list[str], name: str = "n") -> dict[str, Any]:
    return {
        "uid": uid,
        "name": name,
        "file_path": axis_test_file_path(name),
        "axis_container_kinds_json": json.dumps([{"kind": k} for k in kinds]),
        "container_kinds": kinds,
        "workspace_id": AXIS_TEST_WORKSPACE,
    }


class FakeLanceDB:
    def __init__(self, rows: list[dict[str, Any]]):
        self._sym_table = FakeLanceTable(rows)
