# Surgical Context — Technical Concept

## 0. Whole Meaning

Surgical Context is a context operating system for code assistants. Its core promise is not generic "chat with code"; it makes context selection explicit, measurable, and inspectable before an LLM answers.

The product loop:

1. Index code into a graph of files, symbols, and relationships.
2. Index docs into vector chunks and graph anchors.
3. Listen to editor state, including unsaved overlays.
4. Given a symbol and question, assemble the smallest useful context packet.
5. Route the prompt to a local or cloud model.
6. Return both the answer and the context contract so the user can inspect what the model saw.

The architectural center of gravity is retrieval correctness. If symbol identity, call resolution, branch isolation, and prompt observability are correct, model quality can improve steadily. If those are weak, better models only hide retrieval mistakes.

## 0.1. Release Shape

The active product target is the **Local Developer Product**. It is a single-tenant, local-first VS Code tool that can be used from a developer machine without a managed SaaS dependency.

Local v0.1 includes:
- VS Code UI: Chat, Inspector, Impact, Settings, and Dashboard.
- Python sidecar.
- Local graph/vector/history defaults: Neo4j Docker, LanceDB, SQLite.
- Ask/inspect/impact flows for symbol, file, workspace, and direct LLM questions.
- Local repository docs indexing.
- Prompt-context transparency and privacy controls.

Future layers are deliberately separate:
- **Team:** shared customer-owned storage, admin/user roles, connectable doc sources, and tenant API contract links between project-published manifests.
- **Enterprise / Platform:** alternate database connectors, audit stores, optional LLM proxy gateway transport, dedicated deployments, microservice split, and parser/indexer hot-path rewrites after profiling.

## 1. Storage Provider Layer

Data is split across provider families, each optimized for its role. The local defaults are Neo4j, LanceDB, and SQLite. These defaults will sit behind provider connectors so storage boundaries are explicit; real alternate backends are a later Team/Enterprise layer.

**GraphProvider** — graph topology only. Default: Neo4j. Nodes: `File`, `Symbol`, `DocAnchor`. Edges: `CONTAINS`, `CALLS_DIRECT`/`CALLS_DYNAMIC`/`CALLS_INFERRED`, `COVERS`, `FROM` (with type classification), `DEPENDS_ON`, `IMPORTS`, `AFFECTS` (Phase 5 reverse dependencies). No code text stored in graph storage (ADR-001). ✅ Neo4j default running via Docker.

**VectorProvider** — semantic index for documentation chunks and optional symbol embeddings. Default: LanceDB local. Model: `all-MiniLM-L6-v2` (384-dim). ✅ Implemented.

**HistoryProvider** — user dialogs, messages, ask snapshots, inspector snapshots, and impact snapshots. Default planned: SQLite local. Enterprise modes may point to a customer-managed audit/history store.

**Local FS** — source of truth for all text content. Read on demand using line coordinates from graph.

---

## 2. The Sidecar Binary

External Python process. VS Code communicates via FastAPI (localhost HTTP). Fault-isolated: if sidecar hangs, editor stays responsive.

### Implemented endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness check |
| POST | `/index` | Index a project directory into the configured graph/vector providers |
| POST | `/index/file` | Re-index one saved file |
| POST | `/index/files` | Queue and batch multiple saved-file updates |
| GET | `/index/queue` | Inspect indexing queue state |
| POST | `/index/docs` | Index a documentation directory into the vector provider + DocAnchor graph |
| POST | `/ask` | Assemble `PromptContext`, query Ollama, return answer + JSON contract |
| POST | `/ask/stream` | Streaming answer endpoint over JSON-safe server-sent events |
| POST | `/search` | Semantic search over indexed docs |
| POST | `/search/unified` | Blend symbols, graph neighbors, and docs in one result set |
| POST | `/overlay` | Push unsaved file content into memory |
| DELETE | `/overlay` | Clear overlay for a file (on save/close) |
| POST | `/feedback` | Record accept/reject feedback against a retrieval snapshot |
| GET | `/impact` | Return downstream symbols/files affected by a symbol |
| POST | `/auth/token` | Generate a user token for multi-user mode |
| GET | `/auth/users` | List active users |
| GET | `/status/cloud` | Report graph provider local/cloud status |
| GET | `/audit/actions` | Return recent audit entries |
| GET | `/metrics` | Prometheus-style metrics |

