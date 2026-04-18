# Storage Layer — Spec

## Overview

Two database clients. Each optimized for its data type. Neither stores source code content (ADR-001).

---

## Neo4jClient (`sidecar/database/neo4j_client.py`)

### Connection

```python
Neo4jClient(uri, user, password)
```

Opens a `neo4j.GraphDatabase.driver` connection. Call `.close()` when done. In `/ask`, created per request and closed in a `finally` block.

### Schema (ADR-001 compliant)

**Nodes:**

| Label | Properties | Notes |
|---|---|---|
| `File` | `path, hash, last_indexed` | `hash` = hex of raw bytes; `last_indexed` = Neo4j `timestamp()` |
| `Symbol` | `uid, name, kind, hash, range` | `range = [start_line, end_line]`; no `file_path` |
| `DocAnchor` | `chunk_id` | Key into LanceDB `docs` table; no content |

**Relationships:**

| Type | Pattern | Description |
|---|---|---|
| `CONTAINS` | `(File)→(Symbol)` | Symbol belongs to this file |
| `CALLS` | `(Symbol)→(Symbol)` | Direct function call |
| `FROM` | `(DocAnchor)→(File)` | Doc chunk originates from this file |
| `COVERS` | `(DocAnchor)→(Symbol)` | Doc chunk describes this symbol |

### Methods

#### upsert_file_structure(file_path, file_hash, symbols)
Two operations per call:
1. `MERGE (f:File {path}) SET f.hash, f.last_indexed`
2. For each symbol: `MERGE (s:Symbol {uid}) SET s.name, s.kind, s.hash, s.range` + `MERGE (f)-[:CONTAINS]->(s)`

Uses `MERGE` throughout — safe to re-run on re-index.

#### link_calls(calls: list[dict])
Each call: `{caller_uid, callee_name}`.  
Cypher: `MATCH (caller {uid}) MATCH (callee {name}) WHERE caller <> callee MERGE (caller)-[:CALLS]->(callee)`

Callee matched by **name** only — simplified resolution, no import tracking yet.

---

## LanceDBClient (`sidecar/database/lancedb_client.py`)

### Connection

```python
LanceDBClient()
```

Opens (or creates) LanceDB at path from `LANCEDB_PATH` env var (default `./data/lancedb`). Creates two tables on first run.

### Tables

#### docs
| Column | Type | Description |
|---|---|---|
| `id` | string | `"{file_path}::{chunk_index}"` |
| `file_path` | string | Source markdown file |
| `chunk` | string | Raw chunk text |
| `pending` | list[string] | Identifier names not yet linked to a symbol |
| `vector` | float32[384] | `all-MiniLM-L6-v2` embedding of `chunk` |

#### symbols
| Column | Type | Description |
|---|---|---|
| `uid` | string | Symbol UID (matches Neo4j) |
| `name` | string | Symbol name |
| `file_path` | string | Source code file |
| `code` | string | Symbol source lines |
| `vector` | float32[384] | `all-MiniLM-L6-v2` embedding of `code` |

### Methods

#### upsert_chunks(file_path, chunks)
Delete all rows where `file_path = X`, then insert new rows with `pending=[]`.

#### upsert_symbol_embeddings(symbols)
`symbols`: list of `{uid, name, file_path, code}`. Embeds `code` field. Delete-then-insert per uid.

#### search(query, limit=5) → list[dict]
ANN search over `docs` table. Returns `[{file_path, chunk}]`.

#### search_symbols(query, limit=5, threshold=0.4) → list[dict]
ANN search over `symbols` table. Filters by `_distance <= threshold`. Returns `[{uid, name, file_path, distance}]`.

#### get_pending() → dict[str, list[str]]
Returns `{chunk_id: [name, ...]}` for all docs chunks with non-empty `pending` list.

#### set_pending(chunk_id, pending)
Delete-then-insert for the target row. LanceDB `update()` cannot handle empty list fields, so this is the safe write pattern.

### Embedding Model

`EMBED_MODEL` env var (default `all-MiniLM-L6-v2`). Loaded once at `LanceDBClient.__init__`. 384-dimensional vectors. Runs locally, no network calls after first download.

---

## Limitations (current)

- `link_calls` matches callee by name — name collisions across files create incorrect `[:CALLS]` edges.
- No transaction batching in `_upsert_nodes` — N symbols = N round-trips to Neo4j.
- `set_pending` is delete-then-insert — concurrent writes to the same chunk_id would lose data (not an issue in current single-process design).
- LanceDB delete filter uses string interpolation — values with single quotes in file paths would break the filter.

---

## Planned Extensions

- Batch `_upsert_nodes` into a single parameterized Cypher `UNWIND` call
- Import-aware call resolution (resolve callee by `uid` not `name`)
- Parameterized LanceDB delete filter to handle paths with special characters
