# Indexer — Spec

## Overview

`indexer_main.py` — full project indexing pipeline. Walks a directory, extracts symbols and calls, embeds symbol bodies, resolves pending DocAnchors.

Entry point: `python indexer_main.py [path]` (defaults to repo root).  
Also called programmatically from `POST /index` and `run_demo.py`.

---

## File Collection

### _collect_files(project_path) → list[str]

1. Loads `.gitignore` from `project_path` (falls back to repo root `.gitignore`) using `pathspec` library.
2. `os.walk` with in-place dir pruning — ignored directories are removed from `dirs[:]` so `os.walk` never descends into them.
3. Filters files by extension — only extensions registered in the language adapter registry are included.
4. Skips dot-files (`name.startswith('.')`).
5. Skips files matched by gitignore pattern.

Supported extensions are auto-derived from registered adapters (ADR-005):
```python
from sidecar.parser.registry import REGISTRY
_INDEXED_EXTENSIONS = {ext for adapter in REGISTRY.supported_adapters() for ext in adapter.file_extensions}
```
Currently: `.py`, `.pyi` (Python), `.ts`, `.tsx` (TypeScript).

New languages can be added by creating an adapter in `sidecar/parser/adapters/` — no core changes needed.

---

## Indexing Phases

### Phase 1 — Symbol extraction (per file)

```python
symbols = extractor.extract(file_path)
file_hash = open(file_path, 'rb').read().hex()
db.upsert_file_structure(file_path, file_hash, symbols)
```

All nodes created before any edges — ensures Phase 2 finds all callee nodes.

### Phase 2 — Call linking (per file)

```python
calls = extractor.extract_calls(file_path)
db.link_calls(calls)
```

`MERGE (caller)-[:CALLS]->(callee)` edges. Callee matched by name.

### Phase 3 — Symbol body embeddings

Reads each symbol's source lines from disk using `s.start_line`/`s.end_line`.  
Upserts into LanceDB `symbols` table via `lance.upsert_symbol_embeddings()`.

Enables semantic DocAnchor matching in Phase 4 and in future `/ask` calls.

### Phase 4 — Pending DocAnchor resolution

```python
resolve_pending_anchors(db, lance)
```

Checks LanceDB `docs.pending` against symbols now present in Neo4j. Creates `[:COVERS]` edges for any identifiers that have become resolvable since the last doc index run.

---

## File Hash

Currently: `open(file_path, 'rb').read().hex()` — full file bytes as hex string. Used to set `File.hash` in Neo4j for future cache-invalidation logic (changed hash = re-extract). Not yet used for skipping unchanged files.

---

## Limitations (current)

- File hash is full byte read — not a proper SHA256 digest. Will be standardized in a future pass.
- No incremental indexing — every run re-extracts all symbols and re-links all calls.
- Phase 1 and 2 each open every file individually — 2× file reads per file. Could be merged.

---

## Planned Extensions

- Skip unchanged files using `File.hash` comparison (ADR-001 §hash mismatch = dirty flag)
- Parallel file processing for large repos (`concurrent.futures.ThreadPoolExecutor`)
- Single-pass Phase 1+2 (extract symbols and calls in one tree-sitter parse)
