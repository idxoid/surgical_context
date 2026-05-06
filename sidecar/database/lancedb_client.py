import json
import os
import time
from collections import OrderedDict
from collections.abc import Callable

import lancedb
import pyarrow as pa
from sentence_transformers import SentenceTransformer

from sidecar.database.embedding_cache import EmbeddingCache, EmbeddingCacheKey
from sidecar.database.embedding_registry import (
    EmbeddingMetadata,
    EmbeddingModelMismatch,
    compute_chunk_hash,
    compute_embedding_hash,
    get_model_metadata,
)
from sidecar.workspace import DEFAULT_WORKSPACE_ID

DB_PATH = os.getenv("LANCEDB_PATH", "./data/lancedb")
EMBED_MODEL = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")
EMBED_CACHE_ENABLED = os.getenv("EMBED_CACHE_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "32"))
EMBED_THROTTLE_MS = int(os.getenv("EMBED_THROTTLE_MS", "0"))
EMBED_LOW_PRIORITY = os.getenv("EMBED_LOW_PRIORITY", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
EMBED_LOW_PRIORITY_THROTTLE_MS = int(os.getenv("EMBED_LOW_PRIORITY_THROTTLE_MS", "25"))
LANCEDB_DELETE_BATCH_SIZE = int(os.getenv("LANCEDB_DELETE_BATCH_SIZE", "256"))
DOCS_TABLE = "docs"
SYMBOLS_TABLE = "symbols"


def _l2_to_score(distance: float) -> float:
    """Map LanceDB L2 distance to a [0, 1] similarity score.

    SentenceTransformer ``all-MiniLM-L6-v2`` produces L2-normalized vectors,
    so for any two unit vectors ``a`` and ``b``:
        ||a - b||² = 2 - 2·cos(a, b)
    LanceDB returns the *non-squared* L2 distance ``d = ||a - b||``, so:
        cos(a, b) = 1 - d² / 2
    Mapping cosine similarity into [0, 1] gives ``(1 + cos) / 2``.

    The previous formula ``max(0, 1 - d)`` cut off at d = 1.0, which throws
    away signal from any moderately similar pair (typical similarities give
    d in the 0.8–1.4 range). The corrected score keeps the ranking smooth.
    """
    cos = 1.0 - (distance * distance) / 2.0
    return max(0.0, min(1.0, (1.0 + cos) / 2.0))


DOCS_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string()),
        pa.field("workspace_id", pa.string()),
        pa.field("file_path", pa.string()),
        pa.field("chunk", pa.string()),
        pa.field("pending", pa.list_(pa.string())),
        pa.field("vector", pa.list_(pa.float32(), 384)),
        pa.field("embedding_metadata", pa.string()),  # JSON serialized
    ]
)

SYMBOLS_SCHEMA = pa.schema(
    [
        pa.field("uid", pa.string()),
        pa.field("workspace_id", pa.string()),
        pa.field("name", pa.string()),
        pa.field("file_path", pa.string()),
        pa.field("code", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), 384)),
        pa.field("embedding_metadata", pa.string()),  # JSON serialized
    ]
)


