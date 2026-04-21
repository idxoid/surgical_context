# Surgical Context - Project Analysis Index

> Status: merged into the canonical docs. This file now acts as a short index so the analysis does not drift away from the main documentation.

## Whole Meaning

The product meaning now lives in [concept.md](concept.md): Surgical Context is a context operating system for code assistants, centered on explicit, measurable, inspectable retrieval before an LLM answers.

## Current Gaps

The active gap list now lives in [road_map.md](road_map.md), under **Current Stabilization Backlog**:

| Priority | Canonical home | Focus |
|---|---|---|
| P0 | [road_map.md](road_map.md) | API coverage, sidecar safety checks, auth-boundary enforcement, and keeping docs synced to code |
| P1 | [road_map.md](road_map.md) | Stable UID, scoped call resolution, workspace/branch isolation, Git branch invalidation |
| P2 | [road_map.md](road_map.md) | Metrics, prompt-contract observability, extension context inspector, unified search |
| P3 | [road_map.md](road_map.md) | Retrieval cache, feedback loop, backpressure, embedding throttling, background indexing/rebuild queue |

## Supporting Specs

- [spec_uid_stability.md](spec_uid_stability.md) - stable symbol identity.
- [spec_call_resolution_pipeline.md](spec_call_resolution_pipeline.md) - scoped call resolver.
- [spec_branch_isolation.md](spec_branch_isolation.md) - workspace and branch boundaries.
- [spec_unified_ranking.md](spec_unified_ranking.md) - graph plus semantic ranking.
- [spec_prompt_contract_observability.md](spec_prompt_contract_observability.md) - scores, provenance, pruning, and trace metadata.
- [spec_retrieval_cache.md](spec_retrieval_cache.md) - cache layers after correctness keys stabilize.
- [spec_learning_loop.md](spec_learning_loop.md) - feedback and learning loop after observability exists.

## Maintenance Rule

When implementation status changes, update [docs/README.md](README.md), [concept.md](concept.md), and [road_map.md](road_map.md) first. Use this file only as a navigation index.
