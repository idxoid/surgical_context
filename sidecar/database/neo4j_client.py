from neo4j import GraphDatabase

from sidecar.parser.protocol import ImportEdge, InheritanceEdge, SymbolMetadata
from sidecar.workspace import DEFAULT_WORKSPACE_ID

_CALL_REL_TYPES = {
    "CALLS",
    "CALLS_DIRECT",
    "CALLS_SCOPED",
    "CALLS_IMPORTED",
    "CALLS_DYNAMIC",
    "CALLS_INFERRED",
    "CALLS_GUESS",
}


class Neo4jClient:
    def __init__(self, uri, user, password):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        self.driver.close()

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

    def prune_symbols_for_file(
        self,
        file_path: str,
        keep_uids: list[str],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Remove symbols no longer present in a file while preserving unchanged symbols."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
                WHERE NOT s.uid IN $keep_uids
                OPTIONAL MATCH (s)-[r]-(other:Symbol)
                WHERE r IS NULL OR (
                    type(r) IN ['CALLS', 'CALLS_DIRECT', 'CALLS_SCOPED', 'CALLS_IMPORTED',
                                'CALLS_DYNAMIC', 'CALLS_INFERRED', 'CALLS_GUESS', 'DEPENDS_ON',
                                'IMPLEMENTS', 'OVERRIDES', 'AFFECTS']
                    AND coalesce(r.workspace_id, $workspace_id) = $workspace_id
                )
                DELETE r, c
                WITH collect(DISTINCT s) AS symbols
                UNWIND symbols AS sym
                OPTIONAL MATCH (:File)-[:CONTAINS]->(sym)
                WITH sym, count(*) AS owners
                WHERE owners = 0
                DETACH DELETE sym
                WITH count(*) AS deleted_symbols
                MATCH (w:Workspace {id: $workspace_id})
                WHERE deleted_symbols > 0
                SET w.graph_version = coalesce(w.graph_version, 0) + 1
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
                """
                MATCH (s:Symbol)-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES]->(:Symbol)
                WHERE s.uid IN $symbol_uids
                  AND coalesce(r.workspace_id, $workspace_id) = $workspace_id
                WITH collect(r) AS edges
                FOREACH (edge IN edges | DELETE edge)
                WITH size(edges) AS deleted_edges
                MATCH (w:Workspace {id: $workspace_id})
                WHERE deleted_edges > 0
                SET w.graph_version = coalesce(w.graph_version, 0) + 1
                """,
                symbol_uids=symbol_uids,
                workspace_id=workspace_id,
            )

    def delete_imports_for_file(self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID):
        """Clear stale file import edges before relinking current imports."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (:File {path: $path, workspace_id: $workspace_id})-[r:IMPORTS]->()
                WITH collect(r) AS edges
                FOREACH (edge IN edges | DELETE edge)
                WITH size(edges) AS deleted_edges
                MATCH (w:Workspace {id: $workspace_id})
                WHERE deleted_edges > 0
                SET w.graph_version = coalesce(w.graph_version, 0) + 1
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    def delete_symbols_for_file(self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID):
        """Remove workspace-local edges and orphaned symbols for a File."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
                OPTIONAL MATCH (s)-[r]-(other:Symbol)
                WHERE r IS NULL OR (
                    type(r) IN ['CALLS', 'CALLS_DIRECT', 'CALLS_SCOPED', 'CALLS_IMPORTED',
                                'CALLS_DYNAMIC', 'CALLS_INFERRED', 'CALLS_GUESS', 'DEPENDS_ON',
                                'IMPLEMENTS', 'OVERRIDES', 'AFFECTS']
                    AND coalesce(r.workspace_id, $workspace_id) = $workspace_id
                )
                DELETE r, c
                WITH f, collect(DISTINCT s) AS symbols
                DETACH DELETE f
                WITH symbols
                UNWIND symbols AS sym
                OPTIONAL MATCH (:File)-[:CONTAINS]->(sym)
                WITH sym, count(*) AS owners
                WHERE owners = 0
                DETACH DELETE sym
                WITH count(*) AS deleted_symbols
                MATCH (w:Workspace {id: $workspace_id})
                SET w.graph_version = coalesce(w.graph_version, 0) + 1
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
            MERGE (f)-[:IN_WORKSPACE]->(w)
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
            MATCH (w:Workspace {id: $workspace_id})
            UNWIND $symbols AS symbol
            MERGE (s:Symbol {uid: symbol.uid})
            SET s.name = symbol.name,
                s.kind = symbol.kind,
                s.hash = symbol.content_hash,
                s.range = [symbol.start, symbol.end],
                s.token_estimate = symbol.token_estimate,
                s.qualified_name = symbol.qualified_name,
                s.signature = symbol.signature,
                s.signature_hash = symbol.signature_hash,
                s.signature_status = symbol.signature_status,
                s.language = symbol.language
            MERGE (s)-[:IN_WORKSPACE]->(w)
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

    def link_calls(self, calls: list[dict], workspace_id: str = DEFAULT_WORKSPACE_ID):
        with self.driver.session() as session:
            session.execute_write(self._create_call_relations, calls, workspace_id)
            if calls:
                session.run(
                    """
                    MATCH (w:Workspace {id: $workspace_id})
                    SET w.graph_version = coalesce(w.graph_version, 0) + 1
                    """,
                    workspace_id=workspace_id,
                )

    @staticmethod
    def _create_call_relations(tx, calls, workspace_id):
        if not calls:
            return
        for rel_type, mode, rows in _grouped_call_rows(calls):
            if mode == "uid":
                tx.run(
                    f"""
                    UNWIND $calls AS call
                    MATCH (caller:Symbol {{uid: call.caller_uid}})
                    MATCH (callee:Symbol {{uid: call.callee_uid}})
                    WHERE caller <> callee
                    MERGE (caller)-[r:{rel_type} {{workspace_id: $workspace_id,
                                                   call_site_line: call.call_site_line}}]->(callee)
                    SET r.confidence = call.confidence,
                        r.tier = call.tier,
                        r.resolver = call.resolver
                    """,
                    calls=rows,
                    workspace_id=workspace_id,
                )
            elif mode == "qualified_name":
                tx.run(
                    f"""
                    UNWIND $calls AS call
                    MATCH (caller:Symbol {{uid: call.caller_uid}})
                    MATCH (:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(callee:Symbol {{qualified_name: call.callee_qualified_name}})
                    WHERE caller <> callee
                    MERGE (caller)-[r:{rel_type} {{workspace_id: $workspace_id,
                                                   call_site_line: call.call_site_line}}]->(callee)
                    SET r.confidence = call.confidence,
                        r.tier = call.tier,
                        r.resolver = call.resolver
                    """,
                    calls=rows,
                    workspace_id=workspace_id,
                )
            else:
                tx.run(
                    f"""
                    UNWIND $calls AS call
                    MATCH (caller:Symbol {{uid: call.caller_uid}})
                    MATCH (:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(candidate:Symbol {{name: call.callee_name}})
                    WHERE caller <> candidate
                    WITH call, caller, collect(DISTINCT candidate) AS candidates
                    WHERE size(candidates) = 1
                    WITH call, caller, candidates[0] AS callee
                    MERGE (caller)-[r:{rel_type} {{workspace_id: $workspace_id,
                                                   call_site_line: call.call_site_line}}]->(callee)
                    SET r.confidence = call.confidence,
                        r.tier = call.tier,
                        r.resolver = call.resolver
                    """,
                    calls=rows,
                    workspace_id=workspace_id,
                )

    def link_imports(self, imports: list[ImportEdge], workspace_id: str = DEFAULT_WORKSPACE_ID):
        with self.driver.session() as session:
            session.execute_write(self._create_import_relations, imports, workspace_id)
            if imports:
                session.run(
                    """
                    MATCH (w:Workspace {id: $workspace_id})
                    SET w.graph_version = coalesce(w.graph_version, 0) + 1
                    """,
                    workspace_id=workspace_id,
                )

    @staticmethod
    def _create_import_relations(tx, imports, workspace_id):
        if not imports:
            return
        tx.run(
            """
            UNWIND $imports AS imp
            MATCH (source:File {path: imp.source_file, workspace_id: $workspace_id})
            MATCH (target:File {workspace_id: $workspace_id})
            WHERE target.path ENDS WITH imp.path_suffix
              AND source <> target
            MERGE (source)-[:IMPORTS {type: imp.import_type, workspace_id: $workspace_id}]->(target)
            """,
            imports=[_import_row(imp) for imp in imports],
            workspace_id=workspace_id,
        )

    def link_inheritance(
        self,
        inheritance_edges: list[InheritanceEdge],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        with self.driver.session() as session:
            session.execute_write(
                self._create_inheritance_relations, inheritance_edges, workspace_id
            )
            if inheritance_edges:
                session.run(
                    """
                    MATCH (w:Workspace {id: $workspace_id})
                    SET w.graph_version = coalesce(w.graph_version, 0) + 1
                    """,
                    workspace_id=workspace_id,
                )

    @staticmethod
    def _create_inheritance_relations(tx, inheritance_edges, workspace_id):
        if not inheritance_edges:
            return
        tx.run(
            """
            UNWIND $inheritance_edges AS edge
            MATCH (subclass:Symbol {uid: edge.subclass_uid})
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(superclass:Symbol {name: edge.superclass_name})
            MERGE (subclass)-[r:DEPENDS_ON {workspace_id: $workspace_id}]->(superclass)
            SET r.is_interface = edge.is_interface,
                r.confidence = 0.9,
                r.tier = 'scoped',
                r.resolver = 'inheritance-v1'
            """,
            inheritance_edges=[_inheritance_row(edge) for edge in inheritance_edges],
            workspace_id=workspace_id,
        )


def _split_workspace_id(workspace_id: str) -> dict[str, str]:
    tenant_repo, _, ref = workspace_id.partition("@")
    tenant, _, repo = tenant_repo.partition("/")
    return {
        "id": workspace_id,
        "tenant": tenant or "local",
        "repo": repo or "repo",
        "ref": ref or "main",
        "ref_kind": "commit" if _looks_like_sha(ref) else "branch",
    }


def _looks_like_sha(ref: str) -> bool:
    return len(ref) in range(7, 41) and all(c in "0123456789abcdef" for c in ref.lower())


def _default_confidence(rel_type: str) -> float:
    return {
        "CALLS_DIRECT": 1.0,
        "CALLS_SCOPED": 0.9,
        "CALLS_IMPORTED": 0.85,
        "CALLS_DYNAMIC": 0.7,
        "CALLS_INFERRED": 0.4,
        "CALLS_GUESS": 0.4,
    }.get(rel_type, 0.4)


def _default_tier(rel_type: str) -> str:
    return {
        "CALLS_DIRECT": "direct",
        "CALLS_SCOPED": "scoped",
        "CALLS_IMPORTED": "imported",
        "CALLS_DYNAMIC": "dynamic",
        "CALLS_INFERRED": "guess",
        "CALLS_GUESS": "guess",
    }.get(rel_type, "guess")


def _symbol_row(symbol: SymbolMetadata) -> dict[str, object]:
    return {
        "uid": symbol.uid,
        "name": symbol.name,
        "kind": symbol.kind,
        "content_hash": symbol.content_hash,
        "start": symbol.start_line,
        "end": symbol.end_line,
        "token_estimate": symbol.token_estimate,
        "qualified_name": symbol.qualified_name,
        "signature": symbol.signature,
        "signature_hash": symbol.signature_hash,
        "signature_status": symbol.signature_status,
        "language": symbol.language,
    }


def _call_row(call: dict, rel_type: str) -> dict[str, object]:
    return {
        "caller_uid": call["caller_uid"],
        "callee_uid": call.get("callee_uid"),
        "callee_name": call.get("callee_name"),
        "callee_qualified_name": call.get("callee_qualified_name"),
        "confidence": float(call.get("confidence", _default_confidence(rel_type))),
        "tier": call.get("tier", _default_tier(rel_type)),
        "resolver": call.get("resolver", "scope-v1"),
        "call_site_line": call.get("call_site_line"),
    }


def _call_mode(row: dict[str, object]) -> str:
    if row.get("callee_uid"):
        return "uid"
    if row.get("callee_qualified_name"):
        return "qualified_name"
    return "name"


def _grouped_call_rows(calls: list[dict]) -> list[tuple[str, str, list[dict[str, object]]]]:
    groups: dict[tuple[str, str], list[dict[str, object]]] = {}
    for call in calls:
        rel_type = call.get("rel_type", "CALLS_DIRECT")
        if rel_type not in _CALL_REL_TYPES:
            rel_type = "CALLS_GUESS"
        row = _call_row(call, rel_type)
        key = (rel_type, _call_mode(row))
        groups.setdefault(key, []).append(row)
    return [(rel_type, mode, rows) for (rel_type, mode), rows in groups.items()]


def _import_row(imp: ImportEdge) -> dict[str, str]:
    return {
        "source_file": imp.source_file,
        "path_suffix": imp.target_module_name.lstrip(".").replace(".", "/") + ".py",
        "import_type": imp.import_type,
    }


def _inheritance_row(edge: InheritanceEdge) -> dict[str, object]:
    return {
        "subclass_uid": edge.subclass_uid,
        "superclass_name": edge.superclass_name,
        "is_interface": edge.is_interface,
    }
