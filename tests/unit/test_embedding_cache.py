"""Unit tests for embedding cache and recomputation controls."""

from sidecar.database import lancedb_client as lancedb_client_module
from sidecar.database.embedding_cache import EmbeddingCache, EmbeddingCacheKey
from sidecar.database.embedding_registry import (
    EmbeddingModel,
    compute_chunk_hash,
    compute_embedding_hash,
)
from sidecar.database.lancedb_client import (
    AXIS_SYMBOL_REQUIRED_COLUMNS,
    AXIS_SYMBOLS_SCHEMA,
    SYMBOLS_SCHEMA,
    LanceDBClient,
    _symbols_schema_for_profile,
)
from sidecar.index_profile import AXIS_PYTHON_V1_PROFILE, resolve_index_profile
from sidecar.workspace import DEFAULT_WORKSPACE_ID


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


def test_embedding_cache_get_many_roundtrip(tmp_path):
    cache = EmbeddingCache(str(tmp_path / "embeddings.sqlite3"))
    key1 = EmbeddingCacheKey(
        model_name="all-MiniLM-L6-v2",
        model_version="2.2",
        content_hash=compute_chunk_hash("alpha"),
    )
    key2 = EmbeddingCacheKey(
        model_name="all-MiniLM-L6-v2",
        model_version="2.2",
        content_hash=compute_chunk_hash("beta"),
    )
    cache.set(key1, [1.0, 2.0], embedding_hash=compute_embedding_hash([1.0, 2.0]))
    cache.set(key2, [3.0, 4.0], embedding_hash=compute_embedding_hash([3.0, 4.0]))

    hits = cache.get_many([key1, key2])

    assert hits[key1] == [1.0, 2.0]
    assert hits[key2] == [3.0, 4.0]


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


def test_lancedb_client_embed_reports_progress(tmp_path):
    class FakeModel:
        def encode(self, texts, show_progress_bar=False):
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
    client._embed_batch_size = 1
    client._embed_throttle_seconds = 0
    client._embedding_stats = {"cache_hits": 0, "cache_misses": 0, "encoded": 0}

    progress: list[str] = []
    client._embed(["alpha", "beta"], progress_callback=progress.append)

    assert progress[0].startswith("cache scan:")
    assert progress[-1] == "encode: 2/2"


def test_lancedb_client_delete_symbol_embeddings_batches_predicates(monkeypatch):
    class FakeTable:
        def __init__(self):
            self.predicates: list[str] = []

        def delete(self, predicate: str):
            self.predicates.append(predicate)

    client = object.__new__(LanceDBClient)
    client._sym_table = FakeTable()
    monkeypatch.setattr(lancedb_client_module, "LANCEDB_DELETE_BATCH_SIZE", 2)

    client.delete_symbol_embeddings(["a", "b", "c", "d", "e"])

    assert client._sym_table.predicates == [
        f"(workspace_id = '{DEFAULT_WORKSPACE_ID}' AND uid = 'a') OR "
        f"(workspace_id = '{DEFAULT_WORKSPACE_ID}' AND uid = 'b')",
        f"(workspace_id = '{DEFAULT_WORKSPACE_ID}' AND uid = 'c') OR "
        f"(workspace_id = '{DEFAULT_WORKSPACE_ID}' AND uid = 'd')",
        f"(workspace_id = '{DEFAULT_WORKSPACE_ID}' AND uid = 'e')",
    ]


def test_lancedb_axis_profile_uses_extended_symbol_schema():
    legacy = resolve_index_profile("legacy")
    axis = resolve_index_profile(AXIS_PYTHON_V1_PROFILE)

    assert _symbols_schema_for_profile(legacy) == SYMBOLS_SCHEMA
    axis_schema = _symbols_schema_for_profile(axis)

    assert axis_schema == AXIS_SYMBOLS_SCHEMA
    assert AXIS_SYMBOL_REQUIRED_COLUMNS <= set(axis_schema.names)


def test_lancedb_axis_symbol_payload_defaults_to_empty_axis_fields():
    client = object.__new__(LanceDBClient)
    client._symbol_axis_columns = AXIS_SYMBOL_REQUIRED_COLUMNS

    assert client._axis_symbol_payload({}) == {
        "ast_kind_bits": [],
        "cfg_bits": [],
        "dfg_bits": [],
        "struct_bits": [],
        "axis_evidence_json": "[]",
        "axis_container_kinds_json": "[]",
    }

    client._symbol_axis_columns = set()
    assert client._axis_symbol_payload({"cfg_bits": ["callable_body"]}) == {}


def test_lancedb_client_set_pending_row_reuses_existing_doc_payload():
    class FakeTable:
        def __init__(self):
            self.deleted: list[str] = []
            self.added: list[list[dict]] = []

        def delete(self, predicate: str):
            self.deleted.append(predicate)

        def add(self, rows: list[dict]):
            self.added.append(rows)

    client = object.__new__(LanceDBClient)
    client._table = FakeTable()

    client.set_pending_row(
        {
            "id": "chunk-1",
            "file_path": "/docs/a.md",
            "chunk": "Hello",
            "pending": ["Old"],
            "vector": [1.0, 2.0],
            "embedding_metadata": '{"ok":true}',
        },
        ["New"],
    )

    assert client._table.deleted == [
        f"(workspace_id = '{DEFAULT_WORKSPACE_ID}' AND id = 'chunk-1')"
    ]
    assert client._table.added == [
        [
            {
                "id": "chunk-1",
                "workspace_id": DEFAULT_WORKSPACE_ID,
                "file_path": "/docs/a.md",
                "chunk": "Hello",
                "pending": ["New"],
                "vector": [1.0, 2.0],
                "embedding_metadata": '{"ok":true}',
            }
        ]
    ]


def test_lancedb_client_search_symbols_by_vector_skips_query_embedding():
    class FakeTable:
        def __init__(self):
            self.search_calls: list[list[float]] = []

        def search(self, vector):
            self.search_calls.append(vector)
            return self

        def where(self, predicate: str, prefilter: bool = False):
            assert predicate == f"workspace_id = '{DEFAULT_WORKSPACE_ID}'"
            assert prefilter is True
            return self

        def limit(self, n: int):
            assert n == 3
            return self

        @staticmethod
        def to_list():
            return [
                {
                    "uid": "uid-a",
                    "name": "Alpha",
                    "file_path": "/repo/a.py",
                    "_distance": 0.2,
                    "embedding_metadata": None,
                }
            ]

    class FailingModel:
        def encode(self, texts, show_progress_bar=False):
            raise AssertionError("query text embedding should not be used")

    client = object.__new__(LanceDBClient)
    client._model = FailingModel()
    client._sym_table = FakeTable()

    hits = client.search_symbols_by_vector([1.0, 2.0], limit=3, threshold=0.4)

    assert client._sym_table.search_calls == [[1.0, 2.0]]
    # Score: cos = 1 - d^2/2 = 1 - 0.04/2 = 0.98, mapped to (1+cos)/2 = 0.99
    assert hits == [
        {
            "uid": "uid-a",
            "name": "Alpha",
            "file_path": "/repo/a.py",
            "distance": 0.2,
            "score": 0.99,
        }
    ]
