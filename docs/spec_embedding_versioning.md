# Spec — LanceDB Embedding Versioning (Phase 4)

> **Partly superseded (2026-06-15).** Modules named here from the deleted ranking cascade (`ContextArbitrator`/`UnifiedRanker`/`graph_expander`/`qa_benchmark`/etc.) are gone — axis (`context_engine/axis/`) is the context + eval path. Non-cascade content still applies; see `cascade_cleanup_inventory.md`.


> **Status:** Proposed. Prevents silent quality degradation when embedding models are upgraded or swapped. Prerequisite for any multi-model or cross-version search.

## 1. Problem

LanceDB currently stores embeddings with no metadata about how they were produced. This creates an invisible failure mode:

**Scenario A — Model upgrade:** `all-MiniLM-L6-v2` is replaced with `bge-code`. Re-indexing is partial (only changed files). Queries now compare vectors from two different embedding spaces, producing nonsensical similarity scores. No error is raised.

**Scenario B — Dimension mismatch at scale:** A new embedding model uses 768 dimensions vs 384. LanceDB will silently truncate or error depending on table schema. The failure is schema-level, not caught by application logic.

**Scenario C — Regression hunting:** Retrieval quality drops after a re-index run. Without chunk-level hash tracking, it is impossible to determine whether the degradation came from changed source code, changed embeddings, or a model switch.

## 2. Design

### 2.1 New Column: `embedding_metadata`

Add an `embedding_metadata` JSON column to both LanceDB tables: `docs` and `symbols`.

```python
embedding_metadata = {
    "model_name": "sentence-transformers/all-MiniLM-L6-v2",
    "model_version": "2.2.2",
    "embedding_dim": 384,
    "chunk_hash": "sha256:8f3a...",    # SHA256(chunk text)
    "embedding_hash": "sha256:2b1c...", # SHA256(embedding bytes)
    "indexed_at": "2026-04-19T14:22:00Z"
}
```

All fields required at write time. No nullable fields.

### 2.2 Model Registry

A lightweight in-process registry maps model identifiers to expected dimensions:

```python
# context_engine/database/embedding_registry.py

KNOWN_MODELS: dict[str, dict] = {
    "sentence-transformers/all-MiniLM-L6-v2": {
        "dim": 384,
        "canonical_version": "2.2.2",
    },
    "bge-code": {
        "dim": 768,
        "canonical_version": "1.0",
    },
}

def get_model_info(model_name: str) -> dict:
    if model_name not in KNOWN_MODELS:
        raise UnknownEmbeddingModel(f"Model not in registry: {model_name}")
    return KNOWN_MODELS[model_name]
```

New models are added manually to the registry. This is a deliberate friction — unknown models should not slip in silently.

### 2.3 Write Path

When `LanceDBClient` writes an embedding row, it must also write `embedding_metadata`:

```python
# context_engine/database/lancedb_client.py

def _build_metadata(self, chunk: str, embedding: list[float]) -> dict:
    return {
        "model_name": self._model_name,
        "model_version": self._model_version,
        "embedding_dim": len(embedding),
        "chunk_hash": "sha256:" + sha256(chunk.encode()).hexdigest(),
        "embedding_hash": "sha256:" + sha256(
            struct.pack(f"{len(embedding)}f", *embedding)
        ).hexdigest(),
        "indexed_at": datetime.utcnow().isoformat() + "Z",
    }
```

`self._model_name` and `self._model_version` are injected at `LanceDBClient.__init__()`.

### 2.4 Read Path — Cross-Model Guard

Before executing a similarity search, validate that all indexed rows use the same model as the current runtime model. If a mismatch is detected, raise `EmbeddingModelMismatch` unless the caller explicitly passes `allow_mixed=True`.

