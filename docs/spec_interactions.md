# Cross-Module Interactions — Spec

> **Superseded (2026-06-15).** Describes the legacy ranking cascade / `qa_benchmark` harness, removed in the cascade cleanup — axis (`context_engine/axis/`, `QA/axis_benchmark.py`) is the sole context + eval path now. Kept for historical context; see `cascade_cleanup_inventory.md`.


## Module Map

```
context_engine/indexer/code.py
  ├── context_engine/parser/extractor.py        SymbolExtractor
  │     └── context_engine/parser/registry.py   LanguageAdapterRegistry (adapters/*)
  ├── context_engine/database/neo4j_client.py   Neo4jClient
  ├── context_engine/database/lancedb_client.py LanceDBClient
  └── context_engine/indexer/anchor.py          resolve_pending_anchors()

context_engine/indexer/docs.py
  ├── context_engine/database/lancedb_client.py LanceDBClient
  ├── context_engine/database/neo4j_client.py   Neo4jClient
  └── context_engine/indexer/anchor.py          link_docs_to_symbols()

context_engine/indexer/anchor.py
  ├── context_engine/database/neo4j_client.py   Neo4jClient  (passed in)
  └── context_engine/database/lancedb_client.py LanceDBClient (passed in)

context_engine/context/arbitrator.py
  ├── context_engine/database/neo4j_client.py   Neo4jClient  (passed in)
  └── context_engine/context/overlay.py         InMemoryOverlay (passed in, optional)

context_engine/context/overlay.py
  └── context_engine/parser/extractor.py        SymbolExtractor

context_engine/main.py  (FastAPI)
  ├── context_engine/indexer/code.py            run_indexing()
  ├── context_engine/indexer/docs.py            index_docs()
  ├── context_engine/context/arbitrator.py      ContextArbitrator
  ├── context_engine/context/overlay.py         InMemoryOverlay  [singleton]
  ├── context_engine/workspace_paths.py         resolve_path_under_workspace_root()
  └── context_engine/database/lancedb_client.py LanceDBClient    [singleton]

run_demo.py
  ├── context_engine/indexer/code.py            run_indexing()
  ├── context_engine/indexer/docs.py            index_docs()
  ├── context_engine/context/arbitrator.py      ContextArbitrator
  ├── context_engine/database/neo4j_client.py   Neo4jClient
  └── context_engine/database/lancedb_client.py LanceDBClient

context_engine/ai/
  ├── engine.py                          AIEngine — wired from main.py (Ollama + Anthropic SDK)
  ├── auth.py                            GitHubAuth (device flow OAuth; not wired)
  └── session.py                         SessionManager (token persistence; not wired)
```

---

## Flow 1: Code Indexing

**Trigger:** `POST /index` or `python indexer_main.py <path>` or `run_demo.py`

```
run_indexing(project_path)
│
├─ [collect] _collect_files(project_path)
│     pathspec (.gitignore) → list of .py/.ts/.tsx files
│
├─ [Phase 1] for each file:
│     SymbolExtractor.extract(file_path)
│       → REGISTRY.get_adapter(language).extract_symbols()
│       → tree-sitter query from adapter → tree-sitter parse → SymbolMetadata list
│     Neo4jClient.upsert_file_structure(file_path, hash, symbols)
│       → MERGE File node
│       → MERGE Symbol nodes (uid, name, kind, hash, range)
│       → MERGE (File)-[:CONTAINS]->(Symbol)
│
├─ [Phase 2] for each file:
│     SymbolExtractor.extract_calls(file_path)
│       → REGISTRY.get_adapter(language).extract_calls_from_source()
│       → call query from adapter → tree-sitter parse → [{caller_uid, callee_uid?, callee_name, rel_type, confidence, tier}]
│     Neo4jClient.link_calls(calls)
│       → MERGE (caller)-[:CALLS_* {workspace_id, confidence, tier, resolver}]->(callee)
│
├─ [Phase 3] for each file:
│     SymbolExtractor.extract_from_source(source, file_path)
│       → SymbolMetadata list with start/end lines
│     LanceDBClient.upsert_symbol_embeddings([{uid, name, file_path, code}])
│       → SentenceTransformer.encode(code)
│       → delete-then-insert in `symbols` table
│
└─ [Phase 4]
      resolve_pending_anchors(neo4j, lance)
        → LanceDBClient.get_pending()  →  {chunk_id: [name,...]}
        → Neo4j: MATCH (s:Symbol) RETURN s.uid, s.name  →  name_to_uid
        → for each resolvable name: _add_covers_edge(chunk_id, uid)
        → LanceDBClient.set_pending(chunk_id, still_pending)
```

