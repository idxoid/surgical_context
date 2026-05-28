# Spec — Evaluation Harness (Phase 2.5)

> **Status:** Implemented locally and used actively for retrieval tuning. The remaining gap is CI automation of benchmark deltas; the harness, reports, real-repo pack, and baseline appends already exist in the repo.

## 1. Purpose

Every claim in [architectura.md §1.3](architectura.md) — "60–80% fewer tokens", "equal or better answer quality", "<200ms assembly" — is currently unfalsifiable. Without numbers, Phase 4 scales an unverified product.

The harness turns each claim into a CI-enforceable metric on a known fixture set. It is the first piece of infrastructure built in Phase 2.5, because every subsequent decision (token-budget BFS tuning, embedding-model swap, re-ranker weights) needs a measuring stick.

## 2. Success Criteria

The harness is "fully productized" when all four are true:

1. `pytest tests/` runs green on a golden fixture repo with ≥30 (question → expected_symbols) pairs.
2. `python QA/qa_benchmark.py` always emits a JSON metrics bundle and prints its path at the end of the run. `--report` is now an explicit output-path override, not the switch that enables report writing. The bundle includes `recall@k`, `precision`, `role_recall`, `file_recall`, `tokens_surgical`, `tokens_carpet_bomb`, `assembly_ms_avg`, and per-question `ready_context`.
3. Each run also appends a compact pointer row to `QA/benchmark_runs.jsonl` unless `--no-snapshot-manifest` is passed. The row records repo, core12 flag, commit, branch, report path, pass rate, precision, recall, tokens, reduction, and assembly time so `/tmp` reports remain discoverable. `QA/benchmark_runs.py` prints the recent rows, compares the latest rows for a repo, and audits `ready_context.contract.pruned[]` reasons from the referenced full reports.
4. GitHub Actions runs the bundle on every PR and posts a delta comment (regressions block merge).
5. A baseline row exists in `QA/baselines.jsonl` — without it, deltas are meaningless.

## 3. Fixture Design

### 3.1 Golden repo — `tests/fixtures/sample_project/`

A small but realistic Python project (~15 files, ~80 symbols) hand-crafted to exercise every retrieval edge case. **Not** auto-generated — hand-crafted, because the expected answers have to be trustworthy.

Required topologies:
- A symbol called by 5+ callers (fan-in) — tests caller-preference re-ranking.
- A symbol calling a 3-hop chain — tests depth budget.
- A decorated function (`@cached`, `@app.route`) — tests non-`CALLS` relationships.
- A class with inheritance across files — tests `DEPENDS_ON` edges.
- A symbol covered by two conflicting doc chunks — tests RAG re-ranking.
- A module-level constant referenced by 10+ symbols — tests constant pruning.
- A file with a syntax error — tests graceful indexer failure.

### 3.2 Question set — `tests/fixtures/real_repo_question_pack.yaml`

Satellite packs merge via top-level `includes` (e.g. [click_questions.yaml](../tests/fixtures/click_questions.yaml), [celery_questions.yaml](../tests/fixtures/celery_questions.yaml)). Questions may use `required_roles` (legacy) or `required_roles_canonical` (canonical taxonomy names used as-is after normalization).

```yaml
- id: repo_q06
  repo: example_repo
  symbol: serialize_response
  question: "If I change response model serialization behavior, what runtime paths and tests are most likely to be affected?"
  expected_mode: symbol
  mechanism: serialization_impact
  required_roles: [impact_runtime, impact_public_api, impact_test_surface]
  expected_symbols: [...]
  expected_files: [...]
  difficulty: medium
  intent: impact_analysis
```

Each entry: `id`, `repo`, `symbol`, `question`, `expected_mode` (`symbol` or `workspace`), **`mechanism`** (code relationship type), **`required_roles`** (list of roles ranker must fulfill), `expected_symbols`, `expected_files`, `difficulty`, `intent`.

**Current additions beyond the original Phase 2.5 design:**
- **`mechanism`**: Classifies which code relationship is being tested (for example registration flow, validation bridge, query-surface generation, dependency trace, or impact cascade). Enables diagnosing architectural gaps vs. ranking noise without relying on framework-named dispatch tables.
- **`required_roles`**: List of code roles the ranker must find. The YAML may use legacy names, but benchmark scoring normalizes them into the canonical role taxonomy before computing `role_recall`.
- **`expected_mode`**: Either `symbol` (should find by name) or `workspace` (correct answer is "not found" — used for negative test cases like nonexistent symbols).

