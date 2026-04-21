"""SQLite cache for local embedding vectors keyed by content hash."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any

DEFAULT_EMBEDDING_CACHE_PATH = os.getenv(
    "EMBEDDING_CACHE_PATH",
    "./data/embedding_cache.sqlite3",
)


@dataclass(frozen=True)
class EmbeddingCacheKey:
    model_name: str
    model_version: str
    content_hash: str


class EmbeddingCache:
    """Tiny persistent vector cache for repeated local indexing runs."""

    def __init__(self, db_path: str = DEFAULT_EMBEDDING_CACHE_PATH):
        self.db_path = db_path
        self._lock = Lock()
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._ensure_schema()

    def get(self, key: EmbeddingCacheKey) -> list[float] | None:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT vector
                FROM embedding_cache
                WHERE model_name = ?
                  AND model_version = ?
                  AND content_hash = ?
                """,
                (key.model_name, key.model_version, key.content_hash),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """
                UPDATE embedding_cache
                SET last_used_at = ?
                WHERE model_name = ?
                  AND model_version = ?
                  AND content_hash = ?
                """,
                (now, key.model_name, key.model_version, key.content_hash),
            )
            vector = json.loads(row["vector"])
            return [float(value) for value in vector]

    def set(
        self,
        key: EmbeddingCacheKey,
        vector: list[float],
        *,
        embedding_hash: str,
    ) -> None:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO embedding_cache (
                    model_name,
                    model_version,
                    content_hash,
                    embedding_hash,
                    dimensions,
                    vector,
                    created_at,
                    last_used_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(model_name, model_version, content_hash)
                DO UPDATE SET
                    embedding_hash = excluded.embedding_hash,
                    dimensions = excluded.dimensions,
                    vector = excluded.vector,
                    last_used_at = excluded.last_used_at
                """,
                (
                    key.model_name,
                    key.model_version,
                    key.content_hash,
                    embedding_hash,
                    len(vector),
                    json.dumps(vector),
                    now,
                    now,
                ),
            )

    def stats(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS count FROM embedding_cache").fetchone()
            by_model = conn.execute(
                """
                SELECT model_name, model_version, COUNT(*) AS count
                FROM embedding_cache
                GROUP BY model_name, model_version
                ORDER BY model_name, model_version
                """
            ).fetchall()
            return {
                "path": self.db_path,
                "total": int(total["count"]) if total else 0,
                "models": [
                    {
                        "model_name": row["model_name"],
                        "model_version": row["model_version"],
                        "count": int(row["count"]),
                    }
                    for row in by_model
                ],
            }

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS embedding_cache (
                    model_name TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    embedding_hash TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER NOT NULL,
                    PRIMARY KEY (model_name, model_version, content_hash)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_embedding_cache_last_used
                ON embedding_cache(last_used_at)
                """
            )

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
