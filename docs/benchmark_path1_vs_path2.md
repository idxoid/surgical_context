# Benchmark: ContextArbitrator vs Direct File Read (LLM Judgment)

> **Historical snapshot.** This document predates the cascade removal (2026-06-15): the legacy ranking cascade it references is gone — axis is the sole context path (see `cascade_cleanup_inventory.md`). Kept as a dated record; the findings/benchmarks below are as-of their date.


Evaluation of context quality and answer quality across 65 real-repo questions comparing two retrieval strategies.

| | |
|---|---|
| **Evaluation type** | **LLM Judgment** — qualitative scoring by a judge model, not automated recall/precision from `QA/qa_benchmark.py` |
| **Judge** | Claude Sonnet 4.6 (max effort) |
| **Date** | 2026-05-26 |
| **Branch** | `context-engine-refocus` |
| **Questions** | `QA/repos/real_repo_question_pack.yaml` — 65 questions, 11 repos, 3 intents |

### How the judgment was run

1. **Path 1 (P1)** — The judge takes each question from the pack, runs it through the Surgical Context pipeline (`ContextArbitrator` → `to_system_prompt()`), receives the assembled context + prompt, and answers from that context only.
2. **Path 2 (P2)** — The same questions, but the judge **does not use pre-trained repo knowledge**; it behaves as if seeing the repository for the first time. It gathers context (grep, targeted file reads), then answers. Token usage is split into **preparation** (retrieval, reads, reasoning) vs **question–answer** (prompt excerpt + response).
3. **Comparison** — For each question, the judge compares **context quality** (P1 vs P2) and **answer quality** (P1 vs P2) on the scales defined in §1.4.

Important limitation: "no pre-trained repo knowledge" is a prompt-level rule,
not a technical sandbox. The harness does not erase model weights or prove that
the judge never used general framework knowledge. Treat these results as
LLM-judged context sufficiency under instruction, not as a formal proof that the
answer was derivable only from the supplied snippets.

---

## 1. Methodology

### 1.1 Two paths

**Path 1 (P1) — Surgical Context pipeline**

```python
ctx = arb.get_context_for_symbol(symbol, question=question, token_budget=4000)
prompt = ctx.to_system_prompt()
```

(`token_budget` is clamped to **400–32 000** when invoked via HTTP `/ask`; the harness calls the arbitrator directly with `4000`.)

The judge answers using only this prompt. Full pipeline: intent classification → `UnifiedRanker.get_target()` → BFS graph candidates + vector search → `BudgetPruner.select_under_budget()` → `CodeResolver.resolve()` per node → `PromptCompiler.compile_with_intent()`. Context includes: target symbol, N-hop neighbors, callers, callees, doc bridge results, role annotations.

**Path 2 (P2) — First-time repo read (no pre-trained repo skills)**

For each question the judge **pretends the repo is unseen** — no reliance on weights/memorized APIs. Context is built at judgment time:

1. `grep` the repo for the symbol definition (`def X` / `class X` / `export const X`)
2. Read ~100 lines from the definition site
3. Read 1–2 immediately referenced files (based on grep of called names)
4. Answer using only what was read

No graph traversal. No vector search. No budget management. Token accounting separates **preparation** (grep, reads, relevance reasoning) from **question–answer** (excerpt prompt + final response). See §2.2.

### 1.2 Repos covered

| Repo | Language | Questions |
|---|---|---|
| fastapi | Python | 8 (incl. 1 absent symbol) |
| pydantic | Python | 8 (incl. 1 absent symbol) |
| redux_toolkit | TypeScript | 8 (incl. 1 absent symbol) |
| django | Python | 5 |
| flask | Python | 5 |
| express | JavaScript | 4 |
| nestjs | TypeScript | 4 |
| sqlalchemy | Python | 4 |
| vue | TypeScript | 4 |
| surgical_context | Python/TypeScript | 7 |
| dathund | Python | 8 |

### 1.3 Intents covered

- `explain_behavior` — how does X work
- `trace_dependency` — how does X call Y call Z
- `impact_analysis` — if X changes, what breaks

### 1.4 Quality scoring

Each question rated on two dimensions:

