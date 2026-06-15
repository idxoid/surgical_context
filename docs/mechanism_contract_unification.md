# Mechanism contract tables — post-mortem

> **Superseded (2026-06-15).** Describes the legacy ranking cascade / `qa_benchmark` harness, removed in the cascade cleanup — axis (`sidecar/axis/`, `QA/axis_benchmark.py`) is the sole context + eval path now. Kept for historical context; see `cascade_cleanup_inventory.md`.


## What was removed

Four hardcoded contract table groups in `signal_constants.py` and four corresponding recovery methods in `recovery.py`:

| Deleted constant group | Deleted method | Mechanism |
|---|---|---|
| `TRACE_PUBLISH_CONTRACT_*` | `publish_trace_mandatory_anchor_candidates` | celery publish |
| `TRACE_CONSUME_CONTRACT_*` | `consume_trace_mandatory_anchor_candidates` | celery consume |
| `TRACE_TASK_REGISTRATION_CONTRACT_*` | `task_registration_mandatory_anchor_candidates` | celery task registration |
| `TRACE_ASYNC_RESULT_CONTRACT_*` | `async_result_backend_mandatory_anchor_candidates` | celery result backend |

Each table embedded literal symbol names and file-path substrings (`/app/task.py`, `/worker/strategy.py`) derived directly from the celery benchmark answer key. They were framework-specific, overfitting, and a principle violation — [mechanism_registry.py](../sidecar/context/mechanism_registry.py) explicitly stubs framework dispatch tables; these were the same thing in a different file.

## Why they could be deleted

The root cause was a gap in the parser: `self.attr.method()` calls and `local = self.attr; local.method()` patterns were dropped silently (nested attribute branch returned early). Celery's collaborator chains (`apply_async → create_task_message / send_task_message / Producer`) were invisible to the graph, so the ranker fell back to contract tables to surface them.

The fix was **Tier 4.5 CALLS_TYPED** in [python_adapter.py](../sidecar/parser/adapters/python_adapter.py): instance-attribute type inference (three sources: `_cls` string convention, `__init__` instantiation, class annotations) resolves collaborator calls to qualified targets and emits real `CALLS_DYNAMIC` edges. See [spec_call_resolution_pipeline.md §2.5.1](spec_call_resolution_pipeline.md).

With those edges in the graph, the LLM judge confirmed (q01–q03 pass, role_recall=1.0) that derived-only context is sufficient. Contract tables were deleted unconditionally — no kill-switch, no YAML migration.

## Hard limit accepted

The `apply_async → send_task` hop crosses Celery's `current_app = Proxy(get_current_app)` runtime thread-local proxy. No annotation, no assignment, name collision with `canvas.apply_async`. This hop is **not bridgeable by static analysis** and is accepted as a permanent hard limit. The LLM judge treats the context as complete up to this boundary.

## Follow-up: return-shape foundation

The committed Phase A change after this deletion added return-shape AST markers
to `SymbolMetadata` and persisted them on `Symbol` nodes:

- `returns_mapping`
- `returns_sequence`
- `returns_constructed_type`
- `returns_function_expression`

This does **not** revive mechanism contracts and does **not** author links. It is
the next structural substrate for roles such as `binding_surface` and
`schema_builder`: the graph can now distinguish "this function returns a mapping"
from "this function only calls helpers", while still admitting that field reads,
iteration locals, and value-flow from source shape to output shape are not solved.
The same principle holds: add code-derived facts first; consume them as role
discriminators only after empirical validation.

### Follow-up: `binding_surface` is a composite, not a single predicate

After Phases A–D landed (return-shape markers + READS_ATTR/WRITES_ATTR edges +
iteration-shape markers + two AST-shape `binding_surface` predicates at priority
75), an attempt to drop the legacy "topology-only" `binding_surface` predicate
(`call_fan_out > call_fan_in & type_fan_out > 0 & cross_package_call_out ≥ 1 &
import_in ≥ 20 & depth_from_public ≥ 2`, priority 73) regressed `django_q02`
(rr 1.00 → 0.75, binding_surface count 546 → 504). The legacy predicate is
itself structural — it composes call-graph topology, type fan, import fan, and
depth from public surface — and catches a complementary 42-symbol set that the
AST-shape predicates do not. The three predicates were therefore kept as
complementary composites: AST-shape (Pattern A/B at 75) catches assembled
mappings; topology+type (legacy at 73) catches heavily-imported internal helpers
that construct typed objects across package boundaries.

## What Path 2 (mechanism packs) is today

`_role_backfill_candidates` in [unified_ranker.py](../sidecar/context/unified_ranker.py) remains active as an **opt-in extensibility mechanism**: set `MECHANISM_PACK_PATH` to load a YAML pack (see [celery_publish_consume.yaml](../sidecar/context/mechanism_packs/bundled/celery_publish_consume.yaml) for the schema). It is not loaded by default and does not author graph edges — it only backfills role slots that the derived graph leaves empty.

The principle: **graph = derivative of code and topology. YAML packs are not authors of links.**