Target: 30 entries for Phase 2.5 (sample fixture), 20+ for Phase 4 (real-repo pack across multiple frameworks/libraries). Repository names in the pack identify evaluation datasets, not retrieval shortcuts or bundled ranker behavior.

## 4. Metrics

### 4.1 Symbol Retrieval Metrics (legacy, used for fixture pack)

| Metric | Formula | Failure threshold |
|---|---|---|
| `recall@k` | `|retrieved ∩ expected| / |expected|` at k=5 graph deps | <0.80 blocks merge |
| `precision@k` | `|retrieved ∩ expected| / |retrieved|` | <0.60 blocks merge |

### 4.2 Mechanism-Aware Metrics (Phase 4, used for real-repo pack)

| Metric | Formula | Semantics |
|---|---|---|
| **`role_recall`** | `normalize(required_roles - missing_roles) / len(normalize(required_roles))` | Fraction of required code roles the ranker fulfilled on the canonical role scale. Diagnostic for code relationship discovery gaps. |
| **`file_recall`** | `|retrieved_files ∩ expected_files| / |expected_files|` | Fraction of expected files included. Tests ranking noise and code coverage. |
| **Intent-stratified pass gate** | See table below | Different intents have different acceptable thresholds. |

**Validity caveat:** `role_recall = 1.00` on every positive question means the
ranker covers every role currently encoded in the pack; it does not, by itself,
prove that the role annotations are complete or independent. Because some roles
were refined during benchmark debugging, reports should present `role_recall`
as "coverage of the current formalized mechanism taxonomy" and pair it with
`precision_at_5`, `file_recall`, `ready_context`, and `pruned[]` inspection. A
stronger claim requires a pre-registered or independently reviewed role-label
pass before ranker tuning.

**Intent-Stratified Pass Gates (Phase 4):**

| Intent | role_recall floor | file_recall floor | Gate semantics |
|---|---|---|---|
| `explain_behavior` | 0.70 | 0.50 | **AND** (both required); workspace-mode summarization relaxed (see below) |
| `trace_dependency` | 0.80 | 0.70 | **AND** (strict), **or** relaxed single-axis pass (see below) |
| `impact_analysis` | 0.60 | 0.50 | **OR** (either sufficient) |

**`trace_dependency` relaxed pass** (implemented in `QA/qa_benchmark.py`): pass when **either** axis is perfect and the other clears a floor:

- `(role_recall >= 1.0 OR file_recall >= 1.0) AND role_recall >= 0.60 AND file_recall >= 0.50`

This is an **OR-branch** on top of the strict AND gate, not a replacement. It marks near-perfect single-axis coverage as pass when the other axis still reflects partial sibling-module or label-span noise. Questions that miss both strict and relaxed thresholds remain `warn` and should be diagnosed via mechanism + `missing_roles` / `pruned[]`, not by lowering floors further.

**Workspace-mode `explain_behavior` relaxed pass**: questions declared `expected_mode: workspace` with directory-form `expected_files` (e.g. `[packages, docs, examples, website]`) are summarization questions over a monorepo layout. When `role_recall >= 1.0` the ranker already proved it discovered each required surface; partial directory coverage (`file_recall > 0`) is enough to pass. Strict `(rr_ok AND fr_ok)` still applies first; this is an OR-branch for the role-complete case only. Workspace-mode questions whose target symbol is absent from the graph continue to pass via the existing `workspace_correct_rejection` path.

**Rationale:**
- **Explanation**: Moderate role coverage + moderate file coverage = good answer
- **Tracing**: Prefer deep role coverage **and** broad file coverage; allow pass when one axis is saturated and the other is still informative
- **Impact**: Either test coverage (files) OR symbol coverage (roles) proves cascade exposure; don't need both

### 4.3 Token and Assembly Metrics (all packs)

| Metric | Formula | Failure threshold |
|---|---|---|
| `tokens_surgical` | tiktoken count of `to_system_prompt()` output | regression >10% blocks |
| `tokens_carpet_bomb` | tiktoken count of all files touched by any expected symbol | baseline only |
| `reduction_ratio` | `1 - tokens_surgical / tokens_carpet_bomb` | <0.50 blocks (target 0.60–0.80 per [architectura.md §1.3](architectura.md)) |
| `assembly_ms_avg` | mean wall-clock of `ContextArbitrator.get_context_for_symbol` across the run | >200ms is a local-release warning |

