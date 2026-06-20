from context_engine.indexer.external_boundary import (
    build_project_boundary,
    classify_external_root,
    external_pkg_uid,
    external_root_from_qualified_name,
    external_symbol_uid,
    package_manifest_external_roots,
)
from context_engine.indexer.external_facts import (
    collect_external_call_links,
    collect_external_import_links,
    collect_external_symbol_import_links,
    external_call_link_rows,
    external_symbol_import_rows,
)
from context_engine.indexer.role_clustering import assemble_symbol_rows


def test_external_root_from_qualified_name_truncates_to_root():
    assert external_root_from_qualified_name("httpx._client.Client.get") == "httpx"
    assert external_root_from_qualified_name("sqlalchemy") == "sqlalchemy"


def test_classify_external_root_respects_project_boundary(tmp_path):
    project = tmp_path / "pkg"
    (project / "app").mkdir(parents=True)
    (project / "app" / "__init__.py").write_text("")
    boundary = build_project_boundary(project, file_paths=("app/routes.py",))
    assert classify_external_root("app", boundary) == "internal"
    assert classify_external_root("typing", boundary) == "external"
    assert classify_external_root("local_vendor", boundary) == "skip"


def test_package_manifest_external_roots_include_js_dependencies(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"router":"^2.2.0"},"devDependencies":{"mocha":"^10.0.0"}}'
    )

    roots = package_manifest_external_roots(tmp_path)

    assert {"router", "mocha"} <= roots


def test_collect_external_call_links_skips_in_project_targets():
    boundary = frozenset({"fastapi"})
    calls = [
        {
            "caller_uid": "c1",
            "callee_qualified_name": "fastapi.routing.APIRouter",
            "call_site_line": 10,
        },
        {
            "caller_uid": "c2",
            "callee_qualified_name": "json.dumps",
            "call_site_line": 3,
            "call_kind": "construct",
        },
    ]
    links = collect_external_call_links(calls, boundary=boundary)
    assert len(links) == 1
    assert links[0].external_root == "json"
    assert links[0].callee_member == "dumps"
    assert links[0].kind == "construct"


def test_collect_external_import_links_from_source():
    boundary = frozenset({"myapp"})
    source = "import json\nfrom myapp import routes\nimport typing\n"
    links = collect_external_import_links(source, "svc/main.py", boundary=boundary)
    roots = {link.external_root for link in links}
    assert roots == {"json", "typing"}


def test_collect_external_import_links_from_js_require_manifest_roots():
    boundary = frozenset({"express"})
    source = "var Router = require('router');\nimport bodyParser from 'body-parser';\n"
    links = collect_external_import_links(
        source,
        "lib/express.js",
        boundary=boundary,
        project_external_roots=frozenset({"router", "body-parser"}),
    )
    roots = {link.external_root for link in links}
    assert roots == {"router", "body-parser"}


def test_collect_external_symbol_imports_captures_from_import_names():
    boundary = frozenset({"myapp"})
    source = (
        "from starlette.routing import Router, Mount\n"
        "from pydantic import BaseModel as PModel\n"
        "from myapp import routes\n"
    )

    links = collect_external_symbol_import_links(source, "svc/main.py", boundary=boundary)

    qns = {link.qualified_name for link in links}
    assert qns == {
        "starlette.routing.Router",
        "starlette.routing.Mount",
        "pydantic.BaseModel",
    }
    by_qn = {link.qualified_name: link for link in links}
    # ``as`` alias preserved without losing the upstream identity used by the
    # catalogue.
    assert by_qn["pydantic.BaseModel"].local_alias == "PModel"
    assert by_qn["starlette.routing.Router"].local_alias == "Router"


def test_collect_external_symbol_imports_handles_multiline_parenthesised_body():
    boundary = frozenset({"myapp"})
    source = (
        "from starlette.routing import (\n"
        "    Router,\n"
        "    Mount,  # one of many\n"
        "    Route as R,\n"
        ")\n"
    )

    links = collect_external_symbol_import_links(source, "svc/main.py", boundary=boundary)
    qns = {link.qualified_name for link in links}

    assert qns == {
        "starlette.routing.Router",
        "starlette.routing.Mount",
        "starlette.routing.Route",
    }


