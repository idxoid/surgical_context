# Surgical Context — Product Concept

> **Status:** Consolidated.
>
> This file is now a compact concept pointer. Canonical product strategy, scope,
> and validation live in `product_direction_memo.md`.

## Core Identity

Surgical Context is a **local-first, model-agnostic context engine** for code
understanding and change impact.

Short form:

**Less token waste. Less model lock-in. More explainable code understanding.**

## Canonical Sources

- `product_direction_memo.md` — product thesis, keep/freeze/drop scope, validation path
- `architectura.md` — current architecture and implementation status
- `spec_context_retrieval_layers.md` — retrieval-layer contracts (truth vs hints vs policy)

## Stable Product Contract

- Retrieval fallback ladder remains: `symbol -> file -> workspace -> direct_llm`
- Primary user surfaces remain: `Ask`, `Inspect`, `Impact`
- Prompt contract remains inspectable (provenance, pruning, routing, budget signals)
