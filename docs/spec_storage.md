# Storage Layer — Spec

## Overview

The current implementation uses three local-default storage clients:

- `Neo4jClient` for graph topology and metadata.
- `LanceDBClient` for vector retrieval over docs and symbol bodies.
- `SQLiteHistoryProvider` for local conversations and sanitized request snapshots.

These are treated as default provider implementations behind the staged storage connector layer. Retrieval-facing provider protocols and test fakes exist for vector/workspace/graph-driver seams; full graph/vector connector wrappers are still in progress. See [spec_storage_connectors.md](spec_storage_connectors.md).

Neither graph storage nor tenant API graph storage may store raw source code content (ADR-001). Vector and history storage are governed by storage policy because they may contain text snippets, embeddings, prompts, answers, or prompt-context snapshots.

### Provider Families

| Family | Current Default | Responsibility |
|---|---|---|
| `GraphProvider` | Neo4j | Topology: files, symbols, edges, workspaces, DocAnchors, tenant API links |
| `VectorProvider` | LanceDB | Semantic indexes: docs, symbol embeddings, embedding metadata |
| `HistoryProvider` | SQLite local | User dialogs, ask snapshots, inspector/impact snapshots, retention policy |

---

## GraphProvider Default: Neo4jClient (`context_engine/database/neo4j_client.py`)

### Connection

```python
Neo4jClient(uri, user, password)
```

Directly constructed clients own a `neo4j.GraphDatabase.driver` and close it with the client. The context_engine instead uses `DatabaseProvider`: one lazily opened process-wide driver plus request-scoped `AuraClient` views. `db_session(...)` closes only the lightweight view; the FastAPI lifespan closes the shared driver.

### Schema (ADR-001 compliant)

**Nodes:**

| Label | Properties | Notes |
|---|---|---|
| `Workspace` | `id, tenant, repo, ref, ref_kind, last_indexed` | Branch/workspace namespace |
| `File` | `path, workspace_id, hash, last_indexed` | `hash` = hex of raw bytes; workspace-scoped |
| `Symbol` | `uid, name, kind, hash, range, qualified_name, signature, signature_hash, signature_status, language` | `uid` = stable UID v2; no `file_path` |
| `DocAnchor` | `chunk_id` | Key into LanceDB `docs` table; no content |

**Relationships:**

| Type | Pattern | Description |
|---|---|---|
| `IN_WORKSPACE` | `(File|Symbol|DocAnchor)→(Workspace)` | Workspace membership |
| `CONTAINS` | `(File)→(Symbol)` | Symbol belongs to this workspace file; carries `workspace_id`, `range`, and `hash` |
| `CALLS_*` | `(Symbol)→(Symbol)` | `CALLS_DIRECT`, `CALLS_SCOPED`, `CALLS_IMPORTED`, `CALLS_DYNAMIC`, `CALLS_INFERRED`, `CALLS_GUESS`; carries `workspace_id`, `confidence`, `tier`, `resolver`, `call_site_line` |
| `AFFECTS` | `(Symbol)→(Symbol)` | Workspace-local reverse dependency materialization |
| `FROM` | `(DocAnchor)→(File)` | Doc chunk originates from this file |
| `COVERS` | `(DocAnchor)→(Symbol)` | Doc chunk describes this symbol; carries `workspace_id`, `anchor_type`, `confidence`, `primary_bias`, `resolver` |

### Methods

#### upsert_file_structure(file_path, file_hash, symbols, workspace_id)
Two operations per call:
1. `MERGE (w:Workspace {id})` and `MERGE (f:File {path, workspace_id}) SET f.hash, f.last_indexed`
2. For each symbol: `MERGE (s:Symbol {uid}) SET s.name, s.kind, s.hash, s.qualified_name, s.signature...` + `MERGE (f)-[:CONTAINS {workspace_id}]->(s)`

Uses `MERGE` throughout — safe to re-run on re-index.

#### link_calls(calls: list[dict])
Each call: `{caller_uid, callee_uid?, callee_qualified_name?, callee_name, rel_type, confidence, tier, resolver, call_site_line}`.

Preferred resolution is by `callee_uid`, then `callee_qualified_name`. Name-only fallback only creates an edge when the target name is unique inside the workspace.

---

## VectorProvider Default: LanceDBClient (`context_engine/database/lancedb_client.py`)

### Connection

```python
LanceDBClient()
```

Connects lazily to LanceDB at `LANCEDB_PATH` (default `./data/lancedb`). Table names and symbol schema depend on the active index profile. The default profile uses `docs`/`symbols`; `axis_python_v1` uses profile-specific doc/symbol tables and also materializes shared or workspace-partitioned adjacency tables.

### Tables

#### docs
| Column | Type | Description |
|---|---|---|
| `id` | string | `"{file_path}::{chunk_index}"` |
| `workspace_id` | string | Workspace scope for branch/tenant isolation |
| `file_path` | string | Source markdown file |
| `chunk` | string | Raw chunk text |
| `pending` | list[string] | Identifier names not yet linked to a symbol |
| `vector` | float32[384] | `all-MiniLM-L6-v2` embedding of `chunk` |
| `embedding_metadata` | string | JSON model/version/content/vector hashes |
| `owner_uid` | string | Owning symbol for in-code docstring/JSDoc anchors; empty for ordinary markdown chunks |

#### symbols
| Column | Type | Description |
|---|---|---|
| `uid` | string | Symbol UID (matches Neo4j) |
| `workspace_id` | string | Workspace scope for branch/tenant isolation |
| `name` | string | Symbol name |
| `file_path` | string | Source code file |
| `code` | string | Symbol source lines |
| `vector` | float32[384] | `all-MiniLM-L6-v2` embedding of `code` |
| `embedding_metadata` | string | JSON model/version/content/vector hashes |

