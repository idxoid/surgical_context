import sqlite3

import pytest

from sidecar.history import SQLiteHistoryProvider, hash_history_text, sanitize_history_payload


def test_sqlite_history_provider_persists_conversations_messages_and_snapshots(tmp_path):
    provider = SQLiteHistoryProvider(str(tmp_path / "history.sqlite3"))
    raw_question = "How does process_payment handle card numbers?"
    raw_answer = "It reads the card number from source code."

    conversation_id = provider.create_conversation(
        workspace_id="local/repo@main",
        user_id="alice",
        title="Payment flow",
        metadata={"intent": "explain", "question": raw_question},
    )
    user_message_id = provider.append_message(
        conversation_id=conversation_id,
        role="user",
        request_id="req-1",
        content_summary="Question about process_payment",
        content_hash=hash_history_text(raw_question),
        symbol="process_payment",
        trace_id="trace-1",
    )
    assistant_message_id = provider.append_message(
        conversation_id=conversation_id,
        role="assistant",
        request_id="req-1",
        content_summary="Explained payment flow",
        content_hash=hash_history_text(raw_answer),
        symbol="process_payment",
        trace_id="trace-1",
        feedback_token="fbk_123",
    )

    provider.save_ask_snapshot(
        assistant_message_id,
        {
            "trace_id": "trace-1",
            "feedback_token": "fbk_123",
            "symbol": "process_payment",
            "intent": "explain",
            "question": raw_question,
            "answer": raw_answer,
            "model_route": {"provider": "ollama"},
            "primary_source": {
                "symbol": "process_payment",
                "file_path": "payments.py",
                "code": "def process_payment(): pass",
            },
        },
    )
    provider.save_inspector_snapshot(
        assistant_message_id,
        {
            "trace_id": "trace-1",
            "primary_symbol": "process_payment",
            "graph_count": 2,
            "documentation": [{"source_file": "docs/payments.md", "content": "raw docs"}],
        },
    )
    provider.save_impact_snapshot(
        assistant_message_id,
        {
            "trace_id": "trace-1",
            "symbol": "process_payment",
            "affected_symbols": [{"symbol": "refund_payment", "code": "raw code"}],
            "affected_files": ["payments.py"],
        },
    )

    conversations = provider.list_conversations(workspace_id="local/repo@main", user_id="alice")
    assert [conversation["id"] for conversation in conversations] == [conversation_id]
    assert conversations[0]["metadata"]["redacted_keys"] == ["question"]

    messages = provider.list_messages(conversation_id)
    assert [message["id"] for message in messages] == [user_message_id, assistant_message_id]
    assert messages[0]["content_hash"] == hash_history_text(raw_question)
    assert messages[1]["feedback_token"] == "fbk_123"

    bundle = provider.get_message_bundle(assistant_message_id)
    assert bundle is not None
    assert bundle["ask_snapshot"]["snapshot"]["model_route"]["provider"] == "ollama"
    assert bundle["ask_snapshot"]["snapshot"]["redacted_keys"] == ["answer", "question"]
    assert bundle["ask_snapshot"]["snapshot"]["primary_source"]["redacted_keys"] == ["code"]
    assert bundle["inspector_snapshot"]["snapshot"]["documentation"][0]["redacted_keys"] == ["content"]
    assert bundle["impact_snapshot"]["snapshot"]["affected_symbols"][0]["redacted_keys"] == ["code"]

    with sqlite3.connect(provider.db_path) as conn:
        stored = "\n".join(row[0] for row in conn.execute("SELECT snapshot_json FROM ask_snapshots"))
    assert raw_question not in stored
    assert raw_answer not in stored
    assert "def process_payment" not in stored


def test_sqlite_history_provider_enforces_scope_and_foreign_keys(tmp_path):
    provider = SQLiteHistoryProvider(str(tmp_path / "history.sqlite3"))
    conversation_id = provider.create_conversation(workspace_id="ws-a", user_id="alice")
    provider.create_conversation(workspace_id="ws-a", user_id="bob")
    provider.create_conversation(workspace_id="ws-b", user_id="alice")

    assert len(provider.list_conversations(workspace_id="ws-a", user_id="alice")) == 1
    assert provider.list_conversations(workspace_id="ws-a", user_id="alice")[0]["id"] == conversation_id

    with pytest.raises(ValueError, match="Unsupported message role"):
        provider.append_message(conversation_id=conversation_id, role="developer")

    with pytest.raises(ValueError, match="Unknown conversation"):
        provider.append_message(conversation_id="missing", role="user")

    with pytest.raises(ValueError, match="Unknown message"):
        provider.save_ask_snapshot("missing", {"trace_id": "trace"})


def test_sqlite_history_provider_does_not_truncate_text_columns(tmp_path):
    provider = SQLiteHistoryProvider(str(tmp_path / "history.sqlite3"))
    conversation_id = provider.create_conversation(workspace_id="ws", user_id="alice")
    long_summary = "safe summary " * 120
    long_metadata = "safe metadata " * 120

    message_id = provider.append_message(
        conversation_id=conversation_id,
        role="user",
        content_summary=long_summary,
        content_hash=hash_history_text(long_summary),
        metadata={"safe_long_metadata": long_metadata},
    )
    provider.save_ask_snapshot(
        message_id,
        {
            "trace_id": "trace-long",
            "safe_long_metadata": long_metadata,
        },
    )

    message = provider.list_messages(conversation_id)[0]
    snapshot = provider.get_ask_snapshot(message_id)

    assert message["content_summary"] == long_summary
    assert message["metadata"]["safe_long_metadata"] == long_metadata
    assert snapshot is not None
    assert snapshot["snapshot"]["safe_long_metadata"] == long_metadata

    with sqlite3.connect(provider.db_path) as conn:
        message_types = {
            row[1]: row[2]
            for row in conn.execute("PRAGMA table_info(messages)")
        }
        snapshot_types = {
            row[1]: row[2]
            for row in conn.execute("PRAGMA table_info(ask_snapshots)")
        }

    assert message_types["content_summary"] == "TEXT"
    assert message_types["metadata_json"] == "TEXT"
    assert snapshot_types["snapshot_json"] == "TEXT"


def test_sanitize_history_payload_redacts_nested_raw_text():
    sanitized = sanitize_history_payload(
        {
            "trace_id": "trace",
            "question": "raw prompt",
            "nested": {
                "content": "raw content",
                "safe": "metadata",
            },
            "items": [{"code": "secret", "symbol": "Payment"}],
        }
    )

    assert sanitized["redacted_keys"] == ["question"]
    assert sanitized["nested"] == {"safe": "metadata", "redacted_keys": ["content"]}
    assert sanitized["items"][0] == {"symbol": "Payment", "redacted_keys": ["code"]}
