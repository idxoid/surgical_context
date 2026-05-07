# Surgical Context — Idea Summary

> **Status:** Kept as a one-screen summary only.
>
> Canonical details moved to `product_direction_memo.md` to avoid drift.

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

- `product_direction_memo.md` — strategy, scope boundaries, validation criteria
- `concept.md` — compact concept identity
- `architectura.md` — implementation and architecture state
