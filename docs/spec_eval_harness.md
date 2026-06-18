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
| `new_questions_python.yaml` | Additional Python questions |

Each question entry includes `repo`, `question`, `expected_files`, and optional `expected_symbols`, `intent`, `mechanism`, `required_roles_canonical`.

## 3. Tools

### 3.1 `QA/axis_benchmark.py`

Read-side benchmark: replays a pack against indexed workspaces under the `axis_python_v1` profile.

```bash
PYTHONPATH=. python -m QA.axis_benchmark \
  --pack QA/fixtures/questions_python.yaml \
  --out /tmp/axis_benchmark \
  --repo surgical_context \
  --intent-budget --context-seeds-per-role 2
```

Requires a pre-indexed workspace for each `repo` in the pack. Repos not indexed are recorded as `skipped`.

Full sweep (all repos in the Python pack):

```bash
PYTHONPATH=. python QA/run_full_benchmark_sweep.py
```

### 3.2 `QA/axis_analysis.py`

Mechanism/bit coverage audit over question packs (no Neo4j required):

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

`tests/integration/test_axis_benchmark_gate.py` indexes this repo under `ci/surgical_context@main`, replays the seven `surgical_context_*` questions, and asserts against:

`QA/fixtures/baselines/p7_surgical_context_axis.json`

Refresh baseline after an intentional improvement (see module docstring in the test file).

## 5. Metrics

Primary axis benchmark outputs (`summary.json`):

- `overall_mean_recall` — mean `file_recall` across scored questions
- `overall_seed_mean_recall` / `overall_pool_mean_recall` — seed vs expanded pool
- `overall_mean_rendered_tokens` — tokens in assembled context
- `overall_mean_context_seconds` — graph expansion + code fetch time

Per-question rows include `file_recall`, `seed_file_recall`, `pool_file_recall`, `rendered_tokens`, `context_seconds`, and `skipped` / `skip_reason` when applicable.

## 6. Related

- [axis_terminology.md](axis_terminology.md) — vocabulary for roles, contracts, traversal
- [engineering_principles.md](engineering_principles.md) — structural-only invariants
- [cascade_cleanup_inventory.md](cascade_cleanup_inventory.md) — what was removed and why
- [spec_sidecar_api.md](spec_sidecar_api.md) — `/ask/axis` and indexing endpoints
