"""Neo4j-backed graph context probe for axis container-kind classification.

This module adapts already-materialized graph topology to the small
``GraphContextProbe`` protocol. It does not classify framework names, package
roots, benchmark roles, or answer-key symbols.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from context_engine.axis.container_kind import GraphContextProbe
from context_engine.axis.library_marker_catalogue import kind_for_external_qualified_name
from context_engine.indexer.fast.error_dispatch_propagation import (
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
        self._proxy_topology_cache: dict[str, bool] = {}
        self._inherits_proxy_object_cache: dict[str, bool] = {}
        self._metadata_bridge_keys_by_uid: dict[str, tuple[str, ...]] | None = None
        # Lazy whole-workspace materializations. The classifier consults the
        # probe once per symbol during the embed phase; answering each call
        # with its own Cypher round-trip made that phase O(symbols) in
        # network latency. Each map below is one aggregate scan, loaded on
        # first use so phase ordering (edges materialized before classify)
        # is unchanged. ``None`` = not loaded yet.
        self._handles_counts: dict[str, int] | None = None
        self._injects_counts: dict[str, int] | None = None
        self._event_signal_uids: set[str] | None = None
        self._proxy_topology_uids: set[str] | None = None
        self._marker_qns_by_uid: dict[str, tuple[str, ...]] | None = None
        self._exception_class_names: set[str] | None = None
        self._lance_kind_ancestors: dict[str, set[str]] | None = None

    def _run_workspace_rows(self, query: str) -> list[Any]:
        try:
            with self.db.driver.session() as session:
                return list(session.run(query, workspace_id=self.workspace_id))
        except Exception:
            return []

    def _load_proxy_topology_uids(self) -> set[str]:
        if self._proxy_topology_uids is None:
            binding_rows = self._run_workspace_rows(
                """
                MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
                WHERE s.kind = 'proxy_binding'
                RETURN DISTINCT s.uid AS uid
                """
            )
            edge_rows = self._run_workspace_rows(
                """
                MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
                    -[proxy_rel:PROXY_OF|RESOLVES_ATTR]->(:Symbol)
                WHERE coalesce(proxy_rel.workspace_id, $workspace_id) = $workspace_id
                RETURN DISTINCT s.uid AS uid
                """
            )
            self._proxy_topology_uids = {
                str(row["uid"]) for row in binding_rows + edge_rows if row.get("uid")
            }
        return self._proxy_topology_uids

    def has_proxy_object_topology(self, symbol_uid: str) -> bool:
        return symbol_uid in self._load_proxy_topology_uids()

    def _load_lance_kind_ancestors(self) -> dict[str, set[str]]:
        """One scan of the symbols table → container_kind → workspace uids.

        ``inherits_proxy_object`` / ``inherits_error_dispatch`` used to load
        the whole table per call. Loading once also lets both methods skip
        their per-symbol ancestor walk entirely when no symbol in this
        workspace carries the kind (the common case)."""
        if self._lance_kind_ancestors is None:
            by_kind: dict[str, set[str]] = {}
            try:
                import lancedb

                table = lancedb.connect("./data/lancedb").open_table("symbols_axis_python_v1")
                rows = (
                    table.to_lance()
                    .to_table(
                        columns=["uid", "container_kinds", "workspace_id"],
                    )
                    .to_pylist()
                )
            except Exception:
                rows = []
            for r in rows:
                if r.get("workspace_id") != self.workspace_id:
                    continue
                for kind in r.get("container_kinds") or []:
                    by_kind.setdefault(str(kind), set()).add(str(r.get("uid")))
            self._lance_kind_ancestors = by_kind
        return self._lance_kind_ancestors

    def _class_ancestor_uids(self, symbol_uid: str) -> set[str]:
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
            return set()
        return {str(uid) for uid in ((record and record.get("ancestor_uids")) or []) if uid}

    def _inherits_container_kind(
        self,
        symbol_uid: str,
        kind: str,
        cache: dict[str, bool],
    ) -> bool:
        cached = cache.get(symbol_uid)
        if cached is not None:
            return cached
        kind_uids = self._load_lance_kind_ancestors().get(kind) or set()
        if not kind_uids:
            cache[symbol_uid] = False
            return False
        hit = bool(self._class_ancestor_uids(symbol_uid) & kind_uids)
        cache[symbol_uid] = hit
        return hit

    def inherits_proxy_object(self, symbol_uid: str) -> bool:
        return self._inherits_container_kind(
            symbol_uid, "proxy_object", self._inherits_proxy_object_cache
        )

    def metadata_bridge_keys(self, symbol_uid: str) -> tuple[str, ...]:
        """Return metadata keys for bridge endpoints in this workspace.

        Loaded in one workspace scan because the classifier asks this question
        for many symbols during embedding.
        """
        if self._metadata_bridge_keys_by_uid is None:
            query = """
            MATCH (s:Symbol)-[r:METADATA_BRIDGE]-(:Symbol)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s)
            RETURN s.uid AS uid, collect(DISTINCT r.key) AS keys
            """
            try:
                with self.db.driver.session() as session:
                    rows = session.run(query, workspace_id=self.workspace_id)
                    self._metadata_bridge_keys_by_uid = {
                        str(row["uid"]): tuple(sorted(str(k) for k in row["keys"] if k))
                        for row in rows
                        if row.get("uid")
                    }
            except Exception:
                self._metadata_bridge_keys_by_uid = {}
        return self._metadata_bridge_keys_by_uid.get(symbol_uid, ())

    def _load_exception_class_names(self) -> set[str]:
        if self._exception_class_names is None:
            rows = self._run_workspace_rows(
                """
                MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(c:Symbol {kind: 'class'})
                WHERE coalesce(c.inherits_builtin_exception, false) = true
                RETURN DISTINCT c.name AS name
                """
            )
            self._exception_class_names = {str(row["name"]) for row in rows if row.get("name")}
        return self._exception_class_names

    def is_error_model_type_name(self, key_name: str, symbol_uid: str) -> bool:
        if not key_name:
            return False
        if is_builtin_exception_type_name(key_name):
            return True
        return key_name in self._load_exception_class_names()

    def inherits_error_dispatch(self, symbol_uid: str) -> bool:
        return self._inherits_container_kind(
            symbol_uid, "error_dispatch", self._inherits_error_dispatch_cache
        )

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
            _KIND_NEIGHBOUR_CYPHER[kind] for kind in set(kinds) if kind in _KIND_NEIGHBOUR_CYPHER
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

    def _load_marker_qns_by_uid(self) -> dict[str, tuple[str, ...]]:
        if self._marker_qns_by_uid is None:
            rows = self._run_workspace_rows(
                """
                MATCH (s:Symbol)-[r:EXTENDS_EXTERNAL|INSTANTIATES_EXTERNAL]->(e:ExternalSymbol)
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                RETURN s.uid AS uid, collect(DISTINCT e.qualified_name) AS qns
                """
            )
            self._marker_qns_by_uid = {
                str(row["uid"]): tuple(str(qn) for qn in (row["qns"] or []) if qn)
                for row in rows
                if row.get("uid")
            }
        return self._marker_qns_by_uid

    def library_marker_kinds(self, symbol_uid: str) -> set[str]:
        cached = self._marker_cache.get(symbol_uid)
        if cached is not None:
            return set(cached)
        kinds: set[str] = set()

        if self.has_proxy_object_topology(symbol_uid):
            kinds.add("proxy_object")

        # Catalogue lookup via the symbol's EXTENDS_EXTERNAL and
        #    INSTANTIATES_EXTERNAL edges. Both are structural proofs:
        #     - EXTENDS_EXTERNAL: ``class C(Marker):`` — subclass inherits
        #       from an upstream catalogue class.
        #     - INSTANTIATES_EXTERNAL: ``v = Marker(...)`` — the Variable
        #       Symbol holds an instance of an upstream catalogue class.
        #    Both edges carry the upstream qualified_name on the
        #    ExternalSymbol node, which the catalogue filter consumes
        #    locally; no name list lives in this probe.
        for qn in self._load_marker_qns_by_uid().get(symbol_uid, ()):
            kind = kind_for_external_qualified_name(qn)
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
        if self._handles_counts is None:
            rows = self._run_workspace_rows(
                """
                MATCH (s:Symbol)-[r:HANDLES]->(:Symbol)
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                RETURN s.uid AS uid, count(r) AS n
                """
            )
            self._handles_counts = {
                str(row["uid"]): int(row["n"] or 0) for row in rows if row.get("uid")
            }
        return self._handles_counts.get(symbol_uid, 0)

    def is_event_signal(self, symbol_uid: str) -> bool:
        """True when ``symbol_uid`` is the target of an EVENT_SUB / EVENT_PUB edge.

        The pub/sub-co-occurrence prune in ``link_hooks`` keeps an EVENT edge only
        for a real signal channel (subscribed via ``@receiver``, or both
        connected AND sent-from), so an incoming EVENT_SUB/EVENT_PUB is the
        graph-context proof of signal topology — the discriminator the
        marker-only ``signal_register`` classifier was waiting for. Hook/event
        phase (4.669b) materialises these before the axis classify (stage 5).
        """
        if self._event_signal_uids is None:
            rows = self._run_workspace_rows(
                """
                MATCH (s:Symbol)<-[r:EVENT_SUB|EVENT_PUB]-(:Symbol)
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                RETURN DISTINCT s.uid AS uid
                """
            )
            self._event_signal_uids = {str(row["uid"]) for row in rows if row.get("uid")}
        return symbol_uid in self._event_signal_uids

    def outgoing_injects_count(self, symbol_uid: str) -> int:
        """Count outgoing ``INJECTS`` edges out of ``symbol_uid``.

        Each ``INJECTS`` edge is one parameter default that resolved to a
        provider symbol via the DI marker pattern (``Depends(provider)``,
        ``Inject(provider)``, …). A function with non-zero count is one
        whose dependency wiring is actually visible in the graph — the
        ``dependency_injection_binding`` contract uses that as cross-symbol
        DFG proof.
        """
        if self._injects_counts is None:
            rows = self._run_workspace_rows(
                """
                MATCH (s:Symbol)-[r:INJECTS]->(:Symbol)
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                RETURN s.uid AS uid, count(r) AS n
                """
            )
            self._injects_counts = {
                str(row["uid"]): int(row["n"] or 0) for row in rows if row.get("uid")
            }
        return self._injects_counts.get(symbol_uid, 0)

    def peer_container_kinds_for(self, qualified_name_prefix: str) -> set[str]:
        return set()


__all__ = ["Neo4jGraphContextProbe"]
