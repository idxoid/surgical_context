"""Neo4j traversal for compiled axis query plans.

This layer consumes ``AxisQueryPlan.expansion_steps``. It does not infer
intent, roles, contracts, or edge policy; the plan already contains the
structural traversal steps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sidecar.axis.query_plan import AxisQueryPlan, GraphExpansionStep

_EDGE_TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True)
class AxisGraphHit:
    seed_uid: str
    uid: str
    name: str
    qualified_name: str
    file_path: str
    step: str
    depth: int

    def to_dict(self) -> dict[str, object]:
        return {
            "seed_uid": self.seed_uid,
            "uid": self.uid,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "file_path": self.file_path,
            "step": self.step,
            "depth": self.depth,
        }


def _safe_rel_pattern(edge_types: tuple[str, ...]) -> str:
    if not edge_types:
        raise ValueError("Expansion step must name at least one edge type")
    invalid = [edge for edge in edge_types if not _EDGE_TYPE_RE.match(edge)]
    if invalid:
        raise ValueError(f"Unsafe edge type in axis traversal: {invalid[0]}")
    return "|".join(edge_types)


def _path_pattern(step: GraphExpansionStep) -> str:
    rels = _safe_rel_pattern(step.edge_types)
    if step.direction == "out":
        return f"(seed)-[rels:{rels}*1..{step.max_depth}]->(n:Symbol)"
    if step.direction == "in":
        return f"(seed)<-[rels:{rels}*1..{step.max_depth}]-(n:Symbol)"
    if step.direction == "both":
        return f"(seed)-[rels:{rels}*1..{step.max_depth}]-(n:Symbol)"
    raise ValueError(f"Unknown graph direction: {step.direction}")


def render_axis_expansion_query(step: GraphExpansionStep) -> str:
    """Render the Cypher for one expansion step."""

    pattern = _path_pattern(step)
    return f"""
    UNWIND $seed_uids AS seed_uid
    MATCH (seed:Symbol {{uid: seed_uid}})
    MATCH path = {pattern}
    WHERE all(rel IN rels WHERE coalesce(rel.workspace_id, $workspace_id) = $workspace_id)
    MATCH (file:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(n)
    RETURN seed.uid AS seed_uid,
           n.uid AS uid,
           coalesce(n.name, '') AS name,
           coalesce(n.qualified_name, '') AS qualified_name,
           file.path AS file_path,
           length(path) AS depth
    ORDER BY depth ASC, uid ASC
    """.strip()


class AxisGraphTraversal:
    """Execute compiled axis graph expansion steps against Neo4j."""

    def __init__(self, db: Any, workspace_id: str) -> None:
        self.db = db
        self.workspace_id = workspace_id

    def expand(
        self,
        seed_uids: list[str],
        plan: AxisQueryPlan,
    ) -> list[AxisGraphHit]:
        if not seed_uids:
            return []
        hits: list[AxisGraphHit] = []
        seen: set[tuple[str, str, str]] = set()
        with self.db.driver.session() as session:
            for step in plan.expansion_steps:
                query = render_axis_expansion_query(step)
                result = session.run(
                    query,
                    seed_uids=seed_uids,
                    workspace_id=self.workspace_id,
                )
                for record in result:
                    key = (
                        str(record.get("seed_uid") or ""),
                        str(record.get("uid") or ""),
                        step.name,
                    )
                    if not key[0] or not key[1] or key in seen:
                        continue
                    seen.add(key)
                    hits.append(
                        AxisGraphHit(
                            seed_uid=key[0],
                            uid=key[1],
                            name=str(record.get("name") or ""),
                            qualified_name=str(record.get("qualified_name") or ""),
                            file_path=str(record.get("file_path") or ""),
                            step=step.name,
                            depth=int(record.get("depth") or 0),
                        )
                    )
        return hits


__all__ = [
    "AxisGraphHit",
    "AxisGraphTraversal",
    "render_axis_expansion_query",
]