```python
def search(self, query: str, limit: int = 5, allow_mixed: bool = False) -> list[dict]:
    if not allow_mixed:
        self._assert_model_consistency()
    ...

def _assert_model_consistency(self):
    """Sample up to 100 rows; check all have same model_name as runtime."""
    sample = self._table.search(None).limit(100).to_list()
    model_names = {row["embedding_metadata"]["model_name"] for row in sample if row.get("embedding_metadata")}
    if len(model_names) > 1:
        raise EmbeddingModelMismatch(
            f"Table contains embeddings from multiple models: {model_names}. "
            "Re-index required, or pass allow_mixed=True to suppress."
        )
    if model_names and self._model_name not in model_names:
        raise EmbeddingModelMismatch(
            f"Runtime model '{self._model_name}' differs from indexed model '{model_names}'. "
            "Re-index required."
        )
```

**Performance note:** `_assert_model_consistency` is called once per `LanceDBClient` instance, cached after first check. Not per query.

### 2.5 Model mismatch recovery

There is no in-place re-embedding migration. When `EmbeddingModelMismatch` fires:

1. Set `EMBED_MODEL` to the target model.
2. Delete `./data/lancedb` (or the affected workspace partition tables).
3. Re-index the project.

Lazy workspace-partition copy from monolithic Lance tables still runs inside
`LanceDBClient._maybe_migrate_workspace_partition` when
`LANCEDB_WORKSPACE_PARTITIONED=true` and a partition is first opened.

## 3. Schema Changes

### `docs` table (new columns)

```python
schema = pa.schema([
    pa.field("file_path", pa.string()),
    pa.field("chunk_id", pa.string()),
    pa.field("chunk", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), 384)),
    pa.field("embedding_metadata", pa.string()),  # JSON blob
])
```

`embedding_metadata` stored as JSON string for schema flexibility (avoids nested struct migration burden on LanceDB version changes).

### Backward Compatibility

Old rows without `embedding_metadata` are tolerated during search if `allow_mixed=True`. The migration utility identifies and marks stale rows.

## 4. New Exceptions

```python
# context_engine/context/types.py or context_engine/database/exceptions.py

class EmbeddingModelMismatch(RuntimeError):
    """Raised when indexed embeddings were produced by a different model than the runtime."""

class UnknownEmbeddingModel(ValueError):
    """Raised when a model_name is not registered in KNOWN_MODELS."""
```

## 5. LanceDBClient Constructor Changes

```python
class LanceDBClient:
    DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

    def __init__(
        self,
        data_path: str = "./data/lancedb",
        model_name: str = DEFAULT_MODEL,
    ):
        self._model_name = model_name
        self._model_version = get_model_info(model_name)["canonical_version"]
        self._consistency_checked = False
        ...
```

Callers in `context_engine/main.py`, `QA/qa_benchmark.py`, and tests that construct `LanceDBClient()` continue to work unchanged (default model unchanged). Only callers explicitly switching models need to update.

## 6. Tests

`tests/unit/test_embedding_versioning.py`:

| Test | Condition |
|---|---|
| `test_metadata_written_on_upsert` | After writing a chunk, row has `embedding_metadata` with all required keys |
| `test_chunk_hash_deterministic` | Same text → same `chunk_hash` across two writes |
| `test_embedding_hash_changes_with_text` | Different text → different `embedding_hash` |
| `test_model_mismatch_raises` | Table has rows from model A, runtime is model B → `EmbeddingModelMismatch` |
| `test_allow_mixed_bypasses_guard` | `search(..., allow_mixed=True)` does not raise on mismatch |
| `test_unknown_model_raises_at_init` | `LanceDBClient(model_name="made_up_model")` → `UnknownEmbeddingModel` |
| `test_consistency_check_cached` | `_assert_model_consistency` called only once per instance |

## 7. Success Criteria

1. Unit tests green.
2. `qa_benchmark.py --no-index` does not raise `EmbeddingModelMismatch` (existing index is consistent).
3. Manually running with a different `EMBEDDING_MODEL` env var and a stale index raises a clear error with re-index instructions.

## 8. Phase Sequencing

Implement after ContextDeduplicator (independent, but lower urgency). Requires updating:
- `context_engine/database/lancedb_client.py` — write and read paths
- `context_engine/indexer/docs.py` — pass metadata on write
- `context_engine/indexer/code.py` — pass metadata on symbol embedding write
- New: `context_engine/database/embedding_registry.py`
