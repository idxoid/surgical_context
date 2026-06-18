# Retrieval kernel (target architecture)

> **Superseded (2026-06-15).** Describes the legacy ranking cascade / `qa_benchmark` harness, removed in the cascade cleanup — axis (`context_engine/axis/`, `QA/axis_benchmark.py`) is the sole context + eval path now. Kept for historical context; see `cascade_cleanup_inventory.md`.


Surgical Context retrieval today spans `unified_ranker`, `ContextArbitrator`, graph expansion, LanceDB, and prompt assembly. This note fixes the **target boundary**: an explicit **retrieval kernel**, **provider protocols**, **mechanism overlays vs scoring**, and **index manifests** — without rewriting production behavior in one step.

Related: spec_unified_ranking (removed), [spec_prompt_contract_observability](spec_prompt_contract_observability.md), [project_gap_analysis](project_gap_analysis.md).

## Goals

1. **One mental model**: gather candidates → extract features → score → budget → assemble evidence → explain decisions (trace).
2. **Swappable storage**: Neo4j / LanceDB / SQLite remain **default adapters** behind protocols; unit tests use **in-memory fakes**.
3. **Clear extension**: new frameworks (Django, NestJS, …) ship as **mechanism overlays** (data), not edits to the scoring formula.
4. **Reproducibility**: every indexed workspace has an **index manifest**; retrieval responses can cite **trace schema version** + manifest id.

## Target package layout (`context_engine/retrieval/`)

| Component | Responsibility |
|-----------|----------------|
| **CandidateSource** | graph BFS pool, vector docs, vector symbols, doc anchors / bridge, fallback targets (module/concept). |
| **FeatureExtractor** | graph score, semantic score, role hints, mechanism context, noise — **signals**, not the final blend formula. |
| **Scorer** | Pure blend / normalization (α, β, γ, marginal gain inputs). |
| **BudgetPlanner** | Token budget policy (defer docs, rescue, caps). |
| **EvidenceAssembler** | Ranked candidates → subgraph + doc chunks → **prompt contract** (today: `SubgraphAssembler` + `PromptCompiler`). |
| **RetrievalTrace** | Structured log: strategy, mechanism, roles, budget, pruned summary, **schema version**. |

**UnifiedRanker** remains a **facade** over graph/vector/recovery/pruning; long-term it delegates to the kernel API instead of owning orchestration.

## Current code → target mapping

| Today | Maps to |
|-------|---------|
| `ranker/graph_candidate_source.py`, BFS in `unified_ranker` | CandidateSource (graph) |
| `ranker/vector_candidate_source.py`, `VectorSearcher` | CandidateSource (vector) |
| `DocResolver` + ranker doc bridge | CandidateSource (docs / anchors) |
| `TargetSelector`, arbitrator concept fallback | CandidateSource (target resolution) |
| `ranker/scoring.py`, noise helpers | FeatureExtractor + Scorer |
| `mechanism_registry`, `role_fulfilment` | Mechanism / role policy **outside** pure Scorer |
| `ranker/budget_selector.py`, `pruning.py` | BudgetPlanner |
| `subgraph_assembler`, `PromptCompiler` | EvidenceAssembler (two-stage is OK) |
| `ctx.ranker_state`, `pruned_details`, `budget` | RetrievalTrace (+ existing fields) |

## Provider protocols (priority)

Minimal set for contract tests:

| Protocol | Role |
|----------|------|
| **GraphProvider** | Symbol/file graph queries used by retrieval (workspace-scoped). |
| **VectorProvider** | Doc + symbol embedding search (single LanceDB client, two surfaces). |
| **WorkspaceProvider** | Repository profile, workspace id, graph version overlay hooks. |
| **CodeProvider** | Resolved file slices (`CodeResolver` + overlay + cache). |

Optional later: **HistoryProvider** (SQLite sessions). Defaults stay Neo4j + LanceDB + existing overlay.

## Mechanisms vs ranking

- **Ranking** = normalized signals + weights + budget (framework-agnostic).
- **Mechanisms** = which roles / recovery / hints apply — today split across `mechanism_registry`, strategy profile, and Pass-1 derived roles on symbols.