For `axis_python_v1`, symbol rows additionally carry `symbol_kind`,
`qualified_name`, AST/CFG/DFG/structural bits, container kinds, evidence and
contract JSON, `file_tier`, and a signature vector. The `axis_adjacency` and
`axis_adjacency_external` tables materialize graph-walk inputs; symbol and
adjacency tables may be physically partitioned per workspace when
`LANCEDB_WORKSPACE_PARTITIONED=true` (the default).

### Methods

#### upsert_chunks(file_path, chunks, workspace_id=DEFAULT_WORKSPACE_ID)
Delete all rows where `(workspace_id, file_path)` match, then insert new rows with `pending=[]`.

#### upsert_chunk_batches(file_chunks, workspace_id=DEFAULT_WORKSPACE_ID)
Bulk variant used by doc indexing. Embeds every chunk, deletes existing rows for
the workspace/file set, and inserts rows carrying the same `workspace_id`.

#### upsert_symbol_embeddings(symbols, workspace_id=DEFAULT_WORKSPACE_ID)
`symbols`: list of `{uid, name, file_path, code, workspace_id?}`. Embeds `code`
field. Delete-then-insert per `(workspace_id, uid)`.

#### search(query, limit=5, workspace_id=DEFAULT_WORKSPACE_ID) → list[dict]
Workspace-scoped ANN search over `docs` table. Returns `[{file_path, chunk}]`.

#### search_symbols(query, limit=5, threshold=0.4, workspace_id=DEFAULT_WORKSPACE_ID) → list[dict]
Workspace-scoped ANN search over `symbols` table. Filters by `_distance <= threshold`. Returns `[{uid, name, file_path, distance}]`.

#### get_pending(workspace_id=DEFAULT_WORKSPACE_ID) → dict[str, list[str]]
Returns `{chunk_id: [name, ...]}` for workspace-local doc chunks with non-empty `pending` list.

#### set_pending(chunk_id, pending, workspace_id=DEFAULT_WORKSPACE_ID)
Delete-then-insert for the target `(workspace_id, chunk_id)` row. LanceDB `update()` cannot handle empty list fields, so this is the safe write pattern.

### Embedding Model

`EMBED_MODEL` defaults to `all-MiniLM-L6-v2`. Model metadata is validated at client construction, but the SentenceTransformer itself is loaded lazily on first embed. The default produces 384-dimensional vectors and runs locally after the first model download. Embedding metadata and the content-hash cache are described in [spec_embedding_versioning.md](spec_embedding_versioning.md).

---

## HistoryProvider Default: SQLiteHistoryProvider (`context_engine/history/sqlite_provider.py`)

### Configuration

| Variable | Default | Description |
|---|---|---|
| `HISTORY_MODE` | `local` | `local`, `ephemeral`, or `disabled` |
| `HISTORY_DB_PATH` | `./data/history/surgical_context.sqlite3` | SQLite file for local mode |
| `HISTORY_RETENTION_DAYS` | unset | Optional non-negative retention window |

### Stored Data

History is metadata-first. The provider persists conversations, messages, selected request ids, ask snapshots, inspector snapshots, and impact snapshots, but sanitizes raw prompts, answers, code bodies, source snippets, free-text comments, and `raw_*` fields before writing JSON payloads.

### Methods

- `create_conversation(...)`, `get_conversation(...)`, `list_conversations(...)`
- `append_message(...)`, `list_messages(...)`, `set_selected_request(...)`
- `save_ask_snapshot(...)`, `save_inspector_snapshot(...)`, `save_impact_snapshot(...)`
- `get_conversation_bundle(...)`, `get_request_bundle(...)`

`DisabledHistoryProvider` returns empty/no-op results. `EphemeralSQLiteHistoryProvider` uses a temporary SQLite database for the current context_engine process.

---

## Limitations (current)

- LanceDB doc/vector search is workspace-filtered, but filters are still string predicates rather than a provider-neutral query API.
- No transaction batching in `_upsert_nodes` — N symbols = N round-trips to Neo4j.
- `set_pending` is delete-then-insert — concurrent writes to the same chunk_id would lose data (not an issue in current single-process design).
- LanceDB delete/search filters are manually quoted string predicates; keep special-character handling covered by tests before broadening path support.
- History storage is local SQLite only; encrypted SQLite, Postgres, and enterprise audit stores are future connector work.
- Neo4j and LanceDB are still concrete defaults for most write/read paths; retrieval-facing protocols exist, but full graph/vector provider wrappers are not complete.

---

## Planned Extensions

- Finish provider protocols/wrappers from [spec_storage_connectors.md](spec_storage_connectors.md): full `GraphProvider` and `VectorProvider` boundaries around the default clients
- Extend storage policy enforcement beyond current history sanitization to vector persistence and any future shared/audit connector
- Add local provider configuration modes for graph/vector defaults first: `local`, `local_docker`, `ephemeral`, and `disabled`
- Defer `customer_managed`, `dedicated_managed`, and `enterprise_audit` provider modes until local defaults and conformance tests are stable
- Batch `_upsert_nodes` into a single parameterized Cypher `UNWIND` call
- Provider-neutral/typed LanceDB filters for doc/vector tables
- Parameterized LanceDB delete filter to handle paths with special characters
- Tenant API contract graph labels and relationships from [spec_tenant_api_graph.md](spec_tenant_api_graph.md): `Service`, `ApiEndpoint`, `ApiSchema`, `EventTopic`, `ContractManifest`, and published metadata-only cross-project links
