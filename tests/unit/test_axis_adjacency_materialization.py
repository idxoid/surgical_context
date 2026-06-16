import json

from sidecar.axis import graph_walk_inproc
from sidecar.indexer.fast.adjacency_materialization import materialize_axis_adjacency

WORKSPACE = "acme/repo@main+axis_python_v1"


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)


class _Session:
    def __init__(self):
        self.runs = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, query, **params):
        self.runs.append((query, params))
        if "CONTAINS" in query:
            return _Result(
                [
                    {
                        "uid": "u:caller",
                        "name": "caller",
                        "path": "/repo/app.py",
                        "kind": "function",
                    },
                    {
                        "uid": "u:callee",
                        "name": "Callee",
                        "path": "/repo/models.py",
                        "kind": "class",
                    },
                ]
            )
        return _Result(
            [
                {"au": "u:caller", "bu": "u:callee", "t": "CALLS_DIRECT"},
                {"au": "u:callee", "bu": "u:caller", "t": "REFERENCES"},
            ]
        )


class _Driver:
    def __init__(self, session):
        self._session = session

    def session(self):
        return self._session


class _Db:
    def __init__(self):
        self.session = _Session()
        self.driver = _Driver(self.session)


class _Lance:
    def __init__(self):
        self.rows = []
        self.workspace_id = ""

    def replace_axis_adjacency(self, rows, *, workspace_id):
        self.rows = rows
        self.workspace_id = workspace_id


def test_materialize_axis_adjacency_writes_workspace_rows():
    db = _Db()
    lance = _Lance()

    count = materialize_axis_adjacency(db, lance, WORKSPACE)

    assert count == 2
    assert lance.workspace_id == WORKSPACE
    by_uid = {row["uid"]: row for row in lance.rows}
    assert by_uid["u:caller"]["workspace_id"] == WORKSPACE
    assert json.loads(by_uid["u:caller"]["out_edges_json"]) == {"CALLS_DIRECT": ["u:callee"]}
    assert json.loads(by_uid["u:callee"]["in_edges_json"]) == {"CALLS_DIRECT": ["u:caller"]}


def test_inproc_walk_uses_materialized_lance_rows(monkeypatch):
    rows = [
        {
            "uid": "u:caller",
            "name": "caller",
            "file_path": "/repo/app.py",
            "kind": "function",
            "out_edges_json": json.dumps({"CALLS_DIRECT": ["u:callee"]}),
            "in_edges_json": "{}",
        },
        {
            "uid": "u:callee",
            "name": "Callee",
            "file_path": "/repo/models.py",
            "kind": "class",
            "out_edges_json": "{}",
            "in_edges_json": json.dumps({"CALLS_DIRECT": ["u:caller"]}),
        },
    ]
    graph_walk_inproc.invalidate_adjacency()
    monkeypatch.setattr(
        graph_walk_inproc,
        "_load_adjacency_from_lance",
        lambda workspace_id: graph_walk_inproc._adjacency_from_lance_rows(rows),
    )

    out = graph_walk_inproc.walk_neighbours(
        db=object(),
        workspace_id=WORKSPACE,
        seed_uids=["u:caller"],
        edges=("CALLS_DIRECT",),
        direction="forward",
        max_hops=1,
    )

    assert [(n.uid, n.name, n.file_path, n.depth, n.reach) for n in out] == [
        ("u:callee", "Callee", "/repo/models.py", 1, 1)
    ]
