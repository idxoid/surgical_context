"""Local user history provider interfaces and defaults."""

from sidecar.history.sqlite_provider import (
    DEFAULT_HISTORY_DB_PATH,
    SQLiteHistoryProvider,
    hash_history_text,
    sanitize_history_payload,
)

__all__ = [
    "DEFAULT_HISTORY_DB_PATH",
    "SQLiteHistoryProvider",
    "hash_history_text",
    "sanitize_history_payload",
]
