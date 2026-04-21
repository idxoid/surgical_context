"""Unit tests for embedding cache and recomputation controls."""

from sidecar.database.embedding_cache import EmbeddingCache, EmbeddingCacheKey
from sidecar.database.embedding_registry import (
    EmbeddingModel,
    compute_chunk_hash,
    compute_embedding_hash,
)
from sidecar.database.lancedb_client import LanceDBClient


def test_embedding_cache_roundtrip(tmp_path):
    cache = EmbeddingCache(str(tmp_path / "embeddings.sqlite3"))
    vector = [0.1, 0.2, 0.3]
    key = EmbeddingCacheKey(
        model_name="all-MiniLM-L6-v2",
        model_version="2.2",
        content_hash=compute_chunk_hash("hello"),
    )

    cache.set(key, vector, embedding_hash=compute_embedding_hash(vector))

    assert cache.get(key) == vector
    assert cache.stats()["total"] == 1
    assert cache.stats()["models"][0]["model_name"] == "all-MiniLM-L6-v2"


def test_lancedb_client_embed_reuses_content_hash_cache(tmp_path):
    class FakeModel:
        def __init__(self):
            self.calls = 0
            self.encoded_texts: list[str] = []

        def encode(self, texts, show_progress_bar=False):
            self.calls += 1
            self.encoded_texts.extend(texts)
            return [[float(len(text)), 1.0] for text in texts]

    client = object.__new__(LanceDBClient)
    client._model = FakeModel()
    client._model_metadata = EmbeddingModel(
        name="sentence-transformers/all-MiniLM-L6-v2",
        version="test",
        dimensions=2,
    )
    client._embedding_cache_enabled = True
    client._embedding_cache = EmbeddingCache(str(tmp_path / "embeddings.sqlite3"))
    client._embed_batch_size = 10
    client._embed_throttle_seconds = 0
    client._embedding_stats = {"cache_hits": 0, "cache_misses": 0, "encoded": 0}

    first = client._embed(["alpha", "alpha", "beta"])
    second = client._embed(["alpha", "beta"])

    assert first == [[5.0, 1.0], [5.0, 1.0], [4.0, 1.0]]
    assert second == [[5.0, 1.0], [4.0, 1.0]]
    assert client._model.calls == 1
    assert client._model.encoded_texts == ["alpha", "beta"]
    assert client.embedding_cache_stats()["runtime"]["encoded"] == 2
    assert client.embedding_cache_stats()["runtime"]["cache_hits"] == 2
