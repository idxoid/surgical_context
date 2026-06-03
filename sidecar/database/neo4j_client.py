import json
from pathlib import Path

from neo4j import GraphDatabase

from sidecar.parser.protocol import ClassApiEdge, ImportEdge, InheritanceEdge, SymbolMetadata
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

# Edge types counted into Symbol.in_degree / out_degree. MUST stay identical to
# the relationship list the ranker read queries aggregate (recovery.py), or the
# materialized degree will not faithfully replace their count(DISTINCT) subquery.
_DEGREE_REL_PATTERN = (
    "CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|"
    "CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT"
)


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
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
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
                MATCH (s:Symbol)-[r:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|CALLS_EXTERNAL|DEPENDS_ON|IMPLEMENTS|OVERRIDES|HAS_API|INHERITED_API|REFERENCES|REFERENCES_EXTERNAL]->()
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
                                'IMPLEMENTS', 'OVERRIDES', 'AFFECTS', 'HAS_API', 'INHERITED_API',
                                'REFERENCES', 'REFERENCES_EXTERNAL']
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
                s.language = symbol.language,
                s.returns_function_expression = symbol.returns_function_expression
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
        if not calls:
            return
        # Pre-resolve `qualified_name` and `name` modes Python-side, exactly like
        # link_imports: one round-trip pulls the workspace symbol index, then each
        # call resolves to a single callee_uid by dict lookup. The Cypher then runs a
        # single index-friendly uid→uid MERGE. The old per-mode queries did
        # workspace-wide `MATCH ...{name: callee_name}` + `collect(DISTINCT)` and
        # `OPTIONAL MATCH ... STARTS WITH surface.qualified_name + '.'` — both
        # O(rows × symbols), dominating graph time on fastapi/pydantic.
        resolved = self._resolve_call_callees(calls, workspace_id=workspace_id)
        with self.driver.session() as session:
            session.execute_write(self._create_call_relations, resolved, workspace_id)
            session.run(
                """
                MATCH (w:Workspace {id: $workspace_id})
                SET w.graph_version = coalesce(w.graph_version, 0) + 1
                """,
                workspace_id=workspace_id,
            )

    def _resolve_call_callees(
        self,
        calls: list[dict],
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> list[dict]:
        """Attach a `callee_uid` to every resolvable call row Python-side.

        Mirrors the Cypher semantics the old per-mode queries implemented:
        - ``qualified_name`` exact match wins; on miss, the longest ``object_api``
          surface whose qualified_name is a prefix of the call's qualified name
          (matching `STARTS WITH surface.qn + '.'` in the old query).
        - ``name`` resolves only when exactly one Symbol carries that name
          workspace-wide (matching `collect(DISTINCT) WHERE size = 1`).
        Rows whose callee cannot be resolved are dropped — same as the Cypher's
        `WHERE callee IS NOT NULL`.
        """
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
                RETURN s.uid AS uid,
                       s.name AS name,
                       coalesce(s.qualified_name, '') AS qn,
                       coalesce(s.kind, '') AS kind
                """,
                workspace_id=workspace_id,
            )
            rows = list(result)
        by_qn: dict[str, str] = {}
        by_name: dict[str, list[str]] = {}
        # Object-API surfaces sorted by qn length DESC, so the longest matching
        # prefix wins (the old query did the same via ORDER BY size(qn) DESC).
        object_api: list[tuple[str, str]] = []
        for r in rows:
            uid = r["uid"]
            if not uid:
                continue
            qn = r["qn"] or ""
            if qn:
                by_qn.setdefault(qn, uid)
            name = r["name"] or ""
            if name:
                by_name.setdefault(name, []).append(uid)
            if r["kind"] == "object_api" and qn:
                object_api.append((qn, uid))
        object_api.sort(key=lambda x: -len(x[0]))

        out: list[dict] = []
        for call in calls:
            if call.get("callee_uid"):
                out.append(call)
                continue
            qn = call.get("callee_qualified_name")
            if qn:
                hit = by_qn.get(qn)
                if hit is None:
                    for surf_qn, surf_uid in object_api:
                        if qn.startswith(surf_qn + "."):
                            hit = surf_uid
                            break
                if hit and hit != call.get("caller_uid"):
                    out.append({**call, "callee_uid": hit})
                    continue
                # qn miss → fall through to unique-name resolution. The qn was
                # the extractor's best guess (typically derived from an import
                # statement), but the in-graph qualified_name can prefix or
                # otherwise diverge from that guess when the project layout
                # adds a path segment (e.g. ``src/dathund_core/X`` stores as
                # ``src.dathund_core.X`` while the import reads
                # ``dathund_core.X``). The downstream name fallback is already
                # gated on workspace-wide uniqueness, so this can only
                # *recover* a call that would otherwise be silently dropped —
                # it cannot bind to the wrong target.
            name = call.get("callee_name")
            if name:
                cands = by_name.get(name) or []
                if len(cands) == 1 and cands[0] != call.get("caller_uid"):
                    out.append({**call, "callee_uid": cands[0]})
        return out

    @staticmethod
    def _create_call_relations(tx, calls, workspace_id):
        if not calls:
            return
        # All rows are now uid→uid (resolved by link_calls); group by rel_type and
        # MERGE in one UNWIND per type. The Cypher is an index lookup on both
        # endpoints — the workspace-wide MATCH/collect of the old name/qn modes is
        # gone.
        by_rel: dict[str, list[dict]] = {}
        for call in calls:
            rel_type = call.get("rel_type", "CALLS_DIRECT")
            if rel_type not in _CALL_REL_TYPES:
                rel_type = "CALLS_GUESS"
            by_rel.setdefault(rel_type, []).append(_call_row(call, rel_type))
        for rel_type, rows in by_rel.items():
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

    def link_imports(self, imports: list[ImportEdge], workspace_id: str = DEFAULT_WORKSPACE_ID):
        if not imports:
            return
        # Resolve each import's candidate suffix list against the workspace's actual
        # File.path set Python-side, then issue an exact-match MERGE. The original
        # query did `target.path ENDS WITH suffix` inside a UNWIND, which is O(N×M)
        # on (imports × files) — link_imports dominated graph time on fastapi (39s).
        # With one round-trip for file paths + indexed equality, that work happens in
        # a Python dict lookup and the Cypher becomes an index-friendly MATCH.
        file_paths = list(self.list_file_paths(workspace_id=workspace_id))
        file_path_set = set(file_paths)
        # Suffix index: every path is registered under each of its trailing 1..4
        # segments. Imports rarely care beyond 3-4 segments, so this is O(M) build
        # for O(1) lookup per suffix (vs O(M) scan with str.endswith).
        suffix_index: dict[str, str] = {}
        for path in file_paths:
            parts = path.split("/")
            for k in range(1, min(5, len(parts)) + 1):
                key = "/" + "/".join(parts[-k:])
                # First registrant wins; later collisions keep the earlier (shorter)
                # path, matching the original "first match" semantics of the loop.
                suffix_index.setdefault(key, path)
        resolved: list[dict[str, object]] = []
        for imp in imports:
            row = _import_row(imp)
            target_path: str | None = None
            for suffix in row["path_suffixes"]:  # type: ignore[index]
                if suffix in file_path_set:
                    target_path = suffix  # type: ignore[assignment]
                    break
                hit = suffix_index.get(suffix)
                if hit is not None:
                    target_path = hit
                    break
            if target_path is None or target_path == imp.source_file:
                continue
            resolved.append(
                {
                    "source_file": imp.source_file,
                    "target_path": target_path,
                    "import_type": imp.import_type,
                }
            )
        with self.driver.session() as session:
            session.execute_write(self._create_import_relations, resolved, workspace_id)
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
            MATCH (target:File {path: imp.target_path, workspace_id: $workspace_id})
            WHERE source <> target
            MERGE (source)-[:IMPORTS {type: imp.import_type, workspace_id: $workspace_id}]->(target)
            """,
            imports=imports,
            workspace_id=workspace_id,
        )

    def delete_external_imports_for_file(
        self,
        file_path: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[r:IMPORTS_EXTERNAL]->(:ExternalPkg)
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
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

    def link_external_boundary(
        self,
        call_links: list[dict],
        import_links: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> tuple[int, int]:
        """Materialize ``(:ExternalPkg)`` targets and ``*_EXTERNAL`` edges (C1)."""
        with self.driver.session() as session:
            calls_created, imports_created = session.execute_write(
                self._create_external_boundary_relations,
                call_links,
                import_links,
                workspace_id,
            )
            if calls_created or imports_created:
                session.run(
                    """
                    MATCH (w:Workspace {id: $workspace_id})
                    SET w.graph_version = coalesce(w.graph_version, 0) + 1
                    """,
                    workspace_id=workspace_id,
                )
            session.run(
                """
                MATCH (e:ExternalPkg {workspace_id: $workspace_id})
                WHERE NOT (e)<-[:CALLS_EXTERNAL|IMPORTS_EXTERNAL|REFERENCES_EXTERNAL {workspace_id: $workspace_id}]-()
                DETACH DELETE e
                """,
                workspace_id=workspace_id,
            )
        return calls_created, imports_created

    @staticmethod
    def _create_external_boundary_relations(tx, call_links, import_links, workspace_id):
        from sidecar.indexer.external_boundary import external_pkg_uid

        roots = sorted(
            {
                *(str(row.get("external_root") or "") for row in call_links),
                *(str(row.get("external_root") or "") for row in import_links),
            }
            - {""}
        )
        if roots:
            tx.run(
                """
                MATCH (w:Workspace {id: $workspace_id})
                UNWIND $roots AS root
                MERGE (e:ExternalPkg {uid: root.uid, workspace_id: $workspace_id})
                SET e.name = root.name,
                    e.root = root.name,
                    e.qualified_name = root.name,
                    e.is_external = true
                MERGE (e)-[:IN_WORKSPACE]->(w)
                """,
                workspace_id=workspace_id,
                roots=[
                    {
                        "name": root,
                        "uid": external_pkg_uid(workspace_id, root),
                    }
                    for root in roots
                ],
            )
        calls_created = 0
        if call_links:
            rec = tx.run(
                """
                UNWIND $rows AS row
                MATCH (caller:Symbol {uid: row.caller_uid})
                MATCH (e:ExternalPkg {uid: row.external_uid, workspace_id: $workspace_id})
                MERGE (caller)-[r:CALLS_EXTERNAL {
                    workspace_id: $workspace_id,
                    call_site_line: row.call_site_line
                }]->(e)
                SET r.confidence = row.confidence,
                    r.resolver = 'external-boundary-v1',
                    r.callee_member = row.callee_member
                RETURN count(r) AS c
                """,
                rows=call_links,
                workspace_id=workspace_id,
            ).single()
            calls_created = int(rec["c"]) if rec else 0
        imports_created = 0
        if import_links:
            rec = tx.run(
                """
                UNWIND $rows AS row
                MATCH (f:File {path: row.file_path, workspace_id: $workspace_id})
                MATCH (e:ExternalPkg {uid: row.external_uid, workspace_id: $workspace_id})
                MERGE (f)-[r:IMPORTS_EXTERNAL {workspace_id: $workspace_id}]->(e)
                SET r.confidence = 1.0,
                    r.resolver = 'external-boundary-v1'
                RETURN count(r) AS c
                """,
                rows=import_links,
                workspace_id=workspace_id,
            ).single()
            imports_created = int(rec["c"]) if rec else 0
        return calls_created, imports_created

    def link_class_api(
        self,
        edges: list[ClassApiEdge],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        with self.driver.session() as session:
            session.execute_write(self._create_class_api_relations, edges, workspace_id)
            if edges:
                session.run(
                    """
                    MATCH (w:Workspace {id: $workspace_id})
                    SET w.graph_version = coalesce(w.graph_version, 0) + 1
                    """,
                    workspace_id=workspace_id,
                )

    def clear_class_api_edges(self, workspace_id: str = DEFAULT_WORKSPACE_ID):
        with self.driver.session() as session:
            session.run(
                """
                MATCH (:Symbol)-[r:HAS_API|INHERITED_API {workspace_id: $workspace_id}]->(:Symbol)
                WHERE coalesce(r.resolver, 'mro-v1') = 'mro-v1'
                WITH collect(r) AS edges
                FOREACH (edge IN edges | DELETE edge)
                WITH size(edges) AS deleted_edges
                MATCH (w:Workspace {id: $workspace_id})
                WHERE deleted_edges > 0
                SET w.graph_version = coalesce(w.graph_version, 0) + 1
                """,
                workspace_id=workspace_id,
            )

    def link_symbol_api_edges(
        self,
        edges: list[ClassApiEdge],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> tuple[int, set[str]]:
        """Create HAS_API edges from non-class property-owner surfaces."""
        if not edges:
            return 0, set()
        with self.driver.session() as session:
            created, touched = session.execute_write(
                self._create_symbol_api_relations,
                edges,
                workspace_id,
            )
            if created:
                session.run(
                    """
                    MATCH (w:Workspace {id: $workspace_id})
                    SET w.graph_version = coalesce(w.graph_version, 0) + 1
                    """,
                    workspace_id=workspace_id,
                )
            return created, touched

    @staticmethod
    def _create_symbol_api_relations(tx, edges: list[ClassApiEdge], workspace_id: str):
        rows = [
            {"owner_uid": edge.class_uid, "method_uid": edge.method_uid}
            for edge in edges
            if edge.edge_type == "HAS_API"
        ]
        if not rows:
            return 0, set()
        rec = tx.run(
            """
            UNWIND $edges AS edge
            MATCH (owner:Symbol {uid: edge.owner_uid})
            MATCH (method:Symbol {uid: edge.method_uid})
            WHERE owner <> method
            MERGE (owner)-[r:HAS_API {
                workspace_id: $workspace_id,
                resolver: 'property-api-v1'
            }]->(method)
            SET r.confidence = 0.9,
                r.tier = 'scoped'
            RETURN count(r) AS created,
                   collect(DISTINCT owner.uid) + collect(DISTINCT method.uid) AS touched
            """,
            edges=rows,
            workspace_id=workspace_id,
        ).single()
        if not rec:
            return 0, set()
        return int(rec["created"] or 0), {
            str(uid) for uid in (rec["touched"] or []) if uid
        }

    @staticmethod
    def _create_class_api_relations(tx, edges: list[ClassApiEdge], workspace_id: str):
        if not edges:
            return
        direct = [edge for edge in edges if edge.edge_type == "HAS_API"]
        inherited = [edge for edge in edges if edge.edge_type == "INHERITED_API"]
        if direct:
            tx.run(
                """
                UNWIND $edges AS edge
                MATCH (cls:Symbol {uid: edge.class_uid})
                MATCH (method:Symbol {uid: edge.method_uid})
                MERGE (cls)-[r:HAS_API {workspace_id: $workspace_id}]->(method)
                SET r.resolver = 'mro-v1',
                    r.confidence = 0.95,
                    r.tier = 'scoped'
                """,
                edges=[
                    {"class_uid": edge.class_uid, "method_uid": edge.method_uid} for edge in direct
                ],
                workspace_id=workspace_id,
            )
        if inherited:
            tx.run(
                """
                UNWIND $edges AS edge
                MATCH (cls:Symbol {uid: edge.class_uid})
                MATCH (method:Symbol {uid: edge.method_uid})
                MERGE (cls)-[r:INHERITED_API {workspace_id: $workspace_id}]->(method)
                SET r.resolver = 'mro-v1',
                    r.confidence = 0.9,
                    r.tier = 'scoped',
                    r.originating_class = edge.originating_class
                """,
                edges=[
                    {
                        "class_uid": edge.class_uid,
                        "method_uid": edge.method_uid,
                        "originating_class": edge.originating_class,
                    }
                    for edge in inherited
                ],
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
        # Builtin-exception inheritance: the base is not an in-graph symbol, so no
        # DEPENDS_ON edge is created above. Mark the subclass so the cascade can
        # derive `error_surface` from this real AST fact (P5: a structural signal,
        # not a name/keyword match — driven by the standard exception hierarchy).
        exc_rows = [
            {"subclass_uid": edge.subclass_uid}
            for edge in inheritance_edges
            if edge.superclass_name.rsplit(".", 1)[-1] in _BUILTIN_EXCEPTION_BASES
        ]
        if exc_rows:
            tx.run(
                """
                UNWIND $rows AS row
                MATCH (s:Symbol {uid: row.subclass_uid})
                SET s.inherits_builtin_exception = true
                """,
                rows=exc_rows,
            )
        # Transitive propagation: a class inheriting an *in-graph* exception
        # (UsageError -> ClickException -> Exception) is also an error type. Mark any
        # subclass on a DEPENDS_ON inheritance chain that reaches a builtin-exception
        # base. Idempotent and order-independent: it walks the full inheritance graph,
        # so it converges regardless of which file linked first.
        tx.run(
            """
            MATCH (bf:File {workspace_id: $workspace_id})-[:CONTAINS]->(base:Symbol)
            WHERE base.inherits_builtin_exception = true
            MATCH (sf:File {workspace_id: $workspace_id})-[:CONTAINS]->(sub:Symbol)
            WHERE coalesce(sub.inherits_builtin_exception, false) = false
              AND (sub)-[:DEPENDS_ON*1..6]->(base)
            SET sub.inherits_builtin_exception = true
            """,
            workspace_id=workspace_id,
        )

    def link_proxy_bindings(
        self,
        proxy_bindings: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Create ProxyBinding nodes + PROXY_OF edges for lazy-proxy module vars.

        A ProxyBinding is a transit anchor (``kind='proxy_binding'``), not a retrieval
        target; the resolution phase forwards calls THROUGH it to the real type. The
        ``PROXY_OF`` edge points at the annotated target type (matched by trailing
        qualified-name segment, robust to source-root prefix differences).
        """
        if not proxy_bindings:
            return
        with self.driver.session() as session:
            session.execute_write(self._create_proxy_relations, proxy_bindings, workspace_id)
            session.run(
                """
                MATCH (w:Workspace {id: $workspace_id})
                SET w.graph_version = coalesce(w.graph_version, 0) + 1
                """,
                workspace_id=workspace_id,
            )

    @staticmethod
    def _create_proxy_relations(tx, proxy_bindings, workspace_id):
        if not proxy_bindings:
            return
        tx.run(
            """
            UNWIND $bindings AS b
            MATCH (f:File {path: b.file_path, workspace_id: $workspace_id})
            MERGE (p:Symbol {uid: b.proxy_uid})
            SET p.name = b.proxy_name,
                p.kind = 'proxy_binding',
                p.qualified_name = b.proxy_qualified_name
            MERGE (f)-[:CONTAINS {workspace_id: $workspace_id}]->(p)
            WITH p, b
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(target:Symbol)
            WHERE target.kind IN ['class', 'function', 'method']
              AND (target.qualified_name = b.target_type
                   OR target.qualified_name ENDS WITH ('.' + split(b.target_type, '.')[-1]))
            WITH p, b, target
            ORDER BY size(target.qualified_name) ASC
            WITH p, b, collect(target)[0] AS target
            WHERE target IS NOT NULL
            MERGE (p)-[r:PROXY_OF {workspace_id: $workspace_id}]->(target)
            SET r.resolver = CASE b.target_source
                               WHEN 'wrapped_callable' THEN 'proxysurface-callable-v1'
                               ELSE 'proxysurface-v1' END,
                r.target_source = coalesce(b.target_source, 'annotation'),
                r.wrapped_callable = b.wrapped_callable,
                r.confidence = coalesce(b.confidence, 1.0)
            """,
            bindings=proxy_bindings,
            workspace_id=workspace_id,
        )

    def resolve_proxy_calls(
        self,
        proxy_calls: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> int:
        """Forward calls on a proxy var to the real type's method via PROXY_OF.

        ``proxy_calls`` are parsed call facts whose ``callee_qualified_name`` is
        ``<proxy_var_qn>.<method>``. We split off the trailing method, match the
        prefix to a ProxyBinding (by trailing var name, prefix-agnostic), follow
        ``PROXY_OF`` to the target type, and wire ``caller -> target.method`` (the
        method found directly on the target or via INHERITED_API). The ``via_proxy``
        edge property marks the hop as transparent for the ranker.
        """
        if not proxy_calls:
            return 0
        rows = []
        for c in proxy_calls:
            qn = c.get("callee_qualified_name") or ""
            if "." not in qn:
                continue
            prefix, _, method = qn.rpartition(".")
            proxy_var = prefix.rpartition(".")[2]
            if not proxy_var or not method or not c.get("caller_uid"):
                continue
            rows.append(
                {
                    "caller_uid": c["caller_uid"],
                    "proxy_var": proxy_var,
                    "method": method,
                    "call_site_line": c.get("call_site_line") or 0,
                }
            )
        if not rows:
            return 0
        query = """
        UNWIND $rows AS row
        MATCH (caller:Symbol {uid: row.caller_uid})
        MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(p:Symbol {kind: 'proxy_binding', name: row.proxy_var})
        MATCH (p)-[:PROXY_OF {workspace_id: $workspace_id}]->(t:Symbol)
        OPTIONAL MATCH (t)-[:HAS_API|INHERITED_API]->(direct:Symbol {name: row.method})
        OPTIONAL MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(own:Symbol {name: row.method})
        WHERE own.qualified_name STARTS WITH t.qualified_name + '.'
        WITH caller, p, row, coalesce(direct, own) AS callee
        WHERE callee IS NOT NULL AND caller <> callee
        MERGE (caller)-[r:CALLS_DYNAMIC {workspace_id: $workspace_id,
                                        call_site_line: row.call_site_line}]->(callee)
        SET r.confidence = 0.75,
            r.tier = 'proxy',
            r.resolver = 'proxysurface-v1',
            r.via_proxy = row.proxy_var
        RETURN count(r) AS created
        """
        try:
            with self.driver.session() as session:
                rec = session.run(query, rows=rows, workspace_id=workspace_id).single()
                created = int(rec["created"]) if rec else 0
                if created:
                    session.run(
                        """
                        MATCH (w:Workspace {id: $workspace_id})
                        SET w.graph_version = coalesce(w.graph_version, 0) + 1
                        """,
                        workspace_id=workspace_id,
                    )
                return created
        except Exception:
            return 0

    def delete_proxy_bindings_for_file(
        self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID
    ):
        """Remove ProxyBinding nodes (and their edges) for a file before relinking."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[:CONTAINS]->(p:Symbol {kind: 'proxy_binding'})
                DETACH DELETE p
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    def link_decorators(
        self,
        decorators: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Create DECORATED_BY and HANDLES edges from decoration facts.

        DECORATED_BY: decorated_symbol -> decorator (handler → registry hook).
        HANDLES: decorator -> decorated_symbol (dispatcher → registered handler).
        Both are derived from the same ``@deco`` AST fact; HANDLES is the inverse
        edge ranker BFS needs to walk from ``@app.route`` / ``@app.task`` outward.
        """
        if not decorators:
            return
        with self.driver.session() as session:
            session.execute_write(self._create_decorator_relations, decorators, workspace_id)
            session.run(
                """
                MATCH (w:Workspace {id: $workspace_id})
                SET w.graph_version = coalesce(w.graph_version, 0) + 1
                """,
                workspace_id=workspace_id,
            )

    @staticmethod
    def _create_decorator_relations(tx, decorators, workspace_id):
        if not decorators:
            return
        tx.run(
            """
            UNWIND $decorators AS d
            MATCH (decorated:Symbol {uid: d.decorated_uid})
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(deco:Symbol)
            WHERE deco.qualified_name = d.decorator_qualified_name
               OR deco.name = d.decorator_name
            WITH decorated, d, deco
            ORDER BY
              CASE WHEN deco.qualified_name = d.decorator_qualified_name THEN 0 ELSE 1 END,
              size(deco.qualified_name) ASC
            WITH decorated, d, collect(deco)[0] AS deco
            WHERE deco IS NOT NULL AND decorated <> deco
            MERGE (decorated)-[r:DECORATED_BY {workspace_id: $workspace_id}]->(deco)
            SET r.resolver = 'decorator-v1',
                r.decorator_name = d.decorator_name
            MERGE (deco)-[h:HANDLES {workspace_id: $workspace_id}]->(decorated)
            SET h.resolver = 'decorator-v1',
                h.decorator_name = d.decorator_name
            """,
            decorators=decorators,
            workspace_id=workspace_id,
        )

    def link_decorator_compositions(
        self,
        compositions: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Create COMPOSES edges from decorated class → each composed symbol.

        Subtype 2 of composition_surface: a class decorated with
        ``@Module({ imports, providers, controllers })`` names the components
        it composes inline. Each name is an AST-visible identifier in an
        array under the decorator's object-literal argument. Resolution to a
        Symbol uses the import-resolved qualified name, falling back to a
        bare-name match to keep external symbols traceable.
        """
        if not compositions:
            return
        with self.driver.session() as session:
            session.execute_write(
                self._create_decorator_composition_relations, compositions, workspace_id
            )
            session.run(
                """
                MATCH (w:Workspace {id: $workspace_id})
                SET w.graph_version = coalesce(w.graph_version, 0) + 1
                """,
                workspace_id=workspace_id,
            )

    @staticmethod
    def _create_decorator_composition_relations(tx, compositions, workspace_id):
        if not compositions:
            return
        tx.run(
            """
            UNWIND $compositions AS c
            MATCH (decorated:Symbol {uid: c.decorated_uid})
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(ref:Symbol)
            WHERE ref.qualified_name = c.referenced_qualified_name
               OR ref.name = c.referenced_name
            WITH decorated, c, ref
            ORDER BY
              CASE WHEN ref.qualified_name = c.referenced_qualified_name THEN 0 ELSE 1 END,
              size(ref.qualified_name) ASC
            WITH decorated, c, collect(ref)[0] AS ref
            WHERE ref IS NOT NULL AND decorated <> ref
            MERGE (decorated)-[r:COMPOSES {workspace_id: $workspace_id}]->(ref)
            SET r.resolver = 'decorator-compose-v1',
                r.decorator_name = c.decorator_name,
                r.decorator_key = c.decorator_key
            """,
            compositions=compositions,
            workspace_id=workspace_id,
        )

    def delete_decorator_compositions_for_file(
        self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID
    ):
        """Clear COMPOSES edges originating from symbols in ``file_path``."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)-[r:COMPOSES]->()
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    def delete_decorators_for_file(self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID):
        """Clear DECORATED_BY / HANDLES edges for symbols defined in a file."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)-[r:DECORATED_BY]->()
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                path=file_path,
                workspace_id=workspace_id,
            )
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)<-[r:HANDLES]-()
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    def link_type_references(
        self,
        references: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Create USES_TYPE edges: referrer symbol -> the project class it names.

        A type reference (parameter/return annotation, annotated assignment,
        ``isinstance``/``issubclass``) is a static AST fact, so this is a derived
        edge. The type is matched to an in-graph symbol by qualified name (exact,
        else trailing-name segment, shortest-qn wins). Types resolving to no
        in-graph symbol (builtins/stdlib/external) produce no edge — project
        classes only, precision over recall.
        """
        if not references:
            return
        with self.driver.session() as session:
            session.execute_write(self._create_type_reference_relations, references, workspace_id)
            session.run(
                """
                MATCH (w:Workspace {id: $workspace_id})
                SET w.graph_version = coalesce(w.graph_version, 0) + 1
                """,
                workspace_id=workspace_id,
            )

    @staticmethod
    def _create_type_reference_relations(tx, references, workspace_id):
        if not references:
            return
        tx.run(
            """
            UNWIND $references AS d
            MATCH (referrer:Symbol {uid: d.referrer_uid})
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(typ:Symbol)
            WHERE (typ.qualified_name = d.type_qualified_name OR typ.name = d.type_name)
              AND typ.kind IN ['class', 'interface', 'type', 'struct', 'enum']
            WITH referrer, d, typ
            ORDER BY
              CASE WHEN typ.qualified_name = d.type_qualified_name THEN 0 ELSE 1 END,
              size(typ.qualified_name) ASC
            WITH referrer, d, collect(typ)[0] AS typ
            WHERE typ IS NOT NULL AND referrer <> typ
            MERGE (referrer)-[r:USES_TYPE {workspace_id: $workspace_id}]->(typ)
            SET r.resolver = 'type-ref-v1',
                r.type_name = d.type_name,
                r.kind = d.kind
            """,
            references=references,
            workspace_id=workspace_id,
        )

    def delete_type_references_for_file(
        self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID
    ):
        """Clear USES_TYPE edges from a file's symbols before relinking."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)-[r:USES_TYPE]->()
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    def link_symbol_references(
        self,
        references: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> tuple[int, set[str]]:
        """Create REFERENCES edges from static symbol alias facts.

        Alias facts are matched to in-graph project symbols. Exact qualified-name
        matches win; optional name fallback is intentionally per-row so broad
        CommonJS defaults do not bind to unrelated same-named symbols.
        """
        if not references:
            return 0, set()
        with self.driver.session() as session:
            created, touched = session.execute_write(
                self._create_symbol_reference_relations,
                references,
                workspace_id,
            )
            if created:
                session.run(
                    """
                    MATCH (w:Workspace {id: $workspace_id})
                    SET w.graph_version = coalesce(w.graph_version, 0) + 1
                    """,
                    workspace_id=workspace_id,
                )
            return created, touched

    @staticmethod
    def _create_symbol_reference_relations(tx, references, workspace_id):
        if not references:
            return 0, set()
        project_rec = tx.run(
            """
            UNWIND range(0, size($references) - 1) AS idx
            WITH idx, $references[idx] AS d
            MATCH (source_file:File {path: d.file_path, workspace_id: $workspace_id})-[:CONTAINS]->(source:Symbol {uid: d.source_uid})
            MATCH (target_file:File {workspace_id: $workspace_id})-[:CONTAINS]->(target:Symbol)
            WHERE source <> target
              AND (
                (coalesce(d.target_qualified_name, '') <> ''
                 AND target.qualified_name = d.target_qualified_name)
                OR (
                  coalesce(d.match_by_name, true) = true
                  AND target.name = d.target_name
                )
              )
            WITH idx, d, source, target, target_file
            ORDER BY
              idx,
              CASE WHEN target.qualified_name = d.target_qualified_name THEN 0
                   WHEN target_file.path = d.file_path THEN 1
                   ELSE 2 END,
              size(coalesce(target.qualified_name, '')) ASC
            WITH idx, d, source, collect(target)[0] AS target
            WHERE target IS NOT NULL AND source <> target
            MERGE (source)-[r:REFERENCES {
                workspace_id: $workspace_id,
                alias_kind: d.kind
            }]->(target)
            SET r.resolver = 'symbol-alias-v1',
                r.confidence = coalesce(d.confidence, 0.75),
                r.tier = 'alias',
                r.target_name = d.target_name,
                r.call_site_line = coalesce(d.line, 0)
            RETURN count(r) AS created,
                   collect(DISTINCT source.uid) + collect(DISTINCT target.uid) AS touched
            """,
            references=references,
            workspace_id=workspace_id,
        ).single()
        external_rec = tx.run(
            """
            UNWIND $references AS d
            WITH d,
                 CASE
                   WHEN coalesce(d.target_qualified_name, '') CONTAINS '.'
                   THEN split(d.target_qualified_name, '.')[0]
                   ELSE coalesce(d.target_qualified_name, '')
                 END AS root
            MATCH (source_file:File {path: d.file_path, workspace_id: $workspace_id})-[:CONTAINS]->(source:Symbol {uid: d.source_uid})
            MATCH (external:ExternalPkg {workspace_id: $workspace_id, root: root})
            WHERE root <> ''
            MERGE (source)-[r:REFERENCES_EXTERNAL {
                workspace_id: $workspace_id,
                alias_kind: d.kind
            }]->(external)
            SET r.resolver = 'symbol-alias-v1',
                r.confidence = coalesce(d.confidence, 0.75),
                r.tier = 'external_alias',
                r.target_name = d.target_name,
                r.target_qualified_name = d.target_qualified_name,
                r.call_site_line = coalesce(d.line, 0)
            RETURN count(r) AS created,
                   collect(DISTINCT source.uid) AS touched
            """,
            references=references,
            workspace_id=workspace_id,
        ).single()
        project_created = int(project_rec["created"] or 0) if project_rec else 0
        external_created = int(external_rec["created"] or 0) if external_rec else 0
        touched_values = []
        if project_rec:
            touched_values.extend(project_rec["touched"] or [])
        if external_rec:
            touched_values.extend(external_rec["touched"] or [])
        return project_created + external_created, {str(uid) for uid in touched_values if uid}

    def link_reexports(
        self,
        reexports: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Create RE_EXPORTS edges: a package ``__init__`` file -> the project symbol
        it surfaces.

        A re-export (``from .submodule import Name`` in an ``__init__``) is a static
        AST fact, so this is a derived edge. The surfaced symbol is matched in-graph
        by qualified name (exact, else trailing-name segment, shortest-qn wins).
        Names resolving to no in-graph symbol (stdlib/external) produce no edge —
        project symbols only, precision over recall. The source is the ``File`` node
        (an ``__init__`` has no Symbol of its own), giving the re-exported symbol a
        ``reexport_in`` signal independent of call/type fan-in.
        """
        if not reexports:
            return
        with self.driver.session() as session:
            session.execute_write(self._create_reexport_relations, reexports, workspace_id)
            session.run(
                """
                MATCH (w:Workspace {id: $workspace_id})
                SET w.graph_version = coalesce(w.graph_version, 0) + 1
                """,
                workspace_id=workspace_id,
            )

    @staticmethod
    def _create_reexport_relations(tx, reexports, workspace_id):
        if not reexports:
            return
        tx.run(
            """
            UNWIND $reexports AS d
            MATCH (initfile:File {path: d.init_file, workspace_id: $workspace_id})
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(sym:Symbol)
            WHERE (sym.qualified_name = d.export_qualified_name OR sym.name = d.export_name)
            WITH initfile, d, sym
            ORDER BY
              CASE WHEN sym.qualified_name = d.export_qualified_name THEN 0 ELSE 1 END,
              size(sym.qualified_name) ASC
            WITH initfile, d, collect(sym)[0] AS sym
            WHERE sym IS NOT NULL
            MERGE (initfile)-[r:RE_EXPORTS {workspace_id: $workspace_id}]->(sym)
            SET r.resolver = 'reexport-v1',
                r.export_name = d.export_name
            """,
            reexports=reexports,
            workspace_id=workspace_id,
        )

    def delete_reexports_for_file(
        self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID
    ):
        """Clear RE_EXPORTS edges from a package __init__ before relinking."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[r:RE_EXPORTS]->(:Symbol)
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    def link_instantiations(
        self,
        instantiations: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Create INSTANTIATES edges: caller symbol -> the project class it constructs.

        A construction (literal ``X(...)`` or ``v(...)`` for a ``type[X]``-typed
        local) is a static AST fact, so this is a derived edge — a refinement of a
        call where the callee is a class. The class is matched in-graph by qualified
        name (exact, else trailing-name segment, shortest-qn wins) and **must be a
        class** (kind filter); names resolving to a function or to no in-graph symbol
        produce no edge. Gives ``factory_surface`` an explicit construction signal
        distinct from a plain caller / the ``type_fan_out(return)`` heuristic.
        """
        if not instantiations:
            return
        with self.driver.session() as session:
            session.execute_write(
                self._create_instantiation_relations, instantiations, workspace_id
            )
            session.run(
                """
                MATCH (w:Workspace {id: $workspace_id})
                SET w.graph_version = coalesce(w.graph_version, 0) + 1
                """,
                workspace_id=workspace_id,
            )

    @staticmethod
    def _create_instantiation_relations(tx, instantiations, workspace_id):
        if not instantiations:
            return
        tx.run(
            """
            UNWIND $instantiations AS d
            MATCH (caller:Symbol {uid: d.caller_uid})
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(cls:Symbol)
            WHERE (cls.qualified_name = d.type_qualified_name OR cls.name = d.type_name)
              AND cls.kind IN ['class', 'interface', 'struct', 'enum']
            WITH caller, d, cls
            ORDER BY
              CASE WHEN cls.qualified_name = d.type_qualified_name THEN 0 ELSE 1 END,
              size(cls.qualified_name) ASC
            WITH caller, d, collect(cls)[0] AS cls
            WHERE cls IS NOT NULL AND caller <> cls
            MERGE (caller)-[r:INSTANTIATES {workspace_id: $workspace_id}]->(cls)
            SET r.resolver = 'instantiate-v1',
                r.type_name = d.type_name
            """,
            instantiations=instantiations,
            workspace_id=workspace_id,
        )

    def delete_instantiations_for_file(
        self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID
    ):
        """Clear INSTANTIATES edges from a file's symbols before relinking."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)-[r:INSTANTIATES]->()
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    def link_injections(
        self,
        injections: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Create INJECTS edges: owner symbol -> the provider wired into its parameters.

        ``def f(x = Marker(provider))`` is a static binding (like an import), so this is
        a derived edge. The provider is matched to an in-graph symbol by qualified name
        (exact, else trailing-name segment, shortest-qn wins). Providers resolving to no
        in-graph symbol (locals/literals/external) produce no edge — project providers
        only, precision over recall.
        """
        if not injections:
            return
        with self.driver.session() as session:
            session.execute_write(self._create_injection_relations, injections, workspace_id)
            session.run(
                """
                MATCH (w:Workspace {id: $workspace_id})
                SET w.graph_version = coalesce(w.graph_version, 0) + 1
                """,
                workspace_id=workspace_id,
            )

    @staticmethod
    def _create_injection_relations(tx, injections, workspace_id):
        if not injections:
            return
        tx.run(
            """
            UNWIND $injections AS d
            MATCH (owner:Symbol {uid: d.owner_uid})
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(prov:Symbol)
            WHERE (prov.qualified_name = d.provider_qualified_name
                   OR prov.name = d.provider_name)
              AND prov.kind IN ['function', 'method', 'class']
            WITH owner, d, prov
            ORDER BY
              CASE WHEN prov.qualified_name = d.provider_qualified_name THEN 0 ELSE 1 END,
              size(prov.qualified_name) ASC
            WITH owner, d, collect(prov)[0] AS prov
            WHERE prov IS NOT NULL AND owner <> prov
            MERGE (owner)-[r:INJECTS {workspace_id: $workspace_id}]->(prov)
            SET r.resolver = 'inject-v1',
                r.provider_name = d.provider_name,
                r.confidence = 0.85
            """,
            injections=injections,
            workspace_id=workspace_id,
        )

    def delete_injections_for_file(
        self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID
    ):
        """Clear INJECTS edges from a file's symbols before relinking."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)-[r:INJECTS]->()
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    def get_symbol_uid_by_name(
        self, name: str, workspace_id: str = DEFAULT_WORKSPACE_ID
    ) -> str | None:
        """Return the UID of the first symbol matching `name` in this workspace, or None."""
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol {name: $name})
                RETURN s.uid AS uid LIMIT 1
                """,
                name=name,
                workspace_id=workspace_id,
            ).single()
        return result["uid"] if result else None

    def get_file_path_for_symbol(
        self, symbol_uid: str, workspace_id: str = DEFAULT_WORKSPACE_ID
    ) -> str:
        """Return the file path containing `symbol_uid`, or '<unknown>'."""
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (s:Symbol {uid: $uid})
                OPTIONAL MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(s)
                RETURN coalesce(f.path, '<unknown>') AS file_path
                """,
                uid=symbol_uid,
                workspace_id=workspace_id,
            ).single()
        return result["file_path"] if result else "<unknown>"


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
        "returns_function_expression": bool(symbol.returns_function_expression),
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


def _import_row(imp: ImportEdge) -> dict[str, object]:
    if imp.import_type == "relative" and imp.target_module_name.startswith("."):
        base = (Path(imp.source_file).parent / imp.target_module_name).resolve()
        module_path = str(base)
        package_paths: list[str] = []
    else:
        module_name = imp.target_module_name.lstrip("./")
        module_path = "/" + module_name.replace(".", "/")
        package_paths = _monorepo_package_import_paths(module_name)
    path_suffixes = [
        f"{module_path}{suffix}"
        for suffix in (
            ".py",
            "/__init__.py",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            "/index.js",
            "/index.jsx",
            "/index.ts",
            "/index.tsx",
        )
    ]
    for package_path in package_paths:
        for suffix in (
            ".py",
            "/__init__.py",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            "/index.js",
            "/index.jsx",
            "/index.ts",
            "/index.tsx",
        ):
            path_suffixes.append(f"{package_path}{suffix}")
    return {
        "source_file": imp.source_file,
        "path_suffixes": sorted(set(path_suffixes)),
        "import_type": imp.import_type,
    }


def _monorepo_package_import_paths(module_name: str) -> list[str]:
    """Return suffixes for package-manager workspace imports.

    NPM/Python package imports often point at a workspace package rather than a
    path that appears literally in the repository. For example
    ``@vue/runtime-core`` lives under ``packages/runtime-core/src/index.ts``.
    Keep this as suffix generation rather than framework-specific routing.
    """
    clean = module_name.strip().strip("/")
    if not clean:
        return []

    parts = [part for part in clean.split("/") if part]
    if not parts:
        return []
    if parts[0].startswith("@") and len(parts) >= 2:
        package_name = parts[1]
        subpath = parts[2:]
    else:
        package_name = parts[0]
        subpath = parts[1:]

    if not package_name:
        return []

    candidates = [
        f"/packages/{package_name}",
        f"/packages/{package_name}/src",
    ]
    if subpath:
        suffix = "/".join(subpath)
        candidates.extend(
            [
                f"/packages/{package_name}/{suffix}",
                f"/packages/{package_name}/src/{suffix}",
            ]
        )
    return candidates


# Python builtin exception hierarchy. A class inheriting one of these is an error
# type, but the base is a builtin (not an in-graph symbol), so the inheritance edge
# is never materialized — leaving the error-ness structurally invisible. This is the
# standard library's own taxonomy, not a project/benchmark fixture: it lets the
# cascade derive `error_surface` from a real AST fact (`class X(..., ValueError)`).
_BUILTIN_EXCEPTION_BASES: frozenset[str] = frozenset(
    {
        "BaseException", "Exception", "ArithmeticError", "AssertionError",
        "AttributeError", "BufferError", "EOFError", "ImportError",
        "ModuleNotFoundError", "LookupError", "IndexError", "KeyError",
        "MemoryError", "NameError", "UnboundLocalError", "OSError", "IOError",
        "FileNotFoundError", "FileExistsError", "PermissionError",
        "NotADirectoryError", "IsADirectoryError", "InterruptedError",
        "ConnectionError", "BrokenPipeError", "ConnectionResetError",
        "ConnectionAbortedError", "ConnectionRefusedError", "TimeoutError",
        "ReferenceError", "RuntimeError", "NotImplementedError", "RecursionError",
        "StopIteration", "StopAsyncIteration", "SyntaxError", "IndentationError",
        "TabError", "SystemError", "TypeError", "ValueError", "UnicodeError",
        "UnicodeDecodeError", "UnicodeEncodeError", "UnicodeTranslateError",
        "Warning", "DeprecationWarning", "UserWarning", "RuntimeWarning",
        "FloatingPointError", "OverflowError", "ZeroDivisionError",
        "EnvironmentError", "GeneratorExit", "KeyboardInterrupt", "SystemExit",
    }
)


def _inheritance_row(edge: InheritanceEdge) -> dict[str, object]:
    return {
        "subclass_uid": edge.subclass_uid,
        "superclass_name": edge.superclass_name,
        "is_interface": edge.is_interface,
    }