**Context quality** — depth and completeness of information provided to the answerer:
- `P1_better` — P1 provides information that P2 does not (call chain, callers, test refs)
- `equal` — both contexts contain the information needed to answer correctly
- `P2_better` — P2 provides cleaner/more targeted context

**Answer quality** — correctness and completeness of the actual answer:
- `P1_better` — P1 answer is more complete or more correct
- `equal` — both answers are correct to the same degree
- `P2_better` — P2 answer is more complete or more correct

---

## 2. Results

### 2.1 Summary

| Dimension | P1 better | Equal | P2 better |
|---|---|---|---|
| Context quality | **45 / 65 (69%)** | 20 / 65 (31%) | 0 / 65 (0%) |
| Answer quality | **5 / 65 (8%)** | 60 / 65 (92%) | 0 / 65 (0%) |

### 2.2 Token usage

| Token type | What it is | Total (65 q) | Avg per question |
|---|---|---|---|
| **P1 — prompt** | `to_system_prompt()` output fed to the answering model | 185,605 | 2,855 |
| **P2 — prompt** | Code excerpts read from files, fed to the answering model | 24,244 | 372 |
| **P2 — agent context** | Tokens consumed by the agent doing the retrieval: grep results, file reads, reasoning about which files matter, forming the context block | ~507,000 | ~7,800 |

**Breakdown of P2 agent context per question (estimate):**

| Component | Tokens |
|---|---|
| grep searches + results | ~100 |
| File reads (~2 files × ~300 lines) | ~1,300 |
| Reasoning (relevance judgment, decide next file) | ~350 |
| Answer generation | ~200 |
| Session overhead (system prompt, accumulated history) | ~5,850 |
| **Total** | **~7,800** |

**Key ratio:**

| Comparison | Ratio |
|---|---|
| P1 prompt vs P2 prompt | P1 is **7.7× larger** |
| P1 prompt vs P2 agent context | P2 agent is **2.7× more expensive** |
| Full cost P1 vs full cost P2 | **P2 costs ~2.7× more** — the savings on prompt tokens are more than offset by LLM-driven retrieval |

P1 transfers all retrieval decisions (which files matter, how many tokens to take, caller vs callee priority) into a deterministic algorithm that runs without LLM tokens. P2 pays an LLM to do the same work on every question.

### 2.3 Questions where P1 answer is decisively better

| ID | Symbol | Question | Why P1 wins |
|---|---|---|---|
| fastapi_q02 | `Depends` | How does DI get resolved before endpoint call? | P2 only found `Depends` dataclass. `solve_dependencies()` is in a different file — P2 never read it. P1 includes it via BFS. |
| fastapi_q04 | `request_body_to_args` | How are request body models validated? | Function is 500 lines. P2 captured only the signature. P1 includes the full validated-fields logic. |
| fastapi_q06 | `serialize_response` | If serialization changes, what breaks? | P1 graph shows callers + test file references. P2 only has the function definition, no callers. |
| pydantic_q06 | `Field` | If alias handling changes, what is affected? | P1 graph traverses to `test_edge_cases.py`, `test_json_schema.py`. P2 only has `Field()` signature. |
| flask_q01 | `Flask` | How does request context management work? | `RequestContext` and `LocalStack` are in separate files. P1 includes both. P2 found `Flask.__init__` but not the context stack internals. |

### 2.4 Where contexts are equal (P1 has more but answer is same)

The majority case (60 questions): P1 provides more code — neighbors, callers, dependency chain — but the answer to the question is already determinable from the symbol definition alone.

Examples:
- `flask_q04` (`before_request`): both paths correctly identify `before_request_funcs[None]` list + short-circuit behavior
- `vue_q02` (`Ref`): both correctly identify `RefImpl`, `trackRefValue`, `triggerRefValue` from `ref.ts`
- `dathund_q01` (`ChainEngine`): both correctly identify the three post-match gates from `chain_engine.py`
- `nestjs_q02` (`Injectable`): both correctly identify `Reflect.defineMetadata(INJECTABLE_WATERMARK)` from the decorator source

### 2.5 Results by intent

