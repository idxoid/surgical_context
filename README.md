# Surgical Context Documentation

This folder contains the current product and technical documentation for the `context-engine-refocus` branch.

---

## Quick Navigation

### **For Understanding the System**
- **[architectura.md](docs/architectura.md)** — how all pieces fit together (start here)
- **[product_direction_memo.md](docs/product_direction_memo.md)** — canonical product thesis, scope boundaries, and validation plan
- **[concept.md](docs/concept.md)** — compact product identity (pointer document)
- **[idea_summary.md](docs/idea_summary.md)** — one-screen pitch summary (pointer document)
- **[project_gap_analysis.md](docs/project_gap_analysis.md)** — short index to the merged project analysis

### **For Implementation**

**Local Development:**
- **[local_development.md](docs/local_development.md)** — local setup, bootstrap, sidecar, and extension dev host

**Code Indexing:**
- **[spec_indexer.md](docs/spec_indexer.md)** — code extraction, call typing, AFFECTS rebuild
- **[spec_parser.md](docs/spec_parser.md)** — language adapters, symbol extraction

**Doc Indexing:**
- **[spec_doc_indexer.md](docs/spec_doc_indexer.md)** — chunking, embedding, DocAnchor linking
- **[spec_doc_anchor.md](docs/spec_doc_anchor.md)** — FROM/COVERS relationships

**Context Assembly:**
- **[spec_arbitrator.md](docs/spec_arbitrator.md)** — current orchestrator contract: unified ranking path, graph-only fallback, and prompt-contract assembly
- **[spec_intent_classifier.md](docs/spec_intent_classifier.md)** — query intent → content tier ranking (Phase 6 design)
- **[spec_token_budget_bfs.md](docs/spec_token_budget_bfs.md)** — BFS with token constraints
- **[spec_context_deduplicator.md](docs/spec_context_deduplicator.md)** — remove redundant symbols
- **[spec_unified_ranking.md](docs/spec_unified_ranking.md)** — current graph + semantic ranker, role backfill, and score blending

**APIs & Infrastructure:**
- **[spec_sidecar_api.md](docs/spec_sidecar_api.md)** — FastAPI endpoints
- **[spec_storage.md](docs/spec_storage.md)** — current Neo4j/LanceDB/SQLite storage behavior
- **[spec_storage_connectors.md](docs/spec_storage_connectors.md)** — planned Graph/Vector/History provider connector layer
- **[spec_language_adapter.md](docs/spec_language_adapter.md)** — plugin architecture (ADR-005)
- **[spec_overlay.md](docs/spec_overlay.md)** — in-memory dirty state
- **[spec_tenant_api_graph.md](docs/spec_tenant_api_graph.md)** — future Team/Enterprise tenant-level API contract graph

**Advanced Topics:**
- **[spec_eval_harness.md](docs/spec_eval_harness.md)** — real-repo benchmark harness, token metrics, and report schema
- **[benchmark_all_repos_context_comparison.md](docs/benchmark_all_repos_context_comparison.md)** — index to all-repo benchmark snapshots
- **[benchmark_mechanism_coverage.md](docs/benchmark_mechanism_coverage.md)** — harness metrics and mechanism-coverage analysis
- **[benchmark_path1_vs_path2.md](docs/benchmark_path1_vs_path2.md)** — LLM Judgment: Surgical Context vs first-time repo read
- **[spec_embedding_versioning.md](docs/spec_embedding_versioning.md)** — managing embedding model versions
- **[spec_affects_index.md](docs/spec_affects_index.md)** — reverse dependency index

### **Planning & Review**
- **[road_map.md](docs/road_map.md)** — phases and timelines
- **[product_direction_memo.md](docs/product_direction_memo.md)** — narrow product direction before the next fork
- **[project_gap_analysis.md](docs/project_gap_analysis.md)** — index for current gaps and supporting specs
- **[review_findings_2026-04-17.md](docs/review_findings_2026-04-17.md)** — external review recommendations
- **[architectural_review.md](docs/architectural_review.md)** — archived historical review (canonical architecture now in `architectura.md`)

