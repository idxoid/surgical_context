"""Role-agnostic vector seed retrieval — intent out of structure."""

from __future__ import annotations

import pytest

import sidecar.axis.role_retrieval as rr
from sidecar.axis.role_retrieval import find_seeds_by_vector


WORKSPACE = "qa_repo/test@axis"


class _Arrow:
    def __init__(self, rows):
        self._rows = rows

    def to_pylist(self):
        return list(self._rows)


class _Lance:
    def __init__(self, rows):
        self._rows = rows

    def to_table(self, columns=None):
        return _Arrow(self._rows)


class _Table:
    def __init__(self, rows):
        self._rows = rows

    def to_lance(self):
        return _Lance(self._rows)


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def open_table(self, name):
        return _Table(self._rows)


def _row(uid, path, vec, ws=WORKSPACE):
    return {"uid": uid, "name": uid, "file_path": path, "vector": vec, "workspace_id": ws}


def _patch(monkeypatch, rows):
    monkeypatch.setattr(rr.lancedb, "connect", lambda *_a, **_k: _Conn(rows))


def test_empty_query_returns_empty(monkeypatch):
    _patch(monkeypatch, [])
    assert find_seeds_by_vector(WORKSPACE, "", embed_fn=lambda t: [0.0]) == []


def test_ranks_by_nearest_vector(monkeypatch):
    rows = [
        _row("u:far", "/a.py", [1.0, 0.0]),
        _row("u:near", "/b.py", [0.0, 1.0]),
    ]
    _patch(monkeypatch, rows)
    out = find_seeds_by_vector(
        WORKSPACE, "q", embed_fn=lambda t: [0.0, 1.0], limit=2,
    )
    assert [c.uid for c in out] == ["u:near", "u:far"]
    assert out[0].role == "vector_seed"


def test_limit_caps_results(monkeypatch):
    rows = [_row(f"u:{i}", f"/{i}.py", [float(i), 0.0]) for i in range(10)]
    _patch(monkeypatch, rows)
    out = find_seeds_by_vector(WORKSPACE, "q", embed_fn=lambda t: [0.0, 0.0], limit=3)
    assert len(out) == 3


def test_no_role_gate_unlike_find_symbols_by_role(monkeypatch):
    """The whole point: a symbol with NO kinds/contracts is still a
    valid seed. find_symbols_by_role would discard it; this must not."""
    rows = [_row("u:plain", "/plain.py", [0.1, 0.2])]
    _patch(monkeypatch, rows)
    out = find_seeds_by_vector(WORKSPACE, "q", embed_fn=lambda t: [0.1, 0.2])
    assert len(out) == 1
    assert out[0].satisfying_kinds == ()
    assert out[0].satisfying_contracts == ()


def test_workspace_isolation(monkeypatch):
    rows = [
        _row("u:mine", "/a.py", [0.0, 1.0], ws=WORKSPACE),
        _row("u:other", "/b.py", [0.0, 1.0], ws="other_ws"),
    ]
    _patch(monkeypatch, rows)
    out = find_seeds_by_vector(WORKSPACE, "q", embed_fn=lambda t: [0.0, 1.0])
    assert [c.uid for c in out] == ["u:mine"]


def test_test_files_fenced_by_default(monkeypatch):
    rows = [
        _row("u:src", "/pkg/core.py", [0.0, 1.0]),
        _row("u:test", "/pkg/tests/test_core.py", [0.0, 1.0]),
    ]
    _patch(monkeypatch, rows)
    out = find_seeds_by_vector(WORKSPACE, "q", embed_fn=lambda t: [0.0, 1.0])
    assert [c.uid for c in out] == ["u:src"]


def test_include_tests_keeps_test_files(monkeypatch):
    rows = [
        _row("u:src", "/pkg/core.py", [0.0, 1.0]),
        _row("u:test", "/pkg/tests/test_core.py", [0.0, 1.0]),
    ]
    _patch(monkeypatch, rows)
    out = find_seeds_by_vector(
        WORKSPACE, "q", embed_fn=lambda t: [0.0, 1.0], include_tests=True,
    )
    assert {c.uid for c in out} == {"u:src", "u:test"}
