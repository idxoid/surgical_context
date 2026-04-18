# Surgical Context — Idea Summary

**Mission:** Surgically precise AI context using local code analysis and a graph knowledge base — minimizing token costs and eliminating hallucinations.

---

## 1. The Storage Trinity

**Neo4j (The Navigator)** — stores only the project "map": nodes (functions, classes) and relationships between them. No source code text stored here.

**LanceDB (The Searcher)** — local vector database for documentation embeddings (`all-MiniLM-L6-v2`). Enables semantic retrieval of relevant doc snippets. ✅ Implemented.

**Local FS (The Source of Truth)** — all actual code and doc text stays on disk. The sidecar reads it on demand, just before sending a prompt.

---

## 2. The Sidecar Binary (Python)

Central orchestrator between VS Code and data sources.

**Indexer** — four-phase pipeline: extract symbols → link CALLS edges → embed symbol bodies → resolve pending DocAnchors. ✅ Implemented (`sidecar/indexer/code.py`).

**Context Arbitrator** — assembles surgical context from Neo4j + local FS into a typed `PromptContext`:
- If symbol has a dirty (unsaved) version → reads from In-Memory Overlay.
- Otherwise → reads coordinates from graph, fetches code from disk.
✅ Implemented (`sidecar/context/arbitrator.py`, `sidecar/context/overlay.py`).

**FastAPI Sidecar** — JSON-RPC over HTTP, endpoints: `POST /index`, `POST /index/docs`, `POST /ask`, `POST /search`, `POST /overlay`, `DELETE /overlay`, `GET /health`. ✅ Implemented (`sidecar/main.py`).

---

## 3. Data Model (Hybrid Graph)

```
(File)-[:CONTAINS]->(Symbol)
(Symbol)-[:CALLS]->(Symbol)
(DocAnchor)-[:COVERS]->(Symbol)
(DocAnchor)-[:FROM]->(File)
```

Symbol node stores: `uid`, `name`, `kind`, `range`, `hash`. No `file_path` — navigate via `[:CONTAINS]` edge (ADR-001). ✅ Implemented.

---

## 4. JSON Prompt Contract

✅ Implemented (`sidecar/context/arbitrator.py` — `PromptContext.to_dict()`).

```json
{
  "primary_source": {
    "symbol": "process_payment",
    "file_path": "sidecar/payments.py",
    "is_dirty": false,
    "code": "..."
  },
  "graph_context": [
    { "symbol": "db_save", "file_path": "sidecar/db.py", "relation": "CALLS", "is_dirty": false, "code": "..." }
  ],
  "documentation": [
    { "chunk_id": "docs/payments.md::3", "source_file": "docs/payments.md", "content": "..." }
  ]
}
```

`/ask` returns this contract under the `"context"` key alongside `"answer"`. The LLM receives a flat text rendering via `to_system_prompt()`.

---

## 5. Key Features

**Surgical Retrieval** — BFS depth 1–2 in Neo4j, only linked symbols included. Reduces context by 60–80%.

**Dirty State Overlay** — unsaved edits visible to AI instantly via in-memory re-parse. TTL = editor session.

**Local LLM** — Ollama/llama3 by default, swappable via `OLLAMA_MODEL` env var. Anthropic SDK integration prepared but deferred.

**Model Round-Robin** (planned) — route simple queries to cheap models, complex to powerful ones.

**Token Dashboard** (planned) — VS Code tab showing real savings per request.

---

## 6. Security (Privacy by Design)

- Source code never leaves the local machine for storage.
- Neo4j SaaS (Phase 4, deferred) stores only the topology map — names and relationships, no code text.
- Vector embeddings stored locally in LanceDB.
- Open question for Phase 4: embedding-inversion risk. Before any LanceDB data syncs to cloud, an ADR must justify the leakage surface (vectors can be partially inverted to recover source text).

---

## 7. Current Status

Pre-release, local dev tool. SaaS (Phase 4) and Marketplace (Phase 5) are deferred until:

- **Phase 2.5** — evaluation harness, structured logging, `/metrics`, token accounting.
- **Phase 3.5** — incremental `POST /index/file`, token-budget-driven BFS, `IMPORTS` / `DEPENDS_ON` edges.

See `road_map.md` and ADR-006 for the full blocking rationale.
