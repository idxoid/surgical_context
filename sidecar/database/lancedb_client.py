import json
import os
import time
from collections import OrderedDict

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
DOCS_TABLE = "docs"
SYMBOLS_TABLE = "symbols"

DOCS_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string()),
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
        throttle_ms = max(EMBED_THROTTLE_MS, EMBED_LOW_PRIORITY_THROTTLE_MS if EMBED_LOW_PRIORITY else 0)
        self._embed_throttle_seconds = throttle_ms / 1000
        self._embedding_stats = {"cache_hits": 0, "cache_misses": 0, "encoded": 0}
        if DOCS_TABLE not in self._db.table_names():
            self._table = self._db.create_table(DOCS_TABLE, schema=DOCS_SCHEMA)
        else:
            self._table = self._db.open_table(DOCS_TABLE)
        if SYMBOLS_TABLE not in self._db.table_names():
            self._sym_table = self._db.create_table(SYMBOLS_TABLE, schema=SYMBOLS_SCHEMA)
        else:
            self._sym_table = self._db.open_table(SYMBOLS_TABLE)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        vectors: list[list[float] | None] = [None] * len(texts)
        missing_by_hash: OrderedDict[str, str] = OrderedDict()
        cache_keys: dict[str, EmbeddingCacheKey] = {}

        for index, text in enumerate(texts):
            content_hash = compute_chunk_hash(text)
            key = EmbeddingCacheKey(
                model_name=EMBED_MODEL,
                model_version=self._model_metadata.version,
                content_hash=content_hash,
            )
            cache_keys[content_hash] = key
            cached = self._embedding_cache.get(key) if self._embedding_cache else None
            if cached is not None:
                vectors[index] = cached
                self._embedding_stats["cache_hits"] += 1
            else:
                missing_by_hash.setdefault(content_hash, text)
                self._embedding_stats["cache_misses"] += 1

        encoded_by_hash: dict[str, list[float]] = {}
        missing_items = list(missing_by_hash.items())
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
            if self._embed_throttle_seconds and start + self._embed_batch_size < len(missing_items):
                time.sleep(self._embed_throttle_seconds)

        for index, text in enumerate(texts):
            if vectors[index] is None:
                vectors[index] = encoded_by_hash[compute_chunk_hash(text)]

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

    def upsert_chunks(self, file_path: str, chunks: list[str]):
        vectors = self._embed(chunks)
        rows = []
        for i, (chunk, vec) in enumerate(zip(chunks, vectors, strict=False)):
            metadata = EmbeddingMetadata(
                model_name=EMBED_MODEL,
                model_version=self._model_metadata.version,
                chunk_hash=compute_chunk_hash(chunk),
                embedding_hash=compute_embedding_hash(vec),
            )
            rows.append(
                {
                    "id": f"{file_path}::{i}",
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
        try:
            self._table.delete(f"file_path = '{file_path}'")
        except Exception:
            pass
        self._table.add(rows)

    def get_pending(self) -> dict[str, list[str]]:
        """Returns {chunk_id: [name, ...]} for all chunks with pending identifiers."""
        df = self._table.to_pandas()
        return {
            row["id"]: list(row["pending"]) for _, row in df.iterrows() if len(row["pending"]) > 0
        }

    def set_pending(self, chunk_id: str, pending: list[str]):
        df = self._table.to_pandas()
        row = df[df["id"] == chunk_id]
        if row.empty:
            return
        row = row.iloc[0]
        try:
            self._table.delete(f"id = '{chunk_id}'")
        except Exception:
            pass
        # Handle missing embedding_metadata in old rows
        embedding_metadata = "{}"
        if "embedding_metadata" in row.index:
            embedding_metadata = row["embedding_metadata"] or "{}"
        self._table.add(
            [
                {
                    "id": chunk_id,
                    "file_path": row["file_path"],
                    "chunk": row["chunk"],
                    "pending": pending,
                    "vector": row["vector"].tolist(),
                    "embedding_metadata": embedding_metadata,
                }
            ]
        )

    def search(self, query: str, limit: int = 5) -> list[dict]:
        vec = self._embed([query])[0]
        results = self._table.search(vec).limit(limit).to_list()

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
            score = None if distance is None else max(0.0, 1.0 - float(distance))
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

    def upsert_symbol_embeddings(self, symbols: list[dict]):
        """symbols: list of {uid, name, file_path, code}"""
        if not symbols:
            return
        codes = [s["code"] for s in symbols]
        vectors = self._embed(codes)
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
        for uid in uids:
            try:
                self._sym_table.delete(f"uid = '{uid}'")
            except Exception:
                pass
        self._sym_table.add(rows)

    def search_symbols(self, query: str, limit: int = 5, threshold: float = 0.4) -> list[dict]:
        """Returns symbols semantically similar to query, with cosine distance."""
        vec = self._embed([query])[0]
        results = self._sym_table.search(vec).limit(limit).to_list()

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
                        "score": max(0.0, 1.0 - float(distance)),
                    }
                )
        return out
