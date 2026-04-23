# Surgical Context — Product Concept

> **Status:** Current product concept for the `context-engine-refocus` branch.
>
> **Short version:** Surgical Context is a local-first, model-agnostic context engine for code understanding and change impact.

---

## 1. Core Idea

Surgical Context should not be understood as "another AI coding assistant".

The core idea is narrower and stronger:

**assemble the minimum sufficient context for a developer question, route it to an appropriate model, and let the user inspect what the model actually saw.**

This changes the product center of gravity:

- not "chat" as the main value
- not "best model" as the main value
- not "full autonomous agent" as the main value

Instead, the main value is:

- lower token spend
- stable behavior across models
- explicit fallback behavior
- inspectable grounding
- stronger answers for understanding and impact questions

Short form:

**Less token waste. Less model lock-in. More explainable code understanding.**

---

## 2. Product Shape

The active target is the **Local Developer Product**.

It is a local-first, single-tenant VS Code tool with a Python sidecar and local storage defaults.

The user-facing loop is:

1. Index local code and repository docs.
2. Capture current editor state, including dirty overlays.
3. Ask a question from a symbol, file, or broad workspace context.
4. Assemble the smallest useful prompt context.
5. Route the request to a local or hosted model.
6. Return both the answer and the context contract.
7. Let the user inspect the supporting symbols, files, docs, route, and likely impact.

The product surface for v0.1 remains:

- `Ask`
- `Inspect`
- `Impact`
- one request history
- one model-routing and context-inspection story

This is the open-source candidate.

---

## 3. What the Product Is and Is Not

### It Is

- a local-first VS Code tool
- a model-agnostic context engine
- a code understanding and change-impact assistant
- a trust layer over retrieval and model routing

### It Is Not

- a direct Cursor / Copilot / Cline competitor on broad agent execution
- a graph visualization product for its own sake
- an enterprise platform in the current phase
- a "support every database and every deployment mode immediately" platform

---

## 4. Why This Exists

Most AI coding tools fail in one of four ways:

1. they send too much context and waste tokens
2. they send the wrong context and still sound confident
3. they depend too much on one model/provider
4. they make it hard to inspect why an answer was produced

Surgical Context exists to attack those problems directly.

The product thesis is:

**if retrieval is structured, budget-aware, and inspectable, then smaller and cheaper prompts can still produce useful answers, and model switching becomes much less dangerous.**

---

## 5. Technical Center of Gravity

The most important subsystem is retrieval correctness, not UI polish and not model cleverness.

If these are strong:

- symbol identity
- call resolution
- workspace scoping
- dirty overlay behavior
- doc anchoring
- prompt-contract observability

then model quality can vary without collapsing the product.

If these are weak, then better models only hide broken retrieval.

That is why the architectural center remains:

**context assembly under budget with explicit provenance.**

---

## 6. Retrieval Ladder

The canonical ask fallback path is:

`symbol -> file -> workspace -> direct_llm`

This ladder is part of the product identity, not only an implementation detail.

It lets the system degrade gracefully:

- use symbol context when precise graph-local grounding exists
- fall back to file context when symbol grounding is missing
- fall back to workspace retrieval for broad questions
- fall back to direct model knowledge only when nothing better exists

The user should be able to see which level was used and why.

---

## 7. Prompt Contract

The prompt contract is one of the strongest differentiators in the project.

It should remain stable across models and visible to the user.

The contract should explain:

- primary source
- graph context
- documentation context
- fallback level
- pruning reasons
- cache hits
- model route
- trace ID
- estimated token / cost signals

This is not just debugging metadata. It is part of the trust model.

---

## 8. Model-Agnostic Routing

The product should keep a stable context and UX layer even when different models answer different requests.

That means:

- one request history
- one ask/inspect/impact thread
- one context contract shape
- one routing story visible to the user

The value is not that many models are available.  
The value is that **switching models does not force a new mental model for the product**.

---

## 9. Storage Boundaries

Storage stays split by role.

### Graph Provider

Default: Neo4j.

Stores:
- files
- symbols
- code relationships
- doc anchors

Does not store raw code bodies.

### Vector Provider

Default: LanceDB.

Stores:
- doc embeddings
- optional symbol embeddings

### History Provider

Default: SQLite.

Stores:
- conversations
- messages
- ask snapshots
- inspector snapshots
- impact snapshots

### Local Filesystem

Source of truth for raw code and raw docs.

This boundary matters for both privacy and future provider swapping.

---

## 10. Why the UI Includes History, Inspector, and Impact

The project accumulated "scaffolding" because the core idea requires a richer UX than a stateless chat box.

These are not random extras:

- **History** keeps one conversation alive across multiple model routes
- **Inspector** explains what the assistant used
- **Impact** answers the practical developer question: "what might this change break?"

So the UI is justified when it supports trust and decision-making.

It becomes scope creep only when it starts competing with full general-purpose agent IDEs.

---

## 11. Immediate Product Strategy

The next phase should optimize for:

- local-first usability
- strong `Ask / Inspect / Impact`
- token and route visibility
- benchmarkable retrieval quality
- real repository validation

The next phase should not optimize for:

- enterprise deployment surface
- multi-tenant platform design
- mandatory proxy/gateway layers
- microservice splits
- speculative backend proliferation

---

## 12. Validation Plan

The concept is worth continuing only if it survives measurement.

The minimum proof should come from 2-3 real repositories and 20-30 real developer questions.

Compare:

1. naive local context
2. Surgical Context pipeline
3. heavy stuffing baseline

Measure:

- input token count
- latency
- rough cost estimate
- grounding quality
- quality on understanding questions
- quality on impact questions
- behavior on missing symbols / broad questions

Starter seed pack:
- [tests/fixtures/real_repo_question_pack.yaml](../tests/fixtures/real_repo_question_pack.yaml)

---

## 13. Near-Term Boundaries

### In Scope

- VS Code local product
- Python sidecar
- Neo4j + LanceDB + SQLite defaults
- ask/inspect/impact flows
- prompt-contract transparency
- local docs indexing
- request history and snapshots
- QA on real repositories

### Out of Scope for Now

- required SaaS
- cross-tenant graph traversal
- enterprise roles and policy surface
- general-purpose autonomous coding agent ambition
- microservice split
- parser rewrites before profiling

---

## 14. Decision

Surgical Context is worth continuing if it stays focused on this identity:

**a local-first, explainable context and routing layer for developer AI, expressed through Ask / Inspect / Impact.**

It is not worth continuing as a generic "AI coding platform" race.
