# Product Direction Memo

> **Status:** Active guidance for the current local-first product line.
>
> **Progress since the original memo:** the repo now has a working real-repo benchmark harness, unified ranking in the sidecar, prompt-contract observability, FastAPI core12 green locally, and early Pydantic core12 coverage. The remaining question is no longer "should we measure?" but "how far can the current context engine generalize across frameworks before we widen scope again?"
>
> **Audience:** maintainer, early contributors, and future design discussions.
>
> **Purpose:** define the real core of Surgical Context, separate it from secondary project scaffolding, and set a narrow validation path.

---

## Executive Summary

Surgical Context should **not** try to become a generic AI coding assistant that competes head-on with Copilot, Cursor, Cline, or Windsurf on agent breadth.

The stronger direction is:

**a local-first, model-agnostic context engine for code understanding and change impact that reduces token spend while preserving answer quality and explainability.**

The product surface for the open-source local release remains:
- `Ask`
- `Inspect`
- `Impact`
- one dialog with request history and snapshots
- explicit model route / context transparency

The main bet is not "best model" or "best autonomous agent". The main bet is:

**better context, less token waste, fewer model-specific assumptions, and more trust in what the assistant used to answer.**

---

## Core Idea

The real core is not "multi-model code chat".

The real core is:

**a context assembly, compression, and routing layer for developer questions that can feed different models with a stable prompt contract, predictable token budget, and explicit fallback behavior.**

That means the system should answer:
- how to gather the minimum sufficient context
- how to preserve useful signal under token pressure
- how to route the same request across local / cheap / expensive models
- how to keep the UX coherent even when different models are used over time

It should not primarily answer:
- how to imitate a full autonomous software agent
- how to become a general team platform before local usage is proven

---

## Product Thesis

If the thesis is valid, users should experience Surgical Context as:

- cheaper than naive prompt stuffing
- more stable across different models
- easier to trust because the context is inspectable
- better at "what does this do?" and "what could this change break?" than generic chat tools

Short form:

**Less token waste. Less model lock-in. More explainable code understanding.**

---

## Strategic Positioning

### What Surgical Context Is

- Local-first VS Code developer product
- Model-agnostic context engine
- Code understanding and change-impact assistant
- Explainable retrieval and routing layer

### What Surgical Context Is Not

- A direct Copilot / Cursor replacement on autonomous coding breadth
- A full enterprise platform in the current phase
- A graph visualization product for its own sake
- A database abstraction showcase

---

## Potential Moat

The durable value is not in a single component such as Neo4j, LanceDB, or the chat UI.

The moat, if it exists, comes from the combination below.

### 1. Stable Prompt Contract

Across different models, keep:
- the same context structure
- the same fallback ladder
- the same observability fields
- the same grounding metadata

This reduces model coupling and allows controlled routing decisions.

### 2. Token-Efficient Context Assembly

The retrieval ladder should remain explicit:

`symbol -> file -> workspace -> direct_llm`

The product value comes from retrieving the **minimum sufficient context**, not the maximum possible context.

### 3. Model-Agnostic Routing

Users should be able to switch between:
- local model
- cheaper hosted model
- higher-quality hosted model

without rewriting the whole application logic or breaking history continuity.

### 4. Inspectable Trust Layer

The user can see:
- what entered the context
- what was pruned
- which fallback was taken
- which model answered
- where the answer is grounded vs inferred
- what the rough token / cost impact was

That is a stronger trust story than "chat answered something plausible".

---

## Why the Project Drifted Into "Scaffolding"

The project naturally accumulated infrastructure because the core idea creates UX requirements:

- one dialog should persist even if different models answered different turns
- the user needs request-level history and snapshots
- Inspector and Impact need to attach to a selected ask, not to the current cursor alone
- model routing and context selection must be visible to be trusted

So the surrounding UI/history layer is not entirely accidental. It became necessary because the product is not just "send prompt to one model".

That said, some of the surrounding work is **supporting structure**, while some of it is **scope drift**. The next phase should separate those clearly.

---

## Keep, Freeze, Drop

### Keep Now

These support the real product thesis and should stay in active scope:

| Area | Why it stays |
| --- | --- |
| Ask / Inspect / Impact | This is the clearest user-facing expression of the product |
| One dialog with request history | Keeps continuity across model switches and request snapshots |
| Prompt/context snapshots | Required for trust, debugging, and feedback |
| Model route visibility | Necessary to make routing understandable |
| Token / cost / cache observability | Central to the efficiency claim |
| Local-first sidecar + local storage defaults | Core to privacy and cost control |
| Fallback ladder | Core retrieval behavior, especially for partial or missing symbol context |

### Freeze For Now

These are valid ideas, but should not drive the next release:

| Area | Why it is frozen |
| --- | --- |
| Tenant API graph across projects | Interesting future horizon, not needed for local product proof |
| LLM proxy gateway | Valuable later, but not required to validate the core local product |
| Provider swapping beyond clear boundaries | Good architecture, but not a release driver |
| Admin / user roles | Team feature without proven local demand yet |
| Microservice split | Premature before performance and adoption pressure |
| Parallel indexing optimization | Should follow profiling, not precede it |

