"""AFFECTS reverse dependency index for cascade-aware reindexing (Phase 5, Gap 4).

The AFFECTS index tracks which symbols/files are transitively affected by a change
to a given symbol. This enables:
1. Incremental reindexing to cascade through dependents
2. Cache invalidation strategies (Phase 6)
3. Impact analysis API (/impact endpoint)

Algorithm:
  For each modified symbol:
    - Walk reverse CALLS/DEPENDS_ON/IMPLEMENTS/OVERRIDES edges (incoming = who depends on me)
    - Compute all reachable dependents up to depth 4 (or until fan-out explodes)
    - Create AFFECTS edges pointing from modified symbol to all reachable dependents
"""

from collections import defaultdict
from collections.abc import Callable

from sidecar.database.neo4j_client import Neo4jClient
from sidecar.workspace import DEFAULT_WORKSPACE_ID

_AFFECTS_REL_TYPES = [
    "CALLS_DIRECT",
    "CALLS_DYNAMIC",
    "CALLS_INFERRED",
    "CALLS_SCOPED",
    "CALLS_IMPORTED",
    "CALLS_GUESS",
    "DEPENDS_ON",
    "IMPLEMENTS",
    "OVERRIDES",
]


class AFFECTSIndexer:
    """Materialized reverse dependency index."""

    MAX_AFFECTS_DEPTH = 4
    MAX_FANOUT_PER_LEVEL = 200
    REBUILD_BATCH_SIZE = 128

    def __init__(self, neo4j_client: Neo4jClient):
        self.db = neo4j_client

    def rebuild_affects(
        self,
        modified_symbol_uids: list[str],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        progress_callback: Callable[[int], None] | None = None,
    ):
        """
        Rebuild AFFECTS edges for symbols that changed.

        For each symbol in modified_symbol_uids, deletes existing AFFECTS edges
        and recomputes all reachable dependents via reverse graph walk.
        """
        unique_uids = list(dict.fromkeys(modified_symbol_uids))
        if not unique_uids:
            return

        with self.db.driver.session() as session:
            session.run(
                """
                UNWIND $uids AS uid
                MATCH (s:Symbol {uid: uid})-[r:AFFECTS]->()
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                uids=unique_uids,
                workspace_id=workspace_id,
            )
            reverse_adjacency = self._load_reverse_adjacency(session, workspace_id)

            for batch in _chunked(unique_uids, self.REBUILD_BATCH_SIZE):
                pairs = self._compute_affected_pairs(reverse_adjacency, batch)
                if pairs:
                    session.run(
                        """
                        UNWIND $pairs AS pair
                        MATCH (s:Symbol {uid: pair.source_uid})
                        MATCH (target:Symbol {uid: pair.target_uid})
                        WHERE s <> target
                        MERGE (s)-[:AFFECTS {workspace_id: $workspace_id}]->(target)
                        """,
                        pairs=pairs,
                        workspace_id=workspace_id,
                    )
                if progress_callback:
                    progress_callback(len(batch))
            session.run(
                """
                MATCH (w:Workspace {id: $workspace_id})
                SET w.graph_version = coalesce(w.graph_version, 0) + 1
                """,
                workspace_id=workspace_id,
            )

    def _load_reverse_adjacency(
        self,
        session,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> dict[str, list[str]]:
        """Load reverse dependency adjacency for the whole workspace once."""
        query = """
        MATCH (dependent:Symbol)-[r]->(dependency:Symbol)
        WHERE type(r) IN $rel_types
          AND coalesce(r.workspace_id, $workspace_id) = $workspace_id
        RETURN dependency.uid AS dependency_uid, dependent.uid AS dependent_uid
        """
        adjacency: dict[str, set[str]] = defaultdict(set)
        result = session.run(
            query,
            rel_types=_AFFECTS_REL_TYPES,
            workspace_id=workspace_id,
        )
        for record in result:
            dependency_uid = record["dependency_uid"]
            dependent_uid = record["dependent_uid"]
            if dependency_uid and dependent_uid and dependency_uid != dependent_uid:
                adjacency[dependency_uid].add(dependent_uid)
        return {
            dependency_uid: sorted(dependent_uids)
            for dependency_uid, dependent_uids in adjacency.items()
        }

    def _compute_affected_pairs(
        self,
        reverse_adjacency: dict[str, list[str]],
        symbol_uids: list[str],
    ) -> list[dict[str, str]]:
        """
        Reverse BFS to find all symbols that depend on each given symbol.

        Returns flattened pairs: {source_uid, target_uid}.
        Traverses up to MAX_AFFECTS_DEPTH.
        """
        pairs: list[dict[str, str]] = []
        for source_uid in symbol_uids:
            frontier = [source_uid]
            visited: set[str] = set()

            for _depth in range(self.MAX_AFFECTS_DEPTH):
                next_frontier: list[str] = []
                seen_level: set[str] = set()
                for current_uid in frontier:
                    for dependent_uid in reverse_adjacency.get(current_uid, []):
                        if dependent_uid == source_uid or dependent_uid in visited or dependent_uid in seen_level:
                            continue
                        seen_level.add(dependent_uid)
                        next_frontier.append(dependent_uid)
                if not next_frontier:
                    break
                if len(next_frontier) > self.MAX_FANOUT_PER_LEVEL:
                    next_frontier = next_frontier[: self.MAX_FANOUT_PER_LEVEL]
                visited.update(next_frontier)
                frontier = next_frontier

            for target_uid in sorted(visited):
                pairs.append({"source_uid": source_uid, "target_uid": target_uid})
        return pairs

    def get_affected_symbols(
        self,
        symbol_uid: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> list[dict]:
        """
        Return all symbols affected by a change to the given symbol.

        Returns list of dicts: {uid, name, file_path, depth}
        """
        query = """
        MATCH (s:Symbol {uid: $uid})-[r:AFFECTS {workspace_id: $workspace_id}]->(affected:Symbol)
        OPTIONAL MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(affected)
        RETURN affected.uid AS uid,
               affected.name AS name,
               coalesce(f.path, '<unknown>') AS file_path,
               1 AS depth
        ORDER BY affected.name
        """

        affected_symbols = []
        with self.db.driver.session() as session:
            result = session.run(query, uid=symbol_uid, workspace_id=workspace_id)
            for record in result:
                affected_symbols.append(
                    {
                        "uid": record["uid"],
                        "name": record["name"],
                        "file_path": record["file_path"],
                        "depth": record["depth"],
                    }
                )

        return affected_symbols

    def get_affected_files(
        self,
        file_path: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> list[str]:
        """
        Return all files affected by a change to symbols in the given file.

        Computes the union of AFFECTS targets for all symbols in the file,
        then deduplicates by file path.
        """
        query = """
        MATCH (f:File {path: $file_path, workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
        MATCH (s)-[:AFFECTS {workspace_id: $workspace_id}]->(affected:Symbol)
        OPTIONAL MATCH (af:File {workspace_id: $workspace_id})-[:CONTAINS]->(affected)
        RETURN DISTINCT coalesce(af.path, '<unknown>') AS file_path
        ORDER BY file_path
        """

        affected_files = []
        with self.db.driver.session() as session:
            result = session.run(query, file_path=file_path, workspace_id=workspace_id)
            for record in result:
                if record["file_path"] != "<unknown>":
                    affected_files.append(record["file_path"])

        return affected_files


def _chunked(items: list[str], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]