| Intent | Questions | P1 answer better | Equal |
|---|---|---|---|
| `explain_behavior` | 32 | 1 (3%) | 31 (97%) |
| `trace_dependency` | 23 | 1 (4%) | 22 (96%) |
| `impact_analysis` | 10 | 3 (30%) | 7 (70%) |

Impact analysis questions benefit most from the graph. Callers, test files, and downstream consumers are not discoverable from a single-file read.

### 2.6 Absent symbols

Three questions used intentionally absent symbols to test robustness:

| ID | Symbol | P1 | P2 |
|---|---|---|---|
| fastapi_q08 | `RouteContext` | Not found in graph | Not found in repo |
| pydantic_q08 | `SchemaRouter` | Not found in graph | Not found in repo |
| rtk_q08 | `SliceRouter` | Not found in graph | Not found in repo |

Both paths handle gracefully.

---

## 3. Interpretation

### What the graph adds

The call graph provides two things that file-reads cannot:

1. **Reverse edges (callers).** `grep` finds definition sites but not call sites. For impact analysis questions ("what breaks if X changes"), callers are the primary answer. P1 graph has `CALLS` edges in both directions.

2. **Multi-hop chains.** When the question traces X→Y→Z, P2 stops at the first file. P1 BFS expansion follows the chain to the correct depth. `fastapi_q02` is the clearest example: `Depends` is a dataclass, `solve_dependencies()` is the actual resolution engine — two hops away.

### What the graph does not add

For `explain_behavior` questions answered by a single well-named function, the symbol definition is sufficient. P1 delivers more tokens but the marginal context does not change the answer.

### Token cost vs benefit

P1 uses 7.7× more tokens on average. The benefit is concentrated:
- Impact analysis: P1 provides decisive value (~30% of questions)
- Deep trace questions: P1 provides decisive value (~4% of questions)
- Single-function explain: P1 and P2 equivalent (~92% of questions)

A budget-aware strategy that routes `explain_behavior` questions to a cheaper retrieval tier and `impact_analysis` to full P1 would recover most of the token cost without losing answer quality.

---

## 4. Future baseline: Cursor-like indexed agent

This benchmark intentionally compares Surgical Context against a first-time
repo-reading path, not against an external IDE agent running inside a developer
environment with its own workspace index. That is a useful baseline for
local/user repositories where the model cannot rely on pre-trained repository
knowledge, but it is not the strongest competing workflow for an open-source
local context engine.

A stronger future baseline is a Cursor-like indexed agent:

1. Use a real repository plus an **unknown local branch delta**, not a fully
   generated toy repository. The branch should add realistic code paths such as
   a route, worker flow, serializer branch, auth rule, config path, or tests.
   This keeps the repository natural while preventing public-pretraining leakage
   on the changed behavior.
2. Freeze the question pack after the delta is written and before any retrieval
   run. For each question, freeze expected files, symbols, mechanisms, and
   evidence roles. Do not update labels after seeing Cursor or sidecar output.
3. Run Cursor as a black-box developer-environment baseline: new chat per
   question, web disabled, no code edits, and an instruction to answer only from
   workspace inspection while citing files/symbols used.
4. Export Cursor agent transcripts from
   `~/.cursor/projects/<slug>/agent-transcripts/` and keep the transcript-derived
   artifact with the benchmark run: files inspected, visible tool/context tokens,
   answer tokens, cited evidence, hallucinated files/symbols, wall time, and
   repeatability across 2-3 runs for hard questions.
5. Treat hidden index context as an explicit limitation. Cursor will not expose
   every internal retrieval token, so the comparison should report observable
   transcript footprint separately from answer quality and grounding quality.

This is not a replacement for the current P1 vs P2 test. It is a higher-danger
external baseline for the context-engine thesis: if Surgical Context can beat
or match a Cursor-like indexed agent on reproducibility, token discovery, and
grounding visibility, the result is much stronger than beating naive vector
stuffing or a shallow direct file-read baseline.

Related note: click/celery-style holdout packs are useful as blind mechanism
tests, but they still need the same LLM-judgment pass before being used as
answer-quality evidence.

---

## 5. Blind holdout judge burn

The Click/Celery blind holdout is useful precisely because retrieval pass rate
and answer sufficiency diverged. It should also be reported with its evaluation
cost, otherwise the result looks cheaper than it is.

