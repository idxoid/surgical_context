"""FastAPI endpoint tests with external services mocked out."""

import importlib
import sys
import types
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from sidecar.indexer.job_log import IndexJobLog


class FakeCtx:
    intent = "exploration"
    mode = "surgical_full"

    def to_system_prompt(self):
        return "compiled prompt"

    def token_count(self):
        return 42

    def to_dict(self):
        return {
            "mode": self.mode,
            "intent": self.intent,
            "primary_source": {"symbol": "process_payment"},
            "graph_context": [],
            "documentation": [],
        }


class FakeDb:
    def __init__(self):
        self.closed = False

    def is_cloud(self):
        return False

    def delete_symbols_for_file(self, file_path, workspace_id="local/surgical_context@main"):
        return None

    def close(self):
        self.closed = True


class FakeContextArbitrator:
    def __init__(
        self, db, overlay=None, vector_db=None, workspace_id="local/surgical_context@main"
    ):
        self.db = db
        self.overlay = overlay
        self.vector_db = vector_db
        self.workspace_id = workspace_id

    def get_context_for_symbol(self, symbol, question="", token_budget=4000):
        if symbol == "missing":
            return "Error: Symbol 'missing' not found in graph."
        return FakeCtx()


@contextmanager
def fake_db_session(user_id="anonymous"):
    db = FakeDb()
    try:
        yield db
    finally:
        db.close()


def import_main_with_fakes(monkeypatch):
    """Import sidecar.main without constructing real LanceDB/LLM clients."""
    sys.modules.pop("sidecar.main", None)

    fake_lancedb = types.ModuleType("sidecar.database.lancedb_client")

    class FakeLanceDBClient:
        def search(self, query, limit=5):
            return []

    fake_lancedb.LanceDBClient = FakeLanceDBClient
    monkeypatch.setitem(sys.modules, "sidecar.database.lancedb_client", fake_lancedb)

    fake_engine = types.ModuleType("sidecar.ai.engine")

    class FakeAIEngine:
        def __init__(self, model_preference="auto"):
            self.model_preference = model_preference

        def chat(self, system_prompt, user_message, token_count=0, intent="exploration"):
            return "fake answer"

        def stream_chat(self, system_prompt, user_message, token_count=0, intent="exploration"):
            yield "fake "
            yield "stream"

    fake_engine.AIEngine = FakeAIEngine
    monkeypatch.setitem(sys.modules, "sidecar.ai.engine", fake_engine)

    main = importlib.import_module("sidecar.main")
    monkeypatch.setattr(main, "ContextArbitrator", FakeContextArbitrator)
    monkeypatch.setattr(main, "db_session", fake_db_session)
    return main