---

## Current Truth

The active target is the **Local Developer Product**: a local-first, single-tenant VS Code tool with the Python FastAPI sidecar, local Neo4j graph, local LanceDB vectors, local SQLite history, and an `Ask / Inspect / Impact` workflow. This is the open-source candidate.

The product is now described more narrowly than before:

- **not** a general AI coding platform
- **yes** a local-first, model-agnostic context engine for code understanding and change impact

The repo currently includes the sidecar, default Neo4j/LanceDB clients, parser/indexer/context modules, a workspace-scoped unified ranker, canonical role taxonomy normalization, tests, QA benchmark tooling, metrics, feedback telemetry, durable indexing jobs, bounded indexing queue, and a VS Code extension under `extension/`.

Recent hardening added request-scoped Neo4j sessions, doc retrieval inside the arbitration pipeline, typed API responses, JSON-safe SSE framing, stable UID v2, scoped call resolution, workspace-scoped graph queries, Git branch-change invalidation helpers, unified search, retrieval caching, feedback tokens, endpoint coverage for the sidecar API, prompt-contract observability fields (`scores`, `provenance`, `pruned`, `ranker` metadata), real-repo benchmark reports with `precision` plus full `ready_context`, topic-aware impact-test filtering, package/module fallback targets for workspace-level questions such as `pydantic.v1`, trace-dependency recovery hardening for sparse import topology (runtime symbol seeding + sibling-directory expansion with explicit recovery provenance), TypeScript `object_api` indexing with cross-language `SEMANTIC_HINT` HTTP route hints (`ts_http_route_hints`), a relaxed `trace_dependency` benchmark gate for near-perfect single-axis recall, and **workspace path sandboxing** (caller-supplied paths must stay under the indexed `project_path`; see [spec_sidecar_api.md](docs/spec_sidecar_api.md#filesystem-path-sandboxing)).

The local setup and smoke-test path live in **[local_development.md](docs/local_development.md)** and `scripts/local_dev.py`. The most important open gaps are broader real-repo benchmark coverage beyond the current FastAPI, Redux Toolkit, and Pydantic baselines, continued precision work on broad/doc-heavy retrieval paths, doc-anchor confidence/type scoring, consistent workspace/branch metadata in the prompt contract, and extension synchronization/accessibility polish. Team/Enterprise ideas such as tenant API graph, alternate database connectors, LLM proxy gateway, RBAC, and microservice splitting stay as future horizons. See **[road_map.md](docs/road_map.md)** for the canonical backlog in this branch.

---

## Writing Documentation

Before writing or updating docs, read **[DOCS_STYLE_GUIDE.md](docs/DOCS_STYLE_GUIDE.md)**.

Quick rules:
- **Be clear** — explain in 2 minutes or less
- **Show examples** — copy-paste-able code
- **Use tables** for data structures
- **Link everything** — internal links to related specs
- **List limitations** — what doesn't work today
- **Add trade-offs** — why this design over alternatives

---

## Document Status Legend

- ✅ **Implemented** — merged in this branch and covered by tests/benchmark
- 🚧 **In Progress** — active development
- 📋 **Planned** — on roadmap, not started
- ⚠️  **Known Issue** — works but with caveats
- ❌ **Not Implemented** — deferred
- 🔄 **Refactoring** — redesign underway (new path may coexist temporarily)

Use these markers directly inside specs for major sections and algorithm blocks.

---

## How Docs Stay in Sync

Every pull request should include doc updates:
- **New feature?** Add a Spec or update the Concept
- **Design change?** Update architectura.md and affected Specs
- **Bug fix?** Add to Limitations if it's a known constraint
- **Phase complete?** Update road_map.md

Stale docs are worse than no docs — when in doubt, ask in code review.

---

## Questions?

- About architecture → ask in [architectura.md](docs/architectura.md)
- About a component → find its `spec_*.md`
- About how to contribute → read [DOCS_STYLE_GUIDE.md](docs/DOCS_STYLE_GUIDE.md)
