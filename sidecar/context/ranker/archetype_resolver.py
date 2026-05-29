"""Divide-and-conquer resolution of statically-unreachable boundaries.

Methodology, not storage: a call graph severed at a dynamic boundary (DI marker,
decorator, proxy, closure, dynamic dispatch) is decomposed recursively rather than
matched by framework name literals.

Three concepts on a single gradient (distinguished by WEIGHT and decision depth,
NOT by presence/absence of graph materialization — a Symbol may also be an
archetype; an archetype may be fully materialized):

- Symbol     — an atom, typically backed by materialization in the graph.
- Archetype  — a recognized pattern (DI / Decorator / Proxy / Closure / DynamicCall)
               with a known one-step resolver.
- Composite  — does not fit a single archetype; split into parts, each resolved
               on its own. A composite is an archetype one loop-level deeper.

``resolve`` walks this: at each node it evaluates archetype-ness with a weight that
governs whether to descend or accept. It runs PARALLEL to the existing naming-based
recovery (feature-gated); naming branches are retired incrementally as coverage
here grows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Archetype kinds. Order is not precedence; a node may match several and the
# weights decide. DYNAMIC_CALL (DC) is the umbrella for runtime dispatch.
ARCHETYPE_DI = "di"
ARCHETYPE_DECORATOR = "decorator"
ARCHETYPE_PROXY = "proxy"
ARCHETYPE_CLOSURE = "closure"
ARCHETYPE_DYNAMIC_CALL = "dynamic_call"


@dataclass
class ResolveStep:
    """One node visited during descent, plus the decision taken there."""

    uid: str
    name: str
    qualified_name: str
    archetypes: list[str] = field(default_factory=list)
    weight: float = 1.0
    descended_via: str = ""  # which archetype resolver produced the next hop(s)


@dataclass
class ResolveResult:
    """Outcome of resolving one starting node."""

    resolved_uids: list[str] = field(default_factory=list)
    steps: list[ResolveStep] = field(default_factory=list)
    truncated: bool = False  # hit depth/visit budget before terminating


class ArchetypeResolver:
    """Recursive-descent resolver over the existing graph + archetype detectors."""

    MAX_DEPTH = 6
    MAX_VISITS = 64

    def __init__(self, host):
        self.host = host
        self.db = host.db
        self.workspace_id = host.workspace_id

    # -- node access -------------------------------------------------------

    def _node(self, uid: str) -> dict | None:
        query = """
        MATCH (s:Symbol {uid: $uid})
        OPTIONAL MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(s)
        RETURN s.uid AS uid, s.name AS name,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(s.kind, '') AS kind,
               coalesce(f.path, '') AS file_path
        """
        try:
            with self.db.driver.session() as session:
                rec = session.run(
                    query, uid=uid, workspace_id=self.workspace_id
                ).single()
                return dict(rec) if rec else None
        except Exception:
            return None

    def _outgoing(self, uid: str, *, by_qualified_name: bool = False) -> list[dict]:
        """Materialized outgoing call/dep edges (the resolver reads what exists).

        ``by_qualified_name`` aggregates edges across all sibling nodes sharing the
        node's qualified_name. ``<locals>`` closures fragment into several uid nodes
        (the uid carries a signature/position component) while ``Celery.task`` links
        to only one of them; the closure boundary must be treated as ONE node, so
        archetype descent unifies its siblings' outgoing edges.
        """
        if by_qualified_name:
            # A closure boundary spans the node AND its nested child closures
            # (def outer: def inner: def _create: ...). They link by qualified_name
            # PREFIX, not equality, so descend the whole nest: any symbol whose qn is
            # the anchor's or extends it with ".<locals>.". Their concrete call edges
            # are what the closure ultimately invokes/generates.
            match = """
            MATCH (anchor:Symbol {uid: $uid})
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
            WHERE s.qualified_name = anchor.qualified_name
               OR s.qualified_name STARTS WITH anchor.qualified_name + '.<locals>.'
            MATCH (s)-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|DECORATED_BY|PROXY_OF]->(t:Symbol)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
              AND NOT t.qualified_name STARTS WITH anchor.qualified_name
            """
        else:
            match = """
            MATCH (s:Symbol {uid: $uid})-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|DECORATED_BY|PROXY_OF]->(t:Symbol)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
            """
        query = match + """
        RETURN DISTINCT t.uid AS uid, t.name AS name,
               coalesce(t.qualified_name, '') AS qualified_name,
               coalesce(t.kind, '') AS kind,
               type(r) AS rel_type
        """
        try:
            with self.db.driver.session() as session:
                return [dict(r) for r in session.run(
                    query, uid=uid, workspace_id=self.workspace_id
                )]
        except Exception:
            return []

    # -- archetype detection (weighted decision point) ---------------------

    @staticmethod
    def _detect_archetypes(node: dict) -> list[str]:
        """Which standard archetypes this node matches. Empty = plain symbol/composite."""
        qn = (node.get("qualified_name") or "").lower()
        archs: list[str] = []
        # Closure: a function defined inside another (tree-sitter qn carries <locals>).
        if "<locals>" in qn or "inner_" in (node.get("name") or "").lower():
            archs.append(ARCHETYPE_CLOSURE)
        return archs

    # -- recursive resolve -------------------------------------------------

    def resolve(self, start_uid: str) -> ResolveResult:
        """Descend from ``start_uid`` through archetype boundaries to symbols."""
        result = ResolveResult()
        visited: set[str] = set()
        self._descend(start_uid, depth=0, visited=visited, result=result)
        return result

    def _descend(
        self, uid: str, *, depth: int, visited: set[str], result: ResolveResult
    ) -> None:
        if uid in visited:
            return
        if depth > self.MAX_DEPTH or len(visited) >= self.MAX_VISITS:
            result.truncated = True
            return
        visited.add(uid)

        node = self._node(uid)
        if node is None:
            return

        archetypes = self._detect_archetypes(node)
        step = ResolveStep(
            uid=uid,
            name=node.get("name") or "",
            qualified_name=node.get("qualified_name") or "",
            archetypes=archetypes,
        )
        result.steps.append(step)
        result.resolved_uids.append(uid)

        # Decision point. Descend through an outgoing edge when either:
        #  (a) the target is itself an archetype boundary (a <locals> closure, or an
        #      archetype-typed relation like PROXY_OF / DECORATED_BY) — a normal node
        #      leading into a boundary (Celery.task -> the closure); or
        #  (b) the CURRENT node is itself an archetype (e.g. a closure), in which case
        #      its concrete call edges are what it generates/invokes and carry the
        #      resolution forward (inner_create_task_cls -> _task_from_fun).
        inside_archetype = bool(archetypes)
        for edge in self._outgoing(uid, by_qualified_name=inside_archetype):
            target_uid = edge.get("uid")
            if not target_uid or target_uid in visited:
                continue
            via = self._edge_descends_via(edge)
            if not via and inside_archetype and self._is_concrete_call(edge):
                via = archetypes[0]  # carry the current archetype's resolution forward
            if via:
                step.descended_via = via
                self._descend(target_uid, depth=depth + 1, visited=visited, result=result)

    @staticmethod
    def _is_concrete_call(edge: dict) -> bool:
        rel = (edge.get("rel_type") or "").upper()
        return rel.startswith("CALLS")

    @staticmethod
    def _edge_descends_via(edge: dict) -> str:
        """Is this outgoing edge a boundary to descend through? Returns archetype kind or ''."""
        rel = (edge.get("rel_type") or "").upper()
        if rel == "PROXY_OF":
            return ARCHETYPE_PROXY
        if rel == "DECORATED_BY":
            return ARCHETYPE_DECORATOR
        target_qn = (edge.get("qualified_name") or "").lower()
        target_name = (edge.get("name") or "").lower()
        if "<locals>" in target_qn or target_name.startswith("inner_"):
            return ARCHETYPE_CLOSURE
        return ""
