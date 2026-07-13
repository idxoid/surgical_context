# ContextBench Integration — Spec

## Overview

`QA/contextbench_adapter.py` converts Surgical Context MCP tool results into the
unified trajectory format consumed by ContextBench. The MCP server can capture
those results to JSONL without changing its normal behavior.

This integration measures Surgical Context as retrieval scaffolding around a
coding agent. It does not replace ContextBench's agent runner or patch grading.

## Experiment design

Run paired control and treatment arms with the same:

- ContextBench instances and base commits;
- agent and foundation model;
- prompt, token budget, step limit, and timeout;
- container image and patch grader.

The control agent uses its normal repository tools. The treatment agent gets
the same tools plus Surgical Context MCP. Compare Pass@1, Context F1, line F1,
efficiency, cost, and per-step coverage.

Start with 20–30 Python instances from `contextbench_verified`. Use the full
500-instance verified subset only after both arms complete the smoke set.

Build a deterministic repository-balanced subset from the upstream task list:

```bash
PYTHONPATH=. python3 -m QA.contextbench_subset \
  --source /path/to/ContextBench/data/selected_500_instances.csv \
  --output /tmp/contextbench/smoke.csv \
  --bench Verified \
  --language python \
  --repos django,flask \
  --limit 20
```

The same generated CSV must be passed to both arms.

### MiniSWE treatment bridge

MiniSWE exposes bash only. Mount `QA/contextbench_http_bridge.py` read-only into
the task container and add `--add-host=host.docker.internal:host-gateway` to its
Docker run arguments. The treatment prompt may then recommend:

```bash
python3 /opt/surgical-context/contextbench_http_bridge.py \
  --workspace bench/django@base \
  "where is URL resolution and dispatch implemented"
```

Configure the mounted process with:

```bash
export SURGICAL_CONTEXT_URL=http://host.docker.internal:8000
export SURGICAL_CONTEXT_WORKSPACE=bench/django@base
export CONTEXTBENCH_INSTANCE_ID=django__django-14434
export CONTEXTBENCH_EVENT_LOG=/contextbench/events.jsonl
export SURGICAL_CONTEXT_SPAN_LINE_RERANK=true  # treatment ablation only
```

The bridge uses only the Python standard library, prints retrieved code into
the MiniSWE observation, and writes the same adapter-compatible event format as
the MCP capture. The checkout at the task's exact `base_commit` must be indexed
under the configured workspace before starting the treatment arm.

Prepare exact-commit workspaces from the subset and gold parquet. The default
is plan-only and writes a reviewable manifest:

```bash
PYTHONPATH=. .venv/bin/python -m QA.contextbench_prepare \
  --subset /tmp/contextbench/smoke.csv \
  --gold /path/to/ContextBench/data/contextbench_verified.parquet \
  --manifest /tmp/contextbench/prepare.json
```

After reviewing the manifest, add `--execute` to clone each trusted GitHub
repository, detach at its exact 40-character `base_commit`, and build a fresh
`axis_python_v1` workspace. Each workspace id includes the commit prefix and
each checkout lives below its instance-specific directory.

## Capture interface

Set these variables in the treatment agent's MCP server environment:

```bash
export SURGICAL_CONTEXT_CONTEXTBENCH_LOG=/tmp/contextbench/events.jsonl
export SURGICAL_CONTEXT_CONTEXTBENCH_INSTANCE_ID=astropy__astropy-12907
```

The server appends one event for each completed tool call:

```json
{"instance_id":"astropy__astropy-12907","tool":"read_symbol","result":{"ok":true,"tool":"read_symbol","file_path":"astropy/modeling/separable.py","start_line":200,"end_line":248,"code":"..."}}
```

Capture is disabled unless both variables are non-empty. The logger removes the
duplicated markdown render and never turns a successful retrieval into an error.
`batch` sub-operations are recorded individually; the outer batch envelope is
not recorded again.

After the agent finishes, append its submitted patch as an event with no tool:

```json
{"instance_id":"astropy__astropy-12907","model_patch":"diff --git ..."}
```

## Conversion

Convert one or many interleaved instance logs:

```bash
PYTHONPATH=. python3 -m QA.contextbench_adapter \
  --input /tmp/contextbench/events.jsonl \
  --output /tmp/contextbench/treatment.predictions.jsonl \
  --repo-root /workspace/astropy
```

`--repo-root` converts absolute MCP paths to repository-relative paths and
drops absolute paths outside the checkout. Omit it when the MCP server already
returns repository-relative paths.

Then evaluate with the upstream package:

```bash
python3 -m contextbench.evaluate \
  --gold data/contextbench_verified.parquet \
  --pred /tmp/contextbench/treatment.predictions.jsonl \
  --out /tmp/contextbench/treatment.results.jsonl
```

The generated prediction records have this shape:

```json
{
  "instance_id": "astropy__astropy-12907",
  "traj_data": {
    "pred_steps": [{"files": ["astropy/modeling/separable.py"], "spans": {}, "symbols": {}}],
    "pred_files": ["astropy/modeling/separable.py"],
    "pred_spans": {},
    "pred_symbols": {}
  },
  "model_patch": "diff --git ..."
}
```

## Mapping rules

| MCP result | ContextBench observation |
|---|---|
| `ask_code` / `investigate` with code | Files, symbols, and source spans |
| `ask_code(render="names")` / `investigate(depth="lean")` | Files and symbols, no source spans |
| `read_symbol` with code | Exact file and source span |
| `find_definition`, `file_outline`, `search_code` | Located files, no claimed source span |
| `callers`, `callees`, `impact` | Structurally exposed files, no claimed source span |
| failed calls and non-context tools | Ignored |

Each MCP result remains a separate trajectory step. Final overlapping or
adjacent line intervals are merged, while per-step intervals remain unchanged
for ContextBench AUC and redundancy metrics.

For context tools, each symbol row may carry `rendered_spans`: the exact source
intervals represented by its current signature/compact/fold render. The adapter
prefers these intervals over the symbol's original `start_line..end_line` and
falls back to the original range only for legacy rows without `rendered_spans`.
An explicit empty list means that the rendered text is synthetic and claims no
source lines.

## Within-symbol line reranking

The experimental span/line ranker is opt-in. It batches query-similarity scores
for windows inside the symbols selected by the first Token Credit pass, then
reruns packing with at most six ranked body lines per symbol. Explicit source
line references in the question are treated as dominant anchors. Enable it for
the MCP treatment arm or the HTTP bridge with:

```bash
export SURGICAL_CONTEXT_SPAN_LINE_RERANK=true
```

For the internal benchmark, use `--span-line-rerank`; the candidate, symbol,
and body-line caps are independently configurable with the corresponding
`--span-rank-*` flags. The feature remains off by default while the ablation is
expanded beyond the smoke set.

## Limitations (current)

- The upstream ContextBench runner does not natively launch Surgical Context;
  the treatment arm must inject the MCP configuration into the chosen agent.
- The agent runner must update `SURGICAL_CONTEXT_CONTEXTBENCH_INSTANCE_ID` for
  every task and append the final submitted patch.
- File-discovery tools expose file context but lack exact end lines, so they do
  not contribute span or line coverage until the agent reads source.
- `--repo-root` is a single root per conversion command. Convert tasks from
  different checkout roots separately, then concatenate their prediction JSONL.

## Related

- [Evaluation harness](spec_eval_harness.md) — the internal axis retrieval benchmark
- [MCP server](../mcp_server/README.md) — agent integration and tool contracts
- [ContextBench](https://github.com/EuniAI/ContextBench) — upstream evaluator and dataset
