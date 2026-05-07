import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from sidecar.history import (
    DisabledHistoryProvider,
    EphemeralSQLiteHistoryProvider,
    SQLiteHistoryProvider,
    build_history_provider,
    hash_history_text,
    parse_retention_days,
    sanitize_history_payload,
)


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
    assert conversations[0]["selected_request_id"] == ""
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
    assert bundle["inspector_snapshot"]["snapshot"]["documentation"][0]["redacted_keys"] == [
        "content"
    ]
    assert bundle["impact_snapshot"]["snapshot"]["affected_symbols"][0]["redacted_keys"] == ["code"]

    provider.set_selected_request(conversation_id, "req-1")
    conversation_bundle = provider.get_conversation_bundle(conversation_id)
    request_bundle = provider.get_request_bundle(conversation_id, "req-1")

    assert conversation_bundle is not None
    assert conversation_bundle["conversation"]["selected_request_id"] == "req-1"
    assert len(conversation_bundle["messages"]) == 2
    assert request_bundle is not None
    assert request_bundle["message"]["id"] == assistant_message_id
    assert request_bundle["ask_snapshot"]["trace_id"] == "trace-1"

    with sqlite3.connect(provider.db_path) as conn:
        stored = "\n".join(
            row[0] for row in conn.execute("SELECT snapshot_json FROM ask_snapshots")
        )
    assert raw_question not in stored
    assert raw_answer not in stored
    assert "def process_payment" not in stored


def test_sqlite_history_provider_enforces_scope_and_foreign_keys(tmp_path):
    provider = SQLiteHistoryProvider(str(tmp_path / "history.sqlite3"))
    conversation_id = provider.create_conversation(workspace_id="ws-a", user_id="alice")
    explicit_conversation_id = provider.create_conversation(
        workspace_id="ws-a",
        user_id="alice",
        conversation_id="dialog-client-1",
    )
    provider.create_conversation(workspace_id="ws-a", user_id="bob")
    provider.create_conversation(workspace_id="ws-b", user_id="alice")

    conversation_ids = {
        item["id"] for item in provider.list_conversations(workspace_id="ws-a", user_id="alice")
    }
    assert conversation_ids == {conversation_id, explicit_conversation_id}
    assert explicit_conversation_id == "dialog-client-1"

    with pytest.raises(ValueError, match="Unsupported message role"):
        provider.append_message(conversation_id=conversation_id, role="developer")

    with pytest.raises(ValueError, match="Unknown conversation"):
        provider.append_message(conversation_id="missing", role="user")

    with pytest.raises(ValueError, match="Unknown message"):
        provider.save_ask_snapshot("missing", {"trace_id": "trace"})

    with pytest.raises(ValueError, match="Unknown conversation"):
        provider.set_selected_request("missing", "req-missing")


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
        message_types = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(messages)")}
        snapshot_types = {
            row[1]: row[2] for row in conn.execute("PRAGMA table_info(ask_snapshots)")
        }

    assert message_types["content_summary"] == "TEXT"
    assert message_types["metadata_json"] == "TEXT"
    assert snapshot_types["snapshot_json"] == "TEXT"


def test_sqlite_history_provider_prunes_retained_conversations(tmp_path):
    provider = SQLiteHistoryProvider(str(tmp_path / "history.sqlite3"), retention_days=7)
    old_conversation_id = provider.create_conversation(workspace_id="ws", user_id="alice")
    kept_conversation_id = provider.create_conversation(workspace_id="ws", user_id="alice")

    old_timestamp = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    with sqlite3.connect(provider.db_path) as conn:
        conn.execute(
            "UPDATE conversations SET created_at = ?, updated_at = ? WHERE id = ?",
            (old_timestamp, old_timestamp, old_conversation_id),
        )

    assert provider.prune_retention() == 1
    assert [
        item["id"] for item in provider.list_conversations(workspace_id="ws", user_id="alice")
    ] == [kept_conversation_id]


def test_history_provider_modes_and_retention_parsing(tmp_path):
    disabled = build_history_provider(mode="disabled")
    assert isinstance(disabled, DisabledHistoryProvider)
    assert disabled.enabled is False
    assert disabled.list_conversations(workspace_id="ws", user_id="alice") == []

    ephemeral = build_history_provider(mode="ephemeral", retention_days=3)
    assert isinstance(ephemeral, EphemeralSQLiteHistoryProvider)
    assert ephemeral.enabled is True
    assert ephemeral.retention_days == 3
    assert "surgical-context-history-" in ephemeral.db_path

    local = build_history_provider(
        mode="local",
        db_path=str(tmp_path / "history.sqlite3"),
        retention_days=parse_retention_days("14"),
    )
    assert isinstance(local, SQLiteHistoryProvider)
    assert local.retention_days == 14
    assert parse_retention_days("") is None
    assert parse_retention_days(None) is None

    with pytest.raises(ValueError, match="zero or greater"):
        parse_retention_days("-1")
    with pytest.raises(ValueError, match="Unsupported HISTORY_MODE"):
        build_history_provider(mode="other")


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
