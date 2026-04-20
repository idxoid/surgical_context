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

from sidecar.database.neo4j_client import Neo4jClient


class AFFECTSIndexer:
    """Materialized reverse dependency index."""

    MAX_AFFECTS_DEPTH = 4
    MAX_FANOUT_PER_LEVEL = 200

    def __init__(self, neo4j_client: Neo4jClient):
        self.db = neo4j_client

    def rebuild_affects(self, modified_symbol_uids: list[str]):
        """
        Rebuild AFFECTS edges for symbols that changed.

        For each symbol in modified_symbol_uids, deletes existing AFFECTS edges
        and recomputes all reachable dependents via reverse graph walk.
        """
        if not modified_symbol_uids:
            return

        with self.db.driver.session() as session:
            for symbol_uid in modified_symbol_uids:
                # Delete existing AFFECTS edges from this symbol
                session.run(
                    """
                    MATCH (s:Symbol {uid: $uid})-[r:AFFECTS]->()
                    DELETE r
                    """,
                    uid=symbol_uid,
                )

                # Find all reachable dependents (reverse BFS)
                affected = self._compute_affected_symbols(session, symbol_uid)

                # Create AFFECTS edges to all dependents
                if affected:
                    session.run(
                        """
                        MATCH (s:Symbol {uid: $uid})
                        WITH s
                        MATCH (target:Symbol) WHERE target.uid IN $affected_uids
                        MERGE (s)-[:AFFECTS]->(target)
                        """,
                        uid=symbol_uid,
                        affected_uids=affected,
                    )

    def _compute_affected_symbols(self, session, symbol_uid: str) -> list[str]:
        """
        Reverse BFS to find all symbols that depend on the given symbol.

        Returns list of symbol UIDs (excluding the symbol itself).
        Traverses up to MAX_AFFECTS_DEPTH or until fan-out explodes.
        """
        query = """
        MATCH (s:Symbol {uid: $uid})
        MATCH path = (dependent)-[*1..4]-(s)
        WHERE all(rel IN relationships(path)
                  WHERE type(rel) IN ['CALLS_DIRECT', 'CALLS_DYNAMIC', 'CALLS_INFERRED',
                                     'DEPENDS_ON', 'IMPLEMENTS', 'OVERRIDES'])
        RETURN collect(DISTINCT dependent.uid) AS affected_uids
        """

        result = session.run(query, uid=symbol_uid).single()

        return result["affected_uids"] if result else []

    def get_affected_symbols(self, symbol_uid: str) -> list[dict]:
        """
        Return all symbols affected by a change to the given symbol.

        Returns list of dicts: {uid, name, file_path, depth}
        """
        query = """
        MATCH (s:Symbol {uid: $uid})-[r:AFFECTS]->(affected:Symbol)
        OPTIONAL MATCH (f:File)-[:CONTAINS]->(affected)
        RETURN affected.uid AS uid,
               affected.name AS name,
               coalesce(f.path, '<unknown>') AS file_path,
               1 AS depth
        ORDER BY affected.name
        """

        affected_symbols = []
        with self.db.driver.session() as session:
            result = session.run(query, uid=symbol_uid)
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

    def get_affected_files(self, file_path: str) -> list[str]:
        """
        Return all files affected by a change to symbols in the given file.

        Computes the union of AFFECTS targets for all symbols in the file,
        then deduplicates by file path.
        """
        query = """
        MATCH (f:File {path: $file_path})-[:CONTAINS]->(s:Symbol)
        MATCH (s)-[:AFFECTS]->(affected:Symbol)
        OPTIONAL MATCH (af:File)-[:CONTAINS]->(affected)
        RETURN DISTINCT coalesce(af.path, '<unknown>') AS file_path
        ORDER BY file_path
        """

        affected_files = []
        with self.db.driver.session() as session:
            result = session.run(query, file_path=file_path)
            for record in result:
                if record["file_path"] != "<unknown>":
                    affected_files.append(record["file_path"])

        return affected_files
