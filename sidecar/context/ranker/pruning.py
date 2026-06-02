"""Budget selection loop: marginal gain, doc deferral, pruning metadata."""

from __future__ import annotations

from sidecar.context.intent_classifier import Intent
from sidecar.context.types import SubgraphNode

from .candidate_pool import Candidate


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
        *,
        floor_override: int | None = None,
        doc_first: bool = False,
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

        stopped_reason = "pool_exhausted"
        min_floor = floor_override or self.host._INTENT_FLOORS.get(intent, 1200)
        # Per-intent thresholds: NAVIGATION stops early (tight scope), DEBUGGING
        # and IMPACT_ANALYSIS expand wider (need more evidence).
        _INTENT_MIN_GAIN = {
            Intent.NAVIGATION: 0.20,
            Intent.EXPLORATION: 0.12,
            Intent.DEBUGGING: 0.08,
            Intent.NEW_FEATURE: 0.10,
            Intent.REFACTORING: 0.12,
            Intent.DESIGN_QUESTION: 0.10,
            Intent.IMPACT_ANALYSIS: 0.08,
        }
        _INTENT_STALL_LIMIT = {
            Intent.NAVIGATION: 4,
            Intent.EXPLORATION: 8,
            Intent.DEBUGGING: 12,
            Intent.NEW_FEATURE: 10,
            Intent.REFACTORING: 8,
            Intent.DESIGN_QUESTION: 10,
            Intent.IMPACT_ANALYSIS: 12,
        }
        min_gain = _INTENT_MIN_GAIN.get(intent, 0.12)
        low_gain_floor = 0.02  # Protect against pure junk
        useful_candidates_seen = 0
        no_progress_streak = 0
        expansion_stall_limit = _INTENT_STALL_LIMIT.get(intent, 8)

        # Doc-tier deferral: hold docs back until code coverage is established,
        # so they don't crowd out role-filling graph symbols.
        # Intents where docs ARE the primary signal (design, new feature) should
        # NOT defer — they need docs early to fill concept/architecture roles.
        # IMPACT_ANALYSIS is also exempt (tier prior already favors docs/tests).
        _DOC_FIRST_INTENTS = (Intent.DESIGN_QUESTION, Intent.NEW_FEATURE, Intent.IMPACT_ANALYSIS)
        defer_docs = intent not in _DOC_FIRST_INTENTS and not doc_first
        min_code_files_before_docs = 3
        deferred_docs: list[Candidate] = []
        # P@5 treats the target + first four graph symbols as the "head".
        # Bridge candidates are useful for role recall, but several bridges in
        # that head tend to crowd out concrete call/depends chains. Defer extras
        # until the head has one bridge and enough structural symbols.
        head_symbol_slots = 4
        head_bridge_cap = 1
        deferred_head_bridges: list[Candidate] = []
        head_bridge_deferred = 0
        head_bridge_replayed = 0
        head_backfill_deferred = 0
        head_backfill_replayed = 0
        defer_head_bridges = False

        def _is_code_file(c: Candidate) -> bool:
            return c.kind != "doc"

        def _selected_symbol_count() -> int:
            return sum(1 for c in chosen if c.kind != "doc")

        def _is_role_backfill(c: Candidate) -> bool:
            return c.kind != "doc" and (
                c.relation == "ROLE_BACKFILL" or self.host._has_role_backfill(c)
            )

        def _is_head_bridge(c: Candidate) -> bool:
            provenance = "".join(str(step) for step in c.provenance)
            return c.kind != "doc" and (
                c.relation in ("DOC_BRIDGE", "SEMANTIC_HINT", "ROLE_BACKFILL")
                or self.host._has_role_backfill(c)
                or "doc-bridge" in provenance
            )

        def _is_marker_chain(c: Candidate) -> bool:
            return c.kind != "doc" and self.host._has_marker_chain(c)

        def _is_relevant_marker_chain(c: Candidate) -> bool:
            return c.kind != "doc" and self.host.role_fulfilment.marker_chain_roles_are_relevant(
                c,
                required_roles,
            )

        def _chosen_head_bridge_count() -> int:
            head_symbols: list[Candidate] = []
            for selected in chosen:
                if selected.kind == "doc":
                    continue
                head_symbols.append(selected)
                if len(head_symbols) >= head_symbol_slots:
                    break
            return sum(1 for selected in head_symbols if _is_head_bridge(selected))

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
                if not closes_missing_role and gain < min_gain and c.relation != "MANDATORY_CALLEE":
                    _record_pruned(
                        c,
                        "expansion_low_gain",
                        gain=gain,
                        token_cost=potential_cost,
                        candidate_roles=candidate_roles,
                    )
                    return "expansion_low_gain"
                budget_balance = projected_balance

            if c.depth >= 2 and gain < 0.25 and c.relation != "MANDATORY_CALLEE":
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

        target_roles_for_marker_chain = set(self.host.role_fulfilment.roles_of(target))
        required_role_set = set(required_roles)
        marker_chain_required = (
            "dependency_solver" in required_role_set
            and bool(target_roles_for_marker_chain & {"api_surface", "config_surface"})
            and any(_is_relevant_marker_chain(candidate) for candidate in pool)
        )

        def _marker_chain_pending_from(idx: int) -> bool:
            if not marker_chain_required:
                return False
            if any(_is_relevant_marker_chain(selected) for selected in chosen):
                return False
            return any(_is_relevant_marker_chain(candidate) for candidate in pool[idx:])

        def _replay_deferred_head_bridges(*, force: bool = False) -> bool:
            """Seat deferred bridges after the symbol head, preserving recall."""
            nonlocal head_bridge_replayed, head_backfill_replayed
            if not deferred_head_bridges:
                return False
            if not force and _selected_symbol_count() < head_symbol_slots:
                return False

            replaying = list(deferred_head_bridges)
            deferred_head_bridges.clear()
            selected_any = False
            chosen_uids = {c.uid for c in chosen}
            for c in replaying:
                if c.uid in chosen_uids:
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
                is_useful = (
                    fills_role
                    or _is_head_bridge(c)
                    or (self.host.scoring.blended(c) > 0.15)
                )
                if gain < min_gain:
                    if not is_useful:
                        _record_pruned(
                            c,
                            "deferred_head_bridge_low_utility",
                            gain=gain,
                            candidate_roles=candidate_roles,
                        )
                        continue
                    if gain < low_gain_floor and not fills_role:
                        _record_pruned(
                            c,
                            "deferred_head_bridge_low_gain_floor",
                            gain=gain,
                            candidate_roles=candidate_roles,
                        )
                        continue

                if _try_select(c, gain, candidate_roles) is None:
                    chosen_uids.add(c.uid)
                    head_bridge_replayed += 1
                    if _is_role_backfill(c):
                        head_backfill_replayed += 1
                    selected_any = True
            return selected_any

        stop_index: int | None = None
        for idx, c in enumerate(pool):
            _replay_deferred_head_bridges()
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
            is_bridge = c.relation in (
                "DOC_BRIDGE",
                "SEMANTIC_HINT",
                "ROLE_BACKFILL",
            ) or self.host._has_role_backfill(c)
            is_mandatory_callee = c.relation == "MANDATORY_CALLEE"
            is_strong_relation = (
                c.relation
                in (
                    "CALLS_DIRECT",
                    "CALLS_SCOPED",
                    "DEPENDS_ON",
                    "IMPLEMENTS",
                    "OVERRIDES",
                )
                or is_mandatory_callee
            )

            # Determine if this candidate provides any unique reasoning signal
            is_useful = (
                fills_role
                or is_bridge
                or is_strong_relation
                or is_mandatory_callee
                or _is_relevant_marker_chain(c)
                or (self.host.scoring.blended(c) > 0.15)
            )

            if is_useful:
                useful_candidates_seen += 1
            # Noisy junk we skip below is not a failed expansion attempt.
            skips_noise_without_role = (
                c.kind != "doc"
                and c.noise_factor < 1.0
                and intent != Intent.IMPACT_ANALYSIS
                and not fills_role
            )
            # Redundant symbols from a file already selected do not represent
            # failed expansion — they are duplicates from the same module.
            is_redundant_same_file = (
                c.kind != "doc"
                and bool(c.file_path)
                and c.file_path in chosen_files
                and not fills_role
            )
            if fills_role:
                no_progress_streak = 0
            elif skips_noise_without_role or is_redundant_same_file:
                pass
            else:
                no_progress_streak += 1

            if (
                spent > base_budget
                and no_progress_streak >= expansion_stall_limit
                and not fills_role
            ):
                deferred_fills_missing = any(
                    any(
                        role in required_roles and role not in fulfilled_roles
                        for role in self.host.role_fulfilment.selection_roles(
                            deferred,
                            target,
                            query=query,
                            mechanism=mechanism,
                            intent=intent,
                            required_roles=required_roles,
                        )
                    )
                    for deferred in deferred_head_bridges
                )
                if deferred_fills_missing and _replay_deferred_head_bridges(force=True):
                    no_progress_streak = 0
                    continue
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
            if is_noisy_code and not is_mandatory_callee:
                if intent == Intent.IMPACT_ANALYSIS:
                    if not fills_role:
                        _record_pruned(
                            c,
                            "impact_noise_penalty",
                            gain=gain,
                            candidate_roles=candidate_roles,
                        )
                        continue
                if not fills_role and not _is_relevant_marker_chain(c):
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

            impact_context_complete = (
                intent == Intent.IMPACT_ANALYSIS
                and spent >= min_floor
                and not missing_roles
                and not fills_role
                and not any(str(step).startswith("impact-") for step in c.provenance)
            )
            if impact_context_complete:
                stopped_reason = "impact_context_complete"
                _record_pruned(
                    c,
                    "impact_context_complete",
                    gain=gain,
                    candidate_roles=candidate_roles,
                )
                stop_index = idx
                break

            if intent != Intent.IMPACT_ANALYSIS and not missing_roles and spent >= min_floor:
                if not fills_role:
                    if _marker_chain_pending_from(idx):
                        pass
                    else:
                        stopped_reason = "role_complete"
                        _record_pruned(
                            c,
                            "role_complete",
                            gain=gain,
                            candidate_roles=candidate_roles,
                        )
                        stop_index = idx
                        break

            if gain < min_gain:
                # Only break if floor is met AND no required roles are missing
                if spent >= min_floor and not missing_roles:
                    if _marker_chain_pending_from(idx):
                        pass
                    else:
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
                if gain < low_gain_floor and not fills_role and not is_mandatory_callee:
                    _record_pruned(
                        c,
                        "low_gain_floor",
                        gain=gain,
                        candidate_roles=candidate_roles,
                    )
                    continue

            if (
                defer_head_bridges
                and _is_head_bridge(c)
                and _selected_symbol_count() < head_symbol_slots
                and _chosen_head_bridge_count() >= head_bridge_cap
            ):
                deferred_head_bridges.append(c)
                head_bridge_deferred += 1
                if _is_role_backfill(c):
                    head_backfill_deferred += 1
                continue

            selected_reason = _try_select(c, gain, candidate_roles)
            if selected_reason is None:
                _replay_deferred_head_bridges()

        if stop_index is None and deferred_head_bridges:
            _replay_deferred_head_bridges(force=True)

        if stop_index is not None:
            if deferred_head_bridges:
                deferred_fills_missing = any(
                    any(
                        role in required_roles and role not in fulfilled_roles
                        for role in self.host.role_fulfilment.selection_roles(
                            deferred,
                            target,
                            query=query,
                            mechanism=mechanism,
                            intent=intent,
                            required_roles=required_roles,
                        )
                    )
                    for deferred in deferred_head_bridges
                )
                if deferred_fills_missing:
                    _replay_deferred_head_bridges(force=True)
            for c in pool[stop_index + 1 :]:
                _record_pruned(c, "not_considered_after_threshold")
            for c in deferred_docs:
                _record_pruned(c, "deferred_doc_not_replayed_after_threshold")
            for c in deferred_head_bridges:
                _record_pruned(c, "deferred_head_bridge_not_replayed_after_threshold")

        # Second pass: deferred docs, now that code-file breadth is established
        # (or the main pass exhausted the pool). Re-evaluate gain against the
        # current ``chosen`` set so docs that became redundant are still skipped.
        if deferred_docs and stopped_reason not in ("marginal_gain_threshold", "role_complete"):
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
            "head_bridge_deferred": head_bridge_deferred,
            "head_bridge_replayed": head_bridge_replayed,
            "head_backfill_deferred": head_backfill_deferred,
            "head_backfill_replayed": head_backfill_replayed,
        }
        return chosen, budget_info, stopped_reason, pruned_details, missing_roles_list
