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

_GENERIC_AUTO_ROLE_PLANS: dict[str, tuple[str, ...]] = {
    "module_composition": (
        "api_surface",
        "composition_surface",
        "integration_surface",
        "runtime_surface",
    ),
    "validation_pipeline": (
        "api_surface",
        "composition_surface",
        "executor",
        "validator_handle",
        "core_runtime",
        "runtime_surface",
    ),
    "worker_execution": (
        "integration_surface",
        "orchestrator",
        "executor",
        "runtime_surface",
    ),
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
        inferred = infer_supporting_roles(
            file_path=getattr(c, "file_path", "") or "",
            primary_role=self.role_of(c),
            name=getattr(c, "name", "") or "",
            kind=getattr(c, "symbol_kind", "") or getattr(c, "kind", "") or "",
        )
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

        if self.host._cluster_to_role:
            uid = getattr(c, "uid", "") or ""
            cluster_id = self.host._derived_role_by_uid.get(uid)
            if cluster_id is not None:
                role = self.host._cluster_to_role.get(cluster_id)
                if role:
                    return str(role)
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
        cid = self.host._derived_role_by_uid.get(uid)
        if cid is None:
            return ""
        return self.host._cluster_to_role.get(cid) or ""

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

    def determine_mechanism_structural(self, target) -> str:
        if not self.host._cluster_to_role or not self.host._derived_role_by_uid:
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
            role = self.canonical_role_for_symbol_uid(sym_uid)
            if role:
                roles_observed.append(role)
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
        auto_mechanism = self.auto_mechanism_from_strategy(target, query=query)
        if auto_mechanism:
            return auto_mechanism
        return "generic"

    def get_required_roles(self, mechanism: str, *, target=None) -> list[str]:
        roles = []
        if mechanism.startswith("auto:"):
            roles = self.roles_for_auto_mechanism(mechanism.removeprefix("auto:"))
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
        if not self.host._cluster_to_role or not self.host._derived_role_by_uid:
            return counts
        for cluster_id in self.host._derived_role_by_uid.values():
            role = self.host._cluster_to_role.get(cluster_id)
            if role:
                counts[role] += 1
        return counts

    def filter_roles_by_workspace_supply(self, roles: list[str]) -> list[str]:
        role_supply = self.role_supply_counts()
        if not role_supply:
            return normalize_roles(roles)
        return normalize_roles([role for role in roles if role_supply.get(role, 0) > 0])

    def target_role_supply_counts(self, target) -> Counter[str]:
        counts: Counter[str] = Counter()
        if target is None or not target.uid:
            return counts
        roles_observed: list[str] = []
        neighbors = self.host._one_hop_connected_symbol_uids(target.uid, limit=48)
        for sym_uid in [target.uid, *neighbors]:
            role = self.canonical_role_for_symbol_uid(sym_uid)
            if role:
                roles_observed.append(role)
        counts.update(roles_observed)
        return counts

    def filter_roles_by_target_supply(self, roles: list[str], target) -> list[str]:
        role_supply = self.target_role_supply_counts(target)
        if not role_supply:
            return []
        return normalize_roles([role for role in roles if role_supply.get(role, 0) > 0])

    def adaptive_role_plan(self, *, target=None) -> list[str]:
        selected: list[str] = []

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
            selected.extend(
                list((self.host.role_catalog.get("role_to_archetypes") or {}).keys())[:6]
            )

        selected = normalize_roles(selected)
        if selected:
            return selected[:5]
        return ["supporting_surface"]

    def roles_for_auto_mechanism(self, archetype: str) -> list[str]:
        for item in (self.host.strategy_profile or {}).get("mechanism_archetypes") or []:
            if item.get("type") == archetype:
                return normalize_roles(item.get("role_plan") or [])
        if archetype in _GENERIC_AUTO_ROLE_PLANS:
            return normalize_roles(_GENERIC_AUTO_ROLE_PLANS[archetype])
        return self.strategy_role_plan()

    def auto_mechanism_from_strategy(self, target, query: str = "") -> str:
        archetypes = (self.host.strategy_profile or {}).get("mechanism_archetypes") or []
        if not archetypes and not self.strategy_role_plan():
            return ""
        haystack = " ".join(
            part.lower()
            for part in (target.name or "", target.file_path or "", query or "")
            if part
        )
        if "module" in haystack and any(
            term in haystack
            for term in (
                "compose",
                "composition",
                "controller",
                "export",
                "feature",
                "import",
                "provider",
            )
        ):
            return "auto:module_composition"
        if any(term in haystack for term in ("receive", "execute", "consume", "process")) and any(
            term in haystack for term in ("worker", "message", "task", "job", "broker")
        ):
            return "auto:worker_execution"
        if not archetypes:
            return ""
        for item in archetypes:
            archetype = item.get("type", "")
            evidence = " ".join(str(piece).lower() for piece in item.get("evidence") or [])
            terms = set(self.host.scoring.focus_query_terms(archetype.replace("_", " ")))
            terms.update(self.host.scoring.focus_query_terms(evidence))
            if terms and any(term in haystack for term in terms):
                return f"auto:{archetype}"
        top = archetypes[0].get("type", "")
        return f"auto:{top}" if top else ""
