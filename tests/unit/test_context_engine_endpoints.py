"""FastAPI endpoint tests with external services mocked out."""

import asyncio
import importlib
import os
import sys
import tempfile
import types
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from context_engine.context_types import SymbolContext
from context_engine.history import DisabledHistoryProvider
from context_engine.indexer.job_log import IndexJobLog


class FakeCtx:
    intent = "exploration"
    mode = "surgical_full"
    index_manifest_id = ""
    index_manifest_schema_version = None
    budget: dict = {}
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

    def get_index_manifest(self, workspace_id=None):
        root = os.environ.get("TEST_WORKSPACE_ROOT")
        if root:
            return {"project_path": root}
        return None

    def delete_symbols_for_file(self, file_path, workspace_id="local/surgical_context@main"):
        return None

    def get_workspace_dashboard_counts(self, workspace_id="local/surgical_context@main"):
        return {"files": 12, "symbols": 47, "symbols_with_docs": 9}

    def close(self):
        self.closed = True


@contextmanager
def fake_db_session(user_id="anonymous"):
    db = FakeDb()
    try:
        yield db
    finally:
        db.close()


def bearer_auth(
    main,
    user_id: str = "alice",
    workspace_id: str = "local/surgical_context@main",
) -> str:
    token = main.user_auth.generate_token(user_id, workspace_id=workspace_id)
    return f"Bearer {token}"


def import_main_with_fakes(monkeypatch):
    """Import context_engine.main without constructing real LanceDB/LLM clients."""
    # Client construction moved from main.py into the api.state factory, so the
    # fake LanceDB/LLM modules below only take effect if api.state (and the app
    # factory that wires it) are re-imported alongside main. Without this the
    # fixture is order-dependent: a prior test that imports main first caches
    # api.state bound to the real clients.
    for _mod in ("context_engine.main", "context_engine.api.app", "context_engine.api.state"):
        sys.modules.pop(_mod, None)
    feedback_dir = tempfile.mkdtemp(prefix="context_engine-feedback-test-")
    monkeypatch.setenv("FEEDBACK_SNAPSHOT_PATH", f"{feedback_dir}/snapshots.jsonl")
    monkeypatch.setenv("FEEDBACK_LOG_PATH", f"{feedback_dir}/feedback.jsonl")
    monkeypatch.setenv("HISTORY_DB_PATH", f"{feedback_dir}/history.sqlite3")

    fake_lancedb = types.ModuleType("context_engine.database.lancedb_client")

    class FakeLanceDBClient:
        def count_docs_workspace(self, workspace_id):
            return 23

        @staticmethod
        def storage_size_bytes():
            return 125_000_000

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
    monkeypatch.setitem(sys.modules, "context_engine.database.lancedb_client", fake_lancedb)

    fake_engine = types.ModuleType("context_engine.ai.engine")

    class FakeAIEngine:
        def __init__(self, model_preference="auto", allow_cloud_llm=True):
            self.model_preference = model_preference
            self.allow_cloud_llm = allow_cloud_llm

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
    monkeypatch.setitem(sys.modules, "context_engine.ai.engine", fake_engine)

    main = importlib.import_module("context_engine.main")

    # /ask uses the axis provider by default. Fake that seam so endpoint tests
    # get a deterministic context; fallback tests override it to return None.
    def fake_context_from_axis(
        question,
        *,
        workspace_id="",
        db=None,
        token_budget=4000,
        anchor_path=None,
        trace_id="",
        user_id="anonymous",
        **_,
    ):
        return FakeCtx()

    monkeypatch.setattr(main, "_context_from_axis", fake_context_from_axis)
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
        authorization=bearer_auth(main),
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
    assert "Ollama request failed" not in body["answer"]
    assert body["context"]["primary_source"]["symbol"] == "process_payment"
    assert body["feedback_token"].startswith("fbk_")
    assert body["model_route"]["degraded"] is True
    assert body["model_route"]["reason"] == "llm_unreachable_context_only"
    assert "error" not in body["model_route"]