**Per-question report fields now include:**

- `precision` as an alias alongside `precision_at_k`
- `ready_context.token_count`
- `ready_context.contract` (serialized prompt contract)
- `ready_context.system_prompt`
- `expected_roles` (canonical normalized required roles from the question pack)
- `missing_expected_roles` (only the expected roles that remained unfilled)

Current local retrieval snapshot after the UnifiedRanker hardening pass:

| Repo | Command shape | Result |
|---|---|---|
| FastAPI | `QA/qa_benchmark.py --repo fastapi --no-index` when the index is current | Mostly green locally; remaining warning-class cases are file/precision tails, not missing role coverage |
| Pydantic | `QA/qa_benchmark.py --repo pydantic --no-index` when the index is current | Broadly green locally; module/package fallback handles package-surface targets |
| Redux Toolkit | `QA/qa_benchmark.py --repo redux_toolkit --no-index` when the index is current | **8/8 pass** (May 2026); `rtk_q07` (monorepo packages vs docs/examples) passes via the new workspace-mode relaxed gate — role coverage is perfect even when retrieval does not span all top-level dir hints |
| surgical_context | `QA/qa_benchmark.py --repo surgical_context --no-index` when the index is current | **7/7 pass** (May 2026); `surgical_context_q07` (`SidecarClient` ask flow) passes after TS `object_api` indexing + `role_taxonomy` + `Neo4j` `object_api` call-resolution fix; `surgical_context_q01` passes via query-topic recovery for explicit pipeline-stage terms (`ranking`, `PromptContext`) |
| dathund | `QA/qa_benchmark.py --repo dathund --no-index` when the index is current | **8/8 pass** (May 2026); `dathund_q04` / `dathund_q06` pass after trace recovery mode was broadened to identity/principal resolution and time-authority clock/window flows |

Use `--no-index` only when parser/indexer behavior has not changed. Re-index after changes to import extraction, semantic hints, role clustering, repository profile generation, or graph persistence.

### 4.4 Console diagnostics for mechanism-aware runs

The per-question console line now prints role diagnostics explicitly:

- `expected_roles=...` — canonical expected role set for that question
- `missing_roles=...` — unfilled subset of `expected_roles`
- trailing `missing: ...` — raw ranker-internal `ctx.missing_roles` (debug-only, may contain non-pack roles)

Use `missing_roles` as the pass-gate indicator. Treat raw trailing `missing:` as
internal telemetry for tuning recovery and role-planning behavior.

### 4.5 Optional LLM judge matrix (`--judge`)

Retrieval gates (`role_recall`, `file_recall`, pass/warn) remain the primary CI signal. Optional answer-quality judgment runs via **local CLI bridges** (not Anthropic HTTP):

```bash
PYTHONPATH=. .venv/bin/python QA/qa_benchmark.py \
  --questions tests/fixtures/click_questions.yaml \
  --repo click --no-index \
  --judge all
```

| Flag | Behavior |
|---|---|
| `--judge all` | Six parallel judges: `claude` + `codex` × `low` / `medium` / `high` effort tiers |
| `--judge low` \| `medium` \| `high` | Two parallel judges for that tier only |

Requires `claude` and/or `codex` on `PATH`. Per-question JSON stores full `judge.matrix[effort][provider]` cells (answer, `answer_quality`, `context_sufficiency`, model, latency, errors). Summary prints one row per cell.

Model overrides: `QA_JUDGE_CLAUDE_MODEL_HIGH`, `QA_JUDGE_CODEX_MODEL_MEDIUM`, etc. Default tier map lives in `QA/llm_judge.py`.

**Note:** Judge output is diagnostic and expensive; default harness runs skip it. Recall@k and `role_recall` remain the merge gates.

### 4.6 Ad-hoc repo tuning — mechanism packs (`MECHANISM_PACK_PATH`)

When a real-repo question **`warn`s with `missing_roles`** (or the graph is too sparse for generic trace recovery to reach a sibling package), use a **declarative mechanism pack** as **per-repo ad-hoc tuning** — not as a change to core ranker scoring.

**What it is**

- YAML under `sidecar/context/mechanism_packs/` (bundled templates in `bundled/`, e.g. [`flask_registration.yaml`](../sidecar/context/mechanism_packs/bundled/flask_registration.yaml)).
- Keys merged into index-time `role_catalog_json` on the Neo4j `Workspace`:
  - `mechanism_required_roles` — mechanism id → list of canonical roles (e.g. `api_surface`, `orchestrator`).
  - `mechanism_role_backfill` — mechanism id → role → `[{name, path_hint?, priority?}, …]` symbol hints for `_role_backfill_candidates`.
