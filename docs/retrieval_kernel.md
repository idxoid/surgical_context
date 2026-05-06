# Retrieval kernel (target architecture)

Surgical Context retrieval today spans `unified_ranker`, `ContextArbitrator`, graph expansion, LanceDB, and prompt assembly. This note fixes the **target boundary**: an explicit **retrieval kernel**, **provider protocols**, **mechanism overlays vs scoring**, and **index manifests** — without rewriting production behavior in one step.

Related: [spec_unified_ranking](spec_unified_ranking.md), [spec_prompt_contract_observability](spec_prompt_contract_observability.md), [project_gap_analysis](project_gap_analysis.md).

## Goals

1. **One mental model**: gather candidates → extract features → score → budget → assemble evidence → explain decisions (trace).
2. **Swappable storage**: Neo4j / LanceDB / SQLite remain **default adapters** behind protocols; unit tests use **in-memory fakes**.
3. **Clear extension**: new frameworks (Django, NestJS, …) ship as **mechanism overlays** (data), not edits to the scoring formula.
4. **Reproducibility**: every indexed workspace has an **index manifest**; retrieval responses can cite **trace schema version** + manifest id.

## Target package layout (`sidecar/retrieval/`)

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
- **Mechanisms** = which roles / recovery / hints apply — today split across `mechanism_registry`, strategy profile, indexed role clusters.

**Mechanism packs** (e.g. `roles.yaml`, `patterns.yaml`, `recovery.yaml`, `noise.yaml`) should act as **declarative overlays**. Canonical role taxonomy stays **index-backed**; YAML must not become a second source of truth for cluster IDs — only defaults and path/name heuristics.

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

1. **RetrievalTrace** in prompt contract + schema version (**implemented first**).
2. Protocols + fake providers + narrow contract tests on kernel-shaped code.
3. Physical package `sidecar/retrieval/` as imports move from `context/ranker` without behavior change.
4. Mandatory manifest written by indexer; sidecar reads by `workspace_id`.
5. Mechanism packs after trace + providers stabilize.

## Implementation notes

- **Trace schema**: `sidecar.retrieval.trace.RETRIEVAL_TRACE_SCHEMA_VERSION` — bump when fields change; clients may rely on shape for dashboards and benchmarks.
- **PromptContext**: `retrieval_trace` dict mirrors trace for JSON export (`to_dict`).
