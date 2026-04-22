# Surgical Context — Idea Summary

**Mission:** Surgically precise AI context using local code analysis and a graph knowledge base — minimizing token costs and eliminating hallucinations.

---

## 1. The Storage Trinity

**Neo4j (The Navigator)** — stores only the project "map": nodes (functions, classes) and relationships between them. No source code text stored here.

**LanceDB (The Searcher)** — local vector database for documentation embeddings (`all-MiniLM-L6-v2`). Enables semantic retrieval of relevant doc snippets. ✅ Implemented.

**SQLite (The Memory)** — planned local history store for conversations, messages, ask snapshots, inspector snapshots, impact snapshots, and feedback tokens. Local-only by default.

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

## 6. New Ideas / Optional Add-ons

These ideas are not required for the Local Developer Product. They stay documented so the architecture can grow without confusing the near-term release target.

**LLM Proxy Gateway** — optional independent service in front of model APIs. The sidecar still keeps model preferences, model selection, prompt assembly, and routing intent. The gateway is an execution proxy: it receives the selected model call and forwards it to the configured LLM provider.

Possible modules / use cases:
- Unified execution API for OpenAI, Anthropic, local Ollama, Bedrock, Vertex, Azure OpenAI, or customer-hosted models.
- Account and expense balancing across one user with many provider accounts, many users sharing one account, or tenant-level account pools.
- Centralized provider credentials, rate limits, budgets, quota tracking, and cost attribution.
- Auditing of model calls, metadata, selected model, token usage, latency, and provider errors.
- PII filtration, redaction, masking, or policy checks before a prompt leaves the local/customer boundary.
- Optional fallback when the selected provider/account is unavailable, if product policy allows fallback.

Boundary: the gateway is not required for the core local product and does not replace sidecar model selection. The extension should need only small configuration changes, if any. The sidecar calls either a direct LLM client or the proxy transport with the already-selected model instruction.

---

## 7. Security (Privacy by Design)

- Source code never leaves the local machine for storage.
- Local Neo4j stores only the topology map — names and relationships, no code text. Future managed graph providers must keep the same boundary.
- Vector embeddings stored locally in LanceDB.
- Local history defaults to SQLite with policy gates around prompt text, response text, source snippets, retention, and redaction.
- Open question for future managed vector providers: embedding-inversion risk. Before any vector data syncs outside the local machine, an ADR must justify the leakage surface because vectors can be partially inverted to recover source text.

---

## 8. Current Status

Pre-release, local dev tool. The active target is the Local Developer Product and open-source candidate. SaaS, marketplace, tenant graph, alternate database backends, and microservice splitting are deferred until the local loop is stable.

Near-term priorities:
- One-command local setup and smoke-testable extension flow.
- SQLite local history and prompt snapshots.
- Streaming chat, Inspector/Impact synchronization, settings UX, and dashboard resilience.
- Remaining prompt-contract observability and local latency SLO checks.

See `road_map.md` and ADR-006 for the full blocking rationale.
