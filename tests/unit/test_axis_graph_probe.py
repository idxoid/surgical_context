from sidecar.axis.graph_probe import Neo4jGraphContextProbe


class _Result:
    def __init__(self, record):
        self._record = record

    def single(self):
        return self._record


class _Session:
    def __init__(self, records):
        self.records = records
        self.runs = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query, **params):
        self.runs.append((query, params))
        record = self.records.pop(0) if self.records else None
        return _Result(record)


class _Driver:
    def __init__(self, records):
        self.session_obj = _Session(records)

    def session(self):
        return self.session_obj


class _Db:
    def __init__(self, records):
        self.driver = _Driver(records)


def test_neo4j_probe_exposes_proxy_binding_as_proxy_object_marker():
    db = _Db([{"symbol_kind": "proxy_binding", "proxy_rel_count": 0}])
    probe = Neo4jGraphContextProbe(db, "ws")

    assert probe.library_marker_kinds("u:proxy") == {"proxy_object"}
    assert probe.library_marker_kinds("u:proxy") == {"proxy_object"}
    assert len(db.driver.session_obj.runs) == 1
    assert db.driver.session_obj.runs[0][1] == {
        "symbol_uid": "u:proxy",
        "workspace_id": "ws",
    }


def test_neo4j_probe_counts_outgoing_edges_to_proxy_markers():
    db = _Db([{"count": 2}])
    probe = Neo4jGraphContextProbe(db, "ws")

    assert probe.outgoing_kind_edges("u:caller", {"proxy_object"}) == 2
    assert probe.outgoing_kind_edges("u:caller", {"data_model"}) == 0


def test_neo4j_probe_computes_caller_package_dispersion_from_files():
    db = _Db([{"paths": ["/repo/pkg/a.py", "/repo/other/b.py", "/repo/pkg/c.py"]}])
    probe = Neo4jGraphContextProbe(db, "ws")

    assert probe.caller_package_dispersion("u:target") == 0.5


def test_neo4j_probe_does_not_infer_cfg_driver_from_plain_graph_context():
    db = _Db([])
    probe = Neo4jGraphContextProbe(db, "ws")

    assert probe.is_cfg_driver("u:driver") is False
    assert db.driver.session_obj.runs == []
