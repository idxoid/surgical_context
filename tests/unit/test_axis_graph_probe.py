import pytest

from context_engine.axis.graph_probe import Neo4jGraphContextProbe


class _Result:
    """One query's response: iterable rows, ``.single()`` = first row."""

    def __init__(self, records):
        self._records = records

    def single(self):
        return self._records[0] if self._records else None

    def __iter__(self):
        return iter(self._records)


class _Session:
    def __init__(self, responses):
        self.responses = responses
        self.runs = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query, **params):
        self.runs.append((query, params))
        records = self.responses.pop(0) if self.responses else []
        return _Result(records)


class _Driver:
    def __init__(self, responses):
        self.session_obj = _Session(responses)

    def session(self):
        return self.session_obj


class _Db:
    """Fake Neo4j client; ``responses`` is one list of records per query run."""

    def __init__(self, responses):
        self.driver = _Driver(responses)


def test_neo4j_probe_exposes_proxy_binding_as_proxy_object_marker():
    # Workspace loads: proxy-binding uids, proxy-edge uids, catalogue map.
    db = _Db(
        [
            [{"uid": "u:proxy"}],
            [],
            [],
        ]
    )
    probe = Neo4jGraphContextProbe(db, "ws")

    assert probe.library_marker_kinds("u:proxy") == {"proxy_object"}
    # 2nd call hits the cache/maps, no further DB runs.
    assert probe.library_marker_kinds("u:proxy") == {"proxy_object"}
    assert len(db.driver.session_obj.runs) == 3
    assert all(call[1] == {"workspace_id": "ws"} for call in db.driver.session_obj.runs)


def test_neo4j_probe_resolves_library_marker_kind_via_catalogue():
    # Proxy loads return nothing; catalogue map carries a known external QN.
    db = _Db(
        [
            [],
            [],
            [{"uid": "u:cls", "qns": ["starlette.routing.Router", "typing.Any"]}],
        ]
    )
    probe = Neo4jGraphContextProbe(db, "ws")

    kinds = probe.library_marker_kinds("u:cls")

    # ``typing.Any`` is not in the catalogue → ignored without name matching.
    assert kinds == {"web_route_register"}


def test_neo4j_probe_catalogue_query_walks_extends_and_instantiates_external():
    # Confirms the probe asks the graph for both edge types in a single
    # query — so a Variable Symbol carrying an INSTANTIATES_EXTERNAL edge to
    # a catalogue entry produces the same marker as a class that EXTENDS it.
    db = _Db(
        [
            [],
            [],
            [{"uid": "u:var", "qns": ["fastapi.applications.FastAPI"]}],
        ]
    )
    probe = Neo4jGraphContextProbe(db, "ws")

    kinds = probe.library_marker_kinds("u:var")

    assert kinds == {"web_route_register"}
    # The catalogue load is the 3rd DB call; confirm it joins both edge
    # types in a single MATCH instead of issuing two separate queries.
    queries = [call[0] for call in db.driver.session_obj.runs]
    assert "EXTENDS_EXTERNAL|INSTANTIATES_EXTERNAL" in queries[2]


def test_neo4j_probe_returns_union_when_file_imports_multiple_marker_packages():
    db = _Db(
        [
            [],
            [],
            [{"uid": "u:cls", "qns": ["celery.app.base.Celery", "werkzeug.local.LocalProxy"]}],
        ]
    )
    probe = Neo4jGraphContextProbe(db, "ws")

    # ``werkzeug.local.LocalProxy`` is no longer a catalogue entry — only the
    # structural celery marker survives the external-edge union.
    assert probe.library_marker_kinds("u:cls") == {"task_register"}


def test_neo4j_probe_ignores_unknown_external_qualified_names():
    db = _Db(
        [
            [],
            [],
            [{"uid": "u:cls", "qns": ["some.unknown.External", "another.Random"]}],
        ]
    )
    probe = Neo4jGraphContextProbe(db, "ws")

    assert probe.library_marker_kinds("u:cls") == set()


def test_neo4j_probe_counts_outgoing_edges_to_proxy_markers():
    db = _Db([[{"count": 2}]])
    probe = Neo4jGraphContextProbe(db, "ws")

    assert probe.outgoing_kind_edges("u:caller", {"proxy_object"}) == 2
    # data_model has no graph-level proof yet; the probe must not invent one.
    assert probe.outgoing_kind_edges("u:caller", {"data_model"}) == 0
    assert "proxy_object" in probe.supported_outgoing_kinds()
    assert "data_model" not in probe.supported_outgoing_kinds()


def test_neo4j_probe_computes_caller_package_dispersion_from_qualified_names():
    db = _Db([[{"qns": ["pkg.a", "other.b", "pkg.c"]}]])
    probe = Neo4jGraphContextProbe(db, "ws")

    # 3 callers across {pkg, other} → (2 - 1) / (3 - 1) = 0.5
    assert probe.caller_package_dispersion("u:target") == pytest.approx(0.5)


def test_neo4j_probe_dispersion_falls_back_to_zero_without_qualified_names():
    db = _Db([[{"qns": ["", "", ""]}]])
    probe = Neo4jGraphContextProbe(db, "ws")

    # Empty qualified names → no structural package boundary visible.
    assert probe.caller_package_dispersion("u:target") == pytest.approx(0.0)


def test_neo4j_probe_resolves_in_workspace_exception_class_key():
    db = _Db([[{"name": "HTTPException"}]])
    probe = Neo4jGraphContextProbe(db, "ws")

    assert probe.is_error_model_type_name("HTTPException", "u:meth") is True
    assert probe.is_error_model_type_name("PlainModel", "u:meth") is False
    # Builtin roots resolve without a DB round-trip.
    assert probe.is_error_model_type_name("ValueError", "u:meth") is True
    # One workspace scan serves every key lookup.
    assert len(db.driver.session_obj.runs) == 1


def test_neo4j_probe_has_proxy_object_topology_for_proxy_binding():
    db = _Db([[{"uid": "u:proxy"}], []])
    probe = Neo4jGraphContextProbe(db, "ws")

    assert probe.has_proxy_object_topology("u:proxy") is True
    assert probe.has_proxy_object_topology("u:other") is False
    # Both proxy loads happen once; lookups after that are map hits.
    assert len(db.driver.session_obj.runs) == 2


def test_neo4j_probe_handles_and_injects_counts_from_one_scan():
    db = _Db(
        [
            [{"uid": "u:app", "n": 3}],
            [{"uid": "u:endpoint", "n": 2}],
        ]
    )
    probe = Neo4jGraphContextProbe(db, "ws")

    assert probe.outgoing_handles_count("u:app") == 3
    assert probe.outgoing_handles_count("u:none") == 0
    assert probe.outgoing_injects_count("u:endpoint") == 2
    assert probe.outgoing_injects_count("u:none") == 0
    # One HANDLES scan + one INJECTS scan regardless of lookup count.
    assert len(db.driver.session_obj.runs) == 2


def test_neo4j_probe_event_signal_from_one_scan():
    db = _Db([[{"uid": "u:signal"}]])
    probe = Neo4jGraphContextProbe(db, "ws")

    assert probe.is_event_signal("u:signal") is True
    assert probe.is_event_signal("u:plain") is False
    assert len(db.driver.session_obj.runs) == 1


def test_neo4j_probe_does_not_infer_cfg_driver_from_plain_graph_context():
    db = _Db([])
    probe = Neo4jGraphContextProbe(db, "ws")

    assert probe.is_cfg_driver("u:driver") is False
    assert db.driver.session_obj.runs == []
