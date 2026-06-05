"""Mechanism routing, required roles, and candidate role assignment."""

from __future__ import annotations

from collections import Counter

from sidecar.context.intent_classifier import Intent
from sidecar.context.mechanism_registry import (
    determine_preloaded_mechanism,
    pick_mechanism_by_role_overlap,
    required_roles_for_mechanism,
)
from sidecar.context.role_taxonomy import infer_supporting_roles, normalize_role, normalize_roles

from .candidate_pool import Candidate

_ROLE_PLAN_EXCLUDED_TARGET_ROLES = {
    "docs_or_concept",
    "impact_runtime",
    "impact_public_api",
    "impact_test_surface",
    "orphan",
}


class RoleFulfilment:
    def __init__(self, host):
        self.host = host

    def role_of(self, c: Candidate | object) -> str:
        explicit = getattr(c, "evidence_role", "")
        if explicit:
            return normalize_role(explicit)
        return normalize_role(self.infer_role(c))

    def supporting_roles_of(self, c: Candidate | object) -> list[str]:
        explicit = normalize_roles(getattr(c, "supporting_roles", []) or [])
        uid = getattr(c, "uid", "") or ""
        primary = self.role_of(c)
        inferred = infer_supporting_roles(
            file_path=getattr(c, "file_path", "") or "",
            primary_role=primary,
            name=getattr(c, "name", "") or "",
            kind=getattr(c, "symbol_kind", "") or getattr(c, "kind", "") or "",
        )
        for role in self.host._derived_supporting_roles_by_uid.get(uid, []):
            if role != primary:
                inferred.append(role)
        if self._is_public_primary_target(c):
            inferred.extend(["api_surface", "impact_public_api"])
            inferred.extend(self._public_target_surface_roles(c))
        return normalize_roles([*explicit, *inferred])

    def roles_of(self, c: Candidate | object) -> list[str]:
        return normalize_roles([self.role_of(c), *self.supporting_roles_of(c)])

    def selection_roles(
        self,
        c: Candidate,
        target,
        *,
        query: str,
        mechanism: str,
        intent: Intent,
        required_roles: list[str],
    ) -> list[str]:
        roles = self.roles_of(c)
        if (
            c.kind == "doc"
            or c.noise_factor >= 1.0
            or intent == Intent.IMPACT_ANALYSIS
            or mechanism == "workspace_structure"
            or self.host._has_role_backfill(c)
            or self.marker_chain_roles_are_relevant(c, required_roles)
            or self.host.scoring.candidate_matches_query_topic(c, target, query=query)
        ):
            return roles

        required = set(normalize_roles(required_roles))
        return [role for role in roles if role not in required]

    def marker_chain_roles_are_relevant(
        self,
        c: Candidate | object,
        required_roles: list[str],
    ) -> bool:
        if not self.host._has_marker_chain(c):
            return False
        required = set(normalize_roles(required_roles)) - {"docs_or_concept"}
        if not required:
            return False
        roles = set(self.roles_of(c))
        if "dependency_solver" in required and "dependency_solver" in roles:
            return True
        return len(roles & required) >= 2

    def candidate_matches_any_role(
        self,
        c: Candidate | object,
        required_roles: list[str],
    ) -> bool:
        required = set(normalize_roles(required_roles))
        return any(role in required for role in self.roles_of(c))

    def infer_role(self, c: Candidate | object) -> str:
        if getattr(c, "kind", "") == "doc":
            return "docs_or_concept"

        uid = getattr(c, "uid", "") or ""
        primary = self.host._derived_primary_role_by_uid.get(uid)
        if primary:
            return normalize_role(primary)
        return "supporting_surface"


    def _is_public_primary_target(self, c: Candidate | object) -> bool:
        """A directly requested public symbol is itself an API surface."""
        if getattr(c, "relation", "") not in {"target", "target_signature_only"}:
            return False
        name = (getattr(c, "name", "") or "").strip()
        if not name or name.startswith("_"):
            return False
        kind = (getattr(c, "symbol_kind", "") or getattr(c, "kind", "") or "").lower()
        if kind not in {
            "class",
            "function",
            "method",
            "object_api",
            "module",
            "variable",
            "proxy_binding",
        }:
            return False
        path = (getattr(c, "file_path", "") or "").replace("\\", "/").lower()
        if any(marker in path for marker in ("/tests/", "/test_", "/docs/", "/examples/")):
            return False
        return True

    def _public_target_surface_roles(self, c: Candidate | object) -> list[str]:
        uid = getattr(c, "uid", "") or ""
        fan_loader = getattr(self.host, "_structural_fan_for_uid", None)
        if uid and callable(fan_loader):
            fan = fan_loader(uid)
        else:
            fan = self.host._structural_fan_by_uid.get(uid, {}) if uid else {}
        call_fan_out = float(fan.get("call_fan_out", 0.0) or 0.0)
        call_fan_in = float(fan.get("call_fan_in", 0.0) or 0.0)
        type_fan_in = float(fan.get("type_fan_in", 0.0) or 0.0)
        handle_fan_in = float(fan.get("handle_fan_in", 0.0) or 0.0)
        alias_api_fan_out = float(fan.get("alias_api_fan_out", 0.0) or 0.0)
        api_fan_out = float(fan.get("api_fan_out", 0.0) or 0.0)
        external_construct_coref_fan_out = float(
            fan.get("external_construct_coref_fan_out", 0.0) or 0.0
        )
        proxy_attr_resolve_fan_out = float(
            fan.get("proxy_attr_resolve_fan_out", 0.0) or 0.0
        )
        if (
            call_fan_out <= 0.0
            and type_fan_in <= 0.0
            and handle_fan_in <= 0.0
            and alias_api_fan_out <= 0.0
            and api_fan_out <= 0.0
            and external_construct_coref_fan_out <= 0.0
            and proxy_attr_resolve_fan_out <= 0.0
        ):
            return []

        kind = (getattr(c, "symbol_kind", "") or getattr(c, "kind", "") or "").lower()
        roles: list[str] = []
        if call_fan_out > 0.0:
            roles.append("composition_surface")
            if call_fan_out >= call_fan_in:
                roles.append("factory_surface")
        if kind in {"variable", "object_api", "module"} and alias_api_fan_out > 0.0:
            roles.append("factory_surface")
            roles.append("runtime_surface")
        if kind in {"variable", "object_api", "module"} and api_fan_out > 0.0:
            roles.append("composition_surface")
            roles.append("runtime_surface")
            if api_fan_out >= 2.0:
                roles.append("factory_surface")
        if kind in {"variable", "object_api", "module"} and external_construct_coref_fan_out > 0.0:
            roles.append("factory_surface")
        if kind == "proxy_binding" and proxy_attr_resolve_fan_out > 0.0:
            roles.append("binding_surface")
        if kind in {"variable", "object_api"} and (call_fan_in > 0.0 or type_fan_in > 0.0):
            roles.append("representation_surface")
        if handle_fan_in > 0.0:
            roles.append("executor")
        return normalize_roles(roles)

    def canonical_role_for_symbol_uid(self, uid: str) -> str:
        return self.host._derived_primary_role_by_uid.get(uid, "")

    def pass1_roles_for_symbol_uid(self, uid: str) -> list[str]:
        """Primary + Pass-1 supporting roles persisted on the symbol."""
        if not uid:
            return []
        primary = self.host._derived_primary_role_by_uid.get(uid, "")
        supporting = self.host._derived_supporting_roles_by_uid.get(uid, [])
        roles = ([primary, *supporting] if primary else list(supporting))
        return normalize_roles(roles)

    def one_hop_connected_symbol_uids(self, target_uid: str, *, limit: int = 48) -> list[str]:
        query = """
        MATCH (t:Symbol {uid: $uid})-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT|HAS_API|INHERITED_API|DECORATED_BY|USES_TYPE|INJECTS|INSTANTIATES|HANDLES|RESOLVES_ATTR]-(n:Symbol)
        WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
        RETURN DISTINCT n.uid AS uid
        LIMIT $limit
        """
        try:
            with self.host.db.driver.session() as session:
                rows = session.run(
                    query,
                    uid=target_uid,
                    workspace_id=self.host.workspace_id,
                    limit=limit,
                )
                return [str(r["uid"]) for r in rows if r.get("uid")]
        except Exception:
            return []

    def delegation_callee_uids(self, caller_uid: str, *, limit: int = 48) -> list[str]:
        """Directed CALLS-out callees — what a symbol *delegates to*.

        Used to follow a facade through its delegation (``FastAPI.get`` →
        ``APIRouter.get``) so the role behind a thin delegator is observed.
        Excludes CALLS_GUESS to keep the hop precise.
        """
        query = """
        MATCH (t:Symbol {uid: $uid})-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED]->(n:Symbol)
        WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id AND n.uid <> $uid
        RETURN DISTINCT n.uid AS uid
        LIMIT $limit
        """
        try:
            with self.host.db.driver.session() as session:
                rows = session.run(
                    query,
                    uid=caller_uid,
                    workspace_id=self.host.workspace_id,
                    limit=limit,
                )
                return [str(r["uid"]) for r in rows if r.get("uid")]
        except Exception:
            return []

    def delegation_callee_uid_map(
        self, caller_uids: list[str], *, limit: int = 48
    ) -> dict[str, list[str]]:
        unique_uids = [uid for uid in dict.fromkeys(caller_uids) if uid]
        if not unique_uids:
            return {}
        if len(unique_uids) == 1:
            return {
                unique_uids[0]: self.delegation_callee_uids(
                    unique_uids[0],
                    limit=limit,
                )
            }
        query = """
        UNWIND $uids AS uid
        MATCH (t:Symbol {uid: uid})
        CALL {
            WITH t
            MATCH (t)-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED]->(n:Symbol)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
              AND n.uid <> t.uid
            RETURN DISTINCT n.uid AS neighbor_uid
            LIMIT $limit
        }
        RETURN t.uid AS uid, collect(neighbor_uid) AS neighbor_uids
        """
        try:
            with self.host.db.driver.session() as session:
                rows = session.run(
                    query,
                    uids=unique_uids,
                    workspace_id=self.host.workspace_id,
                    limit=limit,
                )
                return {
                    str(r["uid"]): [str(uid) for uid in r["neighbor_uids"] if uid]
                    for r in rows
                    if r.get("uid")
                }
        except Exception:
            return {
                uid: self.delegation_callee_uids(uid, limit=limit)
                for uid in unique_uids
            }

    def type_contract_neighbor_uids(self, symbol_uid: str, *, limit: int = 24) -> list[str]:
        """Outgoing USES_TYPE neighbors — type contract surfaces for a symbol."""
        query = """
        MATCH (t:Symbol {uid: $uid})-[r:USES_TYPE]->(n:Symbol)
        WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id AND n.uid <> $uid
        RETURN DISTINCT n.uid AS uid
        LIMIT $limit
        """
        try:
            with self.host.db.driver.session() as session:
                rows = session.run(
                    query,
                    uid=symbol_uid,
                    workspace_id=self.host.workspace_id,
                    limit=limit,
                )
                return [str(r["uid"]) for r in rows if r.get("uid")]
        except Exception:
            return []

    def structural_contract_neighbor_uids(
        self, symbol_uid: str, *, limit: int = 24
    ) -> list[str]:
        """Outgoing contract/topology neighbors used for local role closure.

        These relations are all code-derived: construction targets, base/type
        contracts, registry-owned handlers, and public API members. This keeps
        adaptive role planning aligned with the same graph facts the ranker can
        traverse, without introducing semantic name rules.
        """
        query = """
        MATCH (t:Symbol {uid: $uid})-[r:USES_TYPE|INSTANTIATES|DEPENDS_ON|HANDLES|HAS_API|INHERITED_API]->(n:Symbol)
        WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id AND n.uid <> $uid
        RETURN DISTINCT n.uid AS uid
        LIMIT $limit
        """
        try:
            with self.host.db.driver.session() as session:
                rows = session.run(
                    query,
                    uid=symbol_uid,
                    workspace_id=self.host.workspace_id,
                    limit=limit,
                )
                return [str(r["uid"]) for r in rows if r.get("uid")]
        except Exception:
            return []

    def structural_contract_neighbor_uid_map(
        self, symbol_uids: list[str], *, limit: int = 24
    ) -> dict[str, list[str]]:
        unique_uids = [uid for uid in dict.fromkeys(symbol_uids) if uid]
        if not unique_uids:
            return {}
        if len(unique_uids) == 1:
            return {
                unique_uids[0]: self.structural_contract_neighbor_uids(
                    unique_uids[0],
                    limit=limit,
                )
            }
        query = """
        UNWIND $uids AS uid
        MATCH (t:Symbol {uid: uid})
        CALL {
            WITH t
            MATCH (t)-[r:USES_TYPE|INSTANTIATES|DEPENDS_ON|HANDLES|HAS_API|INHERITED_API]->(n:Symbol)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
              AND n.uid <> t.uid
            RETURN DISTINCT n.uid AS neighbor_uid
            LIMIT $limit
        }
        RETURN t.uid AS uid, collect(neighbor_uid) AS neighbor_uids
        """
        try:
            with self.host.db.driver.session() as session:
                rows = session.run(
                    query,
                    uids=unique_uids,
                    workspace_id=self.host.workspace_id,
                    limit=limit,
                )
                return {
                    str(r["uid"]): [str(uid) for uid in r["neighbor_uids"] if uid]
                    for r in rows
                    if r.get("uid")
                }
        except Exception:
            return {
                uid: self.structural_contract_neighbor_uids(uid, limit=limit)
                for uid in unique_uids
            }

    def determine_mechanism_structural(self, target) -> str:
        if not self.host._derived_primary_role_by_uid:
            return ""
        uid = getattr(target, "uid", "") or ""
        if not uid:
            return ""
        # Barrel __init__ files aggregate the entire package as neighbors, so
        # role overlap scores reflect the package contents rather than the
        # target's own mechanism — skip structural detection for them.
        file_path = (getattr(target, "file_path", "") or "").replace("\\", "/")
        if file_path.endswith("/__init__.py"):
            return ""
        neighbors = self.host._one_hop_connected_symbol_uids(uid, limit=48)
        roles_observed: list[str] = []
        for sym_uid in [uid, *neighbors]:
            roles_observed.extend(self.pass1_roles_for_symbol_uid(sym_uid))
        target_role = self.canonical_role_for_symbol_uid(uid)
        return pick_mechanism_by_role_overlap(
            roles_observed,
            target_role=target_role,
            role_catalog=self.host.role_catalog or None,
        )

    def determine_mechanism(self, target, query: str = "") -> str:
        preloaded = determine_preloaded_mechanism(target, query=query)
        if preloaded:
            return preloaded
        structural = self.determine_mechanism_structural(target)
        if structural:
            return structural
        return "generic"

    def get_required_roles(self, mechanism: str, *, target=None) -> list[str]:
        target_roles = self.target_role_plan_roles(target)
        roles = []
        if mechanism.startswith("auto:"):
            roles = self.adaptive_role_plan(target=target)
        else:
            roles = required_roles_for_mechanism(
                mechanism,
                role_catalog=self.host.role_catalog or None,
            )
            if not roles:
                roles = self.adaptive_role_plan(target=target)

        roles = normalize_roles([*target_roles, *roles])
        roles.append("docs_or_concept")
        return normalize_roles(roles)

    def strategy_role_plan(self) -> list[str]:
        return normalize_roles((self.host.strategy_profile or {}).get("role_plan") or [])

    def role_supply_counts(self) -> Counter[str]:
        cached = getattr(self.host, "_workspace_role_supply_counts_cache", None)
        if cached is not None:
            return Counter(cached)
        counts: Counter[str] = Counter()
        if not self.host._derived_primary_role_by_uid:
            return counts
        for uid in self.host._derived_primary_role_by_uid:
            for observed in self.pass1_roles_for_symbol_uid(uid):
                counts[observed] += 1
        self.host._workspace_role_supply_counts_cache = Counter(counts)
        return counts

    def filter_roles_by_workspace_supply(self, roles: list[str]) -> list[str]:
        role_supply = self.role_supply_counts()
        if not role_supply:
            return normalize_roles(roles)
        return normalize_roles([role for role in roles if role_supply.get(role, 0) > 0])

    def target_role_supply_counts(
        self, target, *, max_depth: int = 5, limit: int = 48
    ) -> Counter[str]:
        """Roles around the target, following delegation with **dynamic depth**.

        Level 1 is the broad 1-hop neighborhood. Then we follow CALLS-out
        delegation (a facade method → what it delegates to, e.g. ``FastAPI.get`` →
        ``APIRouter.get``) and keep expanding **only while new role types keep
        closing** — depth is bounded by role-closure (plus a hard cap), not a fixed
        hop count. This surfaces a role that lives behind a thin delegator (the
        registration_step behind ``FastAPI.get``) without flooding with full N-hop
        neighborhoods. Counts are role presence/frequency; weighting by a symbol's
        role-strength is a deferred refinement (it would require the selection
        margin to return candidates to the pool).
        """
        cache_key = (
            getattr(target, "uid", "") if target is not None else "",
            max_depth,
            limit,
        )
        cache = getattr(self.host, "_target_role_supply_counts_cache", None)
        if cache is not None and cache_key in cache:
            return Counter(cache[cache_key])

        counts: Counter[str] = Counter()
        if target is None or not target.uid:
            return counts
        seen: set[str] = {target.uid}
        frontier = []
        for uid in [
            *self.structural_contract_neighbor_uids(target.uid, limit=limit),
            *self.delegation_callee_uids(target.uid, limit=limit),
        ]:
            if uid not in seen and uid not in frontier:
                frontier.append(uid)
        if not frontier:
            frontier = self.host._one_hop_connected_symbol_uids(target.uid, limit=limit)
        for sym_uid in [target.uid, *frontier]:
            seen.add(sym_uid)
            counts.update(self.pass1_roles_for_symbol_uid(sym_uid))
        depth = 1
        prev_role_set = set(counts)
        while depth < max_depth and frontier:
            next_frontier: list[str] = []
            contract_map = self.structural_contract_neighbor_uid_map(frontier, limit=limit)
            delegation_map = self.delegation_callee_uid_map(frontier, limit=limit)
            for uid in frontier:
                contract_uids = contract_map.get(uid, [])
                if not contract_uids and len(frontier) == 1:
                    contract_uids = self.type_contract_neighbor_uids(uid, limit=limit)
                for contract_uid in contract_uids:
                    if contract_uid in seen:
                        continue
                    seen.add(contract_uid)
                    next_frontier.append(contract_uid)
                    counts.update(self.pass1_roles_for_symbol_uid(contract_uid))
                for callee in delegation_map.get(uid, []):
                    if callee in seen:
                        continue
                    seen.add(callee)
                    next_frontier.append(callee)
                    counts.update(self.pass1_roles_for_symbol_uid(callee))
            if set(counts) == prev_role_set:  # role closure — no new role types added
                break
            prev_role_set = set(counts)
            frontier = next_frontier
            depth += 1
        if cache is not None:
            cache[cache_key] = Counter(counts)
        return counts

    def filter_roles_by_target_supply(self, roles: list[str], target) -> list[str]:
        role_supply = self.target_role_supply_counts(target)
        if not role_supply:
            return []
        return normalize_roles([role for role in roles if role_supply.get(role, 0) > 0])

    def plan_candidate_roles(self, roles) -> list[str]:
        return normalize_roles(
            role for role in roles if role not in _ROLE_PLAN_EXCLUDED_TARGET_ROLES
        )

    def adaptive_role_plan(self, *, target=None) -> list[str]:
        selected: list[str] = []
        target_roles: list[str] = []
        if target is not None and getattr(target, "uid", ""):
            target_roles = self.target_role_plan_roles(target)
            selected.extend(target_roles)

        target_supply = self.target_role_supply_counts(target)
        if target_supply:
            selected.extend(role for role, _ in target_supply.most_common(16))

        strategy_roles = self.strategy_role_plan()
        if strategy_roles:
            if target_supply:
                selected.extend([r for r in strategy_roles if target_supply.get(r, 0) > 0])
            else:
                selected.extend(strategy_roles)

        role_supply = self.role_supply_counts()
        if role_supply:
            selected.extend(role for role, _ in role_supply.most_common(12))

        if not selected and self.host.role_catalog:
            present = self.host.role_catalog.get("present_roles") or {}
            if isinstance(present, dict) and present:
                selected.extend(list(present.keys())[:6])

        selected = self.plan_candidate_roles(selected)
        if not selected:
            return ["supporting_surface"]
        rest = [role for role in selected if role not in set(target_roles)]
        max_roles = 8
        if target_roles:
            max_roles = min(15, max(max_roles, len(target_roles) + 12))
        return normalize_roles([*target_roles, *rest])[:max_roles]

    def target_role_plan_roles(self, target) -> list[str]:
        if target is None or not getattr(target, "uid", ""):
            return []
        roles = [
            *self.pass1_roles_for_symbol_uid(target.uid),
            *self.roles_of(target),
        ]
        return self.plan_candidate_roles(roles)
