"""Unit tests for the re-export transparency bridge."""

from context_engine.axis.reexport_bridge import expand_reexport_bridge
from context_engine.axis.role_retrieval import WorkspaceScan


class _Result:
    def __init__(self, records):
        self._records = records

    def __iter__(self):
        return iter(self._records)


class _Session:
    def __init__(self, records):
        self.records = records
        self.runs = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query, **params):
        self.runs.append((query, params))
        return _Result(self.records)


class _Driver:
    def __init__(self, records):
        self.session_obj = _Session(records)

    def session(self):
        return self.session_obj


class _Db:
    def __init__(self, records):
        self.driver = _Driver(records)


def _scan(rows_by_uid):
    scan = WorkspaceScan.__new__(WorkspaceScan)
    scan.rows_by_uid = rows_by_uid
    return scan


_SHIM_ROW = {
    "file_path": "fastapi/websockets.py",
    "uid": "u:shim",
    "name": "fastapi.websockets",
    "qualified_name": "fastapi.websockets",
}


def test_reexport_bridge_surfaces_pure_reexport_shim_for_anchor():
    db = _Db([_SHIM_ROW])
    scan = _scan({"u:shim": {"file_tier": "reexport"}})

    out = expand_reexport_bridge("WebSocket", db=db, workspace_id="ws", prescanned=scan)

    assert [c.file_path for c in out] == ["fastapi/websockets.py"]
    assert out[0].role == "reexport_bridge"
    assert out[0].satisfying_kinds == ("public_reexport",)
    # Anchor + alias flow into the query params.
    assert db.driver.session_obj.runs[0][1] == {"workspace_id": "ws", "anchor": "WebSocket"}


def test_reexport_bridge_tier_gate_rejects_non_reexport_file():
    # Same graph hit, but the file is a normal core module (imports the symbol
    # without being a pure re-export) — must not be surfaced.
    db = _Db([_SHIM_ROW])
    scan = _scan({"u:shim": {"file_tier": "core"}})

    assert expand_reexport_bridge("WebSocket", db=db, workspace_id="ws", prescanned=scan) == []


def test_reexport_bridge_no_anchor_is_noop():
    db = _Db([_SHIM_ROW])
    assert expand_reexport_bridge("", db=db, workspace_id="ws", prescanned=_scan({})) == []
    assert expand_reexport_bridge(None, db=db, workspace_id="ws", prescanned=_scan({})) == []
    # No query issued when there is no anchor.
    assert db.driver.session_obj.runs == []


def test_reexport_bridge_caps_total():
    rows = [{**_SHIM_ROW, "uid": f"u:{i}", "file_path": f"pkg/shim{i}.py"} for i in range(10)]
    db = _Db(rows)
    scan = _scan({f"u:{i}": {"file_tier": "reexport"} for i in range(10)})

    out = expand_reexport_bridge("X", db=db, workspace_id="ws", prescanned=scan, max_total=3)
    assert len(out) == 3