**Data written:**
- Neo4j: `File`, `Symbol` nodes; `CONTAINS`, `CALLS` edges
- LanceDB `symbols` table: code body embeddings
- Neo4j: additional `COVERS` edges (pending resolution)

---

## Flow 2: Doc Indexing

**Trigger:** `POST /index/docs` or `run_demo.py` or `python context_engine/doc_indexer.py <path>`

```
index_docs(docs_path)
│
├─ glob("**/*.md")
│
├─ for each .md file:
│     _chunk_text(text)
│       → _split_by_sections()   [on ^#{1,3} headings]
│       → _word_split_chunk()    [fallback: 400 words, 80 overlap]
│     LanceDBClient.upsert_chunks(file_path, chunks)
│       → SentenceTransformer.encode(chunks)
│       → delete all rows where file_path=X
│       → insert rows: {id="{path}::{i}", file_path, chunk, pending=[], vector}
│
└─ link_docs_to_symbols(neo4j, lance)
      │
      ├─ lance._table.to_pandas()   →  all doc rows
      ├─ Neo4j: MATCH (s:Symbol) RETURN s.uid, s.name  →  name_to_uid
      │
      └─ for each chunk:
            [semantic] lance.search_symbols(chunk_text, limit=5, threshold=0.4)
              → embed chunk → ANN on `symbols` table → filter by distance
              → _write_anchor(chunk_id, file_path)      [MERGE DocAnchor + [:FROM]]
              → _add_covers_edge(chunk_id, hit.uid)     [MERGE [:COVERS]]
            │
            [identifier] _extract_identifiers(chunk_text)
              → _IDENTIFIER_RE  (CamelCase | UPPER_CASE | snake_case)
              → skip names already matched by semantic search
              → if name in name_to_uid → _add_covers_edge immediately
              → else → pending.append(name)
            │
            lance.set_pending(chunk_id, pending)
```

**Data written:**
- LanceDB `docs` table: chunks with embeddings, `pending` list
- Neo4j: `DocAnchor` nodes; `FROM`, `COVERS` edges

---

## Flow 3: /ask — Surgical Context Assembly

**Trigger:** `POST /ask {symbol, question, file_path?}`

**Path guard:** when `file_path` is set (file fallback), `main._sandbox_path()` resolves it under the workspace `project_path` from the index manifest before `_read_file_context()` opens the file. Outside root → `403`; no manifest → `400`.

```
ask(req)
│
├─ Neo4jClient()                          [new connection per request]
│
├─ ContextArbitrator(db, overlay)
│     get_context_for_symbol(symbol)
│       │
│       ├─ Neo4j: MATCH (s:Symbol {name}) OPTIONAL MATCH (s)-[:CALLS]->(dep)
│       │         RETURN s, collect(dep)
│       │
│       ├─ _read_code(target_node)
│       │     Neo4j: MATCH (f:File)-[:CONTAINS]->(s {uid}) RETURN f.path
│       │     InMemoryOverlay.has(file_path) ?
│       │       yes → overlay.read_lines(file_path, start, end)
│       │       no  → open(file_path).readlines()[start-1:end]
│       │
│       └─ for each dep: _read_code(dep_node)   [same logic]
│
├─ LanceDBClient.search(symbol + question, limit=3)
│     → embed query → ANN on `docs` table
│     → [{file_path, chunk}]
│
├─ context += doc chunks as "--- DOCUMENTATION ---" section
│
└─ AIEngine.chat(system_prompt, question)     [context_engine/ai/engine.py via main.py]
      → Ollama by default (`MODEL_PREFERENCE=ollama`)
      → Anthropic SDK only when `ALLOW_CLOUD_LLM=true` and routing selects cloud
      → {"symbol": ..., "answer": ..., "context": PromptContract}
```