def test_ask_endpoint_persists_private_feedback_snapshot(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    question = "How does this work?"
    auth = bearer_auth(main)

    body = main.ask(
        main.AskRequest(symbol="process_payment", question=question),
        authorization=auth,
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
    auth = bearer_auth(main)
    ask_body = main.ask(
        main.AskRequest(symbol="process_payment", question="How does this work?"),
        authorization=auth,
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
        authorization=auth,
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
        authorization=bearer_auth(main, "alice"),
    )

    with pytest.raises(HTTPException) as exc_info:
        main.record_feedback(
            main.FeedbackRequest(
                feedback_token=ask_body["feedback_token"],
                kind="explicit_accept",
            ),
            authorization=bearer_auth(main, "bob"),
        )

    assert exc_info.value.status_code == 403


def test_history_ask_endpoint_persists_selected_request_and_sanitized_snapshots(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    auth = bearer_auth(main)

    body = main.record_history_ask(
        main.HistoryAskRecordRequest(
            conversation_id="dialog-local-1",
            request_id="req-history",
            prompt_summary="Ask about process_payment",
            prompt_hash="prompt-hash",
            answer_summary="Assistant response recorded",
            answer_hash="answer-hash",
            symbol="process_payment",
            trace_id="trace-history",
            feedback_token="fbk_history",
            ask_snapshot={
                "context": FakeCtx().to_dict(),
                "raw_prompt": "raw user question must not be stored",
                "code": "def secret(): pass",
            },
            inspector_snapshot={
                "primary_symbol": "process_payment",
                "content": "raw inspector content",
            },
            impact_snapshot={
                "symbol": "process_payment",
                "affected_symbols": [{"symbol": "caller", "code": "raw code"}],
            },
        ),
        authorization=auth,
    )

    assert body["status"] == "recorded"
    assert body["conversation_id"] == "dialog-local-1"
    assert body["selected_request_id"] == "req-history"

    conversations = main.history_conversations(authorization=auth)
    assert conversations["conversations"][0]["id"] == body["conversation_id"]
    assert conversations["conversations"][0]["selected_request_id"] == "req-history"

    bundle = main.history_conversation(body["conversation_id"], authorization=auth)
    assert bundle["conversation"]["selected_request_id"] == "req-history"
    assert len(bundle["messages"]) == 2
    assert bundle["messages"][1]["ask_snapshot"]["snapshot"]["redacted_keys"] == [
        "code",
        "raw_prompt",
    ]

    request_bundle = main.history_request_bundle(
        body["conversation_id"],
        "req-history",
        authorization=auth,
    )
    assert request_bundle["message"]["id"] == body["assistant_message_id"]
    assert request_bundle["ask_snapshot"]["feedback_token"] == "fbk_history"
    assert request_bundle["inspector_snapshot"]["snapshot"]["redacted_keys"] == ["content"]
    assert request_bundle["impact_snapshot"]["snapshot"]["affected_symbols"][0][
        "redacted_keys"
    ] == ["code"]

    with pytest.raises(HTTPException) as exc_info:
        main.history_conversation(
            body["conversation_id"],
            authorization=bearer_auth(main, "bob"),
        )

    assert exc_info.value.status_code == 403


def test_history_ask_endpoint_is_quiet_when_history_disabled(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    monkeypatch.setattr(main, "history_provider", DisabledHistoryProvider())

    body = main.record_history_ask(
        main.HistoryAskRecordRequest(
            conversation_id="dialog-disabled",
            request_id="req-disabled",
            prompt_summary="Ask about disabled history",
            answer_summary="No-op",
        ),
        authorization=bearer_auth(main),
    )

    assert body == {
        "status": "disabled",
        "conversation_id": "dialog-disabled",
        "user_message_id": "",
        "assistant_message_id": "",
        "selected_request_id": "req-disabled",
    }
    assert main.history_conversations(authorization=bearer_auth(main)) == {"conversations": []}


def test_ask_stream_endpoint_emits_json_sse(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    response = main.ask_stream(main.AskRequest(symbol="process_payment", question="Stream it"))

    assert response.media_type == "text/event-stream"


def _parse_sse_events(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    event_name = ""
    data_lines: list[str] = []
    for line in body.splitlines():
        if not line.strip():
            if event_name and data_lines:
                import json

                events.append((event_name, json.loads("\n".join(data_lines))))
            event_name = ""
            data_lines = []
            continue
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())
    return events


async def _read_streaming_response(response) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, str):
            chunks.append(chunk.encode("utf-8"))
        else:
            chunks.append(chunk)
    return b"".join(chunks)


def test_ask_stream_degrades_when_llm_unreachable(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    def fail_stream(*args, **kwargs):
        raise RuntimeError("Ollama streaming failed: connection refused")

    monkeypatch.setattr(main.ai_engine, "stream_chat", fail_stream)

    response = main.ask_stream(
        main.AskRequest(
            symbol="process_payment",
            question="How does this work when Ollama is offline?",
        ),
        x_trace_id="trace-stream-degraded",
    )
    body = asyncio.run(_read_streaming_response(response)).decode("utf-8")
    events = _parse_sse_events(body)

    chunks = [p["content"] for name, p in events if name == "chunk"]
    assert len(chunks) == 1
    assert "degraded context-only response" in chunks[0]
    assert "Ollama streaming failed" not in chunks[0]

    context_events = [p for name, p in events if name == "context"]
    assert len(context_events) == 1
    assert context_events[0]["trace_id"] == "trace-stream-degraded"
    assert context_events[0]["context"]["primary_source"]["symbol"] == "process_payment"
    assert context_events[0]["feedback_token"].startswith("fbk_")
    assert context_events[0]["context"]["metadata"]["assembly"]["model_route"]["degraded"] is True
    assert "error" not in context_events[0]["context"]["metadata"]["assembly"]["model_route"]

    assert any(name == "done" for name, _ in events)
    assert not any(name == "error" for name, _ in events)


def test_ask_stream_emits_trace_event_on_l3_cache_hit(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    req = main.AskRequest(symbol="process_payment", question="Cached?")
    first = main.ask_stream(req)
    asyncio.run(_read_streaming_response(first))

    second = main.ask_stream(req)
    body = asyncio.run(_read_streaming_response(second)).decode("utf-8")
    events = _parse_sse_events(body)
    trace_events = [payload for name, payload in events if name == "trace"]

    assert len(trace_events) >= 2
    cache_trace = trace_events[1]
    assert cache_trace["stage"] == "llm"
    assert cache_trace["cache_hits"] == ["l3_response"]
    assert cache_trace["model_route"]["cached"] is True
    assert cache_trace["model_route"]["cache_layer"] == "l3_response"
    assert any(name == "chunk" for name, _ in events)


def test_ask_endpoint_falls_back_when_symbol_is_missing(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    monkeypatch.setattr(main, "_context_from_axis", lambda *a, **k: None)

    body = main.ask(main.AskRequest(symbol="missing", question="Where?"))

    assert body["answer"] == "fake answer"
    assert body["context"]["mode"] == "workspace"
    assert body["context"]["budget"]["ask_level"] == "workspace"
    assert body["context"]["budget"]["missing_symbol"] == "missing"
    assert body["context"]["budget"]["fallback_from"] == "symbol"
    assert body["context"]["budget"]["fallback_reason"] == "symbol_not_found"
    assert body["context"]["budget"]["fallback_ladder"] == [
        "symbol",
        "file",
        "workspace",
        "direct_llm",
    ]
    assert body["context"]["budget"]["warnings"] == [
        {
            "code": "symbol_not_found",
            "severity": "warning",
            "message": "Symbol 'missing' was not found; using workspace context.",
        }
    ]


def test_ask_endpoint_uses_file_fallback_before_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_WORKSPACE_ROOT", str(tmp_path))
    main = import_main_with_fakes(monkeypatch)
    monkeypatch.setattr(main, "_context_from_axis", lambda *a, **k: None)
    source_file = tmp_path / "checkout.py"
    source_file.write_text("def checkout():\n    return 'ok'\n", encoding="utf-8")

    body = main.ask(
        main.AskRequest(
            symbol="missing",
            question="Where?",
            file_path=str(source_file),
        )
    )

    assert body["context"]["mode"] == "file"
    assert body["context"]["primary_source"]["file_path"] == str(source_file)
    assert body["context"]["budget"]["ask_level"] == "file"
    assert body["context"]["budget"]["warnings"][0]["message"] == (
        "Symbol 'missing' was not found; using file context."
    )


def test_ask_endpoint_falls_back_to_direct_llm_when_no_context(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    monkeypatch.setattr(main, "_context_from_axis", lambda *a, **k: None)

    class EmptyVectorDb:
        def search(self, query, limit=5):
            return []

        def search_symbols(self, query, limit=5, threshold=1.0):
            return []

    monkeypatch.setattr(main, "vector_db", EmptyVectorDb())

    body = main.ask(main.AskRequest(symbol="missing", question="Where?"))

    assert body["context"]["mode"] == "direct"
    assert body["context"]["budget"]["ask_level"] == "direct_llm"
    assert body["context"]["budget"]["fallback_reason"] == "symbol_not_found"
    assert body["context"]["budget"]["warnings"][0]["message"] == (
        "Symbol 'missing' was not found; using direct LLM context."
    )


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
    assert 'sidecar_ask_context_total{mode="surgical_full"' in response.body.decode()


def test_index_stats_returns_workspace_catalog_counts(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    body = main.index_stats(x_workspace="local/surgical_context@main")

    assert body == {
        "status": "ok",
        "workspace_id": "local/surgical_context@main",
        "indexed_files": 12,
        "indexed_symbols": 47,
        "doc_chunks": 23,
        "symbols_with_docs": 9,
        "storage_bytes": 125_000_000,
    }


def test_unified_search_blends_docs_and_symbols(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    body = main.unified_search(
        main.UnifiedSearchRequest(query="payment flow", symbol="process_payment", limit=5),
        x_trace_id="search-trace",
    )

    assert body["trace_id"] == "search-trace"
    assert body["total"] >= 2
    assert {result["type"] for result in body["results"]} == {"doc", "symbol"}


def test_unified_search_includes_axis_graph_neighbors(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    # The real graph walk needs a live graph; here we mock the axis adapter to
    # assert the wiring (include_graph + symbol -> adapter -> graph:neighbor rows).
    monkeypatch.setattr(
        main,
        "_axis_graph_neighbors",
        lambda **kw: [
            {
                "type": "symbol",
                "title": "validate_amount",
                "file_path": "/repo/payment.py",
                "content": "",
                "score": 0.5,
                "scores": {"graph": 0.5},
                "provenance": ["graph:neighbor"],
                "metadata": {"uid": "u:validate_amount", "depth": 1, "reach": 1},
            }
        ],
    )

    body = main.unified_search(
        main.UnifiedSearchRequest(query="payment", symbol="process_payment", limit=5),
    )

    assert any("graph:neighbor" in r["provenance"] for r in body["results"])


def test_auth_required_accepts_valid_bearer_token(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    monkeypatch.setattr(main, "AUTH_REQUIRED", True)
    token = main.user_auth.generate_token("Alice", workspace_id="local/surgical_context@main")

    body = main.ask(
        main.AskRequest(symbol="process_payment", question="How does this work?"),
        authorization=f"Bearer {token}",
    )

    assert body["symbol"] == "process_payment"
    assert body["user"] == "alice"


def test_index_file_endpoint_tracks_job(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_WORKSPACE_ROOT", str(tmp_path))
    main = import_main_with_fakes(monkeypatch)

    source_file = tmp_path / "app.py"
    source_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

    fake_anchor = types.ModuleType("context_engine.indexer.anchor")
    fake_anchor.resolve_pending_anchors = lambda db, vector_db, workspace_id=None: None
    monkeypatch.setitem(sys.modules, "context_engine.indexer.anchor", fake_anchor)

    fake_code = types.ModuleType("context_engine.indexer.code")
    fake_code.hash_file = lambda file_path: "abc123"
    fake_code.index_file = lambda file_path, db, vector_db, extractor, workspace_id=None: None
    monkeypatch.setitem(sys.modules, "context_engine.indexer.code", fake_code)

    fake_extractor = types.ModuleType("context_engine.parser.extractor")

    class FakeSymbolExtractor:
        pass

    fake_extractor.SymbolExtractor = FakeSymbolExtractor
    monkeypatch.setitem(sys.modules, "context_engine.parser.extractor", fake_extractor)
    monkeypatch.setattr(main, "IndexJobLog", lambda: IndexJobLog(f"{tmp_path}/jobs.sqlite3"))

    body = main.index_file_endpoint(main.IndexFileRequest(file_path=str(source_file), queue=False))

    assert body["status"] == "indexed"
    assert body["file_path"] == str(source_file)
    assert body["job_id"] > 0


def test_queued_index_registers_root_before_overlay_and_index_file(monkeypatch, tmp_path):
    main = import_main_with_fakes(monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()
    source_file = project / "app.py"
    source_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

    class ManifestDb(FakeDb):
        def __init__(self):
            super().__init__()
            self._manifest = None

        def get_workspace_graph_version(self, workspace_id=None):
            return 1

        def save_index_manifest(self, manifest, workspace_id=None):
            self._manifest = dict(manifest)

        def get_index_manifest(self, workspace_id=None):
            return self._manifest

    manifest_db = ManifestDb()

    @contextmanager
    def manifest_db_session(user_id="anonymous"):
        yield manifest_db

    monkeypatch.setattr(main, "db_session", manifest_db_session)
    monkeypatch.setattr(
        main,
        "_enqueue_index_files",
        lambda files, workspace_id, user_id: [
            main.EnqueueResult(
                accepted=True,
                status="queued",
                file_path=files[0],
                workspace_id=workspace_id,
                queue_depth=1,
            )
        ],
    )

    index_body = main.index(
        main.IndexRequest(project_path=str(project), queue=True),
        authorization=bearer_auth(main, workspace_id="local/proj@main"),
        x_workspace="local/proj@main",
    )
    assert index_body["status"] == "queued"

    overlay_body = main.update_overlay(
        main.OverlayRequest(
            file_path=str(source_file),
            content="def hello():\n    return 'ok'\n",
        ),
        authorization=bearer_auth(main, workspace_id="local/proj@main"),
        x_workspace="local/proj@main",
    )
    assert overlay_body["file_path"] == str(source_file.resolve())

    fake_anchor = types.ModuleType("context_engine.indexer.anchor")
    fake_anchor.resolve_pending_anchors = lambda db, vector_db, workspace_id=None: None
    monkeypatch.setitem(sys.modules, "context_engine.indexer.anchor", fake_anchor)
    fake_code = types.ModuleType("context_engine.indexer.code")
    fake_code.hash_file = lambda file_path: "abc123"
    fake_code.index_file = lambda *args, **kwargs: []
    monkeypatch.setitem(sys.modules, "context_engine.indexer.code", fake_code)
    fake_extractor = types.ModuleType("context_engine.parser.extractor")

    class FakeSymbolExtractor:
        pass

    fake_extractor.SymbolExtractor = FakeSymbolExtractor
    monkeypatch.setitem(sys.modules, "context_engine.parser.extractor", fake_extractor)
    monkeypatch.setattr(main, "IndexJobLog", lambda: IndexJobLog(f"{tmp_path}/jobs.sqlite3"))

    file_body = main.index_file_endpoint(
        main.IndexFileRequest(file_path=str(source_file), queue=False)
    )
    assert file_body["status"] == "indexed"


def test_index_file_endpoint_queues_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_WORKSPACE_ROOT", str(tmp_path))
    main = import_main_with_fakes(monkeypatch)

    source_file = tmp_path / "app.py"
    source_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

    body = main.index_file_endpoint(main.IndexFileRequest(file_path=str(source_file)))

    assert body["status"] == "queued"
    assert body["file_path"] == str(source_file)
    assert body["job_id"] == 0
    assert body["queue_depth"] == 1


def test_index_files_endpoint_coalesces_duplicate_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_WORKSPACE_ROOT", str(tmp_path))
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


def test_process_index_batch_skips_unsupported_extensions(monkeypatch, tmp_path):
    main = import_main_with_fakes(monkeypatch)
    skipped_file = tmp_path / "settings.json"
    skipped_file.write_text('{"editor.tabSize": 2}\n', encoding="utf-8")
    metric_calls = []

    monkeypatch.setattr(
        main.default_metrics,
        "increment",
        lambda name, value=1, labels=None: metric_calls.append((name, value, labels)),
    )
    monkeypatch.setattr(
        "context_engine.indexer.code.index_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("index_file should not run")),
    )

    main._process_index_batch(
        [
            main.IndexWorkItem(
                file_path=str(skipped_file),
                workspace_id="local/surgical_context@main",
                user_id="alice",
            )
        ]
    )

    assert (
        "sidecar_index_queue_skipped_total",
        1,
        {"reason": "unsupported_extension", "workspace": "local/surgical_context@main"},
    ) in metric_calls


def test_process_index_batch_runs_axis_finalize_after_batch(monkeypatch, tmp_path):
    main = import_main_with_fakes(monkeypatch)
    source_file = tmp_path / "settings.py"
    source_file.write_text(
        "class Settings:\n    host: str = 'localhost'\n",
        encoding="utf-8",
    )
    changed_uids = ["settings-class-uid"]
    finalize_calls = []

    monkeypatch.setenv("INDEX_PROFILE", "axis_python_v1")
    monkeypatch.setattr(
        "context_engine.indexer.git_committed.should_index_file",
        lambda file_path, **kwargs: True,
    )
    monkeypatch.setattr(
        main, "vector_db", types.SimpleNamespace(index_profile_name="axis_python_v1")
    )
    monkeypatch.setattr(
        "context_engine.indexer.code.hash_file",
        lambda file_path: "hash-settings",
    )
    monkeypatch.setattr(
        "context_engine.indexer.code.index_file",
        lambda path, db, lance, extractor, **kwargs: (
            kwargs.get("collected_adjacency_seeds", set()).update({"seed-uid"}) or changed_uids
        ),
    )
    monkeypatch.setattr(
        "context_engine.indexer.fast.pipeline.run_axis_incremental_finalize",
        lambda db, lance, workspace_id, **kwargs: (
            finalize_calls.append({"workspace_id": workspace_id, **kwargs}) or {}
        ),
    )
    monkeypatch.setattr(
        "context_engine.indexer.affects.AFFECTSIndexer",
        lambda db: types.SimpleNamespace(
            rebuild_affects=lambda uids, workspace_id: None,
        ),
    )
    monkeypatch.setattr(
        "context_engine.indexer.anchor.resolve_pending_anchors",
        lambda db, vector_db, workspace_id=None: None,
    )

    class HashDb(FakeDb):
        def get_file_hashes(self, paths, workspace_id=None):
            return {}

    @contextmanager
    def batch_db_session(user_id="anonymous"):
        yield HashDb()

    monkeypatch.setattr(main, "db_session", batch_db_session)

    main._process_index_batch(
        [
            main.IndexWorkItem(
                file_path=str(source_file),
                workspace_id="local/surgical_context@main",
                user_id="alice",
            )
        ]
    )

    assert len(finalize_calls) == 1
    assert finalize_calls[0]["workspace_id"] == "local/surgical_context@main+axis_python_v1"
    assert changed_uids[0] in finalize_calls[0]["seed_uids"]
    assert "seed-uid" in finalize_calls[0]["seed_uids"]
    assert finalize_calls[0]["project_path"] == str(tmp_path)


def test_ask_rejects_file_path_outside_workspace_root(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    monkeypatch.setenv("TEST_WORKSPACE_ROOT", str(root))
    main = import_main_with_fakes(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        main._resolve_ask_context(
            req=main.AskRequest(question="q", file_path=str(outside), token_budget=1000),
            user_id="alice",
            workspace_id="local/surgical_context@main",
            db=FakeDb(),
        )
    assert exc_info.value.status_code == 403


def test_impact_endpoint_returns_affected_symbols(monkeypatch):
    monkeypatch.setenv("INDEX_PROFILE", "axis_python_v1")
    main = import_main_with_fakes(monkeypatch)
    from context_engine.axis import impact_surface

    seen_session_users: list[str] = []
    seen_surface_args: list[dict] = []

    class FakeDriverDb(FakeDb):
        def resolve_impact_symbol_uid(self, name, workspace_id="local/surgical_context@main", *, file_path=None):
            return "symbol-1"

        def get_symbol_uid_by_name(self, name, workspace_id="local/surgical_context@main"):
            return "symbol-1"

        def get_file_path_for_symbol(self, uid, workspace_id="local/surgical_context@main"):
            return "/repo/app.py"

    @contextmanager
    def impact_db_session(user_id="anonymous"):
        seen_session_users.append(user_id)
        yield FakeDriverDb()

    def fake_build_impact_surface(
        *,
        db,
        symbol_uid,
        symbol_name,
        file_path,
        workspace_id,
        max_depth,
    ):
        seen_surface_args.append(
            {
                "symbol_uid": symbol_uid,
                "symbol_name": symbol_name,
                "file_path": file_path,
                "workspace_id": workspace_id,
                "max_depth": max_depth,
                "db": db,
            }
        )
        return {
            "affected_symbols": [
                {
                    "uid": "affected-1",
                    "name": "caller",
                    "file_path": "/repo/caller.py",
                    "depth": 1,
                    "kind": "reverse_calls",
                    "edge_type": "CALLS_*",
                    "utility_score": 0.95,
                }
            ],
            "affected_files": ["/repo/caller.py"],
            "max_depth": 3,
        }

    monkeypatch.setattr(impact_surface, "build_impact_surface", fake_build_impact_surface)
    monkeypatch.setattr(main, "db_session", impact_db_session)

    body = main.impact(
        symbol="process_payment",
        max_depth=2,
        authorization=bearer_auth(main),
    )

    assert body["symbol_uid"] == "symbol-1"
    assert body["affected_count"] == 1
    assert body["affected_files"] == ["/repo/caller.py"]
    assert body["max_depth"] == 3
    assert body["affected_symbols"][0]["edge_type"] == "CALLS_*"
    assert seen_surface_args[0]["symbol_uid"] == "symbol-1"
    assert seen_surface_args[0]["symbol_name"] == "process_payment"
    assert seen_surface_args[0]["file_path"] == "/repo/app.py"
    assert seen_surface_args[0]["max_depth"] == 2
    assert seen_surface_args[0]["workspace_id"] == "local/surgical_context@main+axis_python_v1"
    assert seen_session_users == ["alice"]


def test_cloud_status_uses_request_user_for_db_session(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    seen_session_users: list[str] = []

    class FakeCloudDb(FakeDb):
        def health_check(self):
            return {"status": "ok"}

        def is_cloud(self):
            return True

        def is_fallback(self):
            return False

    @contextmanager
    def cloud_db_session(user_id="anonymous"):
        seen_session_users.append(user_id)
        yield FakeCloudDb()

    monkeypatch.setattr(main, "db_session", cloud_db_session)

    body = main.cloud_status(authorization=bearer_auth(main, "alice"))

    assert body == {
        "cloud_enabled": True,
        "using_aura": True,
        "using_fallback": False,
        "health": {"status": "ok"},
    }
    assert seen_session_users == ["alice"]


def test_cloud_status_returns_degraded_payload_when_graph_unavailable(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    @contextmanager
    def failing_db_session(user_id="anonymous"):
        del user_id
        from neo4j.exceptions import AuthError

        raise AuthError("The client is unauthorized due to authentication failure.")

    monkeypatch.setattr(main, "db_session", failing_db_session)

    body = main.cloud_status(authorization=bearer_auth(main, "alice"))

    assert body["cloud_enabled"] is False
    assert body["using_aura"] is False
    assert body["health"]["status"] == "unhealthy"
    assert "unauthorized" in body["health"]["error"].lower()
    assert "NEO4J_PASSWORD" in body["health"]["hint"]


def test_audit_actions_endpoint_returns_actions(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    seen: dict[str, object] = {}

    class FakeAuditLog:
        def get_recent_actions(self, user_id=None, limit=100):
            seen["user_id"] = user_id
            seen["limit"] = limit
            return [{"user_id": user_id or "alice", "action": "query"}]

    monkeypatch.setattr(main, "audit_log", FakeAuditLog())

    body = main.audit_actions(limit=1, authorization=bearer_auth(main, "alice"))

    assert body == {
        "actions": [{"user_id": "alice", "action": "query"}],
        "total": 1,
    }
    assert seen == {"user_id": "alice", "limit": 1}


def test_audit_actions_endpoint_rejects_cross_user_reads(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        main.audit_actions(
            user_id="bob",
            authorization=bearer_auth(main, "alice"),
        )

    assert exc_info.value.status_code == 403


def test_auth_token_endpoint_returns_token(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    body = main.auth_token(
        user_id="Alice",
        x_workspace="local/surgical_context@main",
    )

    assert body["user_id"] == "alice"
    assert body["token"]
    assert body["expires_in_hours"] == 24
    assert main.user_auth.get_workspace_from_token(body["token"]) == "local/surgical_context@main"


def test_auth_token_requires_bearer_when_auth_required(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    monkeypatch.setattr(main, "AUTH_REQUIRED", True)

    with pytest.raises(HTTPException) as exc_info:
        main.auth_token(user_id="Alice")

    assert exc_info.value.status_code == 401


def test_auth_token_allows_self_refresh_when_auth_required(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    monkeypatch.setattr(main, "AUTH_REQUIRED", True)
    token = main.user_auth.generate_token("Alice", workspace_id="local/surgical_context@main")

    body = main.auth_token(user_id="Alice", authorization=f"Bearer {token}")

    assert body["user_id"] == "alice"
    assert body["token"]


def test_auth_token_rejects_cross_user_mint_when_auth_required(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    monkeypatch.setattr(main, "AUTH_REQUIRED", True)
    token = main.user_auth.generate_token("Alice", workspace_id="local/surgical_context@main")

    with pytest.raises(HTTPException) as exc_info:
        main.auth_token(user_id="Bob", authorization=f"Bearer {token}")

    assert exc_info.value.status_code == 403


def test_resolve_request_user_ignores_x_user_id_by_default(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    user_id = main._resolve_request_user(x_user_id="Alice")

    assert user_id == "anonymous"


def test_resolve_request_user_honors_bearer_over_x_user_id(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    token = main.user_auth.generate_token("carol", workspace_id="local/surgical_context@main")

    user_id = main._resolve_request_user(
        x_user_id="Alice",
        authorization=f"Bearer {token}",
    )

    assert user_id == "carol"


def test_resolve_workspace_ignores_spoofed_header_without_token(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    workspace_id = main._resolve_workspace(x_workspace="local/evil@main")

    assert workspace_id == main.DEFAULT_WORKSPACE_ID


def test_resolve_workspace_rejects_header_token_mismatch(monkeypatch):
    main = import_main_with_fakes(monkeypatch)
    token = main.user_auth.generate_token("alice", workspace_id="local/surgical_context@main")

    with pytest.raises(HTTPException) as exc_info:
        main._resolve_workspace(
            x_workspace="local/evil@main",
            authorization=f"Bearer {token}",
        )

    assert exc_info.value.status_code == 403


def test_list_users_requires_bearer_token(monkeypatch):
    main = import_main_with_fakes(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        main.list_users()

    assert exc_info.value.status_code == 401


def test_index_rejects_workspace_root_hijack(monkeypatch, tmp_path):
    main = import_main_with_fakes(monkeypatch)
    registered = tmp_path / "victim"
    registered.mkdir()
    attacker = tmp_path / "attacker"
    attacker.mkdir()

    class ManifestDb(FakeDb):
        def __init__(self):
            super().__init__()
            self._manifest = {"project_path": str(registered)}

        def get_index_manifest(self, workspace_id=None):
            return self._manifest

        def save_index_manifest(self, manifest, workspace_id=None):
            self._manifest = dict(manifest)

    manifest_db = ManifestDb()

    @contextmanager
    def manifest_db_session(user_id="anonymous"):
        yield manifest_db

    monkeypatch.setattr(main, "db_session", manifest_db_session)

    with pytest.raises(HTTPException) as exc_info:
        main.index(
            main.IndexRequest(project_path=str(attacker), queue=True),
            authorization=bearer_auth(main, workspace_id="local/victim@main"),
            x_workspace="local/victim@main",
        )

    assert exc_info.value.status_code == 403
    assert "already registered" in str(exc_info.value.detail).lower()


def test_index_rejects_repo_name_mismatch(monkeypatch, tmp_path):
    main = import_main_with_fakes(monkeypatch)
    project = tmp_path / "wrong-name"
    project.mkdir()

    with pytest.raises(HTTPException) as exc_info:
        main.index(
            main.IndexRequest(project_path=str(project), queue=True),
            authorization=bearer_auth(main, workspace_id="local/expected@main"),
            x_workspace="local/expected@main",
        )

    assert exc_info.value.status_code == 403
    assert "does not match" in str(exc_info.value.detail).lower()
    main = import_main_with_fakes(monkeypatch)

    def fail_context(*args, **kwargs):
        raise RuntimeError("neo4j://secret-host:7687 connection refused")

    monkeypatch.setattr(main, "_resolve_ask_context", fail_context)

    response = main.ask_stream(
        main.AskRequest(symbol="process_payment", question="Fail early"),
        x_trace_id="trace-stream-error",
    )
    body = asyncio.run(_read_streaming_response(response)).decode("utf-8")
    events = _parse_sse_events(body)
    error_events = [payload for name, payload in events if name == "error"]

    assert len(error_events) == 1
    assert error_events[0]["error"] == "An internal error occurred"
    assert "neo4j" not in error_events[0]["error"]
    assert error_events[0]["trace_id"] == "trace-stream-error"


def test_index_file_endpoint_redacts_internal_error(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_WORKSPACE_ROOT", str(tmp_path))
    main = import_main_with_fakes(monkeypatch)
    source_file = tmp_path / "app.py"
    source_file.write_text("def hello():\n    pass\n", encoding="utf-8")

    def fail_now(*args, **kwargs):
        raise RuntimeError("neo4j://secret-host:7687 write failed")

    monkeypatch.setattr(main, "_index_file_now", fail_now)

    with pytest.raises(HTTPException) as exc_info:
        main.index_file_endpoint(main.IndexFileRequest(file_path=str(source_file), queue=False))

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail["error"] == "An internal error occurred"
    assert "neo4j" not in exc_info.value.detail["error"]


def test_index_files_endpoint_redacts_sync_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_WORKSPACE_ROOT", str(tmp_path))
    main = import_main_with_fakes(monkeypatch)
    source_file = tmp_path / "app.py"
    source_file.write_text("def hello():\n    pass\n", encoding="utf-8")

    def fail_now(*args, **kwargs):
        raise RuntimeError("lancedb path /secret/data failed")

    monkeypatch.setattr(main, "_index_file_now", fail_now)

    body = main.index_files_endpoint(
        main.IndexFilesRequest(file_paths=[str(source_file)], queue=False)
    )

    failed = [result for result in body["results"] if result["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["reason"] == "index_failed"
    assert "lancedb" not in failed[0]["reason"]