Judge protocol limitation: the six judge models were asked to answer and score
from the supplied sidecar context, but they were not otherwise sandboxed. The
only explicit anti-leak constraint was the instruction to avoid pre-trained repo
knowledge. There was no ablation proof, no capped 4k/8k/16k frontier sweep, and
no technical way to remove general Click/Celery knowledge from model weights.
So the matrix is strong evidence that the context is useful and inspectable,
but weaker evidence that each passing answer was forced by the context alone.

Local run artifacts (not durable; copy into a repo artifact directory before
using them in docs, releases, or public writeups):

- `/tmp/qa_judge_runs/click_judge.json`
- `/tmp/qa_judge_runs/celery_judge.json`
- `/tmp/qa_judge_runs/judge_matrix.tsv`

Latest local run:

| Slice | Retrieval pass | Sidecar prompt tokens | Carpet tokens | Judge calls | Judge tokens | Judge / sidecar |
|---|---:|---:|---:|---:|---:|---:|
| Click | 5/5 | 15,182 | 274,969 | 30 | 112,077 | 7.4x |
| Celery | 5/5 | 21,062 | 576,864 | 30 | 154,850 | 7.4x |
| **Total** | **10/10** | **36,244** | **851,833** | **60** | **266,927** | **7.4x** |

Judge outcome from the same matrix:

| Slice | Pass | Warn | Fail | Context sufficient |
|---|---:|---:|---:|---:|
| Click | 22 | 6 | 2 | 21/30 |
| Celery | 12 | 15 | 3 | 10/30 |
| **Total** | **34** | **21** | **5** | **31/60** |

Full judge matrix (one row per question/provider/tier; raw TSV includes notes):

