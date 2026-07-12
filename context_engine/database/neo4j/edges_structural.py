"""Class-API, inheritance, type/symbol-reference and re-export edges."""

from typing import Any

from context_engine.database.neo4j._common import (
    _BUILTIN_EXCEPTION_BASES,
    _CLASS_API_EDGE_DELETE_BATCH_SIZE,
    _EVENT_DISPATCH_BASES,
    _batched_class_api_edges,
    _bump_workspace_graph_version,
    _inheritance_row,
)
from context_engine.parser.protocol import (
    ClassApiEdge,
    InheritanceEdge,
)
from context_engine.workspace import DEFAULT_WORKSPACE_ID


class StructuralEdgesMixin:
    driver: Any

    def link_class_api(
        self,
        edges: list[ClassApiEdge],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        if not edges:
            return
        with self.driver.session() as session:
            for batch in _batched_class_api_edges(edges):
                session.execute_write(self._create_class_api_relations, batch, workspace_id)
            _bump_workspace_graph_version(session, workspace_id)

    def clear_class_api_edges(self, workspace_id: str = DEFAULT_WORKSPACE_ID):
        with self.driver.session() as session:
            deleted_total = 0
            while True:
                rec = session.run(
                    """
                    MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->
                          (:Symbol)-[r:HAS_API|INHERITED_API {workspace_id: $workspace_id}]->(:Symbol)
                    WHERE coalesce(r.resolver, 'mro-v1') = 'mro-v1'
                    WITH r LIMIT $limit
                    DELETE r
                    RETURN count(*) AS deleted_edges
                    """,
                    workspace_id=workspace_id,
                    limit=_CLASS_API_EDGE_DELETE_BATCH_SIZE,
                ).single()
                deleted = int(rec["deleted_edges"] or 0) if rec else 0
                deleted_total += deleted
                if deleted < _CLASS_API_EDGE_DELETE_BATCH_SIZE:
                    break
            if deleted_total:
                _bump_workspace_graph_version(session, workspace_id)

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
                _bump_workspace_graph_version(session, workspace_id)
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
        return int(rec["created"] or 0), {str(uid) for uid in (rec["touched"] or []) if uid}

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
                _bump_workspace_graph_version(session, workspace_id)

    @staticmethod
    def _create_inheritance_relations(tx, inheritance_edges, workspace_id):
        if not inheritance_edges:
            return
        rows = [_inheritance_row(edge) for edge in inheritance_edges]
        tx.run(
            """
            UNWIND $inheritance_edges AS edge
            MATCH (subclass:Symbol {uid: edge.subclass_uid})<-[:CONTAINS]-(subfile:File {workspace_id: $workspace_id})
            MATCH (supfile:File {workspace_id: $workspace_id})-[:CONTAINS]->(superclass:Symbol {name: edge.superclass_name})
            // Resolve the base within the subclass file's OWN import scope: the
            // superclass must be defined in the same file, or in a file the
            // subclass's file imports — exactly how Python resolves the name.
            // A bare-name workspace-wide match instead links the subclass to
            // EVERY same-named class (a cartesian blow-up: Django ``tests/``
            // carry 1068 ``Meta`` / 267 ``Model`` classes, so each
            // ``class X(models.Model)`` fanned out to all 267 — millions of
            // spurious DEPENDS_ON that make the *1..6 ancestor walk explode and
            // poison the shared graph). Same-file / imported-file scoping
            // collapses it to the one real base while preserving cross-file
            // inheritance like celery ``TaskPool`` (prefork.py) -> ``BasePool``
            // (base.py, which prefork.py imports).
            WHERE superclass <> subclass
              AND (supfile = subfile OR (subfile)-[:IMPORTS]->(supfile))
            MERGE (subclass)-[r:DEPENDS_ON {workspace_id: $workspace_id}]->(superclass)
            SET r.is_interface = edge.is_interface,
                r.confidence = 0.9,
                r.tier = 'scoped',
                r.resolver = 'inheritance-v2'
            """,
            inheritance_edges=rows,
            workspace_id=workspace_id,
        )
        # Snapshot every parsed base on the subclass Symbol so the
        # EXTENDS_EXTERNAL post-pass (which runs after IMPORTS_EXTERNAL_SYMBOL
        # is materialized) can resolve any base that the local-name match
        # above could not. Two parallel lists are stored:
        #   parsed_base_names  — bare head names (``Router``, ``Starlette``);
        #                        used to match ``IMPORTS_EXTERNAL_SYMBOL``
        #                        rows where the imported name IS the base.
        #   parsed_base_paths  — full dotted expressions (``routing.Router``,
        #                        ``Starlette``); used for the module-attr
        #                        case where the file imports a module and the
        #                        base is an attribute on it.
        tx.run(
            """
            UNWIND $inheritance_edges AS edge
            MATCH (subclass:Symbol {uid: edge.subclass_uid})
            WITH subclass,
                 collect(DISTINCT edge.superclass_name) AS names,
                 collect(DISTINCT edge.superclass_path) AS paths
            SET subclass.parsed_base_names = names,
                subclass.parsed_base_paths = paths
            """,
            inheritance_edges=rows,
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
        # (UsageError -> ClickException -> Exception) is also an error type.
        # Iterate direct inheritance edges instead of a global variable-length
        # path search; the latter explodes once the parser sees rich multi-line
        # generic base lists.
        for _ in range(6):
            rec = tx.run(
                """
                MATCH (sf:File {workspace_id: $workspace_id})-[:CONTAINS]->(sub:Symbol)
                      -[r:DEPENDS_ON]->(base:Symbol)
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                  AND coalesce(sub.inherits_builtin_exception, false) = false
                  AND coalesce(base.inherits_builtin_exception, false) = true
                SET sub.inherits_builtin_exception = true
                RETURN count(DISTINCT sub) AS updated
                """,
                workspace_id=workspace_id,
            ).single()
            if not rec or int(rec["updated"] or 0) == 0:
                break
        # Event-dispatcher inheritance: ``extends EventEmitter`` / ``extends Subject``
        # bases are often external npm types with no in-graph Symbol. Mark the
        # subclass so downstream passes can treat it as a dispatch container.
        event_rows = [
            {"subclass_uid": edge.subclass_uid}
            for edge in inheritance_edges
            if edge.superclass_name.rsplit(".", 1)[-1] in _EVENT_DISPATCH_BASES
        ]
        if event_rows:
            tx.run(
                """
                UNWIND $rows AS row
                MATCH (s:Symbol {uid: row.subclass_uid})
                SET s.inherits_event_dispatcher = true
                """,
                rows=event_rows,
            )
        for _ in range(6):
            rec = tx.run(
                """
                MATCH (sf:File {workspace_id: $workspace_id})-[:CONTAINS]->(sub:Symbol)
                      -[r:DEPENDS_ON]->(base:Symbol)
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                  AND coalesce(sub.inherits_event_dispatcher, false) = false
                  AND coalesce(base.inherits_event_dispatcher, false) = true
                SET sub.inherits_event_dispatcher = true
                RETURN count(DISTINCT sub) AS updated
                """,
                workspace_id=workspace_id,
            ).single()
            if not rec or int(rec["updated"] or 0) == 0:
                break

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
            _bump_workspace_graph_version(session, workspace_id)

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
                _bump_workspace_graph_version(session, workspace_id)
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
        touched_values: list[str] = []
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
            _bump_workspace_graph_version(session, workspace_id)

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

    def delete_reexports_for_file(self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID):
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

    def link_flow_pairs(
        self,
        pairs: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> int:
        """Create FLOWS_INTO edges: call A's result feeds call B's arguments.

        The pair is a static AST fact extracted per caller function
        (``x = A(...); B(x)`` / ``B(A(...))``); this is the first primary edge
        of the DATAFLOW axis (AFFECTS is a derived closure rebuilt per change,
        so the pairs must live under their own type to survive that rebuild).
        Endpoint resolution mirrors ``link_calls``: extractor-computed uid wins,
        else exact qualified name, else a workspace-globally-unique name;
        unresolvable endpoints drop the pair — project symbols only, precision
        over recall. The caller's uid rides the edge as a property so the
        incremental path can clear a reindexed file's pairs by caller.
        """
        if not pairs:
            return 0
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (f:File {workspace_id: $workspace_id})-[rel:CONTAINS]->(s:Symbol)
                RETURN s.uid AS uid,
                       s.name AS name,
                       coalesce(s.qualified_name, '') AS qn,
                       f.path AS path,
                       coalesce(rel.start_line, s.range[0], 0) AS line
                """,
                workspace_id=workspace_id,
            )
            rows = list(result)
        # Source order before the setdefault map — same-qn twins must resolve
        # to the same symbol on every fresh ref (see _resolve_call_callees).
        rows.sort(
            key=lambda row: (
                str(row.get("path") or ""),
                int(row.get("line") or 0),
                str(row.get("qn") or ""),
                str(row.get("uid") or ""),
            )
        )
        by_qn: dict[str, str] = {}
        by_name: dict[str, list[str]] = {}
        for row in rows:
            uid = row["uid"]
            if not uid:
                continue
            if row["qn"]:
                by_qn.setdefault(row["qn"], uid)
            if row["name"]:
                by_name.setdefault(row["name"], []).append(uid)

        def _resolve(uid: str, qn: str, name: str) -> str | None:
            if uid:
                return uid
            if qn and qn in by_qn:
                return by_qn[qn]
            candidates = by_name.get(name or "", [])
            if len(candidates) == 1:
                return candidates[0]
            return None

        resolved: list[dict] = []
        for pair in pairs:
            source = _resolve(
                str(pair.get("source_uid") or ""),
                str(pair.get("source_qualified_name") or ""),
                str(pair.get("source_name") or ""),
            )
            target = _resolve(
                str(pair.get("target_uid") or ""),
                str(pair.get("target_qualified_name") or ""),
                str(pair.get("target_name") or ""),
            )
            if not source or not target or source == target:
                continue
            resolved.append(
                {
                    "source_uid": source,
                    "target_uid": target,
                    "caller_uid": str(pair.get("caller_uid") or ""),
                    "line": int(pair.get("line") or 0),
                }
            )
        if not resolved:
            return 0
        with self.driver.session() as session:
            session.execute_write(self._create_flow_pair_relations, resolved, workspace_id)
            _bump_workspace_graph_version(session, workspace_id)
        return len(resolved)

    @staticmethod
    def _create_flow_pair_relations(tx, rows, workspace_id):
        if not rows:
            return
        tx.run(
            """
            UNWIND $rows AS d
            MATCH (a:Symbol {uid: d.source_uid})
            MATCH (b:Symbol {uid: d.target_uid})
            WHERE a <> b
            MERGE (a)-[r:FLOWS_INTO {workspace_id: $workspace_id,
                                     caller_uid: d.caller_uid}]->(b)
            SET r.resolver = 'arg-flow-v1',
                r.line = d.line
            """,
            rows=rows,
            workspace_id=workspace_id,
        )

    def delete_flow_pairs_for_callers(
        self,
        caller_uids: list[str],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Clear FLOWS_INTO edges recorded at the given caller sites.

        Called with a reindexed file's current + removed symbol uids before
        relinking — the caller anchors the edge only as a property (the edge
        itself connects the two callees), so a deleted caller function cannot
        take its pairs down via DETACH DELETE and must be cleared here.
        """
        if not caller_uids:
            return
        with self.driver.session() as session:
            session.run(
                """
                MATCH ()-[r:FLOWS_INTO {workspace_id: $workspace_id}]->()
                WHERE r.caller_uid IN $caller_uids
                DELETE r
                """,
                workspace_id=workspace_id,
                caller_uids=list(caller_uids),
            )