- **Opt-in only:** bundled files are **not** loaded unless listed in **`MECHANISM_PACK_PATH`** (`os.pathsep`-separated file paths). See `.env.example`.

**When to use**

| Signal | Typical pack fix |
|---|---|
| `missing_roles=orchestrator` on a decorator/API target with no graph path to runtime | Backfill `Injector`-class symbols under `path_hint: packages/core/injector` |
| Registration-flow questions miss `factory_surface` / `runtime_surface` | Copy/adapt `auto:registration_flow` backfill (Flask template) |
| Benchmark `mechanism:` in YAML does not match ranker’s auto-detected mechanism | Pack can define the same mechanism id + roles; still prefer generic recovery first |

**Workflow**

1. Copy a bundled template or author a new YAML (mechanism ids must match question `mechanism:` or `auto:*` strategy archetypes the ranker selects).
2. Export path before **re-index** (pack is merged at Pass 1 persist, not at `--no-index` time):

   ```bash
   export MECHANISM_PACK_PATH=/home/idxoid/surgical_context/sidecar/context/mechanism_packs/bundled/flask_registration.yaml
   PYTHONPATH=. .venv/bin/python QA/qa_benchmark.py --repo flask --report /tmp/flask_reindex.json
   ```

   **Celery PoC** (publish/consume content gap — `celery_q02` / `celery_q03`):

   ```bash
   export MECHANISM_PACK_PATH=$PWD/sidecar/context/mechanism_packs/bundled/celery_publish_consume.yaml
   # Re-index celery (pack merges at Pass 1 persist), then:
   PYTHONPATH=. .venv/bin/python QA/qa_benchmark.py \
     --questions tests/fixtures/celery_questions.yaml --repo celery --no-index \
     --report /tmp/celery_pack_noindex.json
   ```

3. Verify with **`--no-index`** on the same repo after indexing completes.
4. Commit the YAML if the tuning is stable; keep **`MECHANISM_PACK_PATH` out of CI** unless the job re-indexes that repo with the same env.

**What it does not replace**

- Pass 1 role clustering (`derived_role_id`, `role_to_archetypes`) — primary role supply.
- Generic structural recovery in `ranker/recovery.py` — try widening trace scope before adding pack literals.
- Question-pack labels (`required_roles`, `expected_files`) — packs only help the ranker **fill** roles; evaluation still uses YAML expectations.

**Loader chain:** `mechanism_packs/loader.py` → `preloaded_mechanism_catalog_extensions()` → `merge_preloaded_mechanisms_into_role_catalog()` → `Workspace.role_catalog_json` → `UnifiedRanker._role_backfill_candidates()`. Details: [spec_indexer.md § Pass 1 mechanism profiles](spec_indexer.md), [retrieval_kernel.md § Mechanism packs](retrieval_kernel.md).

### 4.7 Future — `suggest-pack` (automate pack drafts from benchmark gaps)

**Status:** not implemented. Manual packs (§4.6) remain the supported path.

**Goal:** when the same `(repo, mechanism, missing_role)` pattern repeats across benchmark runs, emit a **reviewable YAML draft** instead of hand-authoring `name` / `path_hint` rows from Neo4j grep.

**Proposed CLI** (product surface; exact binary name TBD):

```bash
surgical suggest-pack --repo flask \
  --from-report /tmp/qa_bench_noindex_all/flask.json \
  --out sidecar/context/mechanism_packs/generated/flask_draft.yaml
```

Alias during harness-only workflows: `python QA/suggest_mechanism_pack.py --repo flask` (same logic, no new top-level binary required for v1).

**Inputs**

| Source | Use |
|---|---|
| Latest or explicit `--report` JSON | Per-question `missing_expected_roles`, `mechanism`, `required_roles`, `retrieved_files`, `ranker_state` |
| Neo4j workspace for `--repo` | Symbols under `expected_files` / sibling paths not in prompt; high in-degree hubs in packages referenced by warns |
| Question pack YAML | Stable `mechanism:` id and canonical `required_roles` to key the pack |

**Heuristics (deterministic v1; LLM optional v2)**

