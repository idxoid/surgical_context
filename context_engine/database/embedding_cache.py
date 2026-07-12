"""SQLite cache for local embedding vectors keyed by content hash."""

from __future__ import annotations

import array
import json
import os
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from threading import Lock
from typing import Any

DEFAULT_EMBEDDING_CACHE_PATH = os.getenv(
    "EMBEDDING_CACHE_PATH",
    "./data/embedding_cache.sqlite3",
)

# SQLite default variable limit is 999; each key uses 3 bind params.
_GET_MANY_CHUNK = 300


@dataclass(frozen=True)
class EmbeddingCacheKey:
    model_name: str
    model_version: str
    content_hash: str


def pack_embedding_vector(vector: list[float] | Iterable[float]) -> bytes:
    """Serialize float32 vectors as a compact BLOB."""
    arr = array.array("f", vector)
    return arr.tobytes()


def unpack_embedding_vector(payload: bytes | str | memoryview) -> list[float]:
    """Decode float32 BLOB, with legacy JSON-list fallback."""
    if isinstance(payload, memoryview):
        payload = payload.tobytes()
    if isinstance(payload, bytes):
        # Legacy rows may still store UTF-8 JSON in a BLOB column.
        if payload[:1] in (b"[", b" ") or payload.startswith(b"\xef\xbb\xbf"):
            try:
                decoded = json.loads(payload.decode("utf-8"))
                return [float(value) for value in decoded]
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                pass
        arr = array.array("f")
        arr.frombytes(payload)
        return arr.tolist()
    decoded = json.loads(payload)
    return [float(value) for value in decoded]


class EmbeddingCache:
    """Tiny persistent vector cache for repeated local indexing runs."""

    def __init__(self, db_path: str = DEFAULT_EMBEDDING_CACHE_PATH):
        self.db_path = db_path
        self._lock = Lock()
        self._timings_ms = {
            "get_many_total": 0.0,
            "set_many_total": 0.0,
            "get_many_calls": 0,
            "set_many_calls": 0,
        }
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._ensure_schema()

    def get(self, key: EmbeddingCacheKey) -> list[float] | None:
        hits = self.get_many([key])
        return hits.get(key)

    def get_many(self, keys: list[EmbeddingCacheKey]) -> dict[EmbeddingCacheKey, list[float]]:
        """Fetch many vectors with chunked IN queries and one timestamp update."""
        if not keys:
            return {}

        unique_keys = list(dict.fromkeys(keys))
        now = int(time.time())
        hits: dict[EmbeddingCacheKey, list[float]] = {}
        t0 = time.perf_counter()
        with self._lock, self._connect() as conn:
            for start in range(0, len(unique_keys), _GET_MANY_CHUNK):
                chunk = unique_keys[start : start + _GET_MANY_CHUNK]
                placeholders = ", ".join(["(?, ?, ?)"] * len(chunk))
                params: list[str] = []
                for key in chunk:
                    params.extend([key.model_name, key.model_version, key.content_hash])
                rows = conn.execute(
                    f"""
                    SELECT model_name, model_version, content_hash, vector
                    FROM embedding_cache
                    WHERE (model_name, model_version, content_hash) IN ({placeholders})
                    """,
                    params,
                ).fetchall()
                touched: list[tuple[str, str, str]] = []
                for row in rows:
                    key = EmbeddingCacheKey(
                        model_name=row["model_name"],
                        model_version=row["model_version"],
                        content_hash=row["content_hash"],
                    )
                    hits[key] = unpack_embedding_vector(row["vector"])
                    touched.append((key.model_name, key.model_version, key.content_hash))
                if touched:
                    conn.executemany(
                        """
                        UPDATE embedding_cache
                        SET last_used_at = ?
                        WHERE model_name = ?
                          AND model_version = ?
                          AND content_hash = ?
                        """,
                        [
                            (now, model_name, model_version, content_hash)
                            for model_name, model_version, content_hash in touched
                        ],
                    )
        self._timings_ms["get_many_total"] += (time.perf_counter() - t0) * 1000
        self._timings_ms["get_many_calls"] += 1
        return hits

    def set(
        self,
        key: EmbeddingCacheKey,
        vector: list[float],
        *,
        embedding_hash: str,
    ) -> None:
        self.set_many([(key, vector, embedding_hash)])

    def set_many(
        self,
        items: list[tuple[EmbeddingCacheKey, list[float], str]],
    ) -> None:
        """Bulk upsert vectors as float32 BLOBs."""
        if not items:
            return
        now = int(time.time())
        rows = [
            (
                key.model_name,
                key.model_version,
                key.content_hash,
                embedding_hash,
                len(vector),
                pack_embedding_vector(vector),
                now,
                now,
            )
            for key, vector, embedding_hash in items
        ]
        t0 = time.perf_counter()
        with self._lock, self._connect() as conn:
            conn.executemany(
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
                rows,
            )
        self._timings_ms["set_many_total"] += (time.perf_counter() - t0) * 1000
        self._timings_ms["set_many_calls"] += 1

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
                "timings_ms": {
                    "get_many_total": round(self._timings_ms["get_many_total"], 3),
                    "set_many_total": round(self._timings_ms["set_many_total"], 3),
                    "get_many_calls": self._timings_ms["get_many_calls"],
                    "set_many_calls": self._timings_ms["set_many_calls"],
                },
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
                    vector BLOB NOT NULL,
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
