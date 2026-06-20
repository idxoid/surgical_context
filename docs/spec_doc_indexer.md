# Doc Indexer — Spec

## Overview

`context_engine/indexer/docs.py` — walks a directory for `*.md` files, chunks them, embeds into LanceDB, then links chunks to code symbols in Neo4j via DocAnchor nodes with rich FROM/COVERS relationships.

Entry point: `index_docs(docs_path: str)`, also callable as `python context_engine/indexer/docs.py <path>` or `POST /index/docs` via `context_engine/main.py`.

---

## Chunking Pipeline

### Step 1 — Section split (_split_by_sections)

Splits on heading boundaries matching `^#{1,3} .+`. Each heading starts a new chunk that includes the heading text.

- If no headings found → entire text is one section
- Empty sections are dropped

**Why headings-first:** A spec document with 20 sections of 50 words each should produce 20 focused chunks, not 1 blunt 1000-word blob. Sections map directly to retrieval targets.

### Step 2 — Word-window fallback (_word_split_chunk)

Applied per section when the section exceeds `CHUNK_SIZE = 400` words.

- Window: 400 words
- Overlap: 80 words (20%)
- Sections ≤ 400 words → returned as-is (no split)

### Combined (_chunk_text)

```
_chunk_text(text)
  → _split_by_sections(text)         # list of sections
  → for each section: _word_split_chunk(section)   # flatten
```

---

## Indexing Flow

```
index_docs(docs_path)
  1. glob("**/*.md")
  2. for each file:
       chunks = _chunk_text(file_text)
       lance.upsert_chunks(file_path, chunks)        # embed + store in LanceDB
  3. link_docs_to_symbols(neo4j, lance)              # DocAnchor + FROM/COVERS phase
  4. resolve_pending_anchors(neo4j, lance)           # resolve forward refs
  5. neo4j.close()
```

**DocAnchor linking flow** (`context_engine/indexer/anchor.py`):
- For each chunk:
  1. `_write_anchor()` — create DocAnchor node, set `File.doc_type`, create `FROM {type: "doc"}` edge
  2. Semantic search: `lance.search_symbols(chunk_text, threshold=1.5)` → hits
  3. For each hit: classify anchor type/confidence and create `COVERS {anchor_type, confidence, primary_bias, resolver}` + `FROM {type: "code"}` edge to symbol's file
  4. Identifier extraction: regex match CamelCase, UPPER_CASE, snake_case names
  5. For each extracted name: if found in Neo4j, create typed/confident `COVERS` + `FROM {type: "code"}`; else add to pending
  6. `_link_related_docs()` — extract doc references (spec_*.md, architectura.md, etc.), create typed `FROM {type: "spec"|"architecture"|...}` edges

---

## Chunk IDs

Generated in `LanceDBClient.upsert_chunks()`:
```
id = "{file_path}::{chunk_index}"
```

Index is positional within the file. Re-indexing the same file deletes all previous chunks for that path before inserting new ones.

---

## FROM Relationship Hierarchy (Phase 5+)

Each DocAnchor can have multiple FROM edges with different `type` properties:

| Type | Target | Created by | Meaning |
|---|---|---|---|
| `"doc"` | File (doc source) | `_write_anchor()` | This chunk came from this doc file |
| `"code"` | File (code owner) | `_add_covers_edge()` | Code file containing a COVERS'd symbol |
| `"spec"` | File (spec doc) | `_link_related_docs()` | Spec referenced inline in the chunk |
| `"architecture"` | File (architecture doc) | `_link_related_docs()` | Architecture doc referenced inline |
| `"concept"` | File (concept doc) | `_link_related_docs()` | Concept doc referenced inline |
| `"idea"` | File (idea doc) | `_link_related_docs()` | Idea doc referenced inline |

This enables rich knowledge graph queries: given a code symbol, find all doc chunks that mention it AND the specs/architecture they reference.

## COVERS Relationship Metadata

`COVERS` edges are no longer boolean-only. The linker writes:

| Property | Meaning |
|---|---|
| `anchor_type` | `definition`, `example`, `reference`, `warning`, or `deprecated` |
| `confidence` | Link confidence in `[0, 1]`, based on resolver type, symbol mention, heading match, and code-style mention |
| `primary_bias` | `1.0` for a single/focal symbol, lower for secondary symbols in multi-symbol chunks |
| `resolver` | `identifier`, `semantic`, or `pending_identifier` |

The context ranker (axis; legacy `UnifiedRanker` removed 2026-06-15) consumes these properties when giving vector-retrieved doc chunks graph overlap credit and when using DocAnchor co-mentions as semantic bridge edges.

---

## Limitations (current)

- Only `*.md` files are indexed. Other doc formats (`.rst`, `.txt`, `.adoc`) are ignored.
- Section split only handles `#`, `##`, `###` — deeper headings (`####`+) are treated as body text.
- Chunk IDs are positional — if a section moves within a file, its chunk ID changes and old DocAnchor edges become orphaned until re-indexing.
- Doc reference extraction uses simple regex (`[text](docs/file.md)` or bare filenames) — does not follow transitive references.
- Semantic threshold `SIMILARITY_THRESHOLD = 1.5` is fixed — not tunable per query (designed for all-MiniLM-L6-v2 model, cosine distance scale 0–2).
- Anchor type/confidence is heuristic v1 and may need locale or repo-style tuning for non-English docs.

---

## Planned Extensions

- Support `.rst` and `.txt` doc formats
- Handle `####`–`######` heading levels
- Stable chunk IDs based on heading text hash rather than position
- Transitive doc reference linking (spec → architecture → concepts)
- Configurable similarity threshold per retrieval query (Phase 6+)
