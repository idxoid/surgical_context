"""Canonical axis retrieval pipeline (Phase 1b).

ONE function — :func:`run_axis_retrieval` — runs the full axis read-side:
intent -> workspace scan -> role/vector seeds -> cross-role lookahead ->
structural / inheritance / phased pool passes -> impact / trace mode
passes -> cross-role intersection -> intent-axis ranking -> per-candidate
context expansion. Three consumers share it so they cannot drift:

  * the ``/ask/axis`` endpoint (:func:`sidecar.main.ask_axis`) — shapes
    the result into ``AskAxisResponse``.
  * ``QA.axis_benchmark.run_question`` — measures the seed / pool / bundle
    recall layers off the result; the benchmark is the spec that
    validates *this exact code*.
  * the ``ContextBundle -> PromptContext`` provider
    (``axis_bundles_to_prompt_context``) — Phase 1c.

Design notes that matter for the seam:

* **Module-qualified calls.** Stage functions are reached through their
  *source modules* (``intent_classifier.classify_intent`` rather than a
  bound ``from ... import classify_intent``) so a consumer's monkeypatch
  on ``sidecar.axis.<module>.<fn>`` is honoured — the endpoint test relies
  on this.
* **Caller-owned ``db``.** The endpoint opens one ``db_session`` and the
  benchmark a single ``Neo4jClient``; both pass it through every stage
  instead of re-opening per pass.
* **Optional ``trace``.** The endpoint passes its request trace so each
  stage keeps a span; the benchmark passes nothing and gets a null tracer.
* **Optional ``context_seeds_per_role`` cap.** ``None`` (the benchmark
  path) feeds the whole pool into context expansion; the endpoint passes
  its request value to cap the per-role context seeds. The cap is the only
  knob that separates the two callers — everything else is identical, so
  the benchmark validates the endpoint's pipeline byte-for-byte.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from sidecar.axis import (
    axis_phased,
    axis_ranking,
    context_builder,
    cross_role_boost,
    impact_traversal,
    inheritance_ancestors,
    intent_classifier,
    role_lookahead,
    role_retrieval,
    structural_neighbours,
    trace_traversal,
)
from sidecar.axis.context_builder import ContextBundle
from sidecar.axis.intent_classifier import IntentMatch
from sidecar.axis.proximity import proximity_boost
from sidecar.axis.retrieval_budget import budget_for_intent
from sidecar.axis.role_retrieval import RoleCandidate

# Question-shape pseudo-roles: modes, not retrieval roles. They drive the
# blast-radius / call-chain passes and are excluded from the pools that
# *anchor* those passes.
_MODE_ROLES = frozenset({"impact_analysis", "trace_dependency"})


class _NullTrace:
    """Tracer stand-in: ``.stage(name)`` is a no-op context manager."""

    def stage(self, _name: str):  # noqa: D401 - trivial
        return contextlib.nullcontext()


@dataclass
class AxisRetrievalResult:
    """Everything the three consumers need, shaped by none of them.

    ``raw_by_role`` is the final pool (post intersection + intent-axis
    boost); ``seed_files`` is the pure-retrieval layer captured *before*
    any pool expansion; ``candidates_for_context`` is the flattened list
    actually fed to context expansion (already capped when the caller
    passed ``context_seeds_per_role``); ``bundles`` is empty when
    ``with_context`` is false.
    """

    intent: list[IntentMatch]
    raw_by_role: dict[str, list[RoleCandidate]]
    seed_files: list[str]
    candidates_for_context: list[RoleCandidate]
    bundles: list[ContextBundle] = field(default_factory=list)
    render_mode: str = "full"


def run_axis_retrieval(
    question: str,
    *,
    workspace_id: str,
    db: Any,
    lance: Any,
    top_roles: int = 3,
    per_role_limit: int = 8,
    intent_threshold: float = 0.20,
    with_context: bool = True,
    context_per_seed: int = 4,
    context_seeds_per_role: int | None = None,
    intent_budget: bool = False,
    base_token_budget: int = 4000,
    max_walk_seeds_override: int | None = None,
    render_mode_override: str | None = None,
    anchor_path: str | None = None,
    axis_split: bool = False,
    shallow_passive: bool = False,
    hook_transparency: bool = False,
    token_credit: bool = False,
    trace: Any | None = None,
) -> AxisRetrievalResult:
    """Run the axis read-side pipeline and return its layered result.

    ``db`` is any live Neo4j handle (``db_session`` value or
    ``Neo4jClient``); ``lance`` is a ``LanceDBClient`` used for both intent
    embedding and the vector seeds. ``trace`` may be any object exposing a
    ``stage(name)`` context manager; pass ``None`` for an un-instrumented
    run. ``context_seeds_per_role=None`` feeds the entire pool into context
    expansion (the benchmarked behaviour); a positive value caps the
    per-role context seeds.
    """

    tr = trace if trace is not None else _NullTrace()

    def _embed(text: str):
        return lance._embed([text])[0]  # noqa: SLF001

    with tr.stage("intent"):
        intent = intent_classifier.classify_intent(
            question,
            _embed,
            top_k=top_roles,
            threshold=intent_threshold,
        )

    with tr.stage("retrieval"):
        # One workspace-scoped scan (predicate pushdown + parse once)
        # feeds every role retrieval and the vector seeds.
        scanned = role_retrieval.scan_workspace_rows(workspace_id)
        raw_by_role: dict[str, list] = role_retrieval.find_symbols_by_roles(
            workspace_id,
            [m.role for m in intent],
            query_text=question,
            embed_fn=_embed,
            limit=per_role_limit,
            prescanned=scanned,
        )

    # Seed layer — pure vector/role retrieval, captured BEFORE any
    # graph-walk pool expansion (lookahead is itself a pool pass).
    seed_files: set[str] = {
        getattr(c, "file_path", "") or ""
        for cands in raw_by_role.values()
        for c in cands
    }

    # Cross-role *lookahead*: walk K hops from each role's vector
    # candidates, inject neighbours whose container_kinds back a different
    # intent role. Closes the case where the intent classifier picks the
    # right theme but the answer lives in a sibling role. Injection-only —
    # it never displaces vector candidates.
    if len(intent) >= 2 and any(raw_by_role.values()):
        with tr.stage("cross_role_lookahead"):
            raw_by_role = role_lookahead.expand_candidates_via_neighbourhood(
                [m.role for m in intent],
                raw_by_role,
                db=db,
                lance=lance,
                workspace_id=workspace_id,
                prescanned=scanned,
            )

    # Role-AGNOSTIC vector seeds — added AFTER lookahead (which rebuilds
    # the dict around intent roles and would drop a non-intent key). Intent
    # stays a resource manager (ranking + depth), out of structure
    # selection: pure similarity keeps the right nodes when intent misroutes.
    with tr.stage("vector_seeds"):
        raw_by_role["vector_seed"] = role_retrieval.find_seeds_by_vector(
            workspace_id,
            question,
            embed_fn=_embed,
            limit=per_role_limit,
            impact_mode=any(m.role in _MODE_ROLES for m in intent),
            prescanned=scanned,
        )
    seed_files |= {
        getattr(c, "file_path", "") or ""
        for c in raw_by_role.get("vector_seed", [])
    }

    # Structural-neighbour pass — file-level adjacency via undirected
    # AFFECTS, plus the upward inheritance walk and the reactive phased
    # walk (REGISTRY*->CONTROL) seeded by the pool's kinds (not intent).
    existing_pool_for_struct = [
        c
        for role, cands in raw_by_role.items()
        if role not in {"impact_analysis", "structural_neighbour"}
        for c in cands
    ]
    if existing_pool_for_struct:
        with tr.stage("structural_neighbours"):
            affects_pool = structural_neighbours.expand_structural_neighbours(
                existing_pool_for_struct,
                db=db,
                workspace_id=workspace_id,
            )
        ancestor_pool = inheritance_ancestors.expand_inheritance_ancestors(
            existing_pool_for_struct,
            db=db,
            workspace_id=workspace_id,
            exclude_uids=[c.uid for c in affects_pool],
        )
        already = {c.uid for c in (list(affects_pool) + list(ancestor_pool))}
        with tr.stage("phased"):
            phased_pool = axis_phased.expand_phased(
                existing_pool_for_struct,
                db=db,
                lance=lance,
                workspace_id=workspace_id,
                exclude_uids=already,
                prescanned=scanned,
            )
        raw_by_role["structural_neighbour"] = (
            list(affects_pool) + list(ancestor_pool) + list(phased_pool)
        )

    # Mode passes — both anchor on every concrete candidate already
    # nominated, but keep their traversal semantics separate:
    # impact_analysis is blast-radius; trace_dependency is CALLS-only.
    mode_intents_present = {m.role for m in intent if m.role in _MODE_ROLES}
    if mode_intents_present:
        existing_pool = [
            c
            for role, cands in raw_by_role.items()
            if role not in _MODE_ROLES
            for c in cands
        ]
        if existing_pool:
            if "impact_analysis" in mode_intents_present:
                with tr.stage("impact_traversal"):
                    raw_by_role["impact_analysis"] = (
                        impact_traversal.expand_impact_neighbourhood(
                            existing_pool,
                            db=db,
                            workspace_id=workspace_id,
                        )
                    )
            if "trace_dependency" in mode_intents_present:
                with tr.stage("trace_traversal"):
                    raw_by_role["trace_dependency"] = (
                        trace_traversal.expand_trace_neighbourhood(
                            existing_pool,
                            db=db,
                            workspace_id=workspace_id,
                        )
                    )

    # Multi-role *intersection* — weaker signals act as structural
    # constraints, not a separate pool. Skipped under a mode intent, where
    # the right answer often has no proximity to the tangential candidates.
    has_mode_intent = any(m.role in _MODE_ROLES for m in intent)
    if len(intent) >= 2 and not has_mode_intent:
        with tr.stage("cross_role_intersection"):
            for i, match in enumerate(intent):
                primary = raw_by_role.get(match.role) or []
                secondary = {
                    other.role: raw_by_role.get(other.role) or []
                    for j, other in enumerate(intent)
                    if j != i
                }
                raw_by_role[match.role] = (
                    cross_role_boost.intersect_by_cross_role_proximity(
                        primary,
                        secondary,
                        db=db,
                        workspace_id=workspace_id,
                    )
                )

    # Intent-axis ranking — intent as a ranker (not a selector). Boost
    # candidates whose kind-axes match the intent's axes; pools re-sort.
    # Role-agnostic seeds (no kinds) pass through untouched.
    raw_by_role = axis_ranking.apply_intent_axis_boost(
        raw_by_role, [m.role for m in intent]
    )

    # Flatten in intent-role order, then any lookahead-promoted roles.
    # ``raw_by_role`` may carry roles the intent classifier never produced
    # (see ``expand_candidates_via_neighbourhood`` auto-promote); skipping
    # them would discard graph-evidenced candidates.
    intent_role_keys = [m.role for m in intent]
    ordered_keys = intent_role_keys + [
        r for r in raw_by_role if r not in set(intent_role_keys)
    ]
    candidates_for_context: list[RoleCandidate] = []
    seen_keys: set[str] = set()
    for key in ordered_keys:
        if key in seen_keys:
            continue
        seen_keys.add(key)
        cands = raw_by_role.get(key) or []
        if context_seeds_per_role is not None:
            cands = cands[:context_seeds_per_role]
        candidates_for_context.extend(cands)

    # Intent-driven budgeting (opt-in; benchmark leaves it off -> walk all).
    # Echelon 1 is the ACTIVE/PASSIVE split, not a hard pool cap: rank the
    # whole pool by score, give only the top ``max_walk_seeds`` a graph WALK
    # (active — the expensive part), and keep the rest as PASSIVE context
    # (code-bearing, no walk) so their files survive for the token budget
    # without spawning neighbours or eating CPU. ``candidates_for_context``
    # stays the full pool (pool recall is unaffected — the cap is only on the
    # walk). Echelon 2 (token_budget + render_mode) packs the union below.
    token_budget: int | None = None
    render_mode = "full"
    active = candidates_for_context
    passive: list[RoleCandidate] = []
    utility_score_fn = None
    if intent_budget:
        budget = budget_for_intent(intent)
        walk_cap = (
            budget.max_walk_seeds
            if max_walk_seeds_override is None
            else max_walk_seeds_override
        )
        # S_utility = score (S_vector × W_type, already in the candidate score)
        # + B_proximity (path-locality from the ask anchor). The boost only
        # reorders the active/passive split + packing priority; it does not
        # mutate the candidate score the response/bundle carries. anchor_path
        # is None -> boost 0 -> rank by score alone (no downside).
        ranked = sorted(
            candidates_for_context,
            key=lambda c: c.score + proximity_boost(c.file_path, anchor_path),
            reverse=True,
        )

        def _budget_utility_score(c: RoleCandidate) -> float:
            return c.score + proximity_boost(c.file_path, anchor_path)

        utility_score_fn = _budget_utility_score
        if axis_split and len(intent) >= 2:
            # Per-axis walk split: multi-axis questions widen the pool and push
            # the relational seed (whose 1-hop neighbour is the answer) into
            # passive. Give each intent axis an equal, guaranteed share of the
            # walk, +20% capacity per extra axis; then top up the remainder by
            # score so role-agnostic vector_seed / structural (recall-critical)
            # aren't squeezed out.
            n_axes = len(intent)
            walk_cap = round(walk_cap * (1 + 0.20 * (n_axes - 1)))
            per_axis = max(1, walk_cap // n_axes)
            active, seen = [], set()
            for m in intent:
                taken = 0
                for c in ranked:
                    if c.uid in seen or c.role != m.role:
                        continue
                    active.append(c)
                    seen.add(c.uid)
                    taken += 1
                    if taken >= per_axis:
                        break
            for c in ranked:  # fill remainder by score (incl. vector_seed/structural)
                if len(active) >= walk_cap:
                    break
                if c.uid not in seen:
                    active.append(c)
                    seen.add(c.uid)
            passive = [c for c in ranked if c.uid not in seen]
        else:
            active = ranked[:walk_cap]
            passive = ranked[walk_cap:]
        token_budget = budget.effective_tokens(base_token_budget)
        render_mode = (
            budget.render_mode if render_mode_override is None else render_mode_override
        )

    bundles: list[ContextBundle] = []
    if with_context and (active or passive):
        with tr.stage("context"):
            bundles = context_builder.build_context_for_candidates(
                active,
                passive=passive,
                passive_shallow_hops=1 if shallow_passive else 0,
                workspace_id=workspace_id,
                db=db,
                lance=lance,
                max_per_seed=context_per_seed,
                hook_transparency=hook_transparency,
                token_budget=token_budget,
                render_mode=render_mode,
                token_credit=token_credit,
                utility_score_fn=utility_score_fn,
            )

    return AxisRetrievalResult(
        intent=list(intent),
        raw_by_role=raw_by_role,
        seed_files=sorted(f for f in seed_files if f),
        candidates_for_context=candidates_for_context,
        bundles=list(bundles),
        render_mode=render_mode,
    )


__all__ = ["AxisRetrievalResult", "run_axis_retrieval"]
