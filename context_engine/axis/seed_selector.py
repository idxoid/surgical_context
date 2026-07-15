"""Evidence-aware seed selection before the expensive context graph walk.

Retrieval is deliberately recall-oriented: one symbol can be emitted by
several intent roles and by vector, lexical, and semantic-span channels.  A
plain per-role ``[:N]`` loses that consensus and can discard explicit symbol
or span evidence.  This module first aggregates every occurrence by UID, then
applies a *soft* per-role cap:

* explicit anchors and the best exact symbol match per role are hard reserves;
* multi-intent-role and multi-channel support is retained as telemetry;
* the remaining slots preserve the established ranked fill;
* hard reserves overflow only when their own count exceeds the soft cap.

The output stays a list of ``RoleCandidate`` objects so the graph/context
pipeline remains unchanged.  ``SeedSelectionTrace`` provides low-cardinality
telemetry for benchmark and request diagnostics.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace

from context_engine.axis.role_retrieval import RoleCandidate

_ANCHOR_ROLES = frozenset({"anchor_symbol", "overlay_anchor"})


@dataclass(frozen=True)
class SeedSelectionTrace:
    input_occurrences: int
    unique_candidates: int
    selected_candidates: int
    dropped_candidates: int
    per_role_soft_cap: int | None
    reason_counts: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "input_occurrences": self.input_occurrences,
            "unique_candidates": self.unique_candidates,
            "selected_candidates": self.selected_candidates,
            "dropped_candidates": self.dropped_candidates,
            "per_role_soft_cap": self.per_role_soft_cap,
            "reason_counts": dict(self.reason_counts),
        }


@dataclass
class _Aggregate:
    candidate: RoleCandidate
    source_roles: list[str]
    source_role_set: set[str]
    exact_source_roles: set[str]


def _ordered_role_keys(
    raw_by_role: dict[str, list[RoleCandidate]],
    intent_roles: Iterable[str],
) -> list[str]:
    intent_order = list(dict.fromkeys(role for role in intent_roles if role))
    seen = set(intent_order)
    return intent_order + [role for role in raw_by_role if role not in seen]


def _ordered_union(left: Iterable[str], right: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys([*left, *right]))


def _merge_candidate(
    existing: RoleCandidate,
    incoming: RoleCandidate,
    *,
    source_roles: Iterable[str],
) -> RoleCandidate:
    spans = tuple(sorted(set(existing.retrieval_spans) | set(incoming.retrieval_spans)))
    roles = _ordered_union(
        existing.supporting_roles or (existing.role,),
        [*source_roles, *(incoming.supporting_roles or (incoming.role,))],
    )
    # Preserve the first occurrence's ranking/structural payload exactly. The
    # historical flattening contract did so, and these values later drive
    # Token Credit. Allowing another role's score to escape here silently
    # changes post-graph packing.
    return replace(
        existing,
        retrieval_channels=_ordered_union(
            existing.retrieval_channels, incoming.retrieval_channels
        ),
        retrieval_spans=spans,
        exact_symbol_match=existing.exact_symbol_match or incoming.exact_symbol_match,
        supporting_roles=roles,
        selection_reasons=_ordered_union(
            existing.selection_reasons, incoming.selection_reasons
        ),
    )


def _evidence_reasons(
    candidate: RoleCandidate,
    *,
    active_intent_roles: frozenset[str],
) -> tuple[str, ...]:
    reasons: list[str] = []
    supporting_roles = frozenset(candidate.supporting_roles or (candidate.role,))
    if candidate.role in _ANCHOR_ROLES or supporting_roles & _ANCHOR_ROLES:
        reasons.append("explicit_anchor")
    if candidate.exact_symbol_match:
        reasons.append("exact_symbol")
    if candidate.retrieval_spans:
        reasons.append("retrieval_span")
    if len(supporting_roles & active_intent_roles) >= 2:
        reasons.append("multi_intent_role")
    if len(candidate.retrieval_channels) >= 2:
        reasons.append("multi_channel")
    return tuple(reasons)


def _is_hard_reserved(reasons: Iterable[str]) -> bool:
    return "explicit_anchor" in set(reasons)


def select_context_seeds(
    raw_by_role: dict[str, list[RoleCandidate]],
    intent_roles: Iterable[str],
    *,
    per_role_soft_cap: int | None,
    exact_reserve_per_role: int = 1,
) -> tuple[list[RoleCandidate], SeedSelectionTrace]:
    """Aggregate retrieval evidence and select context seeds.

    ``per_role_soft_cap=None`` is the diagnostic uncapped arm. A positive cap
    bounds candidates per source role. Explicit anchors are never dropped and
    can overflow the cap; at most one otherwise-missing exact hit consumes a
    normal role slot. Semantic spans and role/channel consensus remain
    telemetry-only until dedicated gold validates stronger selection policy.
    """
    if per_role_soft_cap is not None and per_role_soft_cap < 1:
        raise ValueError("per_role_soft_cap must be >= 1 or None")
    if exact_reserve_per_role < 0:
        raise ValueError("exact_reserve_per_role must be >= 0")

    intent_role_list = list(intent_roles)
    ordered_roles = _ordered_role_keys(raw_by_role, intent_role_list)
    active_intent_roles = frozenset(role for role in intent_role_list if role)
    aggregates: dict[str, _Aggregate] = {}
    memberships: dict[str, list[str]] = {role: [] for role in ordered_roles}
    input_occurrences = 0
    missing_uid_counter = 0

    for role in ordered_roles:
        seen_in_role: set[str] = set()
        for candidate in raw_by_role.get(role) or ():
            input_occurrences += 1
            key = candidate.uid
            if not key:
                missing_uid_counter += 1
                key = f"__missing_uid__:{missing_uid_counter}"
            if key not in seen_in_role:
                memberships[role].append(key)
                seen_in_role.add(key)
            source_roles = _ordered_union(
                (role,), candidate.supporting_roles or (candidate.role,)
            )
            aggregate = aggregates.get(key)
            if aggregate is None:
                aggregates[key] = _Aggregate(
                    candidate=replace(candidate, supporting_roles=source_roles),
                    source_roles=list(source_roles),
                    source_role_set=set(source_roles),
                    exact_source_roles={role} if candidate.exact_symbol_match else set(),
                )
                continue
            new_roles = [item for item in source_roles if item not in aggregate.source_role_set]
            aggregate.source_roles.extend(new_roles)
            aggregate.source_role_set.update(new_roles)
            if candidate.exact_symbol_match:
                aggregate.exact_source_roles.add(role)
            aggregate.candidate = _merge_candidate(
                aggregate.candidate,
                candidate,
                source_roles=source_roles,
            )

    reasons_by_key = {
        key: _evidence_reasons(
            aggregate.candidate,
            active_intent_roles=active_intent_roles,
        )
        for key, aggregate in aggregates.items()
    }

    selected_keys: list[str] = []
    selected_set: set[str] = set()
    selected_reasons: dict[str, list[str]] = {}

    def admit(key: str, reason: str) -> None:
        bucket = selected_reasons.setdefault(key, [])
        for evidence_reason in reasons_by_key[key]:
            if evidence_reason not in bucket:
                bucket.append(evidence_reason)
        if reason not in bucket:
            bucket.append(reason)
        if key in selected_set:
            return
        selected_set.add(key)
        selected_keys.append(key)

    if per_role_soft_cap is None:
        for role in ordered_roles:
            for key in memberships[role]:
                admit(key, "uncapped")
    else:
        for role in ordered_roles:
            role_keys = memberships[role]
            ranked_slice = set(role_keys[:per_role_soft_cap])
            anchor_keys = [
                key for key in role_keys if _is_hard_reserved(reasons_by_key[key])
            ]
            exact_already_covered = any(
                role in aggregates[key].exact_source_roles
                for key in ranked_slice | selected_set
            )
            exact_keys = (
                []
                if exact_already_covered
                else [
                    key
                    for key in role_keys
                    if role in aggregates[key].exact_source_roles
                    and key not in anchor_keys
                    and key not in selected_set
                    and key not in ranked_slice
                ][:exact_reserve_per_role]
            )
            hard_keys = [key for key in [*anchor_keys, *exact_keys] if key not in selected_set]
            target = max(per_role_soft_cap, len(hard_keys))
            selected_for_role: list[str] = list(hard_keys)
            for key in role_keys:
                if len(selected_for_role) >= target:
                    break
                if key not in selected_for_role:
                    selected_for_role.append(key)
            hard_set = set(hard_keys)
            for key in selected_for_role:
                admit(key, "hard_reserve" if key in hard_set else "ranked_fill")

    selected: list[RoleCandidate] = []
    for key in selected_keys:
        aggregate = aggregates[key]
        selected.append(
            replace(
                aggregate.candidate,
                supporting_roles=tuple(aggregate.source_roles),
                selection_reasons=tuple(selected_reasons.get(key, ())),
            )
        )

    reason_counts = Counter(
        reason
        for candidate in selected
        for reason in candidate.selection_reasons
    )
    trace = SeedSelectionTrace(
        input_occurrences=input_occurrences,
        unique_candidates=len(aggregates),
        selected_candidates=len(selected),
        dropped_candidates=max(0, len(aggregates) - len(selected)),
        per_role_soft_cap=per_role_soft_cap,
        reason_counts=dict(sorted(reason_counts.items())),
    )
    return selected, trace


__all__ = ["SeedSelectionTrace", "select_context_seeds"]