| question_id | provider | tier | model | verdict | correctness | grounding | completeness | ctx_ok | missing_evidence |
|---|---|---|---|---|---|---|---|---|---|
| click_q01 | claude | low | claude-haiku-4-5-20251001 | pass | correct | grounded | complete | yes | none |
| click_q01 | codex | low | gpt-5.4-mini | pass | correct | grounded | complete | yes | none |
| click_q01 | claude | medium | claude-sonnet-4-6 | pass | correct | grounded | complete | yes | none |
| click_q01 | codex | medium | gpt-5.4 | pass | correct | grounded | complete | yes | none |
| click_q01 | claude | high | claude-opus-4-7 | pass | correct | grounded | complete | yes | none |
| click_q01 | codex | high | gpt-5.4 | pass | correct | grounded | complete | yes | none |
| click_q02 | codex | low | gpt-5.4-mini | pass | correct | grounded | complete | yes | none |
| click_q02 | claude | low | claude-haiku-4-5-20251001 | pass | correct | grounded | complete | yes | none |
| click_q02 | codex | medium | gpt-5.4 | pass | correct | grounded | complete | yes | none |
| click_q02 | claude | medium | claude-sonnet-4-6 | pass | correct | grounded | complete | yes | none |
| click_q02 | claude | high | claude-opus-4-7 | warn | partial | grounded | partial | no | body of Context.invoke; base parse_args implementation; _OptionParser internals |
| click_q02 | codex | high | gpt-5.4 | pass | correct | grounded | complete | yes | none |
| click_q03 | codex | low | gpt-5.4-mini | pass | correct | grounded | complete | yes | none |
| click_q03 | claude | low | claude-haiku-4-5-20251001 | fail | wrong | ungrounded | insufficient | no | context gap |
| click_q03 | codex | medium | gpt-5.4 | pass | correct | grounded | complete | yes | none |
| click_q03 | claude | medium | claude-sonnet-4-6 | warn | partial | mixed | partial | no | full constructor signature, default_map inheritance, pass_context injection |
| click_q03 | codex | high | gpt-5.4 | pass | correct | grounded | complete | yes | none |
| click_q03 | claude | high | claude-opus-4-7 | warn | partial | grounded | partial | no | parent-child state inheritance, obj/ensure_object/find_object, __enter__/__exit__, full __init__ |
| click_q04 | claude | low | claude-haiku-4-5-20251001 | fail | wrong | ungrounded | insufficient | no | context gap |
| click_q04 | codex | low | gpt-5.4-mini | pass | correct | grounded | complete | yes | none |
| click_q04 | claude | medium | claude-sonnet-4-6 | warn | partial | mixed | partial | no | type_cast_value body; error/integration roles; full exception propagation chain |
| click_q04 | codex | medium | gpt-5.4 | warn | correct | grounded | partial | no | exact downstream tests/callers |
| click_q04 | claude | high | claude-opus-4-7 | pass | correct | grounded | partial | no | error_surface/integration_surface omitted; no test files in context |
| click_q04 | codex | high | gpt-5.4 | warn | partial | grounded | partial | no | full downstream integration surface |
| click_q05 | codex | low | gpt-5.4-mini | pass | correct | grounded | complete | yes | none |
| click_q05 | claude | low | claude-haiku-4-5-20251001 | pass | correct | grounded | complete | yes | none |
| click_q05 | codex | medium | gpt-5.4 | pass | correct | grounded | complete | yes | none |
| click_q05 | claude | medium | claude-sonnet-4-6 | pass | correct | grounded | complete | yes | none |
| click_q05 | claude | high | claude-opus-4-7 | pass | correct | grounded | complete | yes | none |
| click_q05 | codex | high | gpt-5.4 | pass | correct | grounded | complete | yes | none |
| celery_q01 | claude | low | claude-haiku-4-5-20251001 | warn | partial | mixed | partial | no | full _task_from_fun implementation; actual self.tasks insertion point |
| celery_q01 | codex | low | gpt-5.4-mini | pass | correct | grounded | complete | yes | none |
| celery_q01 | codex | medium | gpt-5.4 | warn | partial | grounded | partial | no | _task_from_fun implementation |
| celery_q01 | claude | medium | claude-sonnet-4-6 | pass | correct | grounded | complete | yes | _task_from_fun body absent; registry insertion covered by register_task/register |
| celery_q01 | codex | high | gpt-5.4 | warn | partial | grounded | partial | no | body of _task_from_fun |
| celery_q01 | claude | high | claude-opus-4-7 | pass | correct | grounded | partial | no | body of _task_from_fun and _pending drain on finalize |
| celery_q02 | claude | low | claude-haiku-4-5-20251001 | fail | wrong | ungrounded | insufficient | no | context gap |
| celery_q02 | codex | low | gpt-5.4-mini | warn | partial | grounded | partial | no | apply_async implementation |
| celery_q02 | codex | medium | gpt-5.4 | warn | correct | grounded | partial | no | apply_async implementation |
| celery_q02 | claude | medium | claude-sonnet-4-6 | warn | partial | grounded | partial | no | apply_async body; message serialization and broker publish logic |
| celery_q02 | codex | high | gpt-5.4 | warn | partial | grounded | partial | no | apply_async implementation |
| celery_q02 | claude | high | claude-opus-4-7 | warn | partial | grounded | partial | no | Task.apply_async body and broker/producer send path |
| celery_q03 | claude | low | claude-haiku-4-5-20251001 | fail | wrong | ungrounded | insufficient | no | context gap |
| celery_q03 | codex | low | gpt-5.4-mini | pass | correct | grounded | complete | yes | none |
| celery_q03 | codex | medium | gpt-5.4 | warn | partial | grounded | partial | no | on_message/process_task invocation path |
| celery_q03 | claude | medium | claude-sonnet-4-6 | pass | correct | grounded | complete | yes | on_task_request body not shown |
| celery_q03 | claude | high | claude-opus-4-7 | pass | correct | grounded | partial | no | process_task/on_task_request/Strategy dispatch/build_tracer bodies |
| celery_q03 | codex | high | gpt-5.4 | warn | partial | grounded | partial | no | on_message/on_task_request/process_task bodies |
| celery_q04 | claude | low | claude-haiku-4-5-20251001 | pass | correct | grounded | complete | yes | none |
| celery_q04 | codex | low | gpt-5.4-mini | pass | correct | grounded | complete | yes | none |
| celery_q04 | codex | medium | gpt-5.4 | pass | correct | grounded | complete | yes | none |
| celery_q04 | claude | medium | claude-sonnet-4-6 | pass | correct | grounded | complete | yes | none |
| celery_q04 | claude | high | claude-opus-4-7 | pass | correct | grounded | complete | yes | none |
| celery_q04 | codex | high | gpt-5.4 | pass | correct | grounded | complete | yes | none |
| celery_q05 | claude | low | claude-haiku-4-5-20251001 | fail | wrong | ungrounded | insufficient | no | context gap |
| celery_q05 | codex | low | gpt-5.4-mini | warn | partial | mixed | partial | no | exact test cases |
| celery_q05 | codex | medium | gpt-5.4 | warn | correct | grounded | partial | no | specific test files/names |
| celery_q05 | claude | medium | claude-sonnet-4-6 | warn | partial | grounded | partial | no | no test files; test impact cannot be assessed |
| celery_q05 | claude | high | claude-opus-4-7 | warn | partial | grounded | partial | no | publisher/producer call sites, test files, callers truncated by budget |
| celery_q05 | codex | high | gpt-5.4 | warn | partial | grounded | partial | no | exact test files/names |

