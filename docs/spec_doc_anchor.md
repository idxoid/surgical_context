# DocAnchor — Spec

## Overview

`context_engine/doc_anchor.py` — links documentation chunks to code symbols in Neo4j. Enables hybrid retrieval: vector search finds a relevant doc chunk → graph traversal finds the code it describes.

---

## Neo4j Node: DocAnchor

**Properties:** `chunk_id` only (ADR-001 — no content, no file paths on nodes).

**Edges:**
- `(DocAnchor)-[:FROM {type: "doc"}]->(File)` — the doc file this chunk was extracted from
- `(DocAnchor)-[:FROM {type: "code"}]->(File)` — code files containing symbols this chunk covers (set when COVERS edges are created)
- `(DocAnchor)-[:FROM {type: "spec"|"architecture"|"concept"|"idea"|...}]->(File)` — other project docs referenced inline in the chunk
- `(DocAnchor)-[:COVERS {anchor_type, confidence, primary_bias, resolver}]->(Symbol)` — code symbols this chunk describes

`FROM` edges carry a `type` property classifying the relationship. `File` nodes receive a `doc_type` property (`"code"`, `"spec"`, `"architecture"`, `"concept"`, `"idea"`, `"documentation"`, `"roadmap"`, `"review"`) derived from filename patterns.
`COVERS` edges carry retrieval metadata: `anchor_type` (`definition`, `example`, `reference`, `warning`, `deprecated`), `confidence` (`0..1`), `primary_bias` (focal-vs-secondary symbol weighting), and `resolver` (`identifier`, `semantic`, or `pending_identifier`).

The `chunk_id` is the key back into LanceDB `docs` table to retrieve the actual text.

---

## Matching Strategy

Two complementary methods run for every chunk:

### 1. Semantic matching (vector search)

```python
lance.search_symbols(chunk_text, limit=5, threshold=1.5)
```

Embeds the chunk text and searches the LanceDB `symbols` table (code body embeddings). Returns symbols whose cosine distance ≤ `SIMILARITY_THRESHOLD = 1.5` (all-MiniLM-L6-v2 cosine distance scale 0–2). Creates `[:COVERS {resolver: "semantic", ...}]` and `[:FROM {type: "code"}]` edges immediately.

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
- Found in Neo4j `name_to_uid` map → create `[:COVERS {resolver: "identifier", ...}]` edge immediately
- Not found → add to `pending` list in LanceDB `docs` table

Every COVERS edge is classified during creation. The classifier is heuristic and content-free from Neo4j's perspective: it reads chunk text from LanceDB, writes only `anchor_type`, `confidence`, `primary_bias`, and `resolver` on the relationship, and leaves DocAnchor nodes as `chunk_id` only.

---

## Lazy Resolution

Identifiers that reference code not yet indexed are stored in `docs.pending` (LanceDB). On every code index run, `resolve_pending_anchors()` is called:

```
resolve_pending_anchors(neo4j, lance)
  1. lance.get_pending()  →  {chunk_id: [name, ...]}
  2. Fetch current name_to_uid from Neo4j
  3. For each pending name now in graph:
       create/update COVERS with resolver="pending_identifier"
       resolved_total += 1
  4. lance.set_pending(chunk_id, still_pending)
```

This handles forward references — doc chunks written before the code they describe get linked as soon as the code is indexed.

---

## ADR-001 Compliance

DocAnchor nodes store **only** `chunk_id`. File path is navigable via `[:FROM]->(File)` edge. Chunk text lives in LanceDB. Pending identifiers live in LanceDB `docs.pending` column. Nothing content-related is stored on the Neo4j node.

---

## Cross-Document Reference Linking

When a chunk contains inline references to other project docs (markdown links or bare filenames like `spec_arbitrator.md`, `architectura.md`), `_link_related_docs()` creates typed FROM edges:

```python
# In chunk text: "see spec_arbitrator.md (removed) for details"
(DocAnchor)-[:FROM {type: "spec"}]->(File {path: "docs/spec_arbitrator.md"})
```

**Doc type classification** (`_classify_doc_type`):

| Filename pattern | `doc_type` |
|---|---|
| `spec_*.md` | `spec` |
| `architectura*.md` | `architecture` |
| `concept*.md` | `concept` |
| `idea_*.md` | `idea` |
| `road_map*.md` | `roadmap` |
| `review_*.md` | `review` |
| other | `documentation` |

**Example navigation queries:**

```cypher
-- All specs referenced by a doc chunk
MATCH (da:DocAnchor)-[:FROM {type: "spec"}]->(f:File)
WHERE da.chunk_id = $chunk_id RETURN f.path

-- All doc chunks covering a symbol, with their source types
MATCH (da:DocAnchor)-[r:COVERS]->(s:Symbol {name: $name})
MATCH (da)-[:FROM]->(f:File)
RETURN da.chunk_id, f.path, f.doc_type, r.anchor_type, r.confidence
ORDER BY r.confidence DESC, f.doc_type

-- Deep: code file → covering docs → referenced specs
MATCH (code:File)-[:CONTAINS]->(s:Symbol)<-[:COVERS]-(da:DocAnchor)
      -[:FROM {type: "spec"}]->(spec:File)
WHERE code.path = $file_path
RETURN DISTINCT spec.path
```

---

## Limitations (current)

- `_IDENTIFIER_RE` may match common English words that happen to be snake_case or CamelCase. False positives remain.
- `[:COVERS]` and `[:FROM]` edges are additive — re-indexing docs does not remove stale edges from deleted chunks. Orphaned DocAnchor nodes accumulate until a full graph wipe.
- `_link_related_docs` only matches filenames; it does not follow transitive doc references (spec references architecture which references concept).
- Anchor classification is heuristic v1 and English-oriented.

---

## Planned Extensions

- Remove orphaned DocAnchor nodes on re-index (detect chunk IDs no longer present in LanceDB)
- Configurable `SIMILARITY_THRESHOLD` via env var or config file
- Transitive doc reference linking (depth > 1)
- Bidirectional discovery surfaced in `/ask` response
