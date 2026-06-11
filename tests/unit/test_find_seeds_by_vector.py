"""Role-agnostic vector seed retrieval — intent out of structure."""

from __future__ import annotations

import pyarrow as pa
import pytest

import sidecar.axis.role_retrieval as rr
from sidecar.axis.role_retrieval import find_seeds_by_vector


WORKSPACE = "qa_repo/test@axis"

_SCAN_COLS = [
    "uid",
    "name",
    "file_path",
    "axis_contracts_json",
    "axis_container_kinds_json",
    "workspace_id",
]


def _apply_ws_filter(rows, filter_str):
    """Simulate Lance predicate pushdown for ``workspace_id = '...'``."""
    if not filter_str:
        return rows
    import re

    m = re.search(r"workspace_id = '(.*)'", filter_str)
    if not m:
        return rows
    ws = m.group(1).replace("''", "'")
    return [r for r in rows if r.get("workspace_id") == ws]


def _to_arrow(rows):
    """Build a real pyarrow Table so the scan's .drop/.column/
    combine_chunks/to_pylist path runs against genuine Arrow."""
    data = {c: [r.get(c) for r in rows] for c in _SCAN_COLS}
    dim = len(rows[0]["vector"]) if rows else 0
    data["vector"] = pa.array(
        [r["vector"] for r in rows],
        type=pa.list_(pa.float32(), dim) if dim else pa.list_(pa.float32()),
    )
    return pa.table(data)


class _Lance:
    def __init__(self, rows):
        self._rows = rows

    def to_table(self, columns=None, filter=None):
        return _to_arrow(_apply_ws_filter(self._rows, filter))


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
    return {
        "uid": uid,
        "name": uid,
        "file_path": path,
        "axis_contracts_json": "[]",
        "axis_container_kinds_json": "[]",
        "workspace_id": ws,
        "vector": vec,
    }


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


def test_tier_weight_table():
    assert rr._tier_weight("core", impact_mode=False) == 1.0
    assert rr._tier_weight("example", impact_mode=False) == 0.2
    assert rr._tier_weight(None, impact_mode=False) == 1.0  # default → core
    assert rr._tier_weight("example", impact_mode=True) == 0.6  # demotion relaxed


def _scan_with_tiers():
    import numpy as np

    # example sits CLOSER to the query (0.05) than core (0.15); only the
    # tier demotion can pull core ahead.
    rows = [
        {"uid": "core", "name": "core", "file_path": "/pkg/core.py",
         "file_tier": "core"},
        {"uid": "ex", "name": "ex", "file_path": "/examples/app.py",
         "file_tier": "example"},
    ]
    vectors = np.array([[0.15, 0.0], [0.05, 0.0]], dtype=float)
    return rr.WorkspaceScan(rows=rows, vectors=vectors)


def test_example_tier_demoted_below_core_in_seed_selection():
    out = find_seeds_by_vector(
        WORKSPACE, "q", embed_fn=lambda t: [0.0, 0.0], limit=1,
        prescanned=_scan_with_tiers(),
    )
    # Despite being vectorially nearer, the example is demoted out of the
    # single seed slot; core takes it.
    assert [c.uid for c in out] == ["core"]


def test_impact_mode_relaxes_example_demotion():
    out = find_seeds_by_vector(
        WORKSPACE, "q", embed_fn=lambda t: [0.0, 0.0], limit=1,
        impact_mode=True, prescanned=_scan_with_tiers(),
    )
    # In impact mode the example is no longer demoted hard enough to lose
    # its nearer distance — it stays the top seed.
    assert [c.uid for c in out] == ["ex"]