Interpretation:

- Production answer cost is the sidecar prompt, not the judge matrix. The judge
  burn is offline validation overhead.
- Still, multi-provider answer-quality evidence is not free: the 60-row judge
  matrix burned **266,927 tokens**, about **7.4x** the retrieved sidecar prompt
  tokens for the same 10 questions.
- The judge matrix is the reason the holdout result is honest: retrieval was
  green on both slices, but Celery still showed many partial/insufficient
  contexts around publish, worker execution, and impact-test evidence.
- Do not present `5/5 retrieval pass` as answer-quality proof. Present it with
  judge burn and judge disagreement.

Claim boundaries from this run:

| Claim | Status | Evidence / caveat |
|---|---|---|
| Surgical Context retrieves much less context than file stuffing on Click/Celery. | Supported | 36,244 sidecar prompt tokens vs. 851,833 carpet tokens across 10 holdout questions. |
| Retrieval pass rate alone is not an answer-quality metric. | Supported | Both slices were 5/5 retrieval-pass, but judge context sufficiency was 31/60 overall. |
| Click holdout retrieval is mostly answer-sufficient. | Supported with caveats | 22 pass, 6 warn, 2 fail; context sufficient on 21/30 judge rows. |
| Celery holdout retrieval exposes remaining multi-hop gaps. | Supported | 12 pass, 15 warn, 3 fail; context sufficient on only 10/30 judge rows, mainly around publish, worker execution, and impact-test evidence. |
| Multi-provider LLM judgment is a useful validation layer. | Supported | It revealed sufficiency gaps hidden by retrieval pass gates, but burned 266,927 judge tokens. |
| The judge matrix proves context-only correctness. | Not supported | Judges were instructed to avoid pre-trained repo knowledge, but the run was not technically sandboxed or ablated. |
| Surgical Context always provides minimal sufficient context. | Not supported | No ablation/frontier test was run; precision and judge warnings show extra or missing evidence still matters. |
| Surgical Context beats Cursor-like indexed agents. | Not tested | Needs the future Cursor-like baseline in §4. |
| Current pass gates can be used as release gates without judge review. | Not supported | The Click/Celery matrix shows pass gates need answer-quality calibration for hard trace/impact questions. |

Strongest honest headline:

> On blind Click/Celery holdouts, Surgical Context reduced context tokens by
> ~95-96% versus file stuffing and recovered all required retrieval roles/files
> under the current gates, but multi-provider LLM judgment showed that answer
> sufficiency still lags on Celery's publish, worker-execution, and impact-test
> flows.

---

## 6. Harder validation protocol

The next validation layer is not another `5/5 retrieval pass` table. It should
measure the answer-context frontier and require explicit evidence citations.

New local tooling:

- `QA/context_frontier.py` — rebuilds smaller context variants from a
  `qa_benchmark.py` report, tests configured budgets, and can optionally run an
  evidence-citation LLM gate.
- `QA/cursor_baseline.py` — external eval helper for Cursor JSONL agent
  transcripts. It reports the observable transcript footprint: visible tokens,
  tool calls, mentioned files, question matches, and expected-file recall. It is
  not a runtime dependency, product feature, or claim that Cursor is part of this
  project.

