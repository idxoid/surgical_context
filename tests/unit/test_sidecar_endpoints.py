"""FastAPI endpoint tests with external services mocked out."""

import importlib
import sys
import tempfile
import types
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from sidecar.context.types import SymbolContext
from sidecar.indexer.job_log import IndexJobLog


class FakeCtx:
    intent = "exploration"
    mode = "surgical_full"
    primary_source = SymbolContext(
        symbol="process_payment",
        file_path="/repo/payment.py",
        relation="PRIMARY",
        direction="self",
        depth=0,
        relevance_score=1.0,
        code="def process_payment(): pass",
    )
    graph_context = [
        SymbolContext(
            symbol="validate_amount",
            file_path="/repo/payment.py",
            relation="CALLS",
            depth=1,
            relevance_score=0.8,
            code="def validate_amount(): pass",
        )
    ]
    documentation = []

    def to_system_prompt(self):
        return "compiled prompt"

    def token_count(self):
        return 42

    def to_dict(self):
        return {
            "mode": self.mode,
            "intent": self.intent,
            "metadata": {
                "assembly": {
                    "trace_id": getattr(self, "trace_id", ""),
                    "workspace_id": getattr(self, "workspace_id", ""),
                    "model_route": getattr(self, "model_route", {}),
                    "feedback_token": getattr(self, "feedback_token", ""),
                }
            },
            "primary_source": {"symbol": "process_payment"},
            "graph_context": [{"symbol": "validate_amount"}],
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
    feedback_dir = tempfile.mkdtemp(prefix="sidecar-feedback-test-")
    monkeypatch.setenv("FEEDBACK_SNAPSHOT_PATH", f"{feedback_dir}/snapshots.jsonl")
    monkeypatch.setenv("FEEDBACK_LOG_PATH", f"{feedback_dir}/feedback.jsonl")

    fake_lancedb = types.ModuleType("sidecar.database.lancedb_client")

    class FakeLanceDBClient:
        def search(self, query, limit=5):
            return [
                {
                    "id": "docs/spec.md::0",
                    "file_path": "/repo/docs/spec.md",
                    "chunk": "Payment docs",
                    "score": 0.7,
                    "distance": 0.3,
                }
            ][:limit]

        def search_symbols(self, query, limit=5, threshold=1.0):
            return [
                {
                    "uid": "symbol-1",
                    "name": "process_payment",
                    "file_path": "/repo/payment.py",
                    "score": 0.9,
                    "distance": 0.1,
                }
            ][:limit]

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

        def route(self, token_count=0, intent="exploration"):
            return {
                "provider": "ollama",
                "model": "fake-model",
                "preference": self.model_preference,
                "reason": "test",
            }

    fake_engine.AIEngine = FakeAIEngine
    monkeypatch.setitem(sys.modules, "sidecar.ai.engine", fake_engine)

    main = importlib.import_module("sidecar.main")
    monkeypatch.setattr(main, "ContextArbitrator", FakeContextArbitrator)
    monkeypatch.setattr(main, "db_session", fake_db_session)

    class FakeIndexQueue:
        def __init__(self):
            self.pending: dict[tuple[str, str], int] = {}

        def enqueue_file(self, file_path, workspace_id, user_id="anonymous"):
            key = (workspace_id, file_path)
            if key in self.pending:
                self.pending[key] += 1
                status = "coalesced"
            else:
                self.pending[key] = 1
                status = "queued"
            return main.EnqueueResult(
                accepted=True,
                status=status,
                file_path=file_path,
                workspace_id=workspace_id,
                queue_depth=len(self.pending),
                generation=self.pending[key],
            )

        def snapshot(self):
            return {
                "pending": len(self.pending),
                "processing": 0,
                "max_pending": 500,
                "batch_size": 50,
                "debounce_ms": 500,
                "last_error": "",
            }

    monkeypatch.setattr(main, "index_queue", FakeIndexQueue())
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
    assert body["feedback_token"].startswith("fbk_")
    assert body["context"]["metadata"]["assembly"]["feedback_token"] == body["feedback_token"]


def test_ask_endpoint_includes_trace_metrics_and_model_route(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    body = main.ask(
        main.AskRequest(symbol="process_payment", question="How does this work?"),
        x_trace_id="trace-test",
    )

    assert body["trace_id"] == "trace-test"
    assert body["model_route"]["model"] == "fake-model"
    assert body["metrics"]["token_counts"]["context"] == 42
    assert body["metrics"]["latency_slo"]["target_ms"] == 200.0
    assert body["metrics"]["latency_slo"]["status"] in {"met", "breached"}
    assert body["context"]["metadata"]["assembly"]["trace_id"] == "trace-test"


def test_ask_endpoint_degrades_when_llm_unreachable(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    def fail_chat(*args, **kwargs):
        raise RuntimeError("Ollama request failed: connection refused")

    monkeypatch.setattr(main.ai_engine, "chat", fail_chat)

    body = main.ask(
        main.AskRequest(
            symbol="process_payment",
            question="How does this work when Ollama is offline?",
        ),
        x_trace_id="trace-degraded",
    )

    assert body["trace_id"] == "trace-degraded"
    assert "degraded context-only response" in body["answer"]
    assert "Ollama request failed" in body["answer"]
    assert body["context"]["primary_source"]["symbol"] == "process_payment"
    assert body["feedback_token"].startswith("fbk_")
    assert body["model_route"]["degraded"] is True
    assert body["model_route"]["reason"] == "llm_unreachable_context_only"


def test_ask_endpoint_persists_private_feedback_snapshot(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    question = "How does this work?"

    body = main.ask(
        main.AskRequest(symbol="process_payment", question=question),
        x_user_id="Alice",
        x_trace_id="trace-feedback",
    )

    snapshot = main.feedback_store.get_snapshot(body["feedback_token"])
    assert snapshot is not None
    assert snapshot.user_id == "alice"
    assert snapshot.workspace_id == body["workspace_id"]
    assert snapshot.trace_id == "trace-feedback"
    assert snapshot.question_hash != question
    assert "question" not in snapshot.to_dict()
    assert "code" not in snapshot.selected_candidates[0]
    assert snapshot.selected_candidates[0]["symbol"] == "process_payment"


def test_feedback_endpoint_records_sanitized_event(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    ask_body = main.ask(
        main.AskRequest(symbol="process_payment", question="How does this work?"),
        x_user_id="Alice",
    )

    body = main.record_feedback(
        main.FeedbackRequest(
            feedback_token=ask_body["feedback_token"],
            kind="explicit_reject",
            details={
                "missing_symbols": ["RequestTimeout.apply"],
                "comment": "This contains raw user prose and should not be stored.",
                "api_key": "secret",
            },
        ),
        x_user_id="Alice",
    )

    assert body["status"] == "recorded"
    assert body["outcome"] == "reject"
    event = main.feedback_store.recent_feedback(limit=1)[0]
    assert event["details"]["missing_symbols"] == ["RequestTimeout.apply"]
    assert event["details"]["comment_present"] is True
    assert event["details"]["comment_length"] > 0
    assert "comment" not in event["details"]
    assert event["details"]["redacted_keys"] == ["api_key"]


def test_feedback_endpoint_enforces_token_user_scope(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    ask_body = main.ask(
        main.AskRequest(symbol="process_payment", question="How does this work?"),
        x_user_id="Alice",
    )

    with pytest.raises(HTTPException) as exc_info:
        main.record_feedback(
            main.FeedbackRequest(
                feedback_token=ask_body["feedback_token"],
                kind="explicit_accept",
            ),
            x_user_id="Bob",
        )

    assert exc_info.value.status_code == 403


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


def test_metrics_endpoint_renders_prometheus_text(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    main.ask(main.AskRequest(symbol="process_payment", question="Metrics?"))
    response = main.metrics()

    assert response.media_type == "text/plain"
    assert "sidecar_requests_total" in response.body.decode()
    assert 'endpoint="/ask"' in response.body.decode()


def test_unified_search_blends_docs_symbols_and_graph(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    body = main.unified_search(
        main.UnifiedSearchRequest(query="payment flow", symbol="process_payment", limit=5),
        x_trace_id="search-trace",
    )

    assert body["trace_id"] == "search-trace"
    assert body["total"] >= 3
    assert {result["type"] for result in body["results"]} == {"doc", "symbol"}
    assert any("graph:neighbor" in result["provenance"] for result in body["results"])


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

    body = main.index_file_endpoint(main.IndexFileRequest(file_path=str(source_file), queue=False))

    assert body["status"] == "indexed"
    assert body["file_path"] == str(source_file)
    assert body["job_id"] > 0


def test_index_file_endpoint_queues_by_default(monkeypatch, tmp_path):
    main = import_main_with_fakes(monkeypatch)

    source_file = tmp_path / "app.py"
    source_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

    body = main.index_file_endpoint(main.IndexFileRequest(file_path=str(source_file)))

    assert body["status"] == "queued"
    assert body["file_path"] == str(source_file)
    assert body["job_id"] == 0
    assert body["queue_depth"] == 1


def test_index_files_endpoint_coalesces_duplicate_paths(monkeypatch, tmp_path):
    main = import_main_with_fakes(monkeypatch)

    source_file = tmp_path / "app.py"
    source_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

    body = main.index_files_endpoint(
        main.IndexFilesRequest(file_paths=[str(source_file), str(source_file)])
    )

    assert body["status"] == "queued"
    assert body["queued"] == 1
    assert body["coalesced"] == 1
    assert body["rejected"] == 0
    assert body["queue_depth"] == 1


def test_index_queue_status_endpoint(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    body = main.index_queue_status()

    assert body["status"] == "ok"
    assert body["queue"]["pending"] == 0


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
