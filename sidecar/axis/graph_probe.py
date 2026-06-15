"""Neo4j-backed graph context probe for axis container-kind classification.

This module adapts already-materialized graph topology to the small
``GraphContextProbe`` protocol. It does not classify framework names, package
roots, benchmark roles, or answer-key symbols.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sidecar.axis.container_kind import GraphContextProbe
from sidecar.axis.library_marker_catalogue import kind_for_external_qualified_name
from sidecar.indexer.fast.error_dispatch_propagation import (
    is_builtin_exception_type_name,
)

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
        self._exception_key_cache: dict[str, bool] = {}
        self._inherits_error_dispatch_cache: dict[str, bool] = {}

    def is_error_model_type_name(self, key_name: str, symbol_uid: str) -> bool:
        if not key_name:
            return False
        if is_builtin_exception_type_name(key_name):
            return True
        cached = self._exception_key_cache.get(key_name)
        if cached is not None:
            return cached
        query = """
        MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(c:Symbol {name: $key_name, kind: 'class'})
        WHERE coalesce(c.inherits_builtin_exception, false) = true
        RETURN count(c) AS n
        """
        try:
            with self.db.driver.session() as session:
                record = session.run(
                    query,
                    key_name=key_name,
                    workspace_id=self.workspace_id,
                ).single()
        except Exception:
            self._exception_key_cache[key_name] = False
            return False
        hit = int((record and record.get("n")) or 0) > 0
        self._exception_key_cache[key_name] = hit
        return hit

    def inherits_error_dispatch(self, symbol_uid: str) -> bool:
        cached = self._inherits_error_dispatch_cache.get(symbol_uid)
        if cached is not None:
            return cached
        query = """
        MATCH (s:Symbol {uid: $symbol_uid})-[:DEPENDS_ON*1..6 {workspace_id: $workspace_id}]->(anc:Symbol {kind: 'class'})
        MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(anc)
        WHERE anc.uid <> $symbol_uid
        RETURN collect(DISTINCT anc.uid) AS ancestor_uids
        """
        try:
            with self.db.driver.session() as session:
                record = session.run(
                    query,
                    symbol_uid=symbol_uid,
                    workspace_id=self.workspace_id,
                ).single()
        except Exception:
            self._inherits_error_dispatch_cache[symbol_uid] = False
            return False
        ancestor_uids = [
            str(uid) for uid in ((record and record.get("ancestor_uids")) or []) if uid
        ]
        if not ancestor_uids:
            self._inherits_error_dispatch_cache[symbol_uid] = False
            return False
        try:
            import lancedb

            table = lancedb.connect("./data/lancedb").open_table("symbols_axis_python_v1")
            rows = table.to_lance().to_table(
                columns=["uid", "container_kinds", "workspace_id"],
            ).to_pylist()
        except Exception:
            self._inherits_error_dispatch_cache[symbol_uid] = False
            return False
        error_dispatch_ancestors = {
            str(r["uid"])
            for r in rows
            if r.get("workspace_id") == self.workspace_id
            and str(r.get("uid")) in ancestor_uids
            and "error_dispatch" in (r.get("container_kinds") or [])
        }
        hit = bool(error_dispatch_ancestors)
        self._inherits_error_dispatch_cache[symbol_uid] = hit
        return hit

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

        # 2) Catalogue lookup via the symbol's EXTENDS_EXTERNAL and
        #    INSTANTIATES_EXTERNAL edges. Both are structural proofs:
        #     - EXTENDS_EXTERNAL: ``class C(Marker):`` — subclass inherits
        #       from an upstream catalogue class.
        #     - INSTANTIATES_EXTERNAL: ``v = Marker(...)`` — the Variable
        #       Symbol holds an instance of an upstream catalogue class.
        #    Both edges carry the upstream qualified_name on the
        #    ExternalSymbol node, which the catalogue filter consumes
        #    locally; no name list lives in this probe.
        catalogue_query = """
        MATCH (s:Symbol {uid: $symbol_uid})-[r:EXTENDS_EXTERNAL|INSTANTIATES_EXTERNAL]->(e:ExternalSymbol)
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
                kind = kind_for_external_qualified_name(str(qn))
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

    def outgoing_handles_count(self, symbol_uid: str) -> int:
        """Count outgoing ``HANDLES`` edges out of ``symbol_uid``.

        A registry Variable like ``app = Flask(...)`` only earns the
        ``route_register_binding`` contract if there is structural evidence
        the registry is actually used — at least one ``HANDLES`` edge to a
        decorated handler. With the pipeline phase ordering
        (instantiations → decorators → embed/axis-classify), HANDLES edges
        are materialised before this probe is consulted.
        """
        query = """
        MATCH (s:Symbol {uid: $symbol_uid})-[r:HANDLES]->(:Symbol)
        WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
        RETURN count(r) AS n
        """
        try:
            with self.db.driver.session() as session:
                record = session.run(
                    query,
                    symbol_uid=symbol_uid,
                    workspace_id=self.workspace_id,
                ).single()
        except Exception:
            return 0
        return int((record and record.get("n")) or 0)

    def is_event_signal(self, symbol_uid: str) -> bool:
        """True when ``symbol_uid`` is the target of an EVENT_SUB / EVENT_PUB edge.

        The pub/sub-co-occurrence prune in ``link_hooks`` keeps an EVENT edge only
        for a real signal channel (subscribed via ``@receiver``, or both
        connected AND sent-from), so an incoming EVENT_SUB/EVENT_PUB is the
        graph-context proof of signal topology — the discriminator the
        marker-only ``signal_register`` classifier was waiting for. Hook/event
        phase (4.669b) materialises these before the axis classify (stage 5).
        """
        query = """
        MATCH (s:Symbol {uid: $symbol_uid})<-[r:EVENT_SUB|EVENT_PUB]-(:Symbol)
        WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
        RETURN count(r) AS n
        """
        try:
            with self.db.driver.session() as session:
                record = session.run(
                    query,
                    symbol_uid=symbol_uid,
                    workspace_id=self.workspace_id,
                ).single()
        except Exception:
            return False
        return int((record and record.get("n")) or 0) > 0

    def outgoing_injects_count(self, symbol_uid: str) -> int:
        """Count outgoing ``INJECTS`` edges out of ``symbol_uid``.

        Each ``INJECTS`` edge is one parameter default that resolved to a
        provider symbol via the DI marker pattern (``Depends(provider)``,
        ``Inject(provider)``, …). A function with non-zero count is one
        whose dependency wiring is actually visible in the graph — the
        ``dependency_injection_binding`` contract uses that as cross-symbol
        DFG proof.
        """
        query = """
        MATCH (s:Symbol {uid: $symbol_uid})-[r:INJECTS]->(:Symbol)
        WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
        RETURN count(r) AS n
        """
        try:
            with self.db.driver.session() as session:
                record = session.run(
                    query,
                    symbol_uid=symbol_uid,
                    workspace_id=self.workspace_id,
                ).single()
        except Exception:
            return 0
        return int((record and record.get("n")) or 0)


__all__ = ["Neo4jGraphContextProbe"]