Budget/frontier dry run:

```bash
PYTHONPATH=. .venv/bin/python QA/context_frontier.py \
  /tmp/qa_judge_runs/click_judge.json \
  /tmp/qa_judge_runs/celery_judge.json \
  --budgets 2000,4000,8000,16000 \
  --output QA/benchmark_artifacts/frontier_dry_run.json
```

Paid evidence-citation gate:

```bash
PYTHONPATH=. .venv/bin/python QA/context_frontier.py \
  /tmp/qa_judge_runs/click_judge.json \
  /tmp/qa_judge_runs/celery_judge.json \
  --budgets 2000,4000,8000,16000 \
  --run-judge \
  --provider codex \
  --effort medium \
  --greedy \
  --max-greedy-attempts 16 \
  --output QA/benchmark_artifacts/frontier_codex_medium.json
```

What this gives:

- smallest passing context found by the configured budget curve;
- optional one-unit greedy pruning after the first passing budget;
- judge token burn per question and per attempt;
- citation gate requiring `file_path`, `symbol`, quote, expected-file/symbol
  citation, and model-reported evidence-role coverage.

What it still does not give:

- mathematical minimality;
- proof that model weights contributed nothing;
- hidden-index accounting for Cursor or other IDE agents.

Private branch delta protocol:

1. Pick a real repo and create a local branch delta that changes behavior not
   present in public training data: route, worker path, serializer branch,
   registry rule, auth/config path, or tests.
2. Freeze the question pack after the delta is written and before any retrieval
   run. Store expected files, expected symbols, required evidence roles, and the
   changed mechanism.
3. Run sidecar retrieval, frontier/evidence gate, and external-agent baselines
   against the same branch and same frozen questions.
4. Report the frontier, not just the full-context result: 2k/4k/8k/16k budgets,
   greedy attempts, answer verdict, citations, missing evidence, judge tokens,
   and wall time.

Cursor transcript baseline:

```bash
PYTHONPATH=. .venv/bin/python QA/cursor_baseline.py \
  --project-substring surgical-context \
  --questions tests/fixtures/click_questions.yaml \
  --output QA/benchmark_artifacts/cursor_visible_baseline.json \
  --tsv QA/benchmark_artifacts/cursor_visible_baseline.tsv
```

The Cursor baseline is intentionally asymmetric because Cursor is an external
developer environment. It can use its private workspace index, while this repo
can only parse exported transcript artifacts and count visible tokens/tool
calls. Treat hidden index context as an explicit limitation, not as zero cost.

---

## 7. Reproducing metrics and artifacts

### Automated harness (P1 pipeline, checked in)

Per-repo retrieval metrics, `ready_context`, role/file recall:

```bash
PYTHONPATH=. .venv/bin/python QA/qa_benchmark.py \
  --questions tests/fixtures/real_repo_question_pack.yaml \
  --repo <repo_id> \
  --no-index
```

Full sweep: `PYTHONPATH=. .venv/bin/python QA/run_full_benchmark_sweep.py` (or repeat per repo).

| Artifact | Location |
|---|---|
| Append-only run log | `QA/benchmark_runs.jsonl` (default; use `--benchmark-log` to override) |
| Latest report JSON | path printed at end of `qa_benchmark.py` run |
| Inspect history | `python QA/benchmark_runs.py` |

Requires a pre-indexed workspace for each repo (`POST /index` or project indexing scripts). See [benchmark_mechanism_coverage.md](benchmark_mechanism_coverage.md) for the latest all-repo snapshot.

### LLM Judgment matrix (this document)

The per-question P1 vs P2 comparison table, token splits, and qualitative scores in §2–3 came from a **one-off judge session** (Claude Sonnet 4.6, max effort). That matrix is **not** stored in the repository. To regenerate:

1. Run the harness above and archive `ready_context` from each row (P1 prompts).
2. Re-run the P2 “first-time repo read” protocol under the same judge rules (see header).
3. Record scores in a new file under `QA/benchmark_artifacts/` if you want a durable copy (recommended name: `path1_vs_path2_judgment.json`).

Do not rely on `/tmp` paths — they are not part of the repo and are not reproducible across machines.
