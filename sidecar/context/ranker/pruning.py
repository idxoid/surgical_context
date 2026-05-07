"""Budget selection loop: marginal gain, doc deferral, pruning metadata."""

from __future__ import annotations

from sidecar.context.intent_classifier import Intent
from sidecar.context.types import SubgraphNode

from .candidate_pool import Candidate
from .scoring import RankerScoring


class BudgetPruner:
    def __init__(self, host):
        self.host = host

    def select_under_budget(
        self,
        pool: list[Candidate],
        target: SubgraphNode,
        query: str,
        intent: Intent,
        mechanism: str,
        required_roles: list[str],
        budget: int,
    ) -> tuple[list[Candidate], dict, str, list[dict], list[str]]:
        chosen: list[Candidate] = []
        spent = self.host.PREAMBLE_TOKENS + target.token_estimate
        base_budget = budget
        max_expansion_multiplier = 3.0
        effective_cap = int(base_budget * max_expansion_multiplier)
        # "Credit/debit" budget model:
        # - start with 2x credit (so total cap is ~3x base)
        # - consume credit when we go above base budget
        # - allow a bounded debit only when we're still closing required roles
        budget_balance = effective_cap - base_budget
        max_debit = int(base_budget * 0.25)
        pruned_details = []
        pruned_uids: set[str] = set()
        chosen_files = {target.file_path}
        fulfilled_roles = set(self.host.role_fulfilment.roles_of(target))
        trace_mode = RankerScoring.trace_dependency_gain_mode(mechanism, query)

        stopped_reason = "pool_exhausted"
        min_floor = self.host._INTENT_FLOORS.get(intent, 1200)
        min_gain = 0.12  # Threshold for stopping
        low_gain_floor = 0.02  # Protect against pure junk
        useful_candidates_seen = 0
        no_progress_streak = 0
        expansion_stall_limit = 8
        # For DI/trace questions, role sets can be "complete" while file-level
        # evidence still lives in other modules; do not take marginal-gain early
        # exit until the context spans enough distinct code files.
        min_trace_code_file_breadth = 3

        # Doc-tier deferral: when symbols still owe coverage breadth, hold docs
        # back so they don't crowd out role-filling code. A doc may "claim" a
        # role via supporting_roles and starve real graph candidates that would
        # bring in additional expected files from runtime/supporting modules.
        # IMPACT_ANALYSIS is exempt — its tier prior already favors docs.
        defer_docs = intent != Intent.IMPACT_ANALYSIS
        min_code_files_before_docs = 3
        deferred_docs: list[Candidate] = []

        def _is_code_file(c: Candidate) -> bool:
            return c.kind != "doc"

        def _record_pruned(
            c: Candidate,
            reason: str,
            *,
            gain: float | None = None,
            token_cost: int | None = None,
            candidate_roles: list[str] | None = None,
        ) -> None:
            if c.uid in pruned_uids:
                return
            pruned_uids.add(c.uid)
            blended_score = self.host.scoring.blended(c)
            roles = (
                candidate_roles
                if candidate_roles is not None
                else self.host.role_fulfilment.roles_of(c)
            )
            cost = token_cost if token_cost is not None else c.token_cost
            pruned_details.append(
                {
                    "kind": c.kind,
                    "uid": c.uid,
                    "name": c.name,
                    "file": c.file_path,
                    "file_path": c.file_path,
                    "relation": c.relation,
                    "role": c.evidence_role,
                    "supporting_roles": roles,
                    "gain": round(gain, 3) if gain is not None else None,
                    "tokens": cost,
                    "token_cost": cost,
                    "reason": reason,
                    "scores": {
                        "graph_score": round(c.graph_score, 3),
                        "semantic_score": round(c.semantic_score, 3),
                        "blended_score": round(blended_score, 3),
                        "intent_weight": round(c.intent_weight, 3),
                        "noise_factor": round(c.noise_factor, 3),
                    },
                    "graph_score": round(c.graph_score, 3),
                    "semantic_score": round(c.semantic_score, 3),
                    "blended_score": round(blended_score, 3),
                    "intent_weight": round(c.intent_weight, 3),
                    "noise_factor": round(c.noise_factor, 3),
                    "provenance": c.provenance,
                }
            )

        def _try_select(c: Candidate, gain: float, candidate_roles: list[str]) -> str | None:
            """Attempt to seat ``c``. Returns None on success, or a skip reason."""
            nonlocal spent, budget_balance
            potential_cost = c.token_cost
            if c.depth >= 2 and gain < 0.25:
                potential_cost = min(c.token_cost, 80)
            if potential_cost > int(base_budget * 1.1):
                _record_pruned(
                    c,
                    "over_budget",
                    gain=gain,
                    token_cost=potential_cost,
                    candidate_roles=candidate_roles,
                )
                return "over_budget"

            if spent + potential_cost > effective_cap:
                _record_pruned(
                    c,
                    "over_effective_cap",
                    gain=gain,
                    token_cost=potential_cost,
                    candidate_roles=candidate_roles,
                )
                return "over_effective_cap"
            if (
                spent + potential_cost > base_budget
                and c.kind == "doc"
                and "docs_or_concept" in candidate_roles
                and "docs_or_concept" not in fulfilled_roles
            ):
                compressed = min(potential_cost, 120)
                if spent + compressed <= effective_cap:
                    c.render_mode = "signature_only"
                    c.token_cost = compressed
                    potential_cost = compressed

            closes_missing_role = any(
                role in required_roles and role not in fulfilled_roles for role in candidate_roles
            )
            if trace_mode and c.kind != "doc" and (c.file_path or ""):
                already = {x.file_path for x in chosen if x.file_path} | {target.file_path or ""}
                if c.file_path not in already:
                    closes_missing_role = True
            if spent + potential_cost > base_budget:
                overflow = spent + potential_cost - base_budget
                projected_balance = budget_balance - overflow
                if projected_balance < -max_debit and not closes_missing_role:
                    _record_pruned(
                        c,
                        "budget_balance_debit_limit",
                        gain=gain,
                        token_cost=potential_cost,
                        candidate_roles=candidate_roles,
                    )
                    return "budget_balance_debit_limit"
                if not closes_missing_role and gain < min_gain:
                    _record_pruned(
                        c,
                        "expansion_low_gain",
                        gain=gain,
                        token_cost=potential_cost,
                        candidate_roles=candidate_roles,
                    )
                    return "expansion_low_gain"
                budget_balance = projected_balance

            if c.depth >= 2 and gain < 0.25:
                c.render_mode = "signature_only"
                c.token_cost = potential_cost

            chosen.append(c)
            spent += potential_cost
            chosen_files.add(c.file_path)
            fulfilled_roles.update(candidate_roles)
            # Role-closing candidates repay some budget debt/usage.
            if closes_missing_role and spent > base_budget:
                budget_balance = min(
                    effective_cap - base_budget,
                    budget_balance + int(min(potential_cost * 0.5, base_budget * 0.1)),
                )
            return None

        stop_index: int | None = None
        for idx, c in enumerate(pool):
            # Selection Gating Logic: Mechanism-Aware
            missing_roles = set(required_roles) - fulfilled_roles
            candidate_roles = self.host.role_fulfilment.selection_roles(
                c,
                target,
                query=query,
                mechanism=mechanism,
                intent=intent,
                required_roles=required_roles,
            )
            gain = self.host._calculate_marginal_gain(
                c,
                chosen,
                target,
                intent=intent,
                mechanism=mechanism,
                query=query,
                required_roles=required_roles,
                candidate_roles=candidate_roles,
            )
            fills_role = any(
                role in required_roles and role not in fulfilled_roles for role in candidate_roles
            )
            adds_new_trace_file = (
                trace_mode
                and c.kind != "doc"
                and (c.file_path or "")
                and c.file_path not in chosen_files
            )
            fills_role_or_trace = fills_role or adds_new_trace_file
            is_bridge = c.relation in (
                "DOC_BRIDGE",
                "SEMANTIC_HINT",
                "ROLE_BACKFILL",
            ) or self.host._has_role_backfill(c)
            is_strong_relation = c.relation in (
                "CALLS_DIRECT",
                "CALLS_SCOPED",
                "DEPENDS_ON",
                "IMPLEMENTS",
                "OVERRIDES",
            )

            # Determine if this candidate provides any unique reasoning signal
            is_useful = (
                fills_role
                or adds_new_trace_file
                or is_bridge
                or is_strong_relation
                or (self.host.scoring.blended(c) > 0.15)
            )

            if is_useful:
                useful_candidates_seen += 1
            # Docs almost never add trace file breadth; noisy junk we skip below is
            # not a failed expansion attempt. Counting them toward the stall streak
            # stopped trace_dependency runs early (expansion_no_progress) before
            # symbols deeper in the sorted pool (e.g. fastapi ``dependencies/*``).
            skips_noise_without_trace_role = (
                c.kind != "doc"
                and c.noise_factor < 1.0
                and intent != Intent.IMPACT_ANALYSIS
                and not fills_role_or_trace
            )
            if fills_role_or_trace:
                no_progress_streak = 0
            elif (trace_mode and c.kind == "doc") or skips_noise_without_trace_role:
                pass
            else:
                no_progress_streak += 1

            if (
                spent > base_budget
                and no_progress_streak >= expansion_stall_limit
                and not fills_role_or_trace
            ):
                stopped_reason = "expansion_no_progress"
                _record_pruned(
                    c,
                    "expansion_no_progress",
                    gain=gain,
                    candidate_roles=candidate_roles,
                )
                stop_index = idx
                break

            # Tests/examples/tutorial snippets can be useful for impact
            # analysis, but for behavior/flow questions they should not enter
            # merely because semantic/doc-bridge retrieval found similar names.
            # Let them through only when they fill a required role; otherwise
            # production code and focused docs should own the budget.
            is_noisy_code = c.kind != "doc" and c.noise_factor < 1.0
            if is_noisy_code:
                if intent == Intent.IMPACT_ANALYSIS:
                    _record_pruned(
                        c,
                        "impact_noise_penalty",
                        gain=gain,
                        candidate_roles=candidate_roles,
                    )
                    continue
                if not fills_role_or_trace:
                    _record_pruned(
                        c,
                        "noise_penalty",
                        gain=gain,
                        candidate_roles=candidate_roles,
                    )
                    continue

            # Hold docs aside until we have enough code coverage. Once code
            # breadth is met, the second pass below replays them in order.
            if defer_docs and c.kind == "doc":
                code_files_chosen = len({x.file_path for x in chosen if _is_code_file(x)})
                if code_files_chosen < min_code_files_before_docs:
                    deferred_docs.append(c)
                    continue

            if gain < min_gain:
                # Only break if floor is met AND no required roles are missing
                if spent >= min_floor and not missing_roles:
                    distinct_code_files = len({x.file_path for x in chosen if _is_code_file(x)})
                    if trace_mode and distinct_code_files < min_trace_code_file_breadth:
                        _record_pruned(
                            c,
                            "marginal_gain_deferred_trace_breadth",
                            gain=gain,
                            candidate_roles=candidate_roles,
                        )
                        continue
                    stopped_reason = "marginal_gain_threshold"
                    _record_pruned(
                        c,
                        "marginal_gain_threshold",
                        gain=gain,
                        candidate_roles=candidate_roles,
                    )
                    stop_index = idx
                    break

                if not is_useful:
                    _record_pruned(
                        c,
                        "low_utility",
                        gain=gain,
                        candidate_roles=candidate_roles,
                    )
                    continue
                if c.kind == "doc" and not fills_role:
                    _record_pruned(
                        c,
                        "low_marginal_gain",
                        gain=gain,
                        candidate_roles=candidate_roles,
                    )
                    continue
                # Unique role-fillers bypass the low-gain floor — without them
                # the role stays unfilled and downstream reasoning loses
                # critical evidence. A large/weak symbol with negative blended
                # score (e.g. fastapi `openapi` in applications.py: 256 tokens
                # of largely-static config logic) still earns its seat here.
                if gain < low_gain_floor and not fills_role_or_trace:
                    _record_pruned(
                        c,
                        "low_gain_floor",
                        gain=gain,
                        candidate_roles=candidate_roles,
                    )
                    continue

            _try_select(c, gain, candidate_roles)

        if stop_index is not None:
            for c in pool[stop_index + 1 :]:
                _record_pruned(c, "not_considered_after_threshold")
            for c in deferred_docs:
                _record_pruned(c, "deferred_doc_not_replayed_after_threshold")

        # Second pass: deferred docs, now that code-file breadth is established
        # (or the main pass exhausted the pool). Re-evaluate gain against the
        # current ``chosen`` set so docs that became redundant are still skipped.
        if deferred_docs and stopped_reason != "marginal_gain_threshold":
            for c in deferred_docs:
                if spent >= budget:
                    _record_pruned(c, "over_budget_after_doc_deferral")
                    continue
                candidate_roles = self.host.role_fulfilment.selection_roles(
                    c,
                    target,
                    query=query,
                    mechanism=mechanism,
                    intent=intent,
                    required_roles=required_roles,
                )
                gain = self.host._calculate_marginal_gain(
                    c,
                    chosen,
                    target,
                    intent=intent,
                    mechanism=mechanism,
                    query=query,
                    required_roles=required_roles,
                    candidate_roles=candidate_roles,
                )
                fills_role = any(
                    role in required_roles and role not in fulfilled_roles
                    for role in candidate_roles
                )
                if gain < min_gain and not fills_role:
                    _record_pruned(
                        c,
                        "deferred_doc_low_marginal_gain",
                        gain=gain,
                        candidate_roles=candidate_roles,
                    )
                    continue
                if gain < low_gain_floor and not fills_role:
                    _record_pruned(
                        c,
                        "deferred_doc_low_gain_floor",
                        gain=gain,
                        candidate_roles=candidate_roles,
                    )
                    continue
                _try_select(c, gain, candidate_roles)

        # Final docs rescue: if docs_or_concept remains unfilled, try to seat
        # one compact doc candidate before computing missing roles.
        if "docs_or_concept" in required_roles and "docs_or_concept" not in fulfilled_roles:
            chosen_uids = {c.uid for c in chosen}
            rescue_docs = [
                c for c in [*pool, *deferred_docs] if c.kind == "doc" and c.uid not in chosen_uids
            ]
            rescue_docs.sort(key=lambda cand: self.host.scoring.blended(cand), reverse=True)
            for c in rescue_docs:
                candidate_roles = self.host.role_fulfilment.selection_roles(
                    c,
                    target,
                    query=query,
                    mechanism=mechanism,
                    intent=intent,
                    required_roles=required_roles,
                )
                if "docs_or_concept" not in candidate_roles:
                    continue
                gain = self.host._calculate_marginal_gain(
                    c,
                    chosen,
                    target,
                    intent=intent,
                    mechanism=mechanism,
                    query=query,
                    required_roles=required_roles,
                    candidate_roles=candidate_roles,
                )
                if _try_select(c, gain, candidate_roles) is None:
                    break

        # If we ran out of useful candidates before hitting the floor, adjust the
        # stopped reason. For sparse marker APIs, the floor may be genuinely
        # unachievable from the graph.
        if stopped_reason == "pool_exhausted" and spent < min_floor:
            if not (set(required_roles) - fulfilled_roles):
                stopped_reason = "context_complete_below_floor"
            elif useful_candidates_seen < 3:
                stopped_reason = "floor_unfilled_sparse_target"
            else:
                stopped_reason = "floor_unfilled_no_useful_candidates"

        missing_roles_list = [r for r in required_roles if r not in fulfilled_roles]

        budget_info = {
            "limit": base_budget,
            "effective_cap": effective_cap,
            "budget_balance": budget_balance,
            "spent": spent,
            "floor": min_floor,
            "reserved": self.host.PREAMBLE_TOKENS,
            "pool_size": len(pool),
            "pruned": len(pruned_details),
        }
        return chosen, budget_info, stopped_reason, pruned_details, missing_roles_list
