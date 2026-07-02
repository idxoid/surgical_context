"""Workspace / file / manifest / profile / degree edges."""

import json
from typing import Any

from context_engine.database.neo4j._common import (
    _DEGREE_REL_PATTERN,
    _WORKSPACE_GRAPH_VERSION_MATCH,
    _WORKSPACE_GRAPH_VERSION_SET,
    _bump_workspace_graph_version,
    _split_workspace_id,
    _symbol_row,
)
from context_engine.parser.protocol import (
    SymbolMetadata,
)
from context_engine.workspace import DEFAULT_WORKSPACE_ID


class WorkspaceMixin:
    driver: Any

    def ensure_workspace(self, workspace_id: str = DEFAULT_WORKSPACE_ID):
        workspace = _split_workspace_id(workspace_id)
        with self.driver.session() as session:
            session.run(
                """
                MERGE (w:Workspace {id: $id})
                SET w.tenant = $tenant,
                    w.repo = $repo,
                    w.ref = $ref,
                    w.ref_kind = $ref_kind,
                    w.last_seen = timestamp(),
                    w.graph_version = coalesce(w.graph_version, 0),
                    w.created_at = coalesce(w.created_at, timestamp())
                """,
                **workspace,
            )

    def upsert_file_structure(
        self,
        file_path: str,
        file_hash: str,
        symbols: list[SymbolMetadata],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        with self.driver.session() as session:
            session.execute_write(self._upsert_nodes, file_path, file_hash, symbols, workspace_id)

    def get_file_hashes(
        self, file_paths: list[str], workspace_id: str = DEFAULT_WORKSPACE_ID
    ) -> dict[str, str]:
        """Return {path: hash} for all known files in the given list."""
        if not file_paths:
            return {}
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (f:File {workspace_id: $workspace_id})
                WHERE f.path IN $paths
                RETURN f.path AS path, f.hash AS hash
                """,
                paths=file_paths,
                workspace_id=workspace_id,
            )
            return {r["path"]: r["hash"] for r in result}

    def get_symbol_index_for_file(
        self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID
    ) -> dict[str, dict]:
        """Return existing symbol hashes/ranges for one workspace file."""
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
                RETURN s.uid AS uid,
                       s.hash AS hash,
                       coalesce(c.start_line, s.range[0], 0) AS start_line,
                       coalesce(c.end_line, s.range[1], 0) AS end_line
                """,
                path=file_path,
                workspace_id=workspace_id,
            )
            return {
                r["uid"]: {
                    "hash": r["hash"],
                    "start_line": r["start_line"],
                    "end_line": r["end_line"],
                }
                for r in result
            }

    def get_workspace_profile_counts(
        self, workspace_id: str = DEFAULT_WORKSPACE_ID
    ) -> dict[str, int]:
        """Return workspace-level counts used by the repository readiness profile."""
        queries = {
            "files": """
                MATCH (f:File {workspace_id: $workspace_id})
                RETURN count(DISTINCT f) AS count
            """,
            "symbols": """
                MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
                RETURN count(DISTINCT s) AS count
            """,
            "calls": """
                MATCH (:Symbol)-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS]->(:Symbol)
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                RETURN count(r) AS count
            """,
            "imports": """
                MATCH (:File {workspace_id: $workspace_id})-[r:IMPORTS]->(:File {workspace_id: $workspace_id})
                RETURN count(r) AS count
            """,
            "inheritance": """
                MATCH (:Symbol)-[r:DEPENDS_ON|IMPLEMENTS|OVERRIDES]->(:Symbol)
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                RETURN count(r) AS count
            """,
            "affects": """
                MATCH (:Symbol)-[r:AFFECTS]->(:Symbol)
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                RETURN count(r) AS count
            """,
        }
        counts: dict[str, int] = {}
        with self.driver.session() as session:
            for key, query in queries.items():
                row = session.run(query, workspace_id=workspace_id).single()
                counts[key] = int(row["count"] if row else 0)
        return counts

    def get_workspace_dashboard_counts(
        self, workspace_id: str = DEFAULT_WORKSPACE_ID
    ) -> dict[str, int]:
        """Return dashboard graph counts in one workspace-scoped query."""
        with self.driver.session() as session:
            row = session.run(
                """
                MATCH (f:File {workspace_id: $workspace_id})
                OPTIONAL MATCH (f)-[:CONTAINS]->(s:Symbol)
                OPTIONAL MATCH (:DocAnchor)-[r:COVERS]->(s)
                WHERE r IS NULL OR coalesce(r.workspace_id, $workspace_id) = $workspace_id
                RETURN count(DISTINCT f) AS files,
                       count(DISTINCT s) AS symbols,
                       count(DISTINCT CASE WHEN r IS NOT NULL THEN s END) AS symbols_with_docs
                """,
                workspace_id=workspace_id,
            ).single()
        return {
            "files": int(row["files"] if row else 0),
            "symbols": int(row["symbols"] if row else 0),
            "symbols_with_docs": int(row["symbols_with_docs"] if row else 0),
        }

    def save_repository_profile(
        self,
        profile: dict,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> None:
        """Persist the index-time repository readiness profile on the Workspace."""
        payload = json.dumps(profile, sort_keys=True)
        with self.driver.session() as session:
            session.run(
                """
                MERGE (w:Workspace {id: $workspace_id})
                SET w.repository_profile_json = $profile_json,
                    w.repository_profile_schema_version = $schema_version,
                    w.repository_profile_updated_at = timestamp()
                """,
                workspace_id=workspace_id,
                profile_json=payload,
                schema_version=profile.get("schema_version", 1),
            )

    def get_repository_profile(
        self,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> dict | None:
        """Load the index-time repository readiness profile from the Workspace."""
        with self.driver.session() as session:
            row = session.run(
                """
                MATCH (w:Workspace {id: $workspace_id})
                RETURN w.repository_profile_json AS profile_json
                """,
                workspace_id=workspace_id,
            ).single()
        if not row or not row["profile_json"]:
            return None
        try:
            payload = json.loads(row["profile_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def get_workspace_graph_version(
        self,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> int | None:
        """Return Workspace.graph_version, or None if the node is missing."""
        with self.driver.session() as session:
            row = session.run(
                """
                MATCH (w:Workspace {id: $workspace_id})
                RETURN coalesce(w.graph_version, 0) AS gv
                """,
                workspace_id=workspace_id,
            ).single()
        if not row:
            return None
        return int(row["gv"])

    def save_index_manifest(
        self,
        manifest: dict,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> None:
        """Persist index manifest JSON on the Workspace node (retrieval reproducibility)."""
        payload = json.dumps(manifest, sort_keys=True)
        schema_version = int(manifest.get("manifest_schema_version", 1))
        with self.driver.session() as session:
            session.run(
                """
                MERGE (w:Workspace {id: $workspace_id})
                SET w.index_manifest_json = $manifest_json,
                    w.index_manifest_schema_version = $schema_version,
                    w.index_manifest_updated_at = timestamp(),
                    w.index_manifest_id = $manifest_id
                """,
                workspace_id=workspace_id,
                manifest_json=payload,
                schema_version=schema_version,
                manifest_id=str(manifest.get("manifest_id", "")),
            )

    def get_index_manifest(
        self,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> dict | None:
        """Load index manifest from the Workspace node."""
        with self.driver.session() as session:
            row = session.run(
                """
                MATCH (w:Workspace {id: $workspace_id})
                RETURN w.index_manifest_json AS manifest_json
                """,
                workspace_id=workspace_id,
            ).single()
        if not row or not row["manifest_json"]:
            return None
        try:
            payload = json.loads(row["manifest_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def list_file_paths(self, workspace_id: str = DEFAULT_WORKSPACE_ID) -> list[str]:
        """Return all File.path values for a workspace (for sandbox cleanup)."""
        with self.driver.session() as session:
            rows = list(
                session.run(
                    """
                    MATCH (f:File {workspace_id: $workspace_id})
                    RETURN f.path AS path
                    """,
                    workspace_id=workspace_id,
                )
            )
        return [str(row["path"]) for row in rows if row.get("path")]

    def prune_symbols_for_file(
        self,
        file_path: str,
        keep_uids: list[str],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Remove symbols no longer present in a file while preserving unchanged symbols."""
        with self.driver.session() as session:
            session.run(
                f"""
                MATCH (f:File {{path: $path, workspace_id: $workspace_id}})-[c:CONTAINS]->(s:Symbol)
                WHERE NOT s.uid IN $keep_uids
                OPTIONAL MATCH (s)-[r]-(other:Symbol)
                WHERE r IS NULL OR (
                    type(r) IN ['CALLS', 'CALLS_DIRECT', 'CALLS_SCOPED', 'CALLS_IMPORTED',
                                'CALLS_DYNAMIC', 'CALLS_INFERRED', 'CALLS_GUESS', 'DEPENDS_ON',
                                'IMPLEMENTS', 'OVERRIDES', 'AFFECTS', 'HAS_API', 'INHERITED_API',
                                'REFERENCES', 'REFERENCES_EXTERNAL']
                    AND coalesce(r.workspace_id, $workspace_id) = $workspace_id
                )
                DELETE r, c
                WITH collect(DISTINCT s) AS symbols
                UNWIND symbols AS sym
                OPTIONAL MATCH (owner:File)-[:CONTAINS]->(sym)
                // count(owner) not count(*): OPTIONAL MATCH keeps a null row for
                // ownerless symbols, so count(*) is never 0 and the delete below
                // would never fire; replaced symbols would linger as file-less
                // orphan nodes still holding their semantic edges.
                WITH sym, count(owner) AS owners
                WHERE owners = 0
                DETACH DELETE sym
                WITH count(*) AS deleted_symbols
                {_WORKSPACE_GRAPH_VERSION_MATCH}
                WHERE deleted_symbols > 0
                {_WORKSPACE_GRAPH_VERSION_SET}
                """,
                path=file_path,
                keep_uids=keep_uids,
                workspace_id=workspace_id,
            )

    def clear_outgoing_symbol_edges(
        self,
        symbol_uids: list[str],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Clear stale outgoing semantic edges for changed symbols before relinking."""
        if not symbol_uids:
            return
        with self.driver.session() as session:
            session.run(
                f"""
                MATCH (s:Symbol)-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|CALLS_EXTERNAL|DEPENDS_ON|IMPLEMENTS|OVERRIDES|HAS_API|INHERITED_API|REFERENCES|REFERENCES_EXTERNAL]->()
                WHERE s.uid IN $symbol_uids
                  AND coalesce(r.workspace_id, $workspace_id) = $workspace_id
                WITH collect(r) AS edges
                FOREACH (edge IN edges | DELETE edge)
                WITH size(edges) AS deleted_edges
                {_WORKSPACE_GRAPH_VERSION_MATCH}
                WHERE deleted_edges > 0
                {_WORKSPACE_GRAPH_VERSION_SET}
                """,
                symbol_uids=symbol_uids,
                workspace_id=workspace_id,
            )

    def prune_orphan_symbols(self, workspace_id: str = DEFAULT_WORKSPACE_ID) -> int:
        """Delete workspace Symbol nodes with no File CONTAINS links.

        Historical owner checks used ``count(*)`` after OPTIONAL MATCH, which
        leaves a null row and prevents ownerless symbols from being deleted.
        This self-healing sweep removes those stale nodes and fixes degrees for
        surviving neighbours that lost edges.
        """
        with self.driver.session() as session:
            rows = session.run(
                """
                MATCH (o:Symbol {workspace_id: $workspace_id})
                WHERE NOT ( (:File)-[:CONTAINS]->(o) )
                RETURN o.uid AS uid
                """,
                workspace_id=workspace_id,
            )
            orphan_uids = [str(row["uid"]) for row in rows if row.get("uid")]
        if not orphan_uids:
            return 0

        neighbors = self.degree_neighbor_uids(orphan_uids, workspace_id=workspace_id)
        orphan_set = set(orphan_uids)
        survivors = sorted(uid for uid in neighbors if uid not in orphan_set)
        with self.driver.session() as session:
            for start in range(0, len(orphan_uids), 2000):
                session.run(
                    """
                    MATCH (o:Symbol {workspace_id: $workspace_id})
                    WHERE o.uid IN $uids
                    DETACH DELETE o
                    """,
                    workspace_id=workspace_id,
                    uids=orphan_uids[start : start + 2000],
                )
            _bump_workspace_graph_version(session, workspace_id)
        if survivors:
            self.recompute_degree_for_closure(survivors, workspace_id=workspace_id)
        return len(orphan_uids)

    def degree_neighbor_uids(
        self,
        seed_uids: list[str],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> list[str]:
        """Direct degree-edge neighbors of ``seed_uids`` (snapshot before mutation).

        Call this before deleting symbols/edges so neighbors that lose an edge are
        still recomputed afterward — once a symbol is DETACH DELETE'd it is no longer
        reachable from the seeds, so the closure must be captured up front.
        """
        if not seed_uids:
            return []
        with self.driver.session() as session:
            rows = session.run(
                f"""
                MATCH (seed:Symbol)-[r:{_DEGREE_REL_PATTERN}]-(neighbor:Symbol)
                WHERE seed.uid IN $seed_uids
                  AND coalesce(r.workspace_id, $workspace_id) = $workspace_id
                RETURN DISTINCT neighbor.uid AS uid
                """,
                seed_uids=seed_uids,
                workspace_id=workspace_id,
            )
            return [str(row["uid"]) for row in rows if row.get("uid")]

    def recompute_degree_for_closure(
        self,
        seed_uids: list[str],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Recompute Symbol.in_degree/out_degree for seed symbols and their 1-hop neighbors.

        Degree is static topology, so the ranker reads it as a node property instead
        of recomputing count(DISTINCT) per query. To stay accurate under incremental
        ``update`` we recompute only the affected closure: when a file is relinked,
        edges change only for its symbols and their direct neighbors. Both endpoints
        of every changed edge land in the closure, so a neighbor whose own file was
        not reindexed still gets its degree corrected here.
        """
        if not seed_uids:
            return
        with self.driver.session() as session:
            session.run(
                f"""
                MATCH (seed:Symbol)
                WHERE seed.uid IN $seed_uids
                OPTIONAL MATCH (seed)-[nr:{_DEGREE_REL_PATTERN}]-(neighbor:Symbol)
                WHERE coalesce(nr.workspace_id, $workspace_id) = $workspace_id
                WITH collect(DISTINCT seed) + collect(DISTINCT neighbor) AS nodes
                UNWIND nodes AS s
                WITH DISTINCT s
                WHERE s IS NOT NULL
                OPTIONAL MATCH ()-[ir:{_DEGREE_REL_PATTERN}]->(s)
                WHERE coalesce(ir.workspace_id, $workspace_id) = $workspace_id
                WITH s, count(DISTINCT ir) AS in_degree
                OPTIONAL MATCH (s)-[orel:{_DEGREE_REL_PATTERN}]->()
                WHERE coalesce(orel.workspace_id, $workspace_id) = $workspace_id
                WITH s, in_degree, count(DISTINCT orel) AS out_degree
                SET s.in_degree = in_degree,
                    s.out_degree = out_degree
                """,
                seed_uids=seed_uids,
                workspace_id=workspace_id,
            )

    def delete_imports_for_file(self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID):
        """Clear stale file import edges before relinking current imports."""
        with self.driver.session() as session:
            session.run(
                f"""
                MATCH (:File {{path: $path, workspace_id: $workspace_id}})-[r:IMPORTS]->()
                WITH collect(r) AS edges
                FOREACH (edge IN edges | DELETE edge)
                WITH size(edges) AS deleted_edges
                {_WORKSPACE_GRAPH_VERSION_MATCH}
                WHERE deleted_edges > 0
                {_WORKSPACE_GRAPH_VERSION_SET}
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    def delete_symbols_for_file(self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID):
        """Remove workspace-local edges and orphaned symbols for a File."""
        with self.driver.session() as session:
            session.run(
                f"""
                MATCH (f:File {{path: $path, workspace_id: $workspace_id}})-[c:CONTAINS]->(s:Symbol)
                OPTIONAL MATCH (s)-[r]-(other:Symbol)
                WHERE r IS NULL OR (
                    type(r) IN ['CALLS', 'CALLS_DIRECT', 'CALLS_SCOPED', 'CALLS_IMPORTED',
                                'CALLS_DYNAMIC', 'CALLS_INFERRED', 'CALLS_GUESS', 'DEPENDS_ON',
                                'IMPLEMENTS', 'OVERRIDES', 'AFFECTS', 'HAS_API', 'INHERITED_API',
                                'REFERENCES', 'REFERENCES_EXTERNAL']
                    AND coalesce(r.workspace_id, $workspace_id) = $workspace_id
                )
                DELETE r, c
                WITH f, collect(DISTINCT s) AS symbols
                DETACH DELETE f
                WITH symbols
                UNWIND symbols AS sym
                OPTIONAL MATCH (owner:File)-[:CONTAINS]->(sym)
                // count(owner) not count(*): see prune_symbols_for_file.
                WITH sym, count(owner) AS owners
                WHERE owners = 0
                DETACH DELETE sym
                WITH count(*) AS deleted_symbols
                {_WORKSPACE_GRAPH_VERSION_MATCH}
                {_WORKSPACE_GRAPH_VERSION_SET}
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    @staticmethod
    def _upsert_nodes(tx, file_path, file_hash, symbols, workspace_id):
        workspace = _split_workspace_id(workspace_id)
        tx.run(
            """
            MERGE (w:Workspace {id: $id})
            SET w.tenant = $tenant,
                w.repo = $repo,
                w.ref = $ref,
                w.ref_kind = $ref_kind,
                w.last_indexed = timestamp(),
                w.graph_version = coalesce(w.graph_version, 0) + 1,
                w.created_at = coalesce(w.created_at, timestamp())
            MERGE (f:File {path: $path, workspace_id: $id})
            SET f.hash = $hash,
                f.last_indexed = timestamp()
            """,
            path=file_path,
            hash=file_hash,
            **workspace,
        )

        if not symbols:
            return
        tx.run(
            """
            MATCH (f:File {path: $file_path, workspace_id: $workspace_id})
            UNWIND $symbols AS symbol
            MERGE (s:Symbol {uid: symbol.uid})
            SET s.workspace_id = $workspace_id,
                s.name = symbol.name,
                s.kind = symbol.kind,
                s.hash = symbol.content_hash,
                s.range = [symbol.start, symbol.end],
                s.token_estimate = symbol.token_estimate,
                s.qualified_name = symbol.qualified_name,
                s.signature = symbol.signature,
                s.signature_hash = symbol.signature_hash,
                s.signature_status = symbol.signature_status,
                s.language = symbol.language,
                s.returns_function_expression = symbol.returns_function_expression,
                s.returns_mapping = symbol.returns_mapping,
                s.returns_sequence = symbol.returns_sequence,
                s.returns_constructed_type = symbol.returns_constructed_type,
                s.iterates_attr_call = symbol.iterates_attr_call,
                s.assembles_mapping_in_loop = symbol.assembles_mapping_in_loop,
                s.is_getter = symbol.is_getter,
                s.is_setter = symbol.is_setter,
                s.is_react_hook = symbol.is_react_hook
            MERGE (f)-[c:CONTAINS {workspace_id: $workspace_id}]->(s)
            SET c.range = [symbol.start, symbol.end],
                c.start_line = symbol.start,
                c.end_line = symbol.end,
                c.hash = symbol.content_hash
            """,
            file_path=file_path,
            workspace_id=workspace_id,
            symbols=[_symbol_row(symbol) for symbol in symbols],
        )