def test_collect_external_symbol_imports_handles_dotted_import_statement():
    boundary = frozenset({"myapp"})
    source = "import urllib.parse\nimport urllib.parse as up\n"

    links = collect_external_symbol_import_links(source, "svc/main.py", boundary=boundary)
    by_alias = {link.local_alias: link for link in links}

    # ``import urllib.parse`` binds ``urllib`` locally, but the catalogue
    # identity is still ``urllib.parse``.
    assert by_alias["parse"].qualified_name == "urllib.parse"
    assert by_alias["up"].qualified_name == "urllib.parse"


def test_collect_external_symbol_imports_skips_relative_and_internal():
    boundary = frozenset({"myapp"})
    source = (
        "from . import sibling\n"
        "from .relative import deeper\n"
        "from myapp.routes import handler\n"
        "from starlette import middleware\n"
    )

    links = collect_external_symbol_import_links(source, "myapp/main.py", boundary=boundary)
    qns = {link.qualified_name for link in links}

    assert qns == {"starlette.middleware"}


def test_collect_external_symbol_imports_skips_star_import():
    boundary = frozenset({"myapp"})
    source = "from starlette.routing import *\n"

    links = collect_external_symbol_import_links(source, "svc/main.py", boundary=boundary)

    assert links == []


def test_external_symbol_import_rows_attach_workspace_scoped_uids():
    from context_engine.indexer.external_facts import ExternalSymbolImportLink

    link = ExternalSymbolImportLink(
        file_path="svc/main.py",
        qualified_name="starlette.routing.Router",
        module="starlette.routing",
        name="Router",
        local_alias="Router",
    )

    rows = external_symbol_import_rows([link], "ws/test")

    assert rows[0]["external_symbol_uid"] == external_symbol_uid(
        "ws/test", "starlette.routing.Router"
    )
    assert rows[0]["external_pkg_uid"] == external_pkg_uid("ws/test", "starlette")
    assert rows[0]["external_root"] == "starlette"


def test_apply_external_boundary_for_file_passes_symbol_imports_through_db_stub():
    from context_engine.indexer.external_facts import apply_external_boundary_for_file

    class _DbStub:
        def __init__(self):
            self.received_symbol_imports = None

        def delete_external_imports_for_file(self, path, *, workspace_id):
            pass

        def link_external_boundary(
            self,
            call_rows,
            import_rows,
            *,
            workspace_id,
            symbol_import_links=None,
        ):
            self.received_symbol_imports = symbol_import_links
            return 0, 0

    db = _DbStub()
    apply_external_boundary_for_file(
        db,
        file_path="svc/main.py",
        source_code="from starlette.routing import Router\n",
        calls=[],
        boundary=frozenset(),
        workspace_id="ws/test",
    )

    assert db.received_symbol_imports is not None
    assert any(
        row["qualified_name"] == "starlette.routing.Router" for row in db.received_symbol_imports
    )


def test_external_call_link_rows_use_stable_uids():
    from context_engine.indexer.external_facts import ExternalCallLink

    link = ExternalCallLink("caller", "json", "dumps", 1, 0.9)
    rows = external_call_link_rows([link], "ws/test")
    assert rows[0]["external_uid"] == external_pkg_uid("ws/test", "json")
    assert rows[0]["kind"] == "call"


def test_assemble_symbol_rows_includes_external_features():
    symbols = [("s1", "function", "svc/gateway.py")]
    rows = assemble_symbol_rows(
        symbols,
        [],
        {},
        external_call_fan_out_per_uid={"s1": 2.5},
        external_root_count_per_uid={"s1": 2},
        external_import_fan_out_by_file={"svc/gateway.py": 3.0},
    )
    row = rows[0]
    assert row.external_call_fan_out == 2.5
    assert row.external_root_count == 2
    assert row.external_import_fan_out == 3.0
    assert row.external_call_out_ratio == 2.5 / (2.5 + 0.05)
