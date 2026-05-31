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
            or self.host.scoring.candidate_matches_query_topic(c, target, query=query)
        ):
            return roles

        required = set(normalize_roles(required_roles))
        return [role for role in roles if role not in required]

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
        if kind not in {"class", "function", "method", "object_api", "module"}:
            return False
        path = (getattr(c, "file_path", "") or "").replace("\\", "/").lower()
        if any(marker in path for marker in ("/tests/", "/test_", "/docs/", "/examples/")):
            return False
        return True

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
        MATCH (t:Symbol {uid: $uid})-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT|HAS_API|INHERITED_API]-(n:Symbol)
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

        roles.append("docs_or_concept")
        return normalize_roles(roles)

    def strategy_role_plan(self) -> list[str]:
        return normalize_roles((self.host.strategy_profile or {}).get("role_plan") or [])

    def role_supply_counts(self) -> Counter[str]:
        counts: Counter[str] = Counter()
        if not self.host._derived_primary_role_by_uid:
            return counts
        for uid in self.host._derived_primary_role_by_uid:
            for observed in self.pass1_roles_for_symbol_uid(uid):
                counts[observed] += 1
        return counts

    def filter_roles_by_workspace_supply(self, roles: list[str]) -> list[str]:
        role_supply = self.role_supply_counts()
        if not role_supply:
            return normalize_roles(roles)
        return normalize_roles([role for role in roles if role_supply.get(role, 0) > 0])

    def target_role_supply_counts(
        self, target, *, max_depth: int = 3, limit: int = 48
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
        counts: Counter[str] = Counter()
        if target is None or not target.uid:
            return counts
        seen: set[str] = {target.uid}
        frontier = self.host._one_hop_connected_symbol_uids(target.uid, limit=limit)
        for sym_uid in [target.uid, *frontier]:
            seen.add(sym_uid)
            counts.update(self.pass1_roles_for_symbol_uid(sym_uid))
        depth = 1
        prev_role_set = set(counts)
        while depth < max_depth and frontier:
            next_frontier: list[str] = []
            for uid in frontier:
                for callee in self.delegation_callee_uids(uid, limit=limit):
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
        return counts

    def filter_roles_by_target_supply(self, roles: list[str], target) -> list[str]:
        role_supply = self.target_role_supply_counts(target)
        if not role_supply:
            return []
        return normalize_roles([role for role in roles if role_supply.get(role, 0) > 0])

    def adaptive_role_plan(self, *, target=None) -> list[str]:
        selected: list[str] = []
        target_roles: list[str] = []
        if target is not None and getattr(target, "uid", ""):
            target_roles = self.pass1_roles_for_symbol_uid(target.uid)
            selected.extend(target_roles)

        target_supply = self.target_role_supply_counts(target)
        if target_supply:
            selected.extend(role for role, _ in target_supply.most_common(6))

        strategy_roles = self.strategy_role_plan()
        if strategy_roles:
            if target_supply:
                selected.extend([r for r in strategy_roles if target_supply.get(r, 0) > 0])
            else:
                selected.extend(strategy_roles)

        role_supply = self.role_supply_counts()
        if role_supply:
            selected.extend(role for role, _ in role_supply.most_common(6))

        if not selected and self.host.role_catalog:
            present = self.host.role_catalog.get("present_roles") or {}
            if isinstance(present, dict) and present:
                selected.extend(list(present.keys())[:6])

        selected = normalize_roles(selected)
        if not selected:
            return ["supporting_surface"]
        rest = [role for role in selected if role not in set(target_roles)]
        return normalize_roles([*target_roles, *rest])[:5]