### Context Arbitrator

Returns a typed `PromptContext` dataclass (`sidecar/context/arbitrator.py`):
1. Detect query intent from user question via `IntentClassifier` (Phase 6.1)
2. Fetch target symbol from the graph provider (uid, range); resolve file path via `(File)-[:CONTAINS]->(Symbol)`
3. Check In-Memory Overlay — if dirty version exists, read from memory (`is_dirty=True`)
4. Otherwise read code from disk by line range
5. Expand graph via BFS (token-budget constrained) to gather all `CALLS` dependencies + reverse deps
6. Compile context tier-aware per intent: code → cross-refs → specs → architecture → concepts → ideas
7. Attaches doc chunks from the vector provider to `PromptContext.documentation`
8. `to_system_prompt()` → flat text for LLM; `to_dict()` → JSON Prompt Contract with `mode` + `intent` fields

Doc retrieval happens before prompt compilation, so intent-aware document selection and `tier_tokens` are produced by the same arbitration path that builds the prompt.

### Intent Classification & Graceful Degradation (Phase 6.1)

`IntentClassifier` (`sidecar/context/intent_classifier.py`):
- Detects one of 6 query intents: **navigation** ("where is X?"), **debugging** ("why does X fail?"), **refactor** ("rename X everywhere"), **exploration** ("how does X work?"), **new_feature** ("add X"), **design_question** ("how should we approach this?")
- Each intent has a unique 6-tier priority: `[code, cross_refs, specs, architecture, concept, idea]` orderings vary
- Example: navigation prioritizes code + cross-refs; new_feature deprioritizes code, prioritizes ideas/concepts

`PromptCompiler.compile_with_intent()`:
- Fills context tiers in priority order per detected intent
- Graceful degradation: if a tier is empty, proceeds to next tier
- If all tiers exhausted → `mode = "standard"` (no surgical context, bare LLM call)
- Otherwise: `mode = "surgical_full"` (code + graph) or `mode = "surgical_doc_only"` (docs only)

PromptContext now includes:
- `mode`: indicates which context tier(s) populated the response
- `intent`: the detected query intent (for observability and model routing)

### In-Memory Overlay

`InMemoryOverlay` holds `{(workspace_id, file_path): content}` for unsaved files. On `POST /overlay`, re-parses symbols from in-memory content via tree-sitter — no disk access. Cleared on save or close.

---

## 3. Indexing Pipeline

Two-phase to ensure all nodes exist before edges are created:

**Phase 1 — Symbol extraction** (per file):
- tree-sitter AST query for `function_definition`, `class_definition`, and module-level UPPER_CASE assignments (`variable`)
- Extract: name, kind, start/end line, content hash, stable UID v2 (`sha256(language:qualified_name|normalized_signature)[:16]`)
- Multi-language: Python (`.py`) + TypeScript (`.ts`, `.tsx`); gitignore-aware file collection
- GraphProvider upsert: `MERGE (s:Symbol {uid})`, `MERGE (f)-[:CONTAINS]->(s)` in the current Neo4j default

**Phase 2 — Call linking** (per file):
- tree-sitter query for `call` nodes
- Walk up AST to find enclosing function (caller)
- GraphProvider: create `CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS` edges using callee UID/qualified-name first, unique name fallback last

**Phase 3 — Symbol embeddings:**
- Read each symbol's source lines from disk
- Embed code bodies into the vector provider `symbols` index

