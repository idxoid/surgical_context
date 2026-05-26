# Benchmark: ContextArbitrator vs Direct File Read (LLM Judgment)

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

---

## 1. Methodology

### 1.1 Two paths

**Path 1 (P1) — Surgical Context pipeline**

```python
ctx = arb.get_context_for_symbol(symbol, question=question, token_budget=4000)
prompt = ctx.to_system_prompt()
```

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

## 4. Reproducing metrics and artifacts

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
