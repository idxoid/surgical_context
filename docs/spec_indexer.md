# Code Indexer — Spec

## Overview

`sidecar/indexer/code.py` — full project code indexing pipeline. Walks a directory, extracts symbols and typed function calls, embeds symbol bodies, resolves pending DocAnchors, rebuilds the AFFECTS reverse-dependency index, and emits a repository readiness profile.

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

## Repository Readiness Profile

Indexing now produces a `repository_profile` in its returned stats. This is the index-time capability contract for the repo: it records not only what was indexed, but what kinds of reasoning are safe to attempt.

Implemented in `sidecar/indexer/repository_profile.py`.

Profiles are persisted on the Neo4j `Workspace` node as index metadata:

```cypher
(:Workspace {
  id,
  repository_profile_json,
  repository_profile_schema_version,
  repository_profile_updated_at
})
```

This keeps sidecar metadata out of the user's checkout. An up-to-date index pass can reuse the last profile from the graph instead of returning an empty "nothing changed" state.

The profile includes:

- supported and unsupported language/file surfaces
- parse coverage and symbol density
- call/import/inheritance density
- framework and mechanism signals
- dynamic surfaces such as decorators, registries, templates, generated APIs, metaprogramming, and C/macros
- capability flags for code navigation, static call reasoning, decorator/runtime registry semantics, doc-code bridge, and impact analysis
- a `reasoning_contract` with allowed and risky reasoning modes

Example:

```json
{
  "indexability": "medium",
  "retrieval_readiness": "partial",
  "capabilities": {
    "code_navigation": "medium",
    "static_call_reasoning": "low",
    "impact_analysis": "shallow_partial"
  },
  "reasoning_contract": {
    "allowed": ["symbol/file navigation over indexed languages"],
    "risky": ["impact is shallow and may miss dynamic/framework edges"]
  }
}
```

This profile should be treated as part of indexing, not as benchmark logic. Benchmark and UI surfaces may read it, but they should not be the first layer to discover that a repo has an unsupported language surface, missing symbol surface, or shallow impact model.

The current implementation is deliberately conservative. It does not claim full framework understanding; it only declares the boundaries that the current graph/parser/doc bridge can support.

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

### Phase 6 — Repository readiness profile (project pass)

The fast project indexer builds a repository profile after graph/doc-anchor phases using:

- collected files and extension distribution
- parsed file count
- observed symbols/calls/imports/inheritance
- AFFECTS rebuild status
- lightweight path/source signals from changed files

The result is included under `stats["repository_profile"]`, persisted to the Neo4j `Workspace`, and printed as a compact readiness line. `stats["repository_profile_store"]` is `neo4j_workspace` when persistence succeeds. If a project pass finds no changed files, the fast indexer loads the existing profile from the workspace when available.

The single-file hot path does not currently rebuild the full repository profile.

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

- The repository profile is a conservative first-pass contract, not a deep framework model.
- Framework and dynamic-surface detection uses lightweight path/source signals; it should guide routing and diagnosis, not replace mechanism-specific evidence.
- The single-file hot path does not currently refresh the full repository profile.
- Current impact capability is still shallow: AFFECTS reachability does not prove behavioral breakage.

---

## Planned Extensions

- Skip unchanged files using `File.hash` comparison (ADR-001 §hash mismatch = dirty flag)
- Parallel file processing for large repos (`concurrent.futures.ThreadPoolExecutor`)
- Single-pass Phase 1+2 (extract symbols and calls in one tree-sitter parse)
