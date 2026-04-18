# DocAnchor — Spec

## Overview

`sidecar/doc_anchor.py` — links documentation chunks to code symbols in Neo4j. Enables hybrid retrieval: vector search finds a relevant doc chunk → graph traversal finds the code it describes.

---

## Neo4j Node: DocAnchor

**Properties:** `chunk_id` only (ADR-001 — no content, no file paths on nodes).

**Edges:**
- `(DocAnchor)-[:FROM]->(File)` — which doc file this chunk came from
- `(DocAnchor)-[:COVERS]->(Symbol)` — which code symbols this chunk describes

The `chunk_id` is the key back into LanceDB `docs` table to retrieve the actual text.

---

## Matching Strategy

Two complementary methods run for every chunk:

### 1. Semantic matching (vector search)

```python
lance.search_symbols(chunk_text, limit=5, threshold=0.4)
```

Embeds the chunk text and searches the LanceDB `symbols` table (code body embeddings). Returns symbols whose cosine distance ≤ `SIMILARITY_THRESHOLD = 0.4`. Creates `[:COVERS]` edges immediately.

### 2. Identifier extraction (regex)

```python
_IDENTIFIER_RE = re.compile(
    r'\b([A-Z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*|[A-Z][A-Z0-9_]*_[A-Z0-9_]+|[a-z][a-z0-9]*_[a-z0-9_]+)\b'
)
```

Matches three identifier conventions:
- `CamelCase` — two or more uppercase letters (excludes single-word like `In`, `My`)
- `UPPER_CASE_WITH_UNDERSCORE` — e.g. `CHUNK_SIZE`, `DB_PATH`
- `snake_case_with_underscore` — e.g. `extract_calls`, `upsert_chunks`

For each extracted name:
- Already matched by semantic search → skip
- Found in Neo4j `name_to_uid` map → create `[:COVERS]` edge immediately
- Not found → add to `pending` list in LanceDB `docs` table

---

## Lazy Resolution

Identifiers that reference code not yet indexed are stored in `docs.pending` (LanceDB). On every code index run, `resolve_pending_anchors()` is called:

```
resolve_pending_anchors(neo4j, lance)
  1. lance.get_pending()  →  {chunk_id: [name, ...]}
  2. Fetch current name_to_uid from Neo4j
  3. For each pending name now in graph:
       _add_covers_edge(chunk_id, uid)
       resolved_total += 1
  4. lance.set_pending(chunk_id, still_pending)
```

This handles forward references — doc chunks written before the code they describe get linked as soon as the code is indexed.

---

## ADR-001 Compliance

DocAnchor nodes store **only** `chunk_id`. File path is navigable via `[:FROM]->(File)` edge. Chunk text lives in LanceDB. Pending identifiers live in LanceDB `docs.pending` column. Nothing content-related is stored on the Neo4j node.

---

## Limitations (current)

- `_IDENTIFIER_RE` may match common English words that happen to be snake_case or CamelCase (e.g. `re_compile`, `In_Progress`). Tightened pattern reduces noise but false positives remain.
- Semantic threshold `0.4` is a fixed constant — not tunable per project.
- `[:COVERS]` edges are additive — re-indexing docs does not remove stale edges from deleted chunks. Orphaned DocAnchor nodes accumulate until a full graph wipe.

---

## Planned Extensions

- Remove orphaned DocAnchor nodes on re-index (detect chunk IDs no longer present in LanceDB)
- Configurable `SIMILARITY_THRESHOLD` via env var or config file
- Bidirectional discovery: given a Symbol, find all DocAnchor nodes that `[:COVERS]` it (already queryable via Cypher, not yet surfaced in `/ask`)
