# Code Indexer — Spec

## Overview

`sidecar/indexer/code.py` — full project code indexing pipeline. Walks a directory, extracts symbols and typed function calls, embeds symbol bodies, resolves pending DocAnchors, and rebuilds the AFFECTS reverse-dependency index.

Entry points:
- CLI: `python sidecar/indexer/code.py [path]` (defaults to repo root)
- Programmatic: `run_indexing(path)`, `index_file(path, db, lance, extractor)`
- API: `POST /index` via `sidecar/main.py`

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

### Phase 2 — Typed call linking (per file, Phase 5+)

```python
calls = extractor.extract_calls(file_path)
db.link_calls(calls)
```

Creates typed call edges: `MERGE (caller)-[:{rel_type}]->(callee)` where `rel_type` ∈ {`CALLS_DIRECT`, `CALLS_DYNAMIC`, `CALLS_INFERRED`}.

**Call types** (from `sidecar/parser/adapters/python_adapter.py`):
- `CALLS_DIRECT` — static/deterministic calls (default, dunders like `__init__`)
- `CALLS_DYNAMIC` — dispatch via `self.method()` or other receivers (confidence: 0.7)
- `CALLS_INFERRED` — string-based patterns (`getattr`, `eval`, globals, etc.) (confidence: 0.4)

**Pre-Phase 5:** all edges were `CALLS_DIRECT`. Backward-compatible: edges default to `CALLS_DIRECT` if no `rel_type` provided.

### Phase 3 — Symbol body embeddings

Reads each symbol's source lines from disk using `s.start_line`/`s.end_line`.  
Upserts into LanceDB `symbols` table via `lance.upsert_symbol_embeddings()`.

Enables semantic DocAnchor matching in Phase 4 and in future `/ask` calls.

### Phase 4 — Pending DocAnchor resolution

```python
resolve_pending_anchors(db, lance)
```

Checks LanceDB `docs.pending` against symbols now present in Neo4j. Creates `[:COVERS]` edges for any identifiers that have become resolvable since the last doc index run.

### Phase 5 — AFFECTS index rebuild (per changed symbol, Phase 5+)

```python
from sidecar.indexer.affects import AFFECTSIndexer
indexer = AFFECTSIndexer(db)
indexer.rebuild_affects(changed_uids)
```

After all symbols for a file are upserted, delete stale `AFFECTS` edges and recompute reverse-dependency paths (depth ≤ 4). Enables cascade-aware incremental reindexing.

Called synchronously at end of `index_file()` — blocks until AFFECTS edges are rebuilt. Future: batch across files.

---

## File Hash & Incremental Indexing

**Implementation** (Phase 5+): `hashlib.sha256(file_bytes).hexdigest()` — stored in `File.hash` in Neo4j.

**Incremental logic** (`run_indexing`):
1. Compute SHA256 hash for all files in project
2. Query Neo4j for stored hashes (via `db.get_file_hashes()`)
3. Filter to changed files only (`current_hash != stored_hash`)
4. Delete symbols for changed files
5. Re-index only changed files
6. Resolve pending DocAnchors (full pass)

This reduces indexing time for large repos with few changes. Full re-index still supported via `delete_symbols_for_file()` + re-upsert.

---

## Limitations (current)

- AFFECTS rebuild is synchronous per file — scales linearly. Future: batch across files or use background workers.
- No parallel file processing — single-threaded walk. Future: `concurrent.futures.ThreadPoolExecutor`.
- Phase 1 and 2 each open every file individually (2× reads). Could merge into single tree-sitter parse.

---

## Planned Extensions

- Skip unchanged files using `File.hash` comparison (ADR-001 §hash mismatch = dirty flag)
- Parallel file processing for large repos (`concurrent.futures.ThreadPoolExecutor`)
- Single-pass Phase 1+2 (extract symbols and calls in one tree-sitter parse)
