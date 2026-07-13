"""Call, import and external-boundary edges."""

from typing import Any

from context_engine.database.neo4j._common import (
    _CALL_REL_TYPES,
    _WORKSPACE_GRAPH_VERSION_MATCH,
    _WORKSPACE_GRAPH_VERSION_SET,
    _bump_workspace_graph_version,
    _call_row,
    _import_row,
)
from context_engine.parser.protocol import (
    ImportEdge,
)
from context_engine.workspace import DEFAULT_WORKSPACE_ID


class CallImportEdgesMixin:
    driver: Any

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
            _bump_workspace_graph_version(session, workspace_id)

    @staticmethod
    def _build_call_resolution_index(
        rows: list[dict],
    ) -> tuple[dict[str, str], dict[str, list[str]], list[tuple[str, str]]]:
        by_qn: dict[str, str] = {}
        by_name: dict[str, list[str]] = {}
        object_api: list[tuple[str, str]] = []
        for row in rows:
            uid = row["uid"]
            if not uid:
                continue
            qn = row["qn"] or ""
            if qn:
                by_qn.setdefault(qn, uid)
            name = row["name"] or ""
            if name:
                by_name.setdefault(name, []).append(uid)
            if row["kind"] == "object_api" and qn:
                object_api.append((qn, uid))
        object_api.sort(key=lambda item: -len(item[0]))
        return by_qn, by_name, object_api

    @staticmethod
    def _qn_callee_uid(
        qn: str,
        by_qn: dict[str, str],
        object_api: list[tuple[str, str]],
    ) -> str | None:
        hit = by_qn.get(qn)
        if hit is not None:
            return hit
        for surf_qn, surf_uid in object_api:
            if qn.startswith(surf_qn + "."):
                return surf_uid
        return None

    @staticmethod
    def _resolve_call_callee_uid(
        call: dict,
        by_qn: dict[str, str],
        by_name: dict[str, list[str]],
        object_api: list[tuple[str, str]],
    ) -> str | None:
        caller_uid = call.get("caller_uid")
        qn = call.get("callee_qualified_name")
        if qn:
            hit = CallImportEdgesMixin._qn_callee_uid(qn, by_qn, object_api)
            if hit and hit != caller_uid:
                return hit
        name = call.get("callee_name")
        if name:
            cands = by_name.get(name) or []
            if len(cands) == 1 and cands[0] != caller_uid:
                return cands[0]
        return None

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
                MATCH (f:File {workspace_id: $workspace_id})-[rel:CONTAINS]->(s:Symbol)
                RETURN s.uid AS uid,
                       s.name AS name,
                       coalesce(s.qualified_name, '') AS qn,
                       coalesce(s.kind, '') AS kind,
                       f.path AS path,
                       coalesce(rel.start_line, s.range[0], 0) AS line
                """,
                workspace_id=workspace_id,
            )
            rows = list(result)
        # Content-stable order before the setdefault maps: the scan order varies
        # per workspace, so duplicate-qn twins (@overload, property setter) would
        # otherwise resolve to a different symbol on every fresh ref. Source
        # order makes the first definition win, deterministically.
        rows.sort(
            key=lambda row: (
                str(row.get("path") or ""),
                int(row.get("line") or 0),
                str(row.get("qn") or ""),
                str(row.get("uid") or ""),
            )
        )
        by_qn, by_name, object_api = self._build_call_resolution_index(rows)

        out: list[dict] = []
        for call in calls:
            if call.get("callee_uid"):
                out.append(call)
                continue
            callee_uid = self._resolve_call_callee_uid(call, by_qn, by_name, object_api)
            if callee_uid:
                out.append({**call, "callee_uid": callee_uid})
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

    @staticmethod
    def _build_import_suffix_index(file_paths: list[str]) -> dict[str, str]:
        suffix_index: dict[str, str] = {}
        for path in file_paths:
            parts = path.split("/")
            for k in range(1, min(5, len(parts)) + 1):
                key = "/" + "/".join(parts[-k:])
                suffix_index.setdefault(key, path)
        return suffix_index

    @staticmethod
    def _resolve_import_target_path(
        imp: ImportEdge,
        row: dict[str, object],
        file_path_set: set[str],
        suffix_index: dict[str, str],
    ) -> str | None:
        path_suffixes = row.get("path_suffixes")
        if not isinstance(path_suffixes, list):
            return None
        for suffix in path_suffixes:
            if not isinstance(suffix, str):
                continue
            if suffix in file_path_set:
                return suffix
            hit = suffix_index.get(suffix)
            if hit is not None:
                return hit
        return None

    def link_imports(self, imports: list[ImportEdge], workspace_id: str = DEFAULT_WORKSPACE_ID):
        if not imports:
            return
        # Resolve each import's candidate suffix list against the workspace's actual
        # File.path set Python-side, then issue an exact-match MERGE. The original
        # query did `target.path ENDS WITH suffix` inside a UNWIND, which is O(N×M)
        # on (imports × files) — link_imports dominated graph time on fastapi (39s).
        # With one round-trip for file paths + indexed equality, that work happens in
        # a Python dict lookup and the Cypher becomes an index-friendly MATCH.
        # list_file_paths lives on WorkspaceMixin; both compose into Neo4jClient.
        # Sorted so ambiguous suffix resolution is content-stable: the File
        # scan comes back in Neo4j store order (insertion/id-reuse dependent),
        # and the suffix index is first-wins — unsorted, a repo with many
        # same-named files (django: hundreds of ``/models.py``) resolved
        # imports differently per reindex, rippling into DEPENDS_ON/AFFECTS.
        file_paths = sorted(self.list_file_paths(workspace_id=workspace_id))  # type: ignore[attr-defined]
        file_path_set = set(file_paths)
        suffix_index = self._build_import_suffix_index(file_paths)
        resolved: list[dict[str, object]] = []
        for imp in imports:
            row = _import_row(imp)
            target_path = self._resolve_import_target_path(
                imp,
                row,
                file_path_set,
                suffix_index,
            )
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
            _bump_workspace_graph_version(session, workspace_id)

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

    def delete_external_imports_for_files(
        self,
        file_paths: list[str],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """List form of ``delete_external_imports_for_file`` — one UNWIND per
        edge type instead of two round-trips per file (the fast pipeline
        refreshes the whole diff set in one call)."""
        if not file_paths:
            return
        with self.driver.session() as session:
            session.run(
                f"""
                UNWIND $paths AS path
                MATCH (f:File {{path: path, workspace_id: $workspace_id}})-[r:IMPORTS_EXTERNAL]->(:ExternalPkg)
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                WITH collect(r) AS edges
                FOREACH (edge IN edges | DELETE edge)
                WITH size(edges) AS deleted_edges
                {_WORKSPACE_GRAPH_VERSION_MATCH}
                WHERE deleted_edges > 0
                {_WORKSPACE_GRAPH_VERSION_SET}
                """,
                paths=file_paths,
                workspace_id=workspace_id,
            )
            session.run(
                """
                UNWIND $paths AS path
                MATCH (f:File {path: path, workspace_id: $workspace_id})
                      -[r:IMPORTS_EXTERNAL_SYMBOL]->(:ExternalSymbol)
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                paths=file_paths,
                workspace_id=workspace_id,
            )

    def delete_external_imports_for_file(
        self,
        file_path: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        with self.driver.session() as session:
            session.run(
                f"""
                MATCH (f:File {{path: $path, workspace_id: $workspace_id}})-[r:IMPORTS_EXTERNAL]->(:ExternalPkg)
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
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
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})
                      -[r:IMPORTS_EXTERNAL_SYMBOL]->(:ExternalSymbol)
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    def link_external_boundary(
        self,
        call_links: list[dict],
        import_links: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        symbol_import_links: list[dict] | None = None,
    ) -> tuple[int, int]:
        """Materialize ``(:ExternalPkg)`` targets and ``*_EXTERNAL`` edges (C1).

        ``symbol_import_links`` (when present) also materializes one
        ``(:ExternalSymbol {qualified_name})`` per ``from M import N`` and an
        ``IMPORTS_EXTERNAL_SYMBOL`` edge from the importing file. Unlike
        ExternalPkg these nodes carry upstream identity at name granularity, so
        the library marker catalogue can look up ``starlette.routing.Router``
        without name-pattern matching at the consumer site.
        """
        symbol_import_links = symbol_import_links or []
        with self.driver.session() as session:
            calls_created, imports_created = session.execute_write(
                self._create_external_boundary_relations,
                call_links,
                import_links,
                workspace_id,
            )
            if symbol_import_links:
                session.execute_write(
                    self._create_external_symbol_imports,
                    symbol_import_links,
                    workspace_id,
                )
            if calls_created or imports_created or symbol_import_links:
                _bump_workspace_graph_version(session, workspace_id)
            session.run(
                """
                MATCH (e:ExternalPkg {workspace_id: $workspace_id})
                WHERE NOT (e)<-[:CALLS_EXTERNAL|IMPORTS_EXTERNAL|REFERENCES_EXTERNAL {workspace_id: $workspace_id}]-()
                DETACH DELETE e
                """,
                workspace_id=workspace_id,
            )
            session.run(
                """
                MATCH (e:ExternalSymbol {workspace_id: $workspace_id})
                WHERE NOT (e)<-[:IMPORTS_EXTERNAL_SYMBOL|EXTENDS_EXTERNAL|INSTANTIATES_EXTERNAL {workspace_id: $workspace_id}]-()
                DETACH DELETE e
                """,
                workspace_id=workspace_id,
            )
        return calls_created, imports_created

    def materialize_file_integrates_with(
        self,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        min_shared: int = 2,
    ) -> int:
        """Workspace pass: link Files that share >=min_shared non-plumbing external imports.

        `(:File)-[:INTEGRATES_WITH {shared}]->(:File)` is a derived co-reference
        edge: two workspace files that both import the same set of integration
        packages (e.g. starlette + anyio for fastapi/routing.py and
        fastapi/concurrency.py) are structurally collaborating around the same
        external boundary, even when their own symbols never call each other.
        Without this edge BFS from a symbol in one file cannot reach the
        sibling file through a structural step — the only path runs through
        the external package node and is currently not part of the traversal
        vocabulary.

        Single-direction edges (``id(f1) < id(f2)``); BFS matches undirected.
        Cleared and recomputed in full each pass for the workspace.
        """
        from context_engine.indexer.external_boundary import EXTERNAL_INTEGRATION_PLUMBING_ROOTS

        plumbing = list(EXTERNAL_INTEGRATION_PLUMBING_ROOTS)
        with self.driver.session() as session:
            session.run(
                """
                MATCH ()-[r:INTEGRATES_WITH]->()
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                workspace_id=workspace_id,
            )
            result = session.run(
                """
                MATCH (e:ExternalPkg {workspace_id: $workspace_id})
                WHERE NOT e.root IN $plumbing
                MATCH (f1:File {workspace_id: $workspace_id})-[:IMPORTS_EXTERNAL]->(e)
                MATCH (f2:File {workspace_id: $workspace_id})-[:IMPORTS_EXTERNAL]->(e)
                WHERE id(f1) < id(f2)
                WITH f1, f2, count(DISTINCT e) AS shared
                WHERE shared >= $min_shared
                MERGE (f1)-[r:INTEGRATES_WITH {workspace_id: $workspace_id}]->(f2)
                SET r.shared = shared
                RETURN count(r) AS created
                """,
                workspace_id=workspace_id,
                plumbing=plumbing,
                min_shared=min_shared,
            ).single()
            created = int((result and result.get("created")) or 0)
            if created:
                _bump_workspace_graph_version(session, workspace_id)
            return created

    @staticmethod
    def _create_external_boundary_relations(tx, call_links, import_links, workspace_id):
        from context_engine.indexer.external_boundary import external_pkg_uid

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
                UNWIND $roots AS root
                MERGE (e:ExternalPkg {uid: root.uid, workspace_id: $workspace_id})
                SET e.name = root.name,
                    e.root = root.name,
                    e.qualified_name = root.name,
                    e.is_external = true
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
                    r.callee_member = row.callee_member,
                    r.kind = coalesce(row.kind, 'call')
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

    def materialize_extends_external(
        self,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> int:
        """Connect class Symbols to the ExternalSymbol nodes they inherit from.

        Two structural cases are handled:

        - **bare-name base**: ``class C(Starlette):`` with
          ``from starlette.applications import Starlette`` —
          ``parsed_base_names`` carries ``Starlette``, the file imports it as
          ``Starlette`` (local_alias), and the existing ExternalSymbol's
          qualified_name is the upstream identity.
        - **module-attr base**: ``class C(routing.Router):`` with
          ``from starlette import routing`` — ``parsed_base_paths`` carries
          ``routing.Router``. The head (``routing``) matches the
          IMPORTS_EXTERNAL_SYMBOL alias; the upstream qualified_name is the
          imported module's qualified_name (``starlette.routing``); the
          actual base's qualified_name is that module plus the tail
          (``starlette.routing.Router``). We materialise this derived
          ExternalSymbol on demand so the same edge type fits both cases.
        """
        from context_engine.indexer.external_boundary import external_symbol_uid

        with self.driver.session() as session:
            session.run(
                """
                MATCH ()-[r:EXTENDS_EXTERNAL]->()
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                workspace_id=workspace_id,
            )
            bare_rec = session.run(
                """
                MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(cls:Symbol)
                WHERE cls.parsed_base_names IS NOT NULL AND size(cls.parsed_base_names) > 0
                UNWIND cls.parsed_base_names AS base_name
                MATCH (f)-[imp:IMPORTS_EXTERNAL_SYMBOL]->(ext:ExternalSymbol)
                WHERE coalesce(imp.workspace_id, $workspace_id) = $workspace_id
                  AND imp.local_alias = base_name
                MERGE (cls)-[r:EXTENDS_EXTERNAL {workspace_id: $workspace_id}]->(ext)
                SET r.parsed_base_name = base_name,
                    r.resolver = 'extends-external-v1'
                RETURN count(r) AS c
                """,
                workspace_id=workspace_id,
            ).single()
            bare_created = int(bare_rec["c"]) if bare_rec else 0

            # Dotted-path case: query candidates, build derived ExternalSymbol
            # rows in Python (so the uid stays deterministic via
            # ``external_symbol_uid``), then merge in one round-trip.
            dotted_candidates = session.run(
                """
                MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(cls:Symbol)
                WHERE cls.parsed_base_paths IS NOT NULL AND size(cls.parsed_base_paths) > 0
                UNWIND cls.parsed_base_paths AS path
                WITH f, cls, path, split(path, '.') AS parts
                WHERE size(parts) > 1
                MATCH (f)-[imp:IMPORTS_EXTERNAL_SYMBOL]->(mod:ExternalSymbol)
                WHERE coalesce(imp.workspace_id, $workspace_id) = $workspace_id
                  AND imp.local_alias = parts[0]
                RETURN cls.uid AS cls_uid,
                       mod.qualified_name AS mod_qn,
                       mod.root AS mod_root,
                       parts AS parts,
                       path AS path
                """,
                workspace_id=workspace_id,
            ).data()

            dotted_rows: list[dict[str, object]] = []
            for row in dotted_candidates:
                mod_qn = str(row.get("mod_qn") or "")
                parts = row.get("parts") or []
                if not mod_qn or len(parts) < 2:
                    continue
                tail = ".".join(str(p) for p in parts[1:])
                target_qn = f"{mod_qn}.{tail}"
                dotted_rows.append(
                    {
                        "cls_uid": str(row.get("cls_uid") or ""),
                        "target_qn": target_qn,
                        "target_uid": external_symbol_uid(workspace_id, target_qn),
                        "target_module": mod_qn,
                        "target_name": tail,
                        "target_root": str(row.get("mod_root") or ""),
                        "parsed_base_name": str(row.get("path") or ""),
                    }
                )

            dotted_created = 0
            if dotted_rows:
                rec = session.run(
                    """
                    UNWIND $rows AS row
                    MERGE (e:ExternalSymbol {uid: row.target_uid, workspace_id: $workspace_id})
                    SET e.qualified_name = row.target_qn,
                        e.module = row.target_module,
                        e.name = row.target_name,
                        e.root = row.target_root,
                        e.is_external = true,
                        e.resolver = 'extends-external-v1-derived'
                    WITH e, row
                    MATCH (cls:Symbol {uid: row.cls_uid})
                    MERGE (cls)-[r:EXTENDS_EXTERNAL {workspace_id: $workspace_id}]->(e)
                    SET r.parsed_base_name = row.parsed_base_name,
                        r.resolver = 'extends-external-v1-derived'
                    RETURN count(r) AS c
                    """,
                    rows=dotted_rows,
                    workspace_id=workspace_id,
                ).single()
                dotted_created = int(rec["c"]) if rec else 0

            created = bare_created + dotted_created
            if created:
                _bump_workspace_graph_version(session, workspace_id)
            return created

    @staticmethod
    def _create_external_symbol_imports(tx, rows, workspace_id):
        """Create ExternalSymbol nodes + IMPORTS_EXTERNAL_SYMBOL edges in one pass.

        Rows are pre-computed by ``external_symbol_import_rows`` and carry the
        workspace-scoped uid + workspace-independent ``qualified_name`` so the
        catalogue can match on the latter regardless of which workspace asks.
        """
        if not rows:
            return 0
        tx.run(
            """
            UNWIND $rows AS row
            MERGE (e:ExternalSymbol {uid: row.external_symbol_uid, workspace_id: $workspace_id})
            SET e.qualified_name = row.qualified_name,
                e.module = row.module,
                e.name = row.name,
                e.root = row.external_root,
                e.is_external = true
            """,
            rows=rows,
            workspace_id=workspace_id,
        )
        rec = tx.run(
            """
            UNWIND $rows AS row
            MATCH (f:File {path: row.file_path, workspace_id: $workspace_id})
            MATCH (e:ExternalSymbol {uid: row.external_symbol_uid, workspace_id: $workspace_id})
            MERGE (f)-[r:IMPORTS_EXTERNAL_SYMBOL {workspace_id: $workspace_id}]->(e)
            SET r.local_alias = row.local_alias,
                r.resolver = 'external-boundary-v1'
            RETURN count(r) AS c
            """,
            rows=rows,
            workspace_id=workspace_id,
        ).single()
        return int(rec["c"]) if rec else 0
