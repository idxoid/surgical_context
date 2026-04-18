# Doc Indexer — Spec

## Overview

`sidecar/doc_indexer.py` — walks a directory for `*.md` files, chunks them, embeds into LanceDB, then links chunks to code symbols in Neo4j via DocAnchor nodes.

Entry point: `index_docs(docs_path: str)`, also callable as `python -m sidecar.doc_indexer <path>`.

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
       lance.upsert_chunks(file_path, chunks)   # embed + store
  3. link_docs_to_symbols(neo4j, lance)          # DocAnchor phase
  4. neo4j.close()
```

---

## Chunk IDs

Generated in `LanceDBClient.upsert_chunks()`:
```
id = "{file_path}::{chunk_index}"
```

Index is positional within the file. Re-indexing the same file deletes all previous chunks for that path before inserting new ones.

---

## Limitations (current)

- Only `*.md` files are indexed. Other doc formats (`.rst`, `.txt`, `.adoc`) are ignored.
- Section split only handles `#`, `##`, `###` — deeper headings (`####`+) are treated as body text.
- Chunk IDs are positional — if a section moves within a file, its chunk ID changes and the old DocAnchor `[:COVERS]` edges become orphaned until re-indexing.

---

## Planned Extensions

- Support `.rst` and `.txt` doc formats
- Handle `####`–`######` heading levels
- Stable chunk IDs based on heading text hash rather than position