class LanceDBClient:
    def __init__(self):
        self._db = lancedb.connect(DB_PATH)
        self._model = SentenceTransformer(EMBED_MODEL)
        self._model_metadata = get_model_metadata(EMBED_MODEL)
        if self._model_metadata is None:
            raise ValueError(f"Unknown embedding model: {EMBED_MODEL}")
        self._embedding_cache_enabled = EMBED_CACHE_ENABLED
        self._embedding_cache = EmbeddingCache() if self._embedding_cache_enabled else None
        self._embed_batch_size = max(1, EMBED_BATCH_SIZE)
        throttle_ms = max(
            EMBED_THROTTLE_MS, EMBED_LOW_PRIORITY_THROTTLE_MS if EMBED_LOW_PRIORITY else 0
        )
        self._embed_throttle_seconds = throttle_ms / 1000
        self._embedding_stats = {"cache_hits": 0, "cache_misses": 0, "encoded": 0}
        self._table = self._open_or_reset_table(
            DOCS_TABLE,
            DOCS_SCHEMA,
            required_columns={"id", "workspace_id", "file_path", "chunk", "pending", "vector"},
        )
        self._sym_table = self._open_or_reset_table(
            SYMBOLS_TABLE,
            SYMBOLS_SCHEMA,
            required_columns={"uid", "workspace_id", "name", "file_path", "code", "vector"},
        )

    def _open_or_reset_table(self, name: str, schema: pa.Schema, *, required_columns: set[str]):
        if name not in self._db.table_names():
            return self._db.create_table(name, schema=schema)
        table = self._db.open_table(name)
        try:
            current = set(table.schema.names)
        except Exception:
            current = set()
        if required_columns.issubset(current):
            return table
        # No in-place migration: reset table and force full reindex.
        self._db.drop_table(name)
        return self._db.create_table(name, schema=schema)

    @staticmethod
    def _quote_delete_value(value: str) -> str:
        return value.replace("'", "''")

    def _delete_doc_rows(
        self,
        file_paths: list[str],
        workspace_id: str,
        *,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        if not file_paths:
            return
        batch_size = max(1, LANCEDB_DELETE_BATCH_SIZE)
        total = len(file_paths)
        for start in range(0, total, batch_size):
            batch = file_paths[start : start + batch_size]
            predicate = " OR ".join(
                (
                    f"(workspace_id = '{self._quote_delete_value(workspace_id)}' "
                    f"AND file_path = '{self._quote_delete_value(file_path)}')"
                )
                for file_path in batch
            )
            try:
                self._table.delete(predicate)
            except Exception:
                for file_path in batch:
                    try:
                        self._table.delete(
                            f"(workspace_id = '{self._quote_delete_value(workspace_id)}' "
                            f"AND file_path = '{self._quote_delete_value(file_path)}')"
                        )
                    except Exception:
                        pass
            if progress_callback:
                progress_callback(f"delete progress: {min(start + len(batch), total)}/{total}")

    def _delete_symbol_rows(
        self,
        uids: list[str],
        workspace_id: str,
        *,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        if not uids:
            return
        batch_size = max(1, LANCEDB_DELETE_BATCH_SIZE)
        total = len(uids)
        for start in range(0, total, batch_size):
            batch = uids[start : start + batch_size]
            predicate = " OR ".join(
                (
                    f"(workspace_id = '{self._quote_delete_value(workspace_id)}' "
                    f"AND uid = '{self._quote_delete_value(uid)}')"
                )
                for uid in batch
            )
            try:
                self._sym_table.delete(predicate)
            except Exception:
                for uid in batch:
                    try:
                        self._sym_table.delete(
                            f"(workspace_id = '{self._quote_delete_value(workspace_id)}' "
                            f"AND uid = '{self._quote_delete_value(uid)}')"
                        )
                    except Exception:
                        pass
            if progress_callback:
                progress_callback(f"delete progress: {min(start + len(batch), total)}/{total}")

    def _embed(
        self,
        texts: list[str],
        progress_callback: Callable[[str], None] | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []

        vectors: list[list[float] | None] = [None] * len(texts)
        missing_by_hash: OrderedDict[str, str] = OrderedDict()
        content_hashes = [compute_chunk_hash(text) for text in texts]
        cache_keys: dict[str, EmbeddingCacheKey] = {
            content_hash: EmbeddingCacheKey(
                model_name=EMBED_MODEL,
                model_version=self._model_metadata.version,
                content_hash=content_hash,
            )
            for content_hash in dict.fromkeys(content_hashes)
        }
        cached_vectors = (
            self._embedding_cache.get_many(list(cache_keys.values()))
            if self._embedding_cache
            else {}
        )

        for index, (text, content_hash) in enumerate(zip(texts, content_hashes, strict=False)):
            key = cache_keys[content_hash]
            cached = cached_vectors.get(key)
            if cached is not None:
                vectors[index] = cached
                self._embedding_stats["cache_hits"] += 1
            else:
                missing_by_hash.setdefault(content_hash, text)
                self._embedding_stats["cache_misses"] += 1

        encoded_by_hash: dict[str, list[float]] = {}
        missing_items = list(missing_by_hash.items())
        if progress_callback:
            progress_callback(
                f"cache scan: total={len(texts)} missing={len(missing_items)} batch_size={self._embed_batch_size}"
            )
        for start in range(0, len(missing_items), self._embed_batch_size):
            batch = missing_items[start : start + self._embed_batch_size]
            encoded = self._model.encode([text for _, text in batch], show_progress_bar=False)
            for (content_hash, _), row in zip(batch, encoded, strict=False):
                vector = [float(value) for value in row]
                encoded_by_hash[content_hash] = vector
                self._embedding_stats["encoded"] += 1
                if self._embedding_cache:
                    self._embedding_cache.set(
                        cache_keys[content_hash],
                        vector,
                        embedding_hash=compute_embedding_hash(vector),
                    )
            if progress_callback:
                progress_callback(
                    f"encode: {min(start + len(batch), len(missing_items))}/{len(missing_items)}"
                )
            if self._embed_throttle_seconds and start + self._embed_batch_size < len(missing_items):
                time.sleep(self._embed_throttle_seconds)

        for index, content_hash in enumerate(content_hashes):
            if vectors[index] is None:
                vectors[index] = encoded_by_hash[content_hash]

        output = []
        for maybe_vector in vectors:
            if maybe_vector is None:
                raise RuntimeError("Embedding vector was not populated")
            output.append(maybe_vector)
        return output

    def embedding_cache_stats(self) -> dict:
        cache_stats = self._embedding_cache.stats() if self._embedding_cache else {"enabled": False}
        return {
            "enabled": self._embedding_cache_enabled,
            "batch_size": self._embed_batch_size,
            "throttle_ms": int(self._embed_throttle_seconds * 1000),
            "runtime": dict(self._embedding_stats),
            "cache": cache_stats,
        }

    def upsert_chunks(
        self,
        file_path: str,
        chunks: list[str],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        self.upsert_chunk_batches([(file_path, chunks)], workspace_id=workspace_id)

    def upsert_chunk_batches(
        self,
        file_chunks: list[tuple[str, list[str]]],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        entries: list[tuple[str, int, str]] = []
        file_paths: list[str] = []
        for file_path, chunks in file_chunks:
            file_paths.append(file_path)
            entries.extend((file_path, i, chunk) for i, chunk in enumerate(chunks))
        if not entries:
            return

        if progress_callback:
            progress_callback(f"prepare: files={len(file_paths)} chunks={len(entries)}")
        t0 = time.perf_counter()
        vectors = self._embed(
            [chunk for _, _, chunk in entries], progress_callback=progress_callback
        )
        if progress_callback:
            progress_callback(f"embed done in {time.perf_counter() - t0:.2f}s")

        rows = []
        for (file_path, i, chunk), vec in zip(entries, vectors, strict=False):
            metadata = EmbeddingMetadata(
                model_name=EMBED_MODEL,
                model_version=self._model_metadata.version,
                chunk_hash=compute_chunk_hash(chunk),
                embedding_hash=compute_embedding_hash(vec),
            )
            rows.append(
                {
                    "id": f"{file_path}::{i}",
                    "workspace_id": workspace_id,
                    "file_path": file_path,
                    "chunk": chunk,
                    "pending": [],
                    "vector": vec,
                    "embedding_metadata": json.dumps(
                        {
                            "model_name": metadata.model_name,
                            "model_version": metadata.model_version,
                            "chunk_hash": metadata.chunk_hash,
                            "embedding_hash": metadata.embedding_hash,
                        }
                    ),
                }
            )

        if progress_callback:
            progress_callback(f"delete existing rows: {len(file_paths)}")
        t0 = time.perf_counter()
        self._delete_doc_rows(file_paths, workspace_id, progress_callback=progress_callback)
        if progress_callback:
            progress_callback(f"delete done in {time.perf_counter() - t0:.2f}s")
            progress_callback(f"add rows: {len(rows)}")
        t0 = time.perf_counter()
        self._table.add(rows)
        if progress_callback:
            progress_callback(f"add done in {time.perf_counter() - t0:.2f}s")

    def get_pending(self, *, workspace_id: str = DEFAULT_WORKSPACE_ID) -> dict[str, list[str]]:
        """Returns {chunk_id: [name, ...]} for all chunks with pending identifiers.

        Uses Lance's native ``WHERE`` clause so the scan is pushed down at
        the storage layer instead of materializing the full table in
        pandas just to filter it.
        """
        rows = self._scan_pending(columns=["id", "pending"], workspace_id=workspace_id)
        return {row["id"]: list(row["pending"]) for row in rows}

    def get_pending_rows(self, *, workspace_id: str = DEFAULT_WORKSPACE_ID) -> list[dict]:
        """Return full doc rows that still have unresolved pending identifiers."""
        return self._scan_pending(columns=None, workspace_id=workspace_id)

    def _scan_pending(self, *, columns: list[str] | None, workspace_id: str) -> list[dict]:
        """Lance-native filtered scan for chunks with pending identifiers."""
        try:
            query = (
                self._table.search()
                .where(
                    "workspace_id = "
                    f"'{self._quote_delete_value(workspace_id)}' AND array_length(pending) > 0",
                    prefilter=True,
                )
                .limit(0)
            )
            if columns:
                query = query.select(columns)
            return query.to_list()
        except Exception:
            # Fallback for older Lance / test fakes that don't support
            # filter-only search. Pay the to_pandas cost only on the slow
            # path; the production path stays fast.
            df = self._table.to_pandas()
            return [
                row.to_dict()
                for _, row in df.iterrows()
                if row.get("workspace_id") == workspace_id and len(row["pending"]) > 0
            ]

    def _set_pending_row(self, row: dict, pending: list[str]):
        chunk_id = row["id"]
        workspace_id = row.get("workspace_id", DEFAULT_WORKSPACE_ID)
        try:
            self._table.delete(
                f"(workspace_id = '{self._quote_delete_value(workspace_id)}' "
                f"AND id = '{self._quote_delete_value(chunk_id)}')"
            )
        except Exception:
            pass
        vector = row["vector"]
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        embedding_metadata = row.get("embedding_metadata") or "{}"
        self._table.add(
            [
                {
                    "id": chunk_id,
                    "workspace_id": workspace_id,
                    "file_path": row["file_path"],
                    "chunk": row["chunk"],
                    "pending": pending,
                    "vector": vector,
                    "embedding_metadata": embedding_metadata,
                }
            ]
        )

    def set_pending_row(self, row: dict, pending: list[str]):
        """Update pending identifiers for an already-fetched doc row."""
        self._set_pending_row(row, pending)

    def set_pending_rows_batch(self, updates: list[tuple[dict, list[str]]]) -> int:
        """Bulk-update pending identifiers for many doc rows in one pass.

        The per-row ``_set_pending_row`` path issues a ``delete`` + ``add``
        against LanceDB for every row. On large doc sets that scales badly
        (each delete/add causes Lance to rewrite fragments). This method
        collapses N deletes + N adds into exactly 1 delete + 1 add,
        turning a ~1 s/row cost into ~tens of milliseconds amortized.

        ``updates`` is an iterable of ``(row_dict, new_pending_list)``
        tuples where ``row_dict`` is the full LanceDB row as previously
        returned by ``get_pending_rows`` / ``_prepare_doc_link_batches``.
        Returns the number of rows rewritten.
        """
        if not updates:
            return 0

        # Build the IN-list for the delete predicate. Lance does not
        # parameterize delete strings, so we escape single quotes manually.
        keys: list[tuple[str, str]] = []
        rows_to_add: list[dict] = []
        for row, pending in updates:
            chunk_id = row["id"]
            workspace_id = row.get("workspace_id", DEFAULT_WORKSPACE_ID)
            keys.append((workspace_id, chunk_id))
            vector = row["vector"]
            if hasattr(vector, "tolist"):
                vector = vector.tolist()
            rows_to_add.append(
                {
                    "id": chunk_id,
                    "workspace_id": workspace_id,
                    "file_path": row["file_path"],
                    "chunk": row["chunk"],
                    "pending": pending,
                    "vector": vector,
                    "embedding_metadata": row.get("embedding_metadata") or "{}",
                }
            )

        predicates = " OR ".join(
            (
                f"(workspace_id = '{self._quote_delete_value(ws)}' "
                f"AND id = '{self._quote_delete_value(cid)}')"
            )
            for ws, cid in keys
        )
        try:
            self._table.delete(predicates)
        except Exception:
            # Match the resilience of _set_pending_row — missing rows are
            # not an error; add() will insert them fresh below.
            pass
        self._table.add(rows_to_add)
        return len(rows_to_add)

    def set_pending(
        self,
        chunk_id: str,
        pending: list[str],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        try:
            rows = (
                self._table.search()
                .where(
                    f"(workspace_id = '{self._quote_delete_value(workspace_id)}' "
                    f"AND id = '{self._quote_delete_value(chunk_id)}')",
                    prefilter=True,
                )
                .limit(1)
                .to_list()
            )
        except Exception:
            df = self._table.to_pandas()
            matched = df[(df["id"] == chunk_id) & (df["workspace_id"] == workspace_id)]
            if matched.empty:
                return
            self._set_pending_row(matched.iloc[0].to_dict(), pending)
            return
        if not rows:
            return
        self._set_pending_row(rows[0], pending)

    def search(
        self, query: str, limit: int = 5, *, workspace_id: str = DEFAULT_WORKSPACE_ID
    ) -> list[dict]:
        vec = self._embed([query])[0]
        results = (
            self._table.search(vec)
            .where(f"workspace_id = '{self._quote_delete_value(workspace_id)}'", prefilter=True)
            .limit(limit)
            .to_list()
        )

        # Guard against cross-model queries (skip check for unversioned rows)
        for r in results:
            meta_str = r.get("embedding_metadata")
            if meta_str:
                try:
                    metadata_dict = json.loads(meta_str)
                    if (
                        metadata_dict.get("model_name")
                        and metadata_dict.get("model_name") != EMBED_MODEL
                    ):
                        raise EmbeddingModelMismatch(
                            f"Query embedding uses {EMBED_MODEL} but database has {metadata_dict.get('model_name')}. "
                            "Run migration: python -m sidecar.database.embedding_migration migrate"
                        )
                except json.JSONDecodeError:
                    pass

        output = []
        for r in results:
            distance = r.get("_distance")
            score = None if distance is None else _l2_to_score(float(distance))
            output.append(
                {
                    "id": r.get("id"),
                    "file_path": r["file_path"],
                    "chunk": r["chunk"],
                    "distance": distance,
                    "score": score,
                }
            )
        return output

    def upsert_symbol_embeddings(
        self,
        symbols: list[dict],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        progress_callback: Callable[[str], None] | None = None,
    ):
        """symbols: list of {uid, name, file_path, code}"""
        if not symbols:
            return
        codes = [s["code"] for s in symbols]
        if progress_callback:
            progress_callback(f"prepare: symbols={len(symbols)}")
        t0 = time.perf_counter()
        vectors = self._embed(codes, progress_callback=progress_callback)
        if progress_callback:
            progress_callback(f"embed done in {time.perf_counter() - t0:.2f}s")
        rows = []
        for s, vec in zip(symbols, vectors, strict=False):
            metadata = EmbeddingMetadata(
                model_name=EMBED_MODEL,
                model_version=self._model_metadata.version,
                chunk_hash=compute_chunk_hash(s["code"]),
                embedding_hash=compute_embedding_hash(vec),
            )
            rows.append(
                {
                    "uid": s["uid"],
                    "workspace_id": str(s.get("workspace_id") or workspace_id),
                    "name": s["name"],
                    "file_path": s["file_path"],
                    "code": s["code"],
                    "vector": vec,
                    "embedding_metadata": json.dumps(
                        {
                            "model_name": metadata.model_name,
                            "model_version": metadata.model_version,
                            "chunk_hash": metadata.chunk_hash,
                            "embedding_hash": metadata.embedding_hash,
                        }
                    ),
                }
            )
        uids = [s["uid"] for s in symbols]
        if progress_callback:
            progress_callback(f"delete existing rows: {len(uids)}")
        t0 = time.perf_counter()
        self._delete_symbol_rows(uids, workspace_id, progress_callback=progress_callback)
        if progress_callback:
            progress_callback(f"delete done in {time.perf_counter() - t0:.2f}s")
            progress_callback(f"add rows: {len(rows)}")
        t0 = time.perf_counter()
        self._sym_table.add(rows)
        if progress_callback:
            progress_callback(f"add done in {time.perf_counter() - t0:.2f}s")

    def delete_symbol_embeddings(
        self,
        uids: list[str],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Remove symbol embedding rows for deleted symbols."""
        self._delete_symbol_rows(uids, workspace_id)

    def search_symbols(
        self,
        query: str,
        limit: int = 5,
        threshold: float = 0.4,
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> list[dict]:
        """Returns symbols semantically similar to query, with cosine distance."""
        vec = self._embed([query])[0]
        return self.search_symbols_by_vector(
            vec, limit=limit, threshold=threshold, workspace_id=workspace_id
        )

    def search_symbols_by_vector(
        self,
        vector: list[float],
        limit: int = 5,
        threshold: float = 0.4,
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> list[dict]:
        """Returns symbols semantically similar to a precomputed embedding vector."""
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        results = (
            self._sym_table.search(vector)
            .where(f"workspace_id = '{self._quote_delete_value(workspace_id)}'", prefilter=True)
            .limit(limit)
            .to_list()
        )

        # Guard against cross-model queries (skip check for unversioned rows)
        for r in results:
            meta_str = r.get("embedding_metadata")
            if meta_str:
                try:
                    metadata_dict = json.loads(meta_str)
                    if (
                        metadata_dict.get("model_name")
                        and metadata_dict.get("model_name") != EMBED_MODEL
                    ):
                        raise EmbeddingModelMismatch(
                            f"Query embedding uses {EMBED_MODEL} but database has {metadata_dict.get('model_name')}. "
                            "Run migration: python -m sidecar.database.embedding_migration migrate"
                        )
                except json.JSONDecodeError:
                    pass

        out = []
        for r in results:
            distance = r.get("_distance", 1.0)
            if distance <= threshold:
                out.append(
                    {
                        "uid": r["uid"],
                        "name": r["name"],
                        "file_path": r["file_path"],
                        "distance": distance,
                        "score": _l2_to_score(float(distance)),
                    }
                )
        return out
