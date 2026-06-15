# Code Indexer — Spec

## Overview

`sidecar/indexer/code.py` — full project code indexing pipeline. Walks a directory, extracts symbols and typed function calls, embeds symbol bodies, resolves pending DocAnchors, rebuilds the AFFECTS reverse-dependency index, and emits a repository readiness profile.

Entry points:
- CLI: `python sidecar/indexer/code.py [path]` (defaults to repo root)
- Programmatic: `run_indexing(path)`, `index_file(path, db, lance, extractor)`
- API: `POST /index` via `sidecar/main.py` — registers `project_path` immediately (including `queue=true` via `register_workspace_project_root()` in `sidecar/retrieval/manifest.py`) before batch workers run; see path sandboxing in [spec_sidecar_api.md](spec_sidecar_api.md#filesystem-path-sandboxing)

---

## File Collection

### _collect_files(project_path) → list[str]

1. Loads `.gitignore` from `project_path` (falls back to repo root `.gitignore`) using `pathspec` library. Patterns are interpreted relative to the indexed project root, not the monorepo checkout root.
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
- extension-based dynamic surfaces (decorators, templates, generated APIs, C/macros)
- a generic retrieval strategy profile (`generic_symbol_context`)
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
    "selected_strategy": "generic_symbol_context",
    "role_plan": ["docs_or_concept"],
    "fallbacks": ["direct_symbol", "semantic_docs"]
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

### Phase 5 — AFFECTS index rebuild (once per batch, Phase 5+)

```python
from sidecar.indexer.affects import AFFECTSIndexer
indexer = AFFECTSIndexer(db)
indexer.rebuild_affects(all_changed_uids)  # single call after all files processed
```

Deletes stale `AFFECTS` edges and recomputes reverse-dependency paths (depth ≤ 4). Called **once per batch**, not per file, to avoid O(N) `_load_reverse_adjacency` scans.

**`index_file` signature:**

```python
def index_file(
    file_path, db, lance, extractor,
    workspace_id=DEFAULT_WORKSPACE_ID,
    *,
    skip_affects: bool = False,
) -> list[str]:
```

- Returns the list of changed symbol UIDs so batch callers can collect them.
- `skip_affects=True` defers the AFFECTS rebuild to the batch caller.
- When `skip_affects=False` (default, hot path via `/index/file`), AFFECTS is rebuilt synchronously for that file — preserving the on-save latency contract.

Batch callers (`_process_index_batch` in `main.py`, `run_fast_indexing`) collect all changed UIDs across files and call `rebuild_affects` once after the loop, then call `LayeredCache.invalidate_files` for the full set of indexed paths.

### Phase 6 — Repository readiness profile (project pass)

The fast project indexer builds a repository profile after graph/doc-anchor phases using:

- collected files and extension distribution
- parsed file count
- observed symbols/calls/imports/inheritance
- AFFECTS rebuild status
- extension-based dynamic surfaces from indexed file paths
- generic `strategy_profile` (indexability only — no keyword mechanism detection)

The result is included under `stats["repository_profile"]`, persisted to the Neo4j `Workspace`, and printed as a compact readiness line. `stats["repository_profile_store"]` is `neo4j_workspace` when persistence succeeds. If a project pass finds no changed files, the fast indexer loads the existing profile from the workspace when available.

The single-file hot path does not currently rebuild the full repository profile.

### Phase 7 — Repository role taxonomy (Pass 1)

Implemented in `sidecar/indexer/role_clustering.py` + `sidecar/indexer/role_cascade.py`. A per-repository role assignment derived from call-graph topology via a **discriminator-first L1/L2 cascade** (see [role_clustering_architecture.md](role_clustering_architecture.md)). `repository_profile` reports indexability/capabilities only (no keyword mechanism detection). Python import extraction avoids a hand-maintained third-party allow-list.

**Pipeline order.** The fast pipeline runs Pass 1 between Phase 4 (DocAnchor resolution) and Phase 6 (repository readiness profile), so the pass sees CALLS-family edges, COVERS, INJECTS, HANDLES, DECORATED_BY, INSTANTIATES, USES_TYPE (with `kind`), and RE_EXPORTS-derived features. The single-file hot path does not run Pass 1.

**Per-symbol structural features.** For every Pass-1 symbol the indexer assembles weighted fan profiles: call/type/api/inject/depend/handle/decorated/construct fans (including USES_TYPE kind-split and F13 partial credit from full-graph consumers outside the test-free symbol set), plus `depth_from_public` (BFS from full-graph public roots, F13), cross-package counts, doc-anchor signals, `reexport_in`, proxy-binding flags, and extraction-time return-shape markers (`returns_mapping`, `returns_sequence`, `returns_constructed_type`, `returns_function_expression`).

**Assignment (schema v3).** No k-means, no cluster ids, no Pass-1 archetype tier:

```
extract_symbol_rows()
  → filter_clustering_rows()
  → assign_l1() / assign_l2() predicates (role_cascade.py)
  → detect_present_roles()  # presence gate
  → persist per-symbol primary + supporting roles
```

Output:
- `RoleAssignmentSummary` on `Workspace` — `method`, `sample_size`, `filtered_sample_size`, `present_roles`, `l1_distribution` (`role_taxonomy_schema_version = 3`).
- `RoleCatalog` — `present_roles` only (`role_catalog_schema_version = 3`).
- per-symbol `derived_primary_role`, `derived_supporting_roles_json`, plus structural fan fields for ranker diagnostics.

**Mechanism profiles in the catalog — REMOVED (2026-06-15).** The indexer no
longer merges `mechanism_registry` extensions into `role_catalog_json`: the merge
was inert (built-in tables empty; YAML `MECHANISM_PACK_PATH` packs were an opt-in
answer-key) and `mechanism_registry` + `mechanism_packs/` were deleted with the
cascade (the consumer `UnifiedRanker` is gone too). The role catalog is now pure
Pass-1 structural roles. See `cascade_cleanup_inventory.md`.

**Persistence.**

```cypher
(:Workspace {
  id,
  role_taxonomy_json,
  role_taxonomy_schema_version,   // 3
  role_catalog_json,
  role_catalog_schema_version,    // 3
  role_taxonomy_updated_at
})

(:Symbol {
  uid,
  derived_primary_role,
  derived_supporting_roles_json,
  call_fan_in,
  call_fan_out,
  type_fan_in,
  returns_mapping,
  returns_sequence,
  returns_constructed_type,
  returns_function_expression,
  ...
})
```

`stats["role_taxonomy"]` exposes `method`, `sample_size`, `filtered_sample_size`, and `present_role_count`. `stats["role_catalog"]` exposes `present_roles` count and preloaded mechanism count. Timing is under `stats["timings_sec"]["role_clustering"]`.

**Consumers.** `UnifiedRanker` reads `derived_primary_role` / `derived_supporting_roles_json` per symbol, `present_roles` from the catalog, optional mechanism templates, structural overlap scoring, then repository `strategy_profile` / `generic`. Inspect an indexed workspace: `python QA/prototype_role_cascade.py --repo fastapi`.

**Trade-offs.**
- Predicate thresholds can miss or over-fire on edge cases; fix by adding structural edges/features, not benchmark answer keys (see [engineering_principles.md](engineering_principles.md)).
- Pass 1 runs on every full project pass, even when changes are small. The "no changed files" branch reuses the existing taxonomy from the Workspace.
- Some roles remain honestly unmapped until dynamic-dispatch / dataflow gaps are closed (`request_router`, parts of `factory_surface`, and binding/data-shape roles). Phase A return-shape markers are persisted and visible to Pass 1, but they do not yet replace field/iteration/value-flow analysis — see [role_signature_findings.md](role_signature_findings.md).

### Fast pipeline — semantic hint phases

After per-file symbol/call linking, the fast project indexer (`sidecar/indexer/fast/pipeline.py`) runs graph-enrichment passes before the embedding batch, in this order:

1. **`framework_hints`** — applies shared typed rules from `semantic_hints.yaml` via `FrameworkHintsIndexer`; creates `SEMANTIC_HINT` edges for framework patterns already present in the indexed graph.
2. **`ts_http_route_hints`** — implemented in `sidecar/indexer/ts_http_route_hints.py`. Scans Python FastAPI route decorators (`@app.post("/ask")`, etc.) and TypeScript HTTP client surfaces (`export const SidecarClient = { ... post('/ask') ... }`). Creates `SEMANTIC_HINT` edges from TS `object_api` symbols to Python handler symbols when paths match. Skips test/QA Python files and prefers `main.py` / `sidecar/` entrypoints when duplicate routes exist.
3. **`proxy` (ProxySurface)** — `_proxy_binding_phase` creates `ProxyBinding` nodes + `PROXY_OF` edges for annotated lazy proxies (`current_app: FlaskProxy = LocalProxy(...)`); `_proxy_call_resolution_phase` forwards calls on those proxy vars through `PROXY_OF` to the real type's method, wiring `CALLS_DYNAMIC {via_proxy}`. See [spec_call_resolution_pipeline.md §5.1](spec_call_resolution_pipeline.md). Runs before degree so forwarded edges are counted.
4. **`degree` (materialized centrality)** — `_degree_phase` recomputes `Symbol.in_degree` / `Symbol.out_degree` so the ranker reads degree as a node property instead of a `count(DISTINCT)` subquery per query (see below).

Stats keys: `framework_hints_applied`, `ts_http_route_hints_applied`, `proxy_bindings`, `proxy_calls_resolved`, `degree_recomputed`. Re-index after changing any pass; `--no-index` benchmark runs assume the graph already contains these edges.

**Materialized degree (`in_degree` / `out_degree`).** Degree over the call/dep/ref/hint edge set is static topology, so it is computed once at index time and stored on each `Symbol`. The ranker's recovery queries read `coalesce(s.in_degree, 0)` instead of re-aggregating edges per query. The edge-type set counted is fixed in `Neo4jClient._DEGREE_REL_PATTERN` and **must** match what the ranker reads. To stay accurate under incremental `update`, degree is recomputed only over the **affected closure** (changed symbols ∪ their 1-hop neighbors, captured before mutation so a removed symbol's neighbor is still corrected), never globally. Newly created `ProxyBinding` nodes are folded into the closure seed set. Verified on click/flask/celery: 100% coverage, zero mismatch vs. a live recompute.

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
- Dynamic-surface flags are extension/path heuristics; they guide routing and diagnosis, not mechanism detection.
- The single-file hot path does not currently refresh the full repository profile or rerun Pass 1.
- Current impact capability is still shallow: AFFECTS reachability does not prove behavioral breakage.
- Residual ranker fallbacks: `mechanism_registry` preloaded packs, structural mechanism overlap, and `unified_ranker._infer_role` when Pass-1 roles are absent.

---

## Planned Extensions

- Skip unchanged files using `File.hash` comparison (ADR-001 §hash mismatch = dirty flag)
- Parallel file processing for large repos (`concurrent.futures.ThreadPoolExecutor`)
- Single-pass Phase 1+2 (extract symbols and calls in one tree-sitter parse)
