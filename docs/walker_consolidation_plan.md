# Axis walker consolidation — plan

Merge the **two** axis graph-walk mechanisms into one core. Latency win;
recall-neutral target. Does NOT depend on seed-floor (that work is done).

## Current state — two walkers

1. **`context_engine/axis/graph_walk.py` → `walk_neighbours`** (the core).
   Used by 5 pool passes: structural_neighbours, inheritance_ancestors,
   impact_traversal, trace_traversal, role_lookahead (+ cross_role_boost
   reuses its `_safe_rel_pattern`/`_safe_max_hops`). Takes a **seed list**
   (batched), `EdgeProfile` whitelist, `direction`, `max_hops`. Extras the
   other lacks: `reach` (distinct-seeds centrality), `cap_by_file`,
   `exclude_tests`, `anchor=file_classes`, workspace-edge filter.

2. **`context_engine/axis/graph_traversal.py` → `AxisGraphTraversal.expand`**
   (the holdout). Used ONLY by `build_context_for_candidates`
   (per-candidate context expansion). Plan-driven: `compile_axis_query`
   (`context_engine/axis/query_plan.py`) compiles a `TraversalMode`
   (`immediate_control_flow` | `deferred_binding_flow`) into
   `ExpansionStep`s (edge_types + direction + max_depth) + stop_conditions.
   Then `expand([uid], plan)` runs `(seed)-[edges*1..N]->(n:Symbol)` per
   candidate. **Has its own duplicate `_safe_rel_pattern`.**

The two issue essentially the same Cypher shape. `AxisGraphTraversal` is a
plan-indirection + per-candidate loop on top of what `walk_neighbours`
already does — but **per-candidate (N queries) instead of batched (1)**.
That per-candidate loop is the latency hotspot
([context_builder.py:142](../context_engine/axis/context_builder.py#L142)).

## Why two layers exist (the precision reason — keep it)
Per-edge-type fan-out differs: `DEPENDS_ON` sparse (1-2 bases), `CALLS`
dense (a fn calls dozens). The pool passes survive flooding via tight
per-profile `cap_by_file`. A naive union walk maximises recall but floods
precision (violates "precision over recall"). So consolidation = **one
core + edge-profile-aware caps**, not one undifferentiated fat walk.

## Plan

1. **Map the 2 TraversalModes to EdgeProfiles.** `immediate_control_flow`
   → a CONTROL/CALLS profile; `deferred_binding_flow` → CALLS ∪ USES_TYPE
   ∪ binding edges. Add to `axis_profiles.AXIS_EDGES` / `EdgeProfile` if
   absent. The mode→edges mapping in `query_plan._expansion_steps_for_mode`
   becomes an `EdgeProfile` lookup. Keep `stop_conditions` as a cap.

2. **Rewrite `build_context_for_candidates` to batch.** Replace the
   per-candidate `for cand: traversal.expand([cand.uid], plan)` loop with a
   single `walk_neighbours(db, ws, [all candidate uids], edges=<mode
   profile>, direction=…, max_hops=<step depth>)`. This collapses N
   per-candidate Cypher round-trips into one batched walk — the latency
   win. Re-bucket the returned neighbours back to their seed via the
   `reach`/seed set if per-seed grouping is needed for `max_per_seed`.

3. **Preserve precision.** Apply `cap_by_file` per edge profile (already in
   the core). If per-seed caps matter (max_per_seed), cap per seed-group;
   `reach` gives centrality for ranking. Verify CALLS fan-out doesn't
   flood the bundle.

4. **Delete the holdout.** Remove `graph_traversal.py` (the duplicate
   `_safe_rel_pattern` + `AxisGraphTraversal`). Keep `query_plan.py` only as
   a thin mode→EdgeProfile map, or inline it and delete.

5. **Validate.** Three-layer benchmark must hold **bundle 0.983 byte-
   identical** (recall-neutral); measure the latency drop (N→1 queries per
   question). Run from `/tmp/axis_tier_v6/summary.json` as the compare base.

## Caveats carried over
- **Overlay/uncommitted + new files:** the unified walk must run LIVE
  (consult the overlay) — `walk_neighbours` already issues live Cypher, so
  this survives. The committed graph could later be precomputed; the same
  code path handles overlay deltas.
- **Doc-anchors:** keep the context-build STAGE as the doc-chunk attach
  point (fetch code + docs for the FINAL candidate set), but strip its
  DISCOVERY role (no new per-candidate graph walk).

## State at plan time (2026-06-11)
Honest base after clean reindex + inheritance cartesian fix + gold triage:
**seed 0.828 / pool 0.944 / bundle 0.983 / 0 zero**, graph 68k nodes
(non-cartesian). All 8 axis workspaces reindexed clean. Commits this
session: file_tier 2657e21/165575d/7125043, inheritance fix 7b9b92a,
file_tier relpath 98193a8, gold triage c8a7970.