**Mechanism packs** (e.g. `roles.yaml`, `patterns.yaml`, `recovery.yaml`, `noise.yaml`) should act as **declarative overlays**. Canonical role taxonomy stays **index-backed**; YAML must not become a second source of truth for role assignment — only defaults and path/name heuristics.

## Index manifest / retrieval snapshot

**Manifest** (required at end of successful indexing):

- `workspace_id`
- repo path / name
- git branch / SHA (when available)
- parser / tree-sitter revisions used
- embedding model id + version
- graph schema / Neo4j migration version
- indexed file counts
- doc index / Lance table generation
- role taxonomy / catalog version string

**Snapshot per request** — optional; prefer manifest id + **RetrievalTrace** for debugging. Heavy per-request snapshots only in debug mode.

## Migration order (agreed)

1. **RetrievalTrace** in prompt contract + schema version (**done**).
2. Protocols + fake providers + narrow contract tests (**done**): `context_engine/retrieval/protocols.py`, `fakes.py`, `tests/unit/test_retrieval_protocols.py`. Production `VectorSearcher` satisfies `VectorSearchProvider`. **Optional DI on `ContextArbitrator`** (**done**): keyword-only `vector_search=` (`VectorSearchProvider`) and `workspace_meta=` (`WorkspaceMetaProvider`); defaults unchanged. **`Neo4jWorkspaceMetaAdapter`** in `context_engine/retrieval/adapters.py` implements workspace meta from Neo4j. HTTP sidecar wires both explicitly via `_context_arbitrator()` (`vector_search=VectorSearcher(vector_db)`, `workspace_meta=neo4j_workspace_meta(db)`).
3. **Stable retrieval package surface** (**done**): `context_engine/retrieval/` exposes trace, protocols, fakes, manifest, adapters, and `neo4j_workspace_meta`; ranker modules remain under `context_engine/context/ranker/` until a deliberate move. Sidecar HTTP (`context_engine/main.py`) installs `context_engine.silence` **before** importing `LanceDBClient` so HF/CUDA stderr noise matches indexer pipelines.
4. **Mandatory manifest** written by indexer; sidecar reads by `workspace_id` (**done**): `context_engine/retrieval/manifest.py` (`INDEX_MANIFEST_SCHEMA_VERSION`), persisted after `run_fast_indexing` to `{project}/.surgical_context/index_manifest.json` and `Workspace.index_manifest_json` in Neo4j; HTTP `GET /index/manifest` with `X-Workspace`.
5. **Mechanism packs** — declarative YAML overlays (**foundation done**): `context_engine/context/mechanism_packs/loader.py` loads only paths from **`MECHANISM_PACK_PATH`** (``os.pathsep``-separated); bundled templates (e.g. `bundled/flask_registration.yaml`) are **opt-in**. Merged into `preloaded_mechanism_catalog_extensions()` and persisted on the workspace `role_catalog_json` at index time. Role names are taxonomy strings from Pass-1 assignment, not a parallel cluster id space. **Ad-hoc repo tuning method:** when evaluation shows persistent `missing_roles` or cross-package gaps the generic recovery cannot close, add or extend a pack (symbol `name` + `path_hint` backfill per mechanism/role), re-index that repo with the env set, then re-run `--no-index` benchmarks — see [spec_eval_harness.md §4.6](spec_eval_harness.md).

## Implementation notes

- **Trace schema**: `context_engine.retrieval.trace.RETRIEVAL_TRACE_SCHEMA_VERSION` — bump when fields change; clients may rely on shape for dashboards and benchmarks.
- **Manifest schema**: `context_engine.retrieval.manifest.INDEX_MANIFEST_SCHEMA_VERSION` — bump when manifest fields change.
- **PromptContext**: `retrieval_trace` dict mirrors trace for JSON export (`to_dict`). **`metadata.index_manifest_id`** / **`index_manifest_schema_version`** when Neo4j holds an index manifest. HTTP: **`/ask`** and **`/ask/stream`** (SSE `context` event) include the same ids at top level; **`/search/unified`** returns `index_manifest_*` plus optional **`retrieval_trace`** when `include_graph` + `symbol` ran the arbitrator.
