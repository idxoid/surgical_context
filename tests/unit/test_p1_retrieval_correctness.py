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


def test_normalize_signature_handles_multiline_parameters():
    raw = """apply_async(self, args=None, kwargs=None,
                    task_id=None, producer=None,
                    **options)"""
    assert normalize_signature(raw, "python") == "apply_async(_,_,_,_,_,**kwargs)->_"


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


def test_python_qualified_names_survive_unicode_before_decorated_class():
    source = '''
"""Queue note — broker sends café payloads."""

@abstract.CallableTask.register
class Task:
    def delay(self, *args, **kwargs):
        return self.apply_async(args, kwargs)

    def apply_async(self, args=None, kwargs=None):
        return None
'''
    adapter = PythonAdapter()
    symbols = adapter.extract_symbols(source, "/repo/task.py")
    qualified = {symbol.name: symbol.qualified_name for symbol in symbols}

    assert qualified["Task"] == "task.Task"
    assert qualified["delay"] == "task.Task.delay"
    assert qualified["apply_async"] == "task.Task.apply_async"

    calls = adapter.extract_calls_from_source(source, "/repo/task.py")
    apply_call = next(call for call in calls if call["callee_name"] == "apply_async")
    assert apply_call["callee_uid"] == qualified_uid(symbols, "apply_async")


def qualified_uid(symbols, name: str) -> str:
    return next(symbol.uid for symbol in symbols if symbol.name == name)


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
    assert "uid: call.callee_uid" in query
    assert ":CALLS_SCOPED" in query
    assert params["workspace_id"] == "acme/repo@feature"


def test_neo4j_link_calls_batches_same_resolution_mode():
    tx = MagicMock()

    Neo4jClient._create_call_relations(
        tx,
        [
            {
                "caller_uid": "caller-1",
                "callee_uid": "callee-1",
                "rel_type": "CALLS_SCOPED",
                "call_site_line": 3,
            },
            {
                "caller_uid": "caller-2",
                "callee_uid": "callee-2",
                "rel_type": "CALLS_SCOPED",
                "call_site_line": 9,
            },
        ],
        "acme/repo@feature",
    )

    tx.run.assert_called_once()
    query = tx.run.call_args.args[0]
    params = tx.run.call_args.kwargs
    assert "UNWIND $calls AS call" in query
    assert ":CALLS_SCOPED" in query
    assert len(params["calls"]) == 2


def test_neo4j_link_calls_falls_back_to_object_api_surface_for_member_qualified_name():
    tx = MagicMock()

    Neo4jClient._create_call_relations(
        tx,
        [
            {
                "caller_uid": "caller",
                "callee_name": "askStream",
                "callee_qualified_name": "extension.src.sidecarClient.SidecarClient.askStream",
                "rel_type": "CALLS_IMPORTED",
                "tier": "imported",
                "confidence": 0.9,
                "resolver": "ts-scope-v1",
                "call_site_line": 12,
            }
        ],
        "local/surgical_context@main",
    )

    query = tx.run.call_args.args[0]
    assert "object_api" in query
    assert "STARTS WITH surface.qualified_name" in query


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