**Phase 4 — Pending DocAnchor resolution:**
- `resolve_pending_anchors()` checks vector-provider pending references against newly indexed symbols
- Creates `[:COVERS]` edges for any identifiers now present in graph

---

## 4. Vector / Doc Layer

**Doc indexer** (`sidecar/indexer/docs.py`):
- Walks directory for `*.md` files
- Section-aware chunking: splits on `#`/`##`/`###` headings first; word-window fallback (400 words, 80 overlap) for oversized sections
- Embeds with `sentence-transformers/all-MiniLM-L6-v2`
- Upserts into the vector provider (delete-then-insert per file in the current LanceDB default)
- Calls `link_docs_to_symbols()` after indexing

**VectorProvider default: LanceDB client** (`sidecar/database/lancedb_client.py`) — two tables:
- `docs`: `id, file_path, chunk, pending: list[str], vector[384]`
- `symbols`: `uid, name, file_path, code, vector[384]`
- `search(query, limit)` — ANN over docs table
- `search_symbols(query, limit, threshold)` — ANN over symbols table with cosine distance filter
- `get_pending()` / `set_pending()` — lazy DocAnchor resolution state

**DocAnchor** (`sidecar/indexer/anchor.py`) — graph-provider node with only `chunk_id` property.
- `[:FROM]` edge to `File` node (source doc file)
- `[:COVERS]` edges to `Symbol` nodes (semantic + identifier matching)
- Unresolved identifiers stored in vector-provider pending metadata; resolved on every index run via `resolve_pending_anchors()`

---

## 5. LLM Integration

**Current:** `AIEngine` supports Ollama (`llama3` default, `OLLAMA_MODEL` env override) and Anthropic Claude when `ANTHROPIC_API_KEY` is set. `MODEL_PREFERENCE=auto` routes by context size and intent; `claude` and `ollama` force a provider.

---

## 6. ADR Summary

**ADR-001** — Code text stays on FS; only topology goes into the graph provider. Keeps graph storage lightweight and code out of graph storage.

**ADR-002** — Python sidecar for MVP. Best ecosystem for tree-sitter + AI libs. Compiled to binary (Nuitka) at launch.

**ADR-003** — Pluggable Graph Provider + Local Dirty Overlay. Solo users can use local Docker; teams can use customer-managed graph storage; local unsaved changes remain in-memory only.

**ADR-004** — Model round-robin by intent + context size. Simple → cheap model, complex → powerful model.

**ADR-005** — LanguageAdapter protocol. All language-specific logic (tree-sitter queries, call resolution) lives behind a protocol so new languages plug in without core edits.

**ADR-006** — Local release before managed release. Cloud/local provider fallback, model routing, and the extension scaffold exist, but managed deployments and marketplace readiness remain blocked on the local daily-driver loop: setup, history, extension polish, and observability.

**ADR-008** — Storage Provider Connectors. Graph, vector, and user-history storage are connector-backed; Neo4j, LanceDB, and SQLite are defaults, not lock-in. Local v0.1 wraps these defaults first; alternate backends come later.

---

## 7. Dev-Phase Priorities

The project is pre-release. SaaS, marketplace, tenant graph, alternate database backends, and microservice splitting are deferred. The active work stream is:

1. **Local daily-driver loop.** Clean setup, sidecar health, local storage defaults, extension commands, and smoke-testable ask/inspect/impact.
2. **Local history.** SQLite conversations, messages, ask snapshots, inspector snapshots, impact snapshots, retention, and privacy gates.
3. **Extension productization.** Streaming answers, prompt selection history, dashboard resilience, settings UX, command placement, and accessibility.
4. **Retrieval observability.** Remaining JSON Prompt Contract fields, pruning explanations, ranker weights, latency SLO checks, and QA harness tuning.
5. **Provider boundaries.** Wrap Neo4j/LanceDB/SQLite behind narrow contracts before adding real alternate backends.
