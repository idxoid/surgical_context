# Surgical Context — Idea Summary

> **Status:** Kept as a one-screen summary only.
>
> Canonical details live in [README.md](../README.md),
> [road_map.md](road_map.md), and [architectura.md](architectura.md).

## One Sentence

Surgical Context is a **local-first, model-agnostic context engine** for code
understanding and change impact.

## Why It Matters

- assemble minimum sufficient context under budget
- keep grounding/provenance visible to the user
- preserve stable behavior across model routes

## Product Shape (current)

- primary surfaces: `Ask`, `Inspect`, `Impact`
- retrieval ladder: `symbol -> file -> workspace -> direct_llm`
- local defaults: Neo4j + LanceDB + SQLite

## Canonical References

- [README.md](../README.md) — product thesis and documentation map
- [road_map.md](road_map.md) — strategy, scope boundaries, validation criteria
- [concept.md](concept.md) — compact concept identity
- [architectura.md](architectura.md) — implementation and architecture state
