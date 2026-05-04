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
- mechanism archetypes and a repository-specific retrieval strategy profile
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
  "strategy_profile": {
    "selected_strategy": "middleware_pipeline_trace",
    "role_plan": ["api_surface", "factory_surface", "composition_surface", "runtime_surface"],
    "mechanism_archetypes": [
      {
        "type": "middleware_pipeline",
        "confidence": 0.78,
        "role_plan": ["api_surface", "factory_surface", "composition_surface", "runtime_surface"]
      }
    ],
    "fallbacks": ["direct_symbol", "semantic_docs", "concept_to_symbol"]
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
- mechanism archetypes inferred from framework and dynamic-surface signals

The result is included under `stats["repository_profile"]`, persisted to the Neo4j `Workspace`, and printed as a compact readiness line. `stats["repository_profile_store"]` is `neo4j_workspace` when persistence succeeds. If a project pass finds no changed files, the fast indexer loads the existing profile from the workspace when available.

The single-file hot path does not currently rebuild the full repository profile.

### Phase 7 — Repository role taxonomy (Pass 1)

Implemented in `sidecar/indexer/role_clustering.py`. A per-repository role taxonomy derived from call-graph topology, intended to replace the hand-curated role naming heuristics that currently live in `mechanism_registry`, `repository_profile._MECHANISM_PATTERNS` / `_ARCHETYPE_ROLE_PLANS`, and `unified_ranker._infer_role`. No name patterns, no path heuristics, no preloaded framework knowledge — a symbol's role comes from its position in the graph.

**Pipeline order.** The fast pipeline runs Pass 1 between Phase 4 (DocAnchor resolution) and Phase 6 (repository readiness profile), so the taxonomy sees both CALLS-style edges and COVERS edges. The single-file hot path does not run Pass 1.

**Per-symbol structural features.** For every Symbol the pass extracts:
- `fan_in`, `fan_out` — counts of incoming/outgoing CALLS-family edges
- `cross_package_in`, `cross_package_out` — same counts restricted to edges whose endpoints sit in different file directories
- `depth_from_public` — BFS distance from any "public" symbol following outgoing CALLS edges; public = a graph source (no callers) that has at least one callee
- `doc_anchor_count` — incoming `:COVERS` edges from `:DocAnchor` chunks
- weighted doc-anchor signals — `definition`, `reference`, and `example` weights derived from `COVERS.anchor_type`, `COVERS.confidence`, and `COVERS.primary_bias`
- `kind` flags (class-like vs function-like)

Features are log-transformed and standardized (zero mean, unit variance) before clustering.

**Clustering.** k-means in `K ∈ [5, 8]`; the pass picks the K with the highest silhouette score on the standardized features. For large repositories, silhouette scoring is computed on a deterministic sample of symbols so Pass 1 does not dominate indexing time. Output:
- `RoleTaxonomy` — `feature_names`, `clusters`, `chosen_k`, `silhouette`, `sample_size`. Each cluster has a `centroid`, `member_count`, and a `signature` listing the top-3 features that distinguish it from the global mean (e.g. `["log_fan_in:+", "depth_from_public:-", "is_function:+"]`).
- `uid → cluster_id` map for every indexed symbol.

**Role catalog autoresolve.** Cluster ids are local to a re-index, so Pass 1 also builds a `RoleCatalog` from each cluster's centroid shape. The catalog maps portable structural archetypes to confidence-ranked clusters:

```json
{
  "active_entrypoint": [
    {"cluster_id": 5, "confidence": 0.82, "evidence": ["log_fan_out:+", "leaf_score:-", "depth_from_public:-"]}
  ],
  "runtime_handle": [
    {"cluster_id": 4, "confidence": 0.88, "evidence": ["log_fan_in:+", "cross_package_in_ratio:+"]}
  ]
}
```

Canonical roles resolve through archetype preferences instead of direct cluster equality. For example, `runtime_surface` resolves to `active_entrypoint`, `runtime_handle`, and `executor`; `api_surface` resolves to `passive_api_surface` and `active_entrypoint`. Passive/config surfaces use typed doc-anchor weight, not raw doc count, so definition/reference anchors can lift public API clusters while example-heavy or weak anchors contribute less. Consumers should treat the result as a scoring preference, not a hard filter.

**Persistence.** Pass 1 writes the taxonomy and role catalog onto the Neo4j `Workspace`, then writes the cluster id onto every `Symbol`:

```cypher
(:Workspace {
  id,
  role_taxonomy_json,
  role_taxonomy_schema_version,
  role_catalog_json,
  role_catalog_schema_version,
  role_taxonomy_updated_at
})

(:Symbol { uid, ..., derived_role_id })
```

`stats["role_taxonomy"]` exposes `chosen_k`, `silhouette`, and `sample_size` for benchmark and progress logs. `stats["role_catalog"]` exposes the number of archetypes and canonical role mappings. Timing is recorded under `stats["timings_sec"]["role_clustering"]`.

**Consumers.** Pass 1 is intentionally side-effect-only for ranking: it persists `derived_role_id`, `role_taxonomy_json`, and `role_catalog_json`, but no query-time code path uses those fields for selection yet. Cutting `mechanism_registry`, `repository_profile`, and `unified_ranker._infer_role` over to the role catalog is a follow-up step. Until that happens, the framework-specific heuristics still drive ranking; Pass 1 just produces the universal substitute next to them so it can be validated on real graphs first.

**Trade-offs.**
- Cluster boundaries may not align with human intuition. The derived "role 3" can group `APIRoute` and `Dependant` together (both data-class-like, leaf-ish) while separating `add_api_route` from `api_route` if their fan-in differs. That is fine for ranking but means the benchmark cannot match by mechanism string equality across repos.
- Cluster ids are not stable across re-indexes if the graph changes meaningfully. The `signature` is the durable identity of a cluster; consumers should match on signature shape, not on raw `cluster_id`.
- Full silhouette scoring is `O(n²)` per K, so the implementation uses deterministic sampling for larger repositories. The score is a model-selection heuristic, not a benchmark metric.
- Pass 1 runs on every full project pass, even when changes are small. The "no changed files" branch reuses the existing taxonomy from the Workspace.

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
- The single-file hot path does not currently refresh the full repository profile or rerun Pass 1.
- Current impact capability is still shallow: AFFECTS reachability does not prove behavioral breakage.
- Pass 1 produces the role taxonomy but no consumer reads it yet — `mechanism_registry`, `repository_profile._MECHANISM_PATTERNS`, and `unified_ranker._infer_role` are still the active sources of role decisions.

---

## Planned Extensions

- Skip unchanged files using `File.hash` comparison (ADR-001 §hash mismatch = dirty flag)
- Parallel file processing for large repos (`concurrent.futures.ThreadPoolExecutor`)
- Single-pass Phase 1+2 (extract symbols and calls in one tree-sitter parse)
