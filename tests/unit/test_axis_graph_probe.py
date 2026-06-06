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
    # 1st run: proxy query, 2nd run: catalogue query (empty for this symbol)
    db = _Db([
        {"symbol_kind": "proxy_binding", "proxy_rel_count": 0},
        {"qns": []},
    ])
    probe = Neo4jGraphContextProbe(db, "ws")

    assert probe.library_marker_kinds("u:proxy") == {"proxy_object"}
    # 2nd call hits the cache, no further DB runs.
    assert probe.library_marker_kinds("u:proxy") == {"proxy_object"}
    assert len(db.driver.session_obj.runs) == 2
    assert db.driver.session_obj.runs[0][1] == {
        "symbol_uid": "u:proxy",
        "workspace_id": "ws",
    }


def test_neo4j_probe_resolves_library_marker_kind_via_catalogue():
    # Proxy query returns nothing; catalogue query returns a known external QN.
    db = _Db([
        {"symbol_kind": "class", "proxy_rel_count": 0},
        {"qns": ["starlette.routing.Router", "typing.Any"]},
    ])
    probe = Neo4jGraphContextProbe(db, "ws")

    kinds = probe.library_marker_kinds("u:cls")

    # ``typing.Any`` is not in the catalogue → ignored without name matching.
    assert kinds == {"web_route_register"}


def test_neo4j_probe_returns_union_when_file_imports_multiple_marker_packages():
    db = _Db([
        {"symbol_kind": "class", "proxy_rel_count": 0},
        {"qns": ["celery.app.Celery", "werkzeug.local.LocalProxy"]},
    ])
    probe = Neo4jGraphContextProbe(db, "ws")

    assert probe.library_marker_kinds("u:cls") == {"task_register", "proxy_object"}


def test_neo4j_probe_ignores_unknown_external_qualified_names():
    db = _Db([
        {"symbol_kind": "class", "proxy_rel_count": 0},
        {"qns": ["some.unknown.External", "another.Random"]},
    ])
    probe = Neo4jGraphContextProbe(db, "ws")

    assert probe.library_marker_kinds("u:cls") == set()


def test_neo4j_probe_counts_outgoing_edges_to_proxy_markers():
    db = _Db([{"count": 2}])
    probe = Neo4jGraphContextProbe(db, "ws")

    assert probe.outgoing_kind_edges("u:caller", {"proxy_object"}) == 2
    # data_model has no graph-level proof yet; the probe must not invent one.
    assert probe.outgoing_kind_edges("u:caller", {"data_model"}) == 0
    assert "proxy_object" in probe.supported_outgoing_kinds()
    assert "data_model" not in probe.supported_outgoing_kinds()


def test_neo4j_probe_computes_caller_package_dispersion_from_qualified_names():
    db = _Db([{"qns": ["pkg.a", "other.b", "pkg.c"]}])
    probe = Neo4jGraphContextProbe(db, "ws")

    # 3 callers across {pkg, other} → (2 - 1) / (3 - 1) = 0.5
    assert probe.caller_package_dispersion("u:target") == 0.5


def test_neo4j_probe_dispersion_falls_back_to_zero_without_qualified_names():
    db = _Db([{"qns": ["", "", ""]}])
    probe = Neo4jGraphContextProbe(db, "ws")

    # Empty qualified names → no structural package boundary visible.
    assert probe.caller_package_dispersion("u:target") == 0.0


def test_neo4j_probe_does_not_infer_cfg_driver_from_plain_graph_context():
    db = _Db([])
    probe = Neo4jGraphContextProbe(db, "ws")

    assert probe.is_cfg_driver("u:driver") is False
    assert db.driver.session_obj.runs == []
