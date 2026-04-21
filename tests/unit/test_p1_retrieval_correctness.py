"""P1 retrieval correctness tests: stable UID, scoped calls, workspace, Git invalidation."""

from unittest.mock import MagicMock

from sidecar.context.graph_expander import GraphExpander
from sidecar.database.neo4j_client import Neo4jClient
from sidecar.indexer.git_sync import GitState, GitStateTracker
from sidecar.parser.adapters.python_adapter import PythonAdapter
from sidecar.parser.uid import compute_uid, normalize_signature
from sidecar.workspace import WorkspaceResolver


def test_normalize_signature_strips_names_and_defaults():
    raw = 'process_payment(user_id: int, amount: float, *, currency: str = "USD") -> Receipt'
    assert normalize_signature(raw, "python") == "process_payment(int,float,*,str)->Receipt"


def test_compute_uid_uses_signature_to_disambiguate_overloads():
    uid_a = compute_uid("parser.parse", "parse(x: str)->AST", "python")
    uid_b = compute_uid("parser.parse", "parse(x: bytes)->AST", "python")
    assert uid_a != uid_b
    assert len(uid_a) == 16


def test_python_uid_is_stable_across_absolute_roots():
    source = "def process_payment(amount: int) -> bool:\n    return amount > 0\n"
    adapter = PythonAdapter()

    uid_a = adapter.extract_symbols(source, "/tmp/a/payments.py")[0].uid
    uid_b = adapter.extract_symbols(source, "/home/user/b/payments.py")[0].uid

    assert uid_a == uid_b


def test_nested_and_method_symbols_have_distinct_qualified_names():
    source = """
class Service:
    def run(self):
        pass

def run():
    def inner():
        pass
    return inner()
"""
    symbols = PythonAdapter().extract_symbols(source, "/repo/app.py")
    qualified = {symbol.name: [] for symbol in symbols}
    for symbol in symbols:
        qualified[symbol.name].append(symbol.qualified_name)

    assert "app.Service.run" in qualified["run"]
    assert "app.run" in qualified["run"]
    assert qualified["inner"] == ["app.run.<locals>.inner"]
    assert len({symbol.uid for symbol in symbols}) == len(symbols)


def test_python_call_resolver_links_same_file_helper_by_uid():
    source = """
def process():
    return validate()

def validate():
    return True
"""
    calls = PythonAdapter().extract_calls_from_source(source, "/repo/payments.py")
    validate_uid = next(
        symbol.uid
        for symbol in PythonAdapter().extract_symbols(source, "/repo/payments.py")
        if symbol.name == "validate"
    )

    call = next(call for call in calls if call["callee_name"] == "validate")
    assert call["callee_uid"] == validate_uid
    assert call["rel_type"] == "CALLS_SCOPED"
    assert call["confidence"] == 0.9


def test_python_call_resolver_records_import_alias_target():
    source = """
from .validation import amount_ok as ok

def process(amount):
    return ok(amount)
"""
    calls = PythonAdapter().extract_calls_from_source(source, "/repo/payments.py")

    call = next(call for call in calls if call["callee_name"] == "ok")
    assert call["rel_type"] == "CALLS_IMPORTED"
    assert call["tier"] == "imported"
    assert call["callee_qualified_name"] == "validation.amount_ok"


def test_neo4j_link_calls_uses_callee_uid_when_available():
    tx = MagicMock()

    Neo4jClient._create_call_relations(
        tx,
        [
            {
                "caller_uid": "caller",
                "callee_uid": "callee",
                "callee_name": "validate",
                "rel_type": "CALLS_SCOPED",
                "tier": "scoped",
                "confidence": 0.9,
                "resolver": "py-scope-v1",
                "call_site_line": 3,
            }
        ],
        "acme/repo@feature",
    )

    query = tx.run.call_args.args[0]
    params = tx.run.call_args.kwargs
    assert "uid: $callee_uid" in query
    assert ":CALLS_SCOPED" in query
    assert params["workspace_id"] == "acme/repo@feature"


def test_workspace_resolver_parses_branch_header():
    workspace = WorkspaceResolver().from_header("acme/surgical_context@feature/payments")
    assert workspace.id == "acme/surgical_context@feature/payments"
    assert workspace.tenant == "acme"
    assert workspace.repo == "surgical_context"
    assert workspace.ref == "feature/payments"
    assert workspace.ref_kind == "branch"


def test_graph_expander_scopes_target_lookup_to_workspace():
    db = MagicMock()
    session = db.driver.session.return_value.__enter__.return_value
    session.run.return_value.single.return_value = None

    GraphExpander(db, workspace_id="acme/repo@main").expand("process")

    assert session.run.call_args.kwargs["workspace_id"] == "acme/repo@main"
    assert "workspace_id" in session.run.call_args.args[0]


def test_git_state_tracker_reports_branch_change(tmp_path, monkeypatch):
    tracker = GitStateTracker(state_file=".state/git.json")
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".state").mkdir()
    (project / ".state" / "git.json").write_text(
        '{"ref": "main", "head": "aaaaaaaa"}', encoding="utf-8"
    )

    def fake_git(path, *args):
        if args == ("branch", "--show-current"):
            return "feature"
        if args == ("rev-parse", "HEAD"):
            return "bbbbbbbb"
        if args == ("diff", "--name-only", "aaaaaaaa", "bbbbbbbb"):
            return "app.py\ndocs/spec.md"
        return ""

    monkeypatch.setattr("sidecar.indexer.git_sync._git", fake_git)

    change_set = tracker.detect_changes(str(project))

    assert change_set.previous == GitState(ref="main", head="aaaaaaaa")
    assert change_set.current == GitState(ref="feature", head="bbbbbbbb")
    assert change_set.branch_changed is True
    assert change_set.changed_files == [str(project / "app.py"), str(project / "docs/spec.md")]