**Reads from:**
- Neo4j: Symbol + File topology
- InMemoryOverlay (if file is dirty) OR Local FS
- LanceDB `docs` table

---

## Flow 4: /overlay — Dirty State Update

**Trigger:** `POST /overlay {file_path, content}` (called on every keypress from VS Code)

**Path guard:** `file_path` is sandboxed to the workspace project root before overlay storage (same rules as Flow 3).

```
update_overlay(req)
│
├─ InMemoryOverlay.update(file_path, content)
│     _files[file_path] = content
│
└─ InMemoryOverlay.get_symbols(file_path)
      SymbolExtractor.extract_from_source(content, file_path)
        → tree-sitter parse (language="python" hardcoded)
        → {name: (start_line, end_line)}
```

**Effect on Flow 3:** Next `/ask` call for the **same user** picks up dirty content via `overlay.has(file_path, workspace_id, user_id)` in `CodeResolver` / `ContextArbitrator`. Overlay keys are `(workspace_id, user_id, file_path)` — not workspace-wide. No Neo4j write — overlay is ephemeral.

---

## Shared State and Ownership

| Object | Owned by | Lifetime | Shared across |
|---|---|---|---|
| `InMemoryOverlay` | `context_engine/main.py` | Process | All `/overlay` + `/ask` requests |
| `LanceDBClient` (vector_db) | `context_engine/main.py` | Process | `/search`, `/ask`, `/index/docs` |
| `Neo4jClient` | Per request | Request | Closed in `finally` |
| `SymbolExtractor` | `InMemoryOverlay.__init__` | Process | All overlay parses |
| `SentenceTransformer` | `LanceDBClient.__init__` | Process | All embeds |

---

## Data Flow Between Stores

```
Local FS  ──[read by]──▶  SymbolExtractor  ──▶  Neo4j (topology)
                                           ──▶  LanceDB symbols (embeddings)

Docs FS   ──[read by]──▶  doc_indexer      ──▶  LanceDB docs (chunks + pending)
                                           ──▶  Neo4j (DocAnchor nodes + edges)

LanceDB docs.pending  ──[resolved by]──▶  Neo4j COVERS edges
LanceDB symbols.vector ──[queried by]──▶  DocAnchor semantic matching

Neo4j topology  ──[traversed by]──▶  ContextArbitrator  ──▶  LLM prompt
LanceDB docs    ──[searched by]──▶   ContextArbitrator  ──▶  LLM prompt
Local FS        ──[read by]──▶       ContextArbitrator  ──▶  LLM prompt
InMemoryOverlay ──[read by]──▶       ContextArbitrator  ──▶  LLM prompt
```

---

## LLM routing (`context_engine/ai/engine.py`)

`main.py` constructs a process-wide `AIEngine` used by `/ask` and `/ask/stream`.

| Backend | When |
|---|---|
| **Ollama** | Default (`MODEL_PREFERENCE=ollama`). Always available when Ollama is running. |
| **Anthropic** | `MODEL_PREFERENCE` is `auto` or `claude`, **`ALLOW_CLOUD_LLM=true`**, and `ANTHROPIC_API_KEY` is set. |
| **Fallback** | Cloud errors → Ollama when local model is reachable. |

Prompt caching: Anthropic `cache_control` on the large code/graph block when above `_MIN_CACHE_TOKENS`. Default model **`claude-sonnet-4-6`** (`ANTHROPIC_MODEL` env). See [spec_sidecar_api.md](spec_sidecar_api.md) configuration tables.

## Unused Modules (stubs)

| Module | Status | Notes |
|---|---|---|
| `context_engine/ai/auth.py` | Not wired | GitHub Device Flow OAuth. Not called from any current entry point. |
| `context_engine/ai/session.py` | Not wired | Persists GitHub token to `~/.config/surgical_context_engine/session.json`. Not called from any current entry point. |

---

## silence.py

`context_engine/silence.py` — installed at startup in `indexer_main.py` and `run_demo.py` via:
```python
from context_engine.silence import install as _silence; _silence()
```

Wraps `sys.stderr` with a filter that drops known noisy lines from HuggingFace Hub, CUDA init warnings, and BertModel load reports. No functional effect on any module — purely output hygiene.
