# Spec — LanceDB Embedding Versioning

> **Status:** Implemented baseline. Docs and symbol rows carry model/version and
> content/vector hashes; search rejects versioned rows produced by another model.
> Unversioned legacy rows remain readable. Full-table consistency scans,
> multi-model querying, and automated re-embedding migrations are not implemented.

## 1. Problem

Partial re-indexing can mix vectors from incompatible embedding spaces after a
model switch. Without row metadata it is also difficult to distinguish source
changes from embedding changes while investigating retrieval regressions.

## 2. Current Design

### 2.1 Row metadata

Both docs and symbol tables store `embedding_metadata` as JSON:

```json
{
  "model_name": "all-MiniLM-L6-v2",
  "model_version": "2.2",
  "chunk_hash": "<sha256 text>",
  "embedding_hash": "<sha256 float bytes>"
}
```

The implementation is in
`context_engine/database/embedding_registry.py` and
`context_engine/database/lancedb_client.py`. JSON keeps the Lance schema flat
while allowing metadata to evolve.

### 2.2 Model registry

`KNOWN_MODELS` currently includes:

| Key | Canonical model | Dimensions |
|---|---|---|
| `all-MiniLM-L6-v2` | `sentence-transformers/all-MiniLM-L6-v2` | 384 |
| `bge-code` | `BAAI/bge-code-v1.5` | 768 |
| `unixcoder` | `microsoft/unixcoder-base` | 768 |

`LanceDBClient` reads the model key from `EMBED_MODEL` and raises `ValueError`
for an unknown key. The active table schemas are currently fixed to 384
dimensions, so selecting a 768-dimensional registry entry still requires a
schema/migration implementation before it is usable.

### 2.3 Write path and cache

Every docs/symbol upsert computes content and embedding hashes and writes the
metadata JSON. `_embed()` uses `EmbeddingCache`, keyed by model name, model
version, and content hash, before invoking SentenceTransformer in configurable
batches.

Relevant controls:

| Variable | Default | Purpose |
|---|---|---|
| `EMBED_MODEL` | `all-MiniLM-L6-v2` | Registry key and runtime model |
| `EMBED_CACHE_ENABLED` | `true` | Enable content-hash embedding cache |
| `EMBED_BATCH_SIZE` | `32` | Encode batch size |
| `EMBED_THROTTLE_MS` | `0` | Delay between encode batches |
| `EMBED_LOW_PRIORITY` | `false` | Apply low-priority throttling |

### 2.4 Read guard

`search()` and symbol-vector search inspect metadata on returned rows. A row
whose `model_name` differs from `EMBED_MODEL` raises
`EmbeddingModelMismatch` with re-index instructions. Rows without metadata are
tolerated for compatibility.

This is a result-set guard, not a full-table preflight: incompatible rows that
do not enter the current nearest-neighbour result are not detected by that
query.

## 3. Recovery

There is no in-place re-embedding migration. To switch models:

1. Set `EMBED_MODEL` to a supported model with a compatible table schema.
2. Delete the affected LanceDB tables or workspace partitions.
3. Run a full project/docs re-index.

Workspace-partitioned symbol and adjacency tables are controlled by
`LANCEDB_WORKSPACE_PARTITIONED` (default `true`). Docs remain workspace-filtered
inside the profile docs table.

## 4. Tests

`tests/unit/test_embedding_cache.py` covers deterministic cache keys, cache
hits/misses, batch behavior, and preservation of metadata through row updates.
DocAnchor tests cover compatibility with metadata-bearing docs rows.

Missing focused coverage:

- mismatch rejection for docs and symbol searches
- unknown model construction
- dimension mismatch behavior for 768-dimensional registry entries
- full re-index recovery across workspace partitions

## 5. Planned Extensions

- Add a table-level consistency preflight cached per opened table.
- Make vector dimensions profile/model-aware before enabling 768-dimensional models.
- Add an explicit workspace re-embed command instead of requiring table deletion.
- Decide whether mixed-model tables should ever be supported; the current policy is to reject and re-index.

## 6. Related

- [spec_storage.md](spec_storage.md) — current LanceDB schemas and partitioning
- [spec_indexer.md](spec_indexer.md) — project and incremental write paths
