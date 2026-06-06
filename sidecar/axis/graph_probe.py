"""Neo4j-backed graph context probe for axis container-kind classification.

This module adapts already-materialized graph topology to the small
``GraphContextProbe`` protocol. It does not classify framework names, package
roots, benchmark roles, or answer-key symbols.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sidecar.axis.container_kind import GraphContextProbe
from sidecar.axis.library_marker_catalogue import LIBRARY_MARKER_CATALOGUE

_CONTROL_EDGE_TYPES = (
    "CALLS",
    "CALLS_DIRECT",
    "CALLS_SCOPED",
    "CALLS_IMPORTED",
    "CALLS_DYNAMIC",
    "CALLS_INFERRED",
    "CALLS_GUESS",
    "HAS_API",
    "INHERITED_API",
)


def _qualified_name_root(qualified_name: str) -> str:
    """Top dotted segment of a Symbol's qualified name.

    A package boundary in Python is its top-level module name. The graph
    materializes ``qualified_name`` on every Symbol; the segment before the
    first dot is therefore the structural package owner — no path-stem or
    directory-convention guessing.
    """
    if not qualified_name:
        return ""
    head, _, _ = qualified_name.partition(".")
    return head.strip()


# ---------------------------------------------------------------------------
# outgoing_kind_edges dispatch table.
#
# Each entry is a Cypher fragment that, given a neighbour node ``n`` already
# reached via ``(seed)-[r]->(n:Symbol)`` (or via marker resolution), boolean-
# proves that ``n`` carries the given container kind from graph-level evidence
# alone. Kinds that are not in this table have no graph-level proof yet, and
# the probe deliberately answers ``0``: lying about them would re-introduce
# name-pattern matching at a different layer.
# ---------------------------------------------------------------------------


_KIND_NEIGHBOUR_CYPHER: dict[str, str] = {
    "proxy_object": (
        "OPTIONAL MATCH (n)-[proxy_rel:PROXY_OF|RESOLVES_ATTR]->(:Symbol) "
        "WHERE coalesce(proxy_rel.workspace_id, $workspace_id) = $workspace_id "
        "WITH n, count(proxy_rel) AS proxy_rel_count "
        "WHERE n.kind = 'proxy_binding' OR proxy_rel_count > 0"
    ),
}


class Neo4jGraphContextProbe(GraphContextProbe):
    """Read structural graph context for one workspace.

    The current marker surface is deliberately small:

    - ``proxy_object`` can be proven from a graph-level ``proxy_binding`` symbol
      or proxy resolution edges.
    - Other marker-only container kinds stay unproven until a structural
      catalogue exists outside the axis layer.
    """

    def __init__(self, db: Any, workspace_id: str) -> None:
        self.db = db
        self.workspace_id = workspace_id
        self._marker_cache: dict[str, set[str]] = {}
        self._dispersion_cache: dict[str, float] = {}

    def outgoing_kind_edges(
        self,
        symbol_uid: str,
        kinds: Iterable[str],
    ) -> int:
        """Count outgoing neighbours that structurally carry one of ``kinds``.

        Driven by :data:`_KIND_NEIGHBOUR_CYPHER`. Kinds that have no entry
        cannot yet be proven from graph topology alone; for those, the probe
        answers ``0`` rather than fabricating a marker. The classifier should
        treat ``0`` as "no structural proof" — never as "definitely zero".
        """
        fragments = [
            _KIND_NEIGHBOUR_CYPHER[kind]
            for kind in set(kinds)
            if kind in _KIND_NEIGHBOUR_CYPHER
        ]
        if not fragments:
            return 0
        total = 0
        for fragment in fragments:
            query = (
                "MATCH (s:Symbol {uid: $symbol_uid})-[r]->(n:Symbol) "
                "WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id "
                "MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(n) "
                f"{fragment} "
                "RETURN count(DISTINCT n) AS count"
            )
            try:
                with self.db.driver.session() as session:
                    record = session.run(
                        query,
                        symbol_uid=symbol_uid,
                        workspace_id=self.workspace_id,
                    ).single()
            except Exception:
                continue
            total += int((record and record.get("count")) or 0)
        return total

    def supported_outgoing_kinds(self) -> set[str]:
        """Container kinds the probe can answer for via graph topology."""
        return set(_KIND_NEIGHBOUR_CYPHER)

    def library_marker_kinds(self, symbol_uid: str) -> set[str]:
        cached = self._marker_cache.get(symbol_uid)
        if cached is not None:
            return set(cached)
        kinds: set[str] = set()

        # 1) Symbol-local proxy marker (existing path). A symbol that the
        #    parser already flagged as ``proxy_binding`` or that resolves
        #    through PROXY_OF / RESOLVES_ATTR carries ``proxy_object``
        #    independently of any external import surface.
        proxy_query = """
        MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol {uid: $symbol_uid})
        OPTIONAL MATCH (s)-[proxy_rel:PROXY_OF|RESOLVES_ATTR]->(:Symbol)
        WHERE coalesce(proxy_rel.workspace_id, $workspace_id) = $workspace_id
        RETURN s.kind AS symbol_kind, count(proxy_rel) AS proxy_rel_count
        """
        try:
            with self.db.driver.session() as session:
                record = session.run(
                    proxy_query,
                    symbol_uid=symbol_uid,
                    workspace_id=self.workspace_id,
                ).single()
        except Exception:
            record = None
        if record:
            symbol_kind = str(record.get("symbol_kind") or "")
            proxy_rel_count = int(record.get("proxy_rel_count") or 0)
            if symbol_kind == "proxy_binding" or proxy_rel_count > 0:
                kinds.add("proxy_object")

        # 2) Catalogue lookup via the symbol's EXTENDS_EXTERNAL edges. This is
        #    the structural inheritance link materialized by the post-pass
        #    that joins parsed_base_names with IMPORTS_EXTERNAL_SYMBOL.local_alias.
        #    Only classes whose declared base IS the imported external symbol
        #    get marked — a much tighter signal than "the file imports it".
        catalogue_query = """
        MATCH (s:Symbol {uid: $symbol_uid})-[r:EXTENDS_EXTERNAL]->(e:ExternalSymbol)
        WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
        RETURN collect(DISTINCT e.qualified_name) AS qns
        """
        try:
            with self.db.driver.session() as session:
                record = session.run(
                    catalogue_query,
                    symbol_uid=symbol_uid,
                    workspace_id=self.workspace_id,
                ).single()
        except Exception:
            record = None
        if record:
            for qn in record.get("qns") or []:
                kind = LIBRARY_MARKER_CATALOGUE.get(str(qn))
                if kind:
                    kinds.add(kind)

        self._marker_cache[symbol_uid] = kinds
        return set(kinds)

    def caller_package_dispersion(self, symbol_uid: str) -> float:
        """Spread of callers across top-level packages.

        Dispersion is computed from caller ``Symbol.qualified_name`` — the top
        dotted segment is the structural package owner. No directory-name
        conventions (``src``/``lib``/``app``) are consulted; if a project does
        not materialize qualified names, the value is conservatively ``0.0``.
        """
        cached = self._dispersion_cache.get(symbol_uid)
        if cached is not None:
            return cached
        rels = "|".join(_CONTROL_EDGE_TYPES)
        query = f"""
        MATCH (caller:Symbol)-[r:{rels}]->(:Symbol {{uid: $symbol_uid}})
        WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
        MATCH (file:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(caller)
        RETURN collect(DISTINCT coalesce(caller.qualified_name, '')) AS qns
        """
        try:
            with self.db.driver.session() as session:
                record = session.run(
                    query,
                    symbol_uid=symbol_uid,
                    workspace_id=self.workspace_id,
                ).single()
        except Exception:
            record = None
        qns = [str(qn) for qn in ((record and record.get("qns")) or []) if qn]
        if len(qns) <= 1:
            value = 0.0
        else:
            roots = {_qualified_name_root(qn) for qn in qns if _qualified_name_root(qn)}
            value = min(1.0, max(0.0, (len(roots) - 1) / (len(qns) - 1)))
        self._dispersion_cache[symbol_uid] = value
        return value

    def is_cfg_driver(self, symbol_uid: str) -> bool:
        # Plain outgoing control fan is too broad to prove "driver". Keep this
        # false until the graph materializes a narrow dispatch-loop marker.
        return False


__all__ = ["Neo4jGraphContextProbe"]
