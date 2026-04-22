# Storage Layer — Spec

## Overview

The current implementation uses two concrete storage clients:

- `Neo4jClient` for graph topology and metadata.
- `LanceDBClient` for vector retrieval over docs and symbol bodies.

These are now treated as default provider implementations behind the planned storage connector layer. See [spec_storage_connectors.md](spec_storage_connectors.md).

Neither graph storage nor tenant API graph storage may store raw source code content (ADR-001). Vector and history storage are governed by storage policy because they may contain text snippets, embeddings, prompts, answers, or prompt-context snapshots.

### Provider Families

| Family | Current Default | Responsibility |
|---|---|---|
| `GraphProvider` | Neo4j | Topology: files, symbols, edges, workspaces, DocAnchors, tenant API links |
| `VectorProvider` | LanceDB | Semantic indexes: docs, symbol embeddings, embedding metadata |
| `HistoryProvider` | Planned SQLite | User dialogs, ask snapshots, inspector/impact snapshots, retention policy |

---

## GraphProvider Default: Neo4jClient (`sidecar/database/neo4j_client.py`)

### Connection

```python
Neo4jClient(uri, user, password)
```

Opens a `neo4j.GraphDatabase.driver` connection. Call `.close()` when done. Sidecar endpoints use request-scoped `db_session(...)`.

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
| `COVERS` | `(DocAnchor)→(Symbol)` | Doc chunk describes this symbol |

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

## VectorProvider Default: LanceDBClient (`sidecar/database/lancedb_client.py`)

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
`symbols`: list of `{uid, name, file_path, code, workspace_id?}`. Embeds `code` field. Delete-then-insert per uid.

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

- LanceDB doc/vector search is not yet strongly workspace-filtered; graph reads and code bodies are workspace-scoped.
- No transaction batching in `_upsert_nodes` — N symbols = N round-trips to Neo4j.
- `set_pending` is delete-then-insert — concurrent writes to the same chunk_id would lose data (not an issue in current single-process design).
- LanceDB delete filter uses string interpolation — values with single quotes in file paths would break the filter.
- No `HistoryProvider` exists yet; dialog history and prompt/impact/inspector snapshots are currently webview/session state rather than durable local product state.
- Neo4j and LanceDB are concrete clients, not yet replaceable through provider interfaces.

---

## Planned Extensions

- Introduce provider protocols from [spec_storage_connectors.md](spec_storage_connectors.md): `GraphProvider`, `VectorProvider`, and `HistoryProvider`
- Add SQLite-backed `HistoryProvider` for local conversations, messages, ask snapshots, inspector snapshots, and impact snapshots
- Add storage policy enforcement before history/vector persistence of raw text, code snippets, prompt text, or response text
- Add local provider configuration modes first: `local`, `local_docker`, `ephemeral`, and `disabled`
- Defer `customer_managed`, `dedicated_managed`, and `enterprise_audit` provider modes until local defaults and conformance tests are stable
- Batch `_upsert_nodes` into a single parameterized Cypher `UNWIND` call
- Strong workspace filters in LanceDB doc/vector tables
- Parameterized LanceDB delete filter to handle paths with special characters
- Tenant API contract graph labels and relationships from [spec_tenant_api_graph.md](spec_tenant_api_graph.md): `Service`, `ApiEndpoint`, `ApiSchema`, `EventTopic`, `ContractManifest`, and published metadata-only cross-project links
