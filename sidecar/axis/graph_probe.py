"""Neo4j-backed graph context probe for axis container-kind classification.

This module adapts already-materialized graph topology to the small
``GraphContextProbe`` protocol. It does not classify framework names, package
roots, benchmark roles, or answer-key symbols.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sidecar.axis.container_kind import GraphContextProbe

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


def _package_root(file_path: str) -> str:
    path = Path(file_path)
    parts = [part for part in path.parts if part not in {"", "."}]
    if not parts:
        return ""
    for marker in ("src", "lib", "app"):
        if marker in parts:
            idx = parts.index(marker)
            if idx + 1 < len(parts):
                return parts[idx + 1]
    if len(parts) >= 2:
        return parts[-2]
    return path.stem


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
        requested = set(kinds)
        if "proxy_object" not in requested:
            return 0
        query = """
        MATCH (s:Symbol {uid: $symbol_uid})-[r]->(n:Symbol)
        WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
        MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(n)
        OPTIONAL MATCH (n)-[proxy_rel:PROXY_OF|RESOLVES_ATTR]->(:Symbol)
        WHERE coalesce(proxy_rel.workspace_id, $workspace_id) = $workspace_id
        WITH n, count(proxy_rel) AS proxy_rel_count
        WHERE n.kind = 'proxy_binding' OR proxy_rel_count > 0
        RETURN count(DISTINCT n) AS count
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
        return int((record and record.get("count")) or 0)

    def library_marker_kinds(self, symbol_uid: str) -> set[str]:
        cached = self._marker_cache.get(symbol_uid)
        if cached is not None:
            return set(cached)
        query = """
        MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol {uid: $symbol_uid})
        OPTIONAL MATCH (s)-[proxy_rel:PROXY_OF|RESOLVES_ATTR]->(:Symbol)
        WHERE coalesce(proxy_rel.workspace_id, $workspace_id) = $workspace_id
        RETURN s.kind AS symbol_kind, count(proxy_rel) AS proxy_rel_count
        """
        kinds: set[str] = set()
        try:
            with self.db.driver.session() as session:
                record = session.run(
                    query,
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
        self._marker_cache[symbol_uid] = kinds
        return set(kinds)

    def caller_package_dispersion(self, symbol_uid: str) -> float:
        cached = self._dispersion_cache.get(symbol_uid)
        if cached is not None:
            return cached
        rels = "|".join(_CONTROL_EDGE_TYPES)
        query = f"""
        MATCH (caller:Symbol)-[r:{rels}]->(:Symbol {{uid: $symbol_uid}})
        WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
        MATCH (file:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(caller)
        RETURN collect(DISTINCT file.path) AS paths
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
        paths = [str(path) for path in ((record and record.get("paths")) or []) if path]
        if len(paths) <= 1:
            value = 0.0
        else:
            roots = {_package_root(path) for path in paths if _package_root(path)}
            value = min(1.0, max(0.0, (len(roots) - 1) / (len(paths) - 1)))
        self._dispersion_cache[symbol_uid] = value
        return value

    def is_cfg_driver(self, symbol_uid: str) -> bool:
        # Plain outgoing control fan is too broad to prove "driver". Keep this
        # false until the graph materializes a narrow dispatch-loop marker.
        return False


__all__ = ["Neo4jGraphContextProbe"]