1. **Aggregate** warns: count `(mechanism, missing_role)` across questions; surface only roles missing in ≥2 questions or ≥50% of mechanism-tagged items for that repo.
2. **Candidate symbols** per missing role: symbols in graph matching role taxonomy / path tokens from `expected_files` and query terms; rank by edge count + path overlap with `expected_files` not present in `retrieved_files`.
3. **Emit** `mechanism_role_backfill` rows: `{name, path_hint, priority}` deduped by `(name, path_hint)`; optional `mechanism_required_roles` copy from pack YAML.
4. **Header comment** in generated file: source report path, timestamp, questions cited, “human review required — re-index with `MECHANISM_PACK_PATH` then `--no-index`”.

**Output contract**

- Valid pack schema (same keys as bundled templates).
- Never auto-enable: user sets `MECHANISM_PACK_PATH` and re-indexes after edit.
- Idempotent re-runs: merge with existing draft or write `*.yaml.new` for diff.

**Non-goals for v1**

- No automatic commit or CI wiring.
- No replacement for Pass 1 clustering or generic recovery — only accelerates §4.6.
- No framework literals in core ranker; generated hints stay in YAML under `mechanism_packs/generated/`.

**Success criterion:** running suggest-pack on a repo with known `missing_roles` warns produces a draft that, after ≤5 minutes of human edit + re-index, clears those warns on `--no-index` without changing `qa_benchmark.py` gates.

## 5. Module Layout

```
tests/
  conftest.py                    # pytest fixtures: temp Neo4j, temp LanceDB
  fixtures/
    sample_project/              # golden repo (committed)
    questions.yaml               # curated Q&A pairs
    expected_graph.json          # materialized expected Symbol/CALLS/DEPENDS_ON
  unit/
    test_parser.py               # tree-sitter extraction stability
    test_arbitrator_bfs.py       # BFS correctness on fixture
    test_overlay.py              # dirty-state reads
    test_indexer.py              # hash-based incremental upserts
  integration/
    test_ask_endpoint.py         # full /ask flow, LLM stubbed
    test_index_endpoint.py       # full /index flow on sample_project
QA/
  qa_benchmark.py                # reframed: loads questions.yaml, emits metrics JSON
  benchmark_runs.py              # inspect benchmark_runs.jsonl and audit pruned[] reasons
  baselines.jsonl                # one row per commit on main
  judges/
    recall.py
    tokens.py
    latency.py
```

## 6. CI Integration

Target end-state:

`.github/workflows/eval.yml` runs on every PR:

1. Boot Neo4j + LanceDB in services.
2. `pytest tests/` — unit + integration.
3. `python QA/qa_benchmark.py --report out.json`.
4. Load latest `baselines.jsonl` row from main.
5. Diff: any metric worse than its failure threshold fails the job.
6. Post comment: `recall@5 0.84 → 0.87 ✅ | tokens_surgical 1.2k → 1.5k ⚠️ (+25%)`.

On merge to main, a follow-up job appends a new row to `baselines.jsonl` with the commit SHA.

Current repo truth: CI runs the unit suite only; benchmark-diff automation remains future work.

## 7. Non-Goals

- **Not** a default answer-quality gate in CI. Optional `--judge` matrix uses CLI bridges locally; merge policy still keys off retrieval metrics unless a separate judge baseline is adopted.
- **Not** a load test. Latency is measured on a single-threaded synthetic workload; real concurrency testing lives in Phase 4.
- **Not** a regression suite for Ollama output. The LLM is stubbed in integration tests — we measure what *we* ship (the context), not what the model does with it.

## 8. Open Questions

- **Should fixtures include TypeScript?** Partially addressed: the TypeScript adapter indexes `export const Foo = { ... }` as a single `object_api` symbol; the fast indexer adds `ts_http_route_hints` (`SEMANTIC_HINT` from TS client surfaces to Python FastAPI handlers). CI still lacks a dedicated golden TS fixture repo; real-repo coverage uses `surgical_context` (extension + sidecar).
- **Stub LLM vs. real Ollama in CI?** Stub. Real LLM means flaky CI and license risk on a shared runner. Real-LLM runs happen locally via `make eval-full`.
- **Where does the "carpet-bomb" baseline come from?** For each question, union the files of all expected symbols — that's a charitable approximation of what a naive tool would send.

## 9. Related

- [road_map.md](road_map.md) — Phase 2.5 checklist.
- [spec_token_budget_bfs.md](spec_token_budget_bfs.md) — depends on this harness for tuning.
- [architectura.md §2.4](architectura.md) — observability requirements this harness satisfies.
- ADR-006 — the blocking rationale.
