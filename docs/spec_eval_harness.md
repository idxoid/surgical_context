# Spec — Evaluation Harness (axis)

> **Status:** Active. The legacy ranking cascade and `QA/qa_benchmark.py` were removed in the cascade cleanup (2026-06-15). Axis (`context_engine/axis/`, `QA/axis_benchmark.py`) is the sole context + eval path. Old harness metrics and snapshots are not comparable with axis results.

## 1. Purpose

Measure axis retrieval on real repositories: `file_recall`, seed/pool recall, token cost, and latency. Every engine change should be validated empirically (see [engineering_principles.md](engineering_principles.md) P7).

## 2. Question packs

Committed under `QA/fixtures/`:

| Pack | Contents |
|---|---|
| `questions_python.yaml` | Python repos (fastapi, pydantic, django, flask, sqlalchemy, surgical_context, dathund, celery, click, …) |
| `questions_non_python.yaml` | Non-Python repos (express, nestjs, redux_toolkit, vue, …) |
| `new_questions_python.yaml` | FAQ/bug-style Python questions (not in structural role profiles doc) |
| `questions_swebench_python_smoke.yaml` | 30-task SWE-bench Lite smoke pack with patch-file, edit-locus, and full-hunk gold |
| `questions_swebench_python.yaml` | Full 300-task SWE-bench Lite pack with patch-file, edit-locus, and full-hunk gold |

Each question entry includes `repo`, `question`, `expected_files`, and optional `expected_symbols`, `intent`, `mechanism`, `anchor`. Legacy cascade fields (`required_roles`, `required_roles_canonical`) were removed — axis gate validates `file_recall` only.

SWE-bench entries also carry `base_commit`. `expected_spans` aliases the precise
old-side `expected_edit_spans`; `expected_hunk_spans` retains unchanged diff
context for diagnostics but is not scored as line gold. By default the benchmark resolves
them to `contextbench/{repo}@{base_commit[:12]}+axis_python_v1` and skips an
entry when that exact workspace has no symbol rows. This prevents old-side line
gold from being scored against a drifting shared `@main` index. For a deliberately
approximate file-only diagnostic, pass `--no-base-commit-workspaces`; do not use
its line/span metrics for comparisons.

## 3. Tools

### 3.1 `QA/axis_benchmark.py`

Read-side benchmark: replays a pack against indexed workspaces under the `axis_python_v1` profile.

```bash
PYTHONPATH=. python -m QA.axis_benchmark \
  --pack QA/fixtures/questions_python.yaml \
  --out /tmp/axis_benchmark \
  --repo surgical_context
```

Requires a pre-indexed workspace for each `repo` in the pack. Repos not indexed are recorded as `skipped`.

Full sweep (all repos in the Python pack):

```bash
PYTHONPATH=. python QA/run_full_benchmark_sweep.py
```

### 3.2 `QA/axis_analysis.py`

Extractor inventory + question-pack catalog (no Neo4j required):

```bash
PYTHONPATH=. python -m QA.axis_analysis --out /tmp/axis_analysis
```

Defaults to all three packs under `QA/fixtures/`. Override with `--pack`.

### 3.3 `QA/axis_role_report.py`

Per-workspace L4 role coverage from persisted `axis_contracts_json` (diagnostic, read-only):

```bash
PYTHONPATH=. python -m QA.axis_role_report --workspace <workspace_id>
```

### 3.4 `QA/axis_judge_run.py`

Optional LLM judge over axis benchmark output (qualitative scoring).

## 4. CI gate (P7)

`tests/integration/test_axis_benchmark_gate.py` indexes this repo under the active profile derived from `ci/surgical_context@main`, replays the eight `surgical_context_*` questions, and asserts against:

`QA/fixtures/baselines/p7_surgical_context_axis.json`

Refresh baseline after an intentional improvement (see module docstring in the test file).

## 5. Metrics

Primary axis benchmark outputs (`summary.json`):

- `overall_mean_recall` — mean `file_recall` across scored questions
- `overall_seed_mean_recall` / `overall_pool_mean_recall` — seed vs expanded pool
- `overall_mean_rendered_tokens` — tokens in assembled context
- `overall_mean_context_seconds` — graph expansion + code fetch time
- `gold_rank_funnel` — complete exact-owner and exact-symbol distribution across
  budget utility order, accepted Token Credit coverage transactions, and the
  final first-wins prompt order. Each stage reports presence, median/p90 rank,
  and recall@1/3/5/10/20/40/80/160; the owner flow also exposes retrieval misses
  rescued by graph expansion and coverage-selected owners lost after dedupe.
- `candidate_rank_token_spend.upgrade_attribution` — exact paid-upgrade token
  totals by seed/related scope, retrieval-backed/graph-only evidence, edge type,
  graph depth, and combined scope/evidence. Each dimension reconciles to the
  reported `upgrade_tokens` total.

Per-question rows include `file_recall`, `seed_recall`, `pool_recall`,
`rendered_tokens`, `context_seconds`, and `gold_rank_audit`. The latter contains
the full owner/symbol rank rows; it does not depend on the top-15 candidate audit
sample. Skipped questions retain `skipped_reason` in `results.jsonl`.

## 6. Related

- [axis_terminology.md](axis_terminology.md) — vocabulary for roles, contracts, traversal
- [question_structural_role_profiles.md](question_structural_role_profiles.md) — gold structural profiles per benchmark question (design draft)
- [logical_roles_structural_closure.md](logical_roles_structural_closure.md) — logical role vs structural closure model
- [engineering_principles.md](engineering_principles.md) — structural-only invariants
- [cascade_cleanup_inventory.md](cascade_cleanup_inventory.md) — what was removed and why
- [spec_sidecar_api.md](spec_sidecar_api.md) — `/ask/axis` and indexing endpoints
