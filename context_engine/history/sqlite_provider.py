"""SQLite-backed local history for conversations and prompt snapshots.

This provider stores product state, not training data. The first local slice is
metadata-first: raw prompts, answers, source snippets, and code bodies are not
persisted. Callers can store summaries, hashes, trace IDs, feedback tokens, and
sanitized prompt-contract metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Any

DEFAULT_HISTORY_DB_PATH = os.getenv(
    "HISTORY_DB_PATH",
    "./data/history/surgical_context.sqlite3",
)
DEFAULT_HISTORY_MODE = os.getenv("HISTORY_MODE", "local").lower()
DEFAULT_HISTORY_RETENTION_DAYS = os.getenv("HISTORY_RETENTION_DAYS", "").strip()

_SENSITIVE_KEYS = {
    "answer",
    "body",
    "code",
    "comment",
    "content",
    "free_text",
    "prompt",
    "question",
    "raw_answer",
    "raw_content",
    "raw_prompt",
    "response",
    "snippet",
    "source_code",
    "text",
}


def hash_history_text(text: str) -> str:
    """Return a stable hash for matching history without storing raw text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sanitize_history_payload(value: Any) -> Any:
    """Drop raw text/code-like fields from JSON payloads before storage."""
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        redacted: list[str] = []
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in _SENSITIVE_KEYS or key_text.lower().startswith("raw_"):
                redacted.append(key_text)
                continue
            sanitized[key_text] = sanitize_history_payload(item)
        if redacted:
            sanitized["redacted_keys"] = sorted(redacted)
        return sanitized
    if isinstance(value, list):
        return [sanitize_history_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_history_payload(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


class SQLiteHistoryProvider:
    """Local SQLite implementation of the HistoryProvider boundary."""

    enabled = True
    mode = "local"

    def __init__(
        self,
        db_path: str = DEFAULT_HISTORY_DB_PATH,
        *,
        retention_days: int | None = None,
    ):
        self.db_path = db_path
        self.retention_days = retention_days
        self._lock = Lock()
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._ensure_schema()
        self.prune_retention()

    def create_conversation(
        self,
        *,
        workspace_id: str,
        user_id: str,
        conversation_id: str | None = None,
        title: str = "",
        selected_request_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        conversation_id = conversation_id or self._new_id("conv")
        now = self._now()
        with self._lock, self._connect() as conn:
            self._prune_retention(conn)
            conn.execute(
                """
                INSERT INTO conversations (
                    id,
                    workspace_id,
                    user_id,
                    title,
                    selected_request_id,
                    metadata_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    workspace_id,
                    user_id,
                    title,
                    selected_request_id,
                    self._json(metadata or {}),
                    now,
                    now,
                ),
            )
        return conversation_id

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            self._prune_retention(conn)
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            return self._conversation_row(row) if row else None

    def list_conversations(
        self,
        *,
        workspace_id: str,
        user_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            self._prune_retention(conn)
            rows = conn.execute(
                """
                SELECT *
                FROM conversations
                WHERE workspace_id = ? AND user_id = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (workspace_id, user_id, max(1, min(limit, 500))),
            ).fetchall()
            return [self._conversation_row(row) for row in rows]

    def append_message(
        self,
        *,
        conversation_id: str,
        role: str,
        request_id: str = "",
        content_summary: str = "",
        content_hash: str = "",
        symbol: str = "",
        trace_id: str = "",
        feedback_token: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if role not in {"user", "assistant", "system"}:
            raise ValueError(f"Unsupported message role: {role}")

        message_id = self._new_id("msg")
        now = self._now()
        with self._lock, self._connect() as conn:
            self._prune_retention(conn)
            if not self._conversation_exists(conn, conversation_id):
                raise ValueError(f"Unknown conversation: {conversation_id}")
            conn.execute(
                """
                INSERT INTO messages (
                    id,
                    conversation_id,
                    role,
                    request_id,
                    content_summary,
                    content_hash,
                    symbol,
                    trace_id,
                    feedback_token,
                    metadata_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    conversation_id,
                    role,
                    request_id,
                    content_summary,
                    content_hash,
                    symbol,
                    trace_id,
                    feedback_token,
                    self._json(metadata or {}),
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE conversations
                SET updated_at = ?
                WHERE id = ?
                """,
                (now, conversation_id),
            )
        return message_id

    def list_messages(self, conversation_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            self._prune_retention(conn)
            rows = conn.execute(
                """
                SELECT *
                FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (conversation_id, max(1, min(limit, 1000))),
            ).fetchall()
            return [self._message_row(row) for row in rows]

    def set_selected_request(self, conversation_id: str, request_id: str) -> None:
        now = self._now()
        with self._lock, self._connect() as conn:
            self._prune_retention(conn)
            if not self._conversation_exists(conn, conversation_id):
                raise ValueError(f"Unknown conversation: {conversation_id}")
            conn.execute(
                """
                UPDATE conversations
                SET selected_request_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (request_id, now, conversation_id),
            )

    def save_ask_snapshot(self, message_id: str, snapshot: dict[str, Any]) -> None:
        self._save_snapshot("ask_snapshots", message_id, snapshot)

    def save_inspector_snapshot(self, message_id: str, snapshot: dict[str, Any]) -> None:
        self._save_snapshot("inspector_snapshots", message_id, snapshot)

    def save_impact_snapshot(self, message_id: str, snapshot: dict[str, Any]) -> None:
        self._save_snapshot("impact_snapshots", message_id, snapshot)

    def get_ask_snapshot(self, message_id: str) -> dict[str, Any] | None:
        return self._get_snapshot("ask_snapshots", message_id)

    def get_inspector_snapshot(self, message_id: str) -> dict[str, Any] | None:
        return self._get_snapshot("inspector_snapshots", message_id)

    def get_impact_snapshot(self, message_id: str) -> dict[str, Any] | None:
        return self._get_snapshot("impact_snapshots", message_id)

    def get_message_bundle(self, message_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            self._prune_retention(conn)
            message = conn.execute(
                "SELECT * FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if not message:
                return None
            return {
                "message": self._message_row(message),
                "ask_snapshot": self._snapshot_row(
                    conn.execute(
                        "SELECT * FROM ask_snapshots WHERE message_id = ?",
                        (message_id,),
                    ).fetchone()
                ),
                "inspector_snapshot": self._snapshot_row(
                    conn.execute(
                        "SELECT * FROM inspector_snapshots WHERE message_id = ?",
                        (message_id,),
                    ).fetchone()
                ),
                "impact_snapshot": self._snapshot_row(
                    conn.execute(
                        "SELECT * FROM impact_snapshots WHERE message_id = ?",
                        (message_id,),
                    ).fetchone()
                ),
            }

    def get_conversation_bundle(
        self,
        conversation_id: str,
        *,
        message_limit: int = 200,
    ) -> dict[str, Any] | None:
        conversation = self.get_conversation(conversation_id)
        if not conversation:
            return None
        messages = self.list_messages(conversation_id, limit=message_limit)
        return {
            "conversation": conversation,
            "messages": [self.get_message_bundle(message["id"]) for message in messages],
        }

    def get_request_bundle(
        self,
        conversation_id: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            self._prune_retention(conn)
            message = conn.execute(
                """
                SELECT *
                FROM messages
                WHERE conversation_id = ? AND request_id = ?
                ORDER BY CASE role WHEN 'assistant' THEN 0 ELSE 1 END, created_at DESC
                LIMIT 1
                """,
                (conversation_id, request_id),
            ).fetchone()
        if not message:
            return None
        return self.get_message_bundle(message["id"])

    def _save_snapshot(self, table: str, message_id: str, snapshot: dict[str, Any]) -> None:
        clean_snapshot = sanitize_history_payload(snapshot)
        snapshot_id = self._new_id(
            {
                "ask_snapshots": "ask_snp",
                "inspector_snapshots": "ins_snp",
                "impact_snapshots": "impact_snp",
            }[table]
        )
        trace_id = str(clean_snapshot.get("trace_id") or "")
        feedback_token = str(clean_snapshot.get("feedback_token") or "")
        symbol = str(clean_snapshot.get("symbol") or clean_snapshot.get("primary_symbol") or "")
        created_at = self._now()

        with self._lock, self._connect() as conn:
            self._prune_retention(conn)
            if not self._message_exists(conn, message_id):
                raise ValueError(f"Unknown message: {message_id}")
            conn.execute(
                f"""
                INSERT INTO {table} (
                    id,
                    message_id,
                    trace_id,
                    feedback_token,
                    symbol,
                    snapshot_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id)
                DO UPDATE SET
                    trace_id = excluded.trace_id,
                    feedback_token = excluded.feedback_token,
                    symbol = excluded.symbol,
                    snapshot_json = excluded.snapshot_json,
                    created_at = excluded.created_at
                """,
                (
                    snapshot_id,
                    message_id,
                    trace_id,
                    feedback_token,
                    symbol,
                    self._json(clean_snapshot),
                    created_at,
                ),
            )

    def _get_snapshot(self, table: str, message_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            self._prune_retention(conn)
            row = conn.execute(
                f"SELECT * FROM {table} WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            return self._snapshot_row(row)

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    selected_request_id TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_history_conversations_scope
                ON conversations(workspace_id, user_id, updated_at);

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    request_id TEXT NOT NULL DEFAULT '',
                    content_summary TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL DEFAULT '',
                    trace_id TEXT NOT NULL DEFAULT '',
                    feedback_token TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_history_messages_conversation
                ON messages(conversation_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_history_messages_request
                ON messages(request_id);

                CREATE TABLE IF NOT EXISTS ask_snapshots (
                    id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL UNIQUE,
                    trace_id TEXT NOT NULL DEFAULT '',
                    feedback_token TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL DEFAULT '',
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS inspector_snapshots (
                    id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL UNIQUE,
                    trace_id TEXT NOT NULL DEFAULT '',
                    feedback_token TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL DEFAULT '',
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS impact_snapshots (
                    id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL UNIQUE,
                    trace_id TEXT NOT NULL DEFAULT '',
                    feedback_token TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL DEFAULT '',
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
                );
                """
            )
            self._ensure_column(
                conn,
                "conversations",
                "selected_request_id",
                "TEXT NOT NULL DEFAULT ''",
            )

    def prune_retention(self) -> int:
        with self._lock, self._connect() as conn:
            return self._prune_retention(conn)

    def _prune_retention(self, conn: sqlite3.Connection) -> int:
        if self.retention_days is None:
            return 0

        cutoff = datetime.now(UTC) - timedelta(days=max(0, self.retention_days))
        cursor = conn.execute(
            """
            DELETE FROM conversations
            WHERE updated_at < ?
            """,
            (cutoff.isoformat(),),
        )
        return cursor.rowcount

    def _conversation_exists(self, conn: sqlite3.Connection, conversation_id: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        return row is not None

    def _message_exists(self, conn: sqlite3.Connection, message_id: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        return row is not None

    def _conversation_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "workspace_id": row["workspace_id"],
            "user_id": row["user_id"],
            "title": row["title"],
            "selected_request_id": row["selected_request_id"],
            "metadata": self._loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _message_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "role": row["role"],
            "request_id": row["request_id"],
            "content_summary": row["content_summary"],
            "content_hash": row["content_hash"],
            "symbol": row["symbol"],
            "trace_id": row["trace_id"],
            "feedback_token": row["feedback_token"],
            "metadata": self._loads(row["metadata_json"]),
            "created_at": row["created_at"],
        }

    def _snapshot_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        return {
            "id": row["id"],
            "message_id": row["message_id"],
            "trace_id": row["trace_id"],
            "feedback_token": row["feedback_token"],
            "symbol": row["symbol"],
            "snapshot": self._loads(row["snapshot_json"]),
            "created_at": row["created_at"],
        }

    def _json(self, value: dict[str, Any]) -> str:
        return json.dumps(sanitize_history_payload(value), separators=(",", ":"), sort_keys=True)

    def _loads(self, value: str) -> dict[str, Any]:
        return json.loads(value) if value else {}

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}_{secrets.token_urlsafe(18)}"

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()


class EphemeralSQLiteHistoryProvider(SQLiteHistoryProvider):
    """SQLite history that is deleted with the current context_engine process."""

    mode = "ephemeral"

    def __init__(self, *, retention_days: int | None = None):
        self._tempdir = TemporaryDirectory(prefix="surgical-context-history-")
        super().__init__(
            os.path.join(self._tempdir.name, "history.sqlite3"),
            retention_days=retention_days,
        )


class DisabledHistoryProvider:
    """No-op history provider for privacy-sensitive or test modes."""

    enabled = False
    mode = "disabled"
    db_path = ""
    retention_days: int | None = None

    def create_conversation(self, **kwargs: Any) -> str:
        return ""

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        return None

    def list_conversations(
        self,
        *,
        workspace_id: str,
        user_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return []

    def append_message(self, **kwargs: Any) -> str:
        return ""

    def list_messages(self, conversation_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return []

    def set_selected_request(self, conversation_id: str, request_id: str) -> None:
        return None

    def save_ask_snapshot(self, message_id: str, snapshot: dict[str, Any]) -> None:
        return None

    def save_inspector_snapshot(self, message_id: str, snapshot: dict[str, Any]) -> None:
        return None

    def save_impact_snapshot(self, message_id: str, snapshot: dict[str, Any]) -> None:
        return None

    def get_ask_snapshot(self, message_id: str) -> dict[str, Any] | None:
        return None

    def get_inspector_snapshot(self, message_id: str) -> dict[str, Any] | None:
        return None

    def get_impact_snapshot(self, message_id: str) -> dict[str, Any] | None:
        return None

    def get_message_bundle(self, message_id: str) -> dict[str, Any] | None:
        return None

    def get_conversation_bundle(
        self,
        conversation_id: str,
        *,
        message_limit: int = 200,
    ) -> dict[str, Any] | None:
        return None

    def get_request_bundle(
        self,
        conversation_id: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        return None

    def prune_retention(self) -> int:
        return 0


def parse_retention_days(value: str | int | None = DEFAULT_HISTORY_RETENTION_DAYS) -> int | None:
    if value is None or value == "":
        return None
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("HISTORY_RETENTION_DAYS must be an integer") from exc
    if days < 0:
        raise ValueError("HISTORY_RETENTION_DAYS must be zero or greater")
    return days


def build_history_provider(
    *,
    mode: str | None = None,
    db_path: str | None = None,
    retention_days: int | None = None,
) -> SQLiteHistoryProvider | EphemeralSQLiteHistoryProvider | DisabledHistoryProvider:
    selected_mode = (mode or DEFAULT_HISTORY_MODE or "local").lower()
    if selected_mode in {"disabled", "off", "none"}:
        return DisabledHistoryProvider()
    if selected_mode == "ephemeral":
        return EphemeralSQLiteHistoryProvider(retention_days=retention_days)
    if selected_mode in {"local", "sqlite", "local_docker"}:
        return SQLiteHistoryProvider(
            db_path or DEFAULT_HISTORY_DB_PATH,
            retention_days=retention_days,
        )
    raise ValueError(f"Unsupported HISTORY_MODE: {selected_mode}")