### Drop From Near-Term Scope

These should stop influencing short-term decisions:

| Area | Why it is out |
| --- | --- |
| Competing on general autonomous coding agents | Crowded market, weak differentiation for this project |
| Visual graph product as a destination | Graph is an internal engine, not the main promise |
| Enterprise-first deployment complexity | Slows validation before product-market signal exists |
| Broad "support every backend" ambitions | Expands scope without proving user value |

---

## Product Risks

### Risk 1: Great Technology, Unclear Product

Users do not ask for "token-efficient context orchestration". They ask:
- why is this assistant expensive?
- why did it lose context?
- why did changing the model break behavior?
- what will I break if I edit this?

The product must present the engine through these user outcomes.

### Risk 2: Two Half-Products

If chat is weaker than major coding assistants, and impact is weaker than dedicated navigation tools, users may not understand why Surgical Context exists.

The answer is to **focus the product around trusted understanding before change**, not broad coding assistance.

### Risk 3: Scope Explosion

History, routing, storage, connectors, and UI all look justified. Without a narrow center, they become endless platform work.

The center must stay:

**minimal sufficient context + model-agnostic routing + explainable impact/inspection**

---

## Who This Product Is For

Best-fit early users:
- developers working in medium or large repos
- privacy-sensitive individuals or teams
- users who bring their own model/provider keys
- developers who care about blast radius before changing code
- users frustrated by black-box retrieval and token waste

Weak-fit users:
- developers who only want autocomplete
- users who mainly want a fully autonomous agent to edit the repo
- teams that only buy fully managed enterprise platforms

---

## Minimal Validation Experiment

The project should prove the core thesis with a small benchmark instead of more platform expansion.

### Current Snapshot

This validation path is now in progress, not hypothetical:

- the benchmark harness runs against the committed real-repo pack in `tests/fixtures/real_repo_question_pack.yaml`
- FastAPI `core12` has already been used to tune duplicate-target selection, role backfill, and prompt-contract observability
- Pydantic `core12` is now evaluated on the same canonical role scale, which exposed a narrower remaining gap: reliable recovery of validator/serializer handles rather than a generic ranking failure

### Benchmark Design

Use 2-3 real repositories and 20-30 real developer questions.

Compare three modes:

1. **Naive baseline**
   - question + active file + simple nearby text

2. **Surgical Context pipeline**
   - structured retrieval
   - explicit fallback ladder
   - prompt contract
   - pruning / token budgeting

3. **Heavy stuffing baseline**
   - larger semantic / file stuffing without careful structure

Starter seed pack for this benchmark:
- [../tests/fixtures/real_repo_question_pack.yaml](../tests/fixtures/real_repo_question_pack.yaml)

### Measurements

Record:
- input tokens
- output tokens
- latency
- rough cost estimate
- answer quality
- grounding quality
- success on broad questions
- success on missing-symbol questions
- success on impact questions

### Success Signal

The idea is likely valid if the pipeline can show something close to:
- 30-70% fewer input tokens than heavy stuffing
- no meaningful answer-quality drop on understanding questions
- better performance on inspect / impact-style questions
- stable behavior when switching between at least two model routes

---

## Three-Week Refocus Plan

### Week 1: Finish the Local Truthful Loop

- complete remaining prompt-contract observability fields
- make token / route / cache / fallback visibility first-class in the UI
- ensure Ask / Inspect / Impact stay synchronized per selected request
- keep the local setup and smoke path stable

### Week 2: Measure Instead of Expanding

- extend the existing benchmark question set only where it improves framework coverage
- run naive vs surgical vs heavy-context comparisons
- capture token, latency, and quality deltas
- document examples where the structured retrieval clearly helps or fails

### Week 3: Tighten Positioning

- revise `concept.md` / landing language around the context-engine thesis
- reduce language that implies "general autonomous coding assistant"
- decide whether the next step is:
  - local open-source developer tool, or
  - extraction of the context engine as a reusable backend / MCP layer

---

## Go / No-Go Criteria

Continue investing if the following becomes true:

- real repos show measurable token savings
- answer quality remains competitive for understanding tasks
- users meaningfully use Inspect / Impact, not just free-form chat
- multi-model routing feels like a benefit rather than complexity
- the trust layer is visible enough that users notice the difference

Pause or rethink if the following becomes true:

- token savings are small or not explainable
- users only care about generic chat and ignore impact/inspection
- routing between models creates confusion without visible benefit
- the product keeps expanding into infrastructure without clearer user pull

---

## Decision

**Worth continuing:** yes, if the project stays centered on local-first, token-efficient, model-agnostic code understanding and change impact.

**Not worth continuing in current form:** if it drifts back toward a broad "AI coding platform" race.

The next fork should treat Surgical Context as:

**an explainable context and routing layer for developer AI, expressed through Ask / Inspect / Impact in a local VS Code product.**
