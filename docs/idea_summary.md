# Surgical Context — Idea Summary

**Mission:** provide the smallest useful context for developer questions, keep it inspectable, and make model switching cheaper and safer.

---

## The One-Sentence Pitch

Surgical Context is a **local-first, model-agnostic context engine** for code understanding and change impact.

It helps answer:
- what this code does
- what supports that answer
- what might break if it changes

without blindly stuffing tokens into a model.

---

## Why It Exists

Most AI coding tools have the same weak point: context.

They often:
- send too much
- send the wrong things
- hide what they sent
- tie the whole experience to one model/provider

Surgical Context focuses on the layer before generation:

- index code and docs
- resolve symbol/file/workspace context
- keep a stable prompt contract
- route requests across models
- show the user what happened

---

## Product Shape

The active release target is a **Local Developer Product** in VS Code.

Core surfaces:
- `Ask`
- `Inspect`
- `Impact`
- request history with snapshots

Core properties:
- local-first
- single-tenant by default
- Neo4j + LanceDB + SQLite defaults
- dirty overlay support
- explicit fallback ladder
- inspectable model route and context contract

---

## Main Value

This product is not trying to win as the broadest coding agent.

Its value is:
- lower token waste
- better retrieval discipline
- more stable behavior across models
- better trust through observability
- stronger support for code understanding before change

Short form:

**Less token waste. Less model lock-in. More explainable code understanding.**

---

## The Retrieval Ladder

The canonical fallback path is:

`symbol -> file -> workspace -> direct_llm`

This matters because developer questions are not all equally precise.

The system should:
- use symbol context when it can
- fall back to file context when needed
- use workspace search for broader questions
- only rely on direct model knowledge when nothing better is available

And the user should be able to see which path was taken.

---

## Why the UI Matters

The surrounding UI is not just decoration.

- **History** keeps one coherent thread across different model routes
- **Inspector** explains the grounding
- **Impact** makes the product useful for real change decisions

The UI is justified when it supports trust and decision-making.

---

## What It Is Not

Surgical Context is not currently trying to be:
- a generic autonomous coding agent
- a graph visualization product for its own sake
- an enterprise platform first
- a "support every backend" platform before proving the local product

---

## Near-Term Proof

The concept should be validated on **2-3 real repositories** and **20-30 real developer questions**.

Starter pack:
- [tests/fixtures/real_repo_question_pack.yaml](../tests/fixtures/real_repo_question_pack.yaml)

The benchmark should compare:
1. naive local context
2. Surgical Context pipeline
3. heavy stuffing baseline

And measure:
- tokens
- latency
- fallback behavior
- grounding quality
- usefulness on understanding and impact questions

---

## Current Direction

Worth continuing if the product stays centered on:

**a local-first, explainable context and routing layer for developer AI.**

Not worth continuing if it drifts back into:

**a broad "AI coding platform" race.**
