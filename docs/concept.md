# Surgical Context — Product Concept

> **Status:** Consolidated.
>
> This file is now a compact concept pointer. Canonical product strategy and
> backlog live in [road_map.md](road_map.md); implementation truth lives in
> [architectura.md](architectura.md).

## Core Identity

Surgical Context is a **local-first, model-agnostic context engine** for code
understanding and change impact.

Short form:

**Less token waste. Less model lock-in. More explainable code understanding.**

## Canonical Sources

- [README.md](../README.md) — product thesis and documentation map
- [road_map.md](road_map.md) — scope boundaries, backlog, and validation path
- [architectura.md](architectura.md) — current architecture and implementation status
- [axis_terminology.md](axis_terminology.md) — axis retrieval vocabulary

## Stable Product Contract

- Retrieval fallback ladder remains: `symbol -> file -> workspace -> direct_llm`
- Primary user surfaces remain: `Ask`, `Inspect`, `Impact`
- Prompt contract remains inspectable (provenance, pruning, routing, budget signals)
