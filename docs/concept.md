# Surgical Context — Technical Concept

## 1. The Storage Trinity

Data is split across three stores, each optimized for its role.

**Neo4j** — graph topology only. Nodes: `File`, `Symbol`, `DocAnchor`. Edges: `CONTAINS`, `CALLS`, `COVERS`, `FROM`. No code text, no file paths stored on nodes (ADR-001). ✅ Running via Docker.

**LanceDB** — local vector index for documentation chunks. Model: `all-MiniLM-L6-v2` (384-dim). ✅ Implemented.

**Local FS** — source of truth for all text content. Read on demand using line coordinates from graph.

---

## 2. The Sidecar Binary

External Python process. VS Code communicates via FastAPI (localhost HTTP). Fault-isolated: if sidecar hangs, editor stays responsive.

### Implemented endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness check |
| POST | `/index` | Index a project directory into Neo4j + LanceDB |
| POST | `/index/docs` | Index a documentation directory into LanceDB + DocAnchor graph |
| POST | `/ask` | Assemble `PromptContext`, query Ollama, return answer + JSON contract |
| POST | `/search` | Semantic search over indexed docs (LanceDB) |
| POST | `/overlay` | Push unsaved file content into memory |
| DELETE | `/overlay` | Clear overlay for a file (on save/close) |

### Context Arbitrator

Returns a typed `PromptContext` dataclass (`sidecar/context/arbitrator.py`):
1. Fetch target symbol from Neo4j (uid, range); resolve file path via `(File)-[:CONTAINS]->(Symbol)`
2. Check In-Memory Overlay — if dirty version exists, read from memory (`is_dirty=True`)
3. Otherwise read code from disk by line range
4. Repeat for all direct `CALLS` dependencies (BFS depth 1)
5. Caller attaches doc chunks from LanceDB to `PromptContext.documentation`
6. `to_system_prompt()` → flat text for LLM; `to_dict()` → JSON Prompt Contract returned to VS Code

### In-Memory Overlay

`InMemoryOverlay` holds `{file_path: content}` for unsaved files. On `POST /overlay`, re-parses symbols from in-memory content via tree-sitter — no disk access. Cleared on save or close.

---

## 3. Indexing Pipeline

Two-phase to ensure all nodes exist before edges are created:

**Phase 1 — Symbol extraction** (per file):
- tree-sitter AST query for `function_definition`, `class_definition`, and module-level UPPER_CASE assignments (`variable`)
- Extract: name, kind, start/end line, content hash, UID (`sha256(file_path:name)`)
- Multi-language: Python (`.py`) + TypeScript (`.ts`, `.tsx`); gitignore-aware file collection
- Neo4j upsert: `MERGE (s:Symbol {uid})`, `MERGE (f)-[:CONTAINS]->(s)`

**Phase 2 — Call linking** (per file):
- tree-sitter query for `call` nodes
- Walk up AST to find enclosing function (caller)
- Neo4j: `MERGE (caller)-[:CALLS]->(callee)` matched by name

**Phase 3 — Symbol embeddings:**
- Read each symbol's source lines from disk
- Embed code bodies into LanceDB `symbols` table

**Phase 4 — Pending DocAnchor resolution:**
- `resolve_pending_anchors()` checks LanceDB `docs.pending` against newly indexed symbols
- Creates `[:COVERS]` edges for any identifiers now present in graph

---

## 4. Vector / Doc Layer

**Doc indexer** (`sidecar/indexer/docs.py`):
- Walks directory for `*.md` files
- Section-aware chunking: splits on `#`/`##`/`###` headings first; word-window fallback (400 words, 80 overlap) for oversized sections
- Embeds with `sentence-transformers/all-MiniLM-L6-v2`
- Upserts into LanceDB (delete-then-insert per file)
- Calls `link_docs_to_symbols()` after indexing

**LanceDB client** (`sidecar/database/lancedb_client.py`) — two tables:
- `docs`: `id, file_path, chunk, pending: list[str], vector[384]`
- `symbols`: `uid, name, file_path, code, vector[384]`
- `search(query, limit)` — ANN over docs table
- `search_symbols(query, limit, threshold)` — ANN over symbols table with cosine distance filter
- `get_pending()` / `set_pending()` — lazy DocAnchor resolution state

**DocAnchor** (`sidecar/indexer/anchor.py`) — Neo4j node with only `chunk_id` property.
- `[:FROM]` edge to `File` node (source doc file)
- `[:COVERS]` edges to `Symbol` nodes (semantic + identifier matching)
- Unresolved identifiers stored in `docs.pending` (LanceDB); resolved on every index run via `resolve_pending_anchors()`

---

## 5. LLM Integration

**Current:** Ollama (`llama3` default, `OLLAMA_MODEL` env override). Stateless — single system + user message per request.

**Planned:** Official Anthropic SDK (`sidecar/ai/engine.py` has commented implementation). Activation deferred to Phase 5.

---

## 6. ADR Summary

**ADR-001** — Code text stays on FS, only topology in Neo4j. Keeps graph lightweight and code off SaaS.

**ADR-002** — Python sidecar for MVP. Best ecosystem for tree-sitter + AI libs. Compiled to binary (Nuitka) at launch.

**ADR-003** — Shared SaaS graph + Local Dirty Overlay. Team shares one Neo4j Aura graph; local unsaved changes handled in-memory only.

**ADR-004** — Model round-robin by intent + context size. Simple → cheap model, complex → powerful model.

**ADR-005** — LanguageAdapter protocol. All language-specific logic (tree-sitter queries, call resolution) lives behind a protocol so new languages plug in without core edits.

**ADR-006** — Quality gates before SaaS. Phase 4 (SaaS) and Phase 5 (launch) are blocked until Phase 2.5 (eval harness + metrics) and Phase 3.5 (incremental indexing + token budget) are green.

---

## 7. Dev-Phase Priorities

The project is pre-release. SaaS and marketplace are deferred. The active work stream is:

1. **Phase 2.5 — Quality Foundation.** Eval harness, structured logging, `/metrics` endpoint, token accounting. Every claim in §1 of the architecture doc must be measurable before it can be scaled.
2. **Phase 3.5 — Arbitration & Indexing Robustness.** Incremental `/index/file`, token-budget-driven BFS instead of hardcoded `*1..2`, re-ranking (callers > callees), `IMPORTS` / `DEPENDS_ON` edges.
3. **Extension scaffold.** The "thin client" half of the architecture does not yet exist on disk — a `extension/` workspace with a minimal chat window is a Phase 1 gap.

Phase 4 and 5 remain on the road map but are explicitly blocked on the above (see ADR-006).
