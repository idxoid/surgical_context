# Surgical Context - Project Analysis Index

> Status: merged into the canonical docs. This file now acts as a short index so the analysis does not drift away from the main documentation.

## Whole Meaning

The product meaning now lives in [concept.md](concept.md): Surgical Context is a context operating system for code assistants, centered on explicit, measurable, inspectable retrieval before an LLM answers.

## Current Gaps

The active gap list now lives in [road_map.md](road_map.md), under **Canonical Backlog**. The release target is the Local Developer Product; older stabilization items are preserved in the roadmap as completed history.

| Priority | Canonical home | Focus |
|---|---|---|
| P0 | [road_map.md](road_map.md) | Local setup, smoke tests, extension health, dashboard resilience, settings, and open-source local usage |
| P1 | [road_map.md](road_map.md) | SQLite history, conversations, prompt snapshots, feedback tokens, retention, and privacy gates |
| P2 | [road_map.md](road_map.md) | Soft fallback ladder, prompt-contract observability, doc-anchor confidence, unified ranking, and latency SLOs |
| P3 | [road_map.md](road_map.md) | Extension streaming, prompt selection sync, accessibility, command placement, and package polish |
| P4 | [road_map.md](road_map.md) | Provider boundaries around Neo4j, LanceDB, and SQLite defaults |
| P5 | [road_map.md](road_map.md) | Future Team/Enterprise horizon: roles, doc sources, tenant API graph, LLM proxy transport, service split, and profiled performance rewrites |

## Supporting Specs

- [retrieval_kernel.md](retrieval_kernel.md) - retrieval kernel target architecture, trace schema, providers, manifests.
- [spec_uid_stability.md](spec_uid_stability.md) - stable symbol identity.
- [spec_call_resolution_pipeline.md](spec_call_resolution_pipeline.md) - scoped call resolver.
- [spec_branch_isolation.md](spec_branch_isolation.md) - workspace and branch boundaries.
- spec_unified_ranking.md (removed) - graph plus semantic ranking.
- [spec_prompt_contract_observability.md](spec_prompt_contract_observability.md) - scores, provenance, pruning, and trace metadata.
- [spec_retrieval_cache.md](spec_retrieval_cache.md) - cache layers after correctness keys stabilize.
- [spec_learning_loop.md](spec_learning_loop.md) - feedback and learning loop after observability exists.

## Maintenance Rule

When implementation status changes, update [docs/README.md](README.md), [concept.md](concept.md), and [road_map.md](road_map.md) first. Use this file only as a navigation index.