def test_ask_endpoint_returns_typed_response(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    body = main.ask(
        main.AskRequest(symbol="process_payment", question="How does this work?"),
        x_user_id="Alice",
    )

    assert body["symbol"] == "process_payment"
    assert body["answer"] == "fake answer"
    assert body["user"] == "alice"
    assert body["cloud"] is False
    assert body["context"]["intent"] == "exploration"


def test_ask_stream_endpoint_emits_json_sse(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    response = main.ask_stream(main.AskRequest(symbol="process_payment", question="Stream it"))

    assert response.media_type == "text/event-stream"


def test_ask_endpoint_returns_not_found(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        main.ask(main.AskRequest(symbol="missing", question="Where?"))

    assert exc_info.value.status_code == 404
    assert "not found" in exc_info.value.detail


def test_auth_required_rejects_missing_bearer_token(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    monkeypatch.setattr(main, "AUTH_REQUIRED", True)

    with pytest.raises(HTTPException) as exc_info:
        main.ask(main.AskRequest(symbol="process_payment", question="How does this work?"))

    assert exc_info.value.status_code == 401
    assert "Missing bearer token" in exc_info.value.detail


def test_auth_required_accepts_valid_bearer_token(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    monkeypatch.setattr(main, "AUTH_REQUIRED", True)
    token = main.user_auth.generate_token("Alice")

    body = main.ask(
        main.AskRequest(symbol="process_payment", question="How does this work?"),
        authorization=f"Bearer {token}",
    )

    assert body["symbol"] == "process_payment"
    assert body["user"] == "alice"


def test_index_file_endpoint_tracks_job(monkeypatch, tmp_path):
    main = import_main_with_fakes(monkeypatch)

    source_file = tmp_path / "app.py"
    source_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

    fake_anchor = types.ModuleType("sidecar.indexer.anchor")
    fake_anchor.resolve_pending_anchors = lambda db, vector_db, workspace_id=None: None
    monkeypatch.setitem(sys.modules, "sidecar.indexer.anchor", fake_anchor)

    fake_code = types.ModuleType("sidecar.indexer.code")
    fake_code.hash_file = lambda file_path: "abc123"
    fake_code.index_file = lambda file_path, db, vector_db, extractor, workspace_id=None: None
    monkeypatch.setitem(sys.modules, "sidecar.indexer.code", fake_code)

    fake_extractor = types.ModuleType("sidecar.parser.extractor")

    class FakeSymbolExtractor:
        pass

    fake_extractor.SymbolExtractor = FakeSymbolExtractor
    monkeypatch.setitem(sys.modules, "sidecar.parser.extractor", fake_extractor)
    monkeypatch.setattr(main, "IndexJobLog", lambda: IndexJobLog(f"{tmp_path}/jobs.sqlite3"))

    body = main.index_file_endpoint(main.IndexFileRequest(file_path=str(source_file)))

    assert body["status"] == "indexed"
    assert body["file_path"] == str(source_file)
    assert body["job_id"] > 0


def test_impact_endpoint_returns_affected_symbols(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    class FakeSession:
        def __init__(self):
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def run(self, query, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return types.SimpleNamespace(single=lambda: {"uid": "symbol-1"})
            return types.SimpleNamespace(single=lambda: {"file_path": "/repo/app.py"})

    class FakeDriverDb(FakeDb):
        def __init__(self):
            super().__init__()
            self.session = FakeSession()
            self.driver = types.SimpleNamespace(session=lambda: self.session)

    @contextmanager
    def impact_db_session(user_id="anonymous"):
        yield FakeDriverDb()

    fake_affects = types.ModuleType("sidecar.indexer.affects")

    class FakeAffectsIndexer:
        MAX_AFFECTS_DEPTH = 4

        def __init__(self, db):
            self.db = db

        def get_affected_symbols(self, symbol_uid, workspace_id="local/surgical_context@main"):
            return [
                {"uid": "affected-1", "name": "caller", "file_path": "/repo/caller.py", "depth": 1}
            ]

        def get_affected_files(self, file_path, workspace_id="local/surgical_context@main"):
            return ["/repo/caller.py"]

    fake_affects.AFFECTSIndexer = FakeAffectsIndexer
    monkeypatch.setitem(sys.modules, "sidecar.indexer.affects", fake_affects)
    monkeypatch.setattr(main, "db_session", impact_db_session)

    body = main.impact(symbol="process_payment")

    assert body["symbol_uid"] == "symbol-1"
    assert body["affected_count"] == 1
    assert body["affected_files"] == ["/repo/caller.py"]


def test_audit_actions_endpoint_returns_actions(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    class FakeAuditLog:
        def get_recent_actions(self, user_id=None, limit=100):
            return [{"user_id": user_id or "alice", "action": "query"}]

    monkeypatch.setattr(main, "audit_log", FakeAuditLog())

    body = main.audit_actions(user_id="alice", limit=1)

    assert body == {
        "actions": [{"user_id": "alice", "action": "query"}],
        "total": 1,
    }


def test_auth_token_endpoint_returns_token(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    body = main.auth_token(user_id="Alice")

    assert body["user_id"] == "alice"
    assert body["token"]
    assert body["expires_in_hours"] == 24
