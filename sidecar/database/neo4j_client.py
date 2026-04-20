from neo4j import GraphDatabase

from sidecar.parser.protocol import ImportEdge, InheritanceEdge, SymbolMetadata


class Neo4jClient:
    def __init__(self, uri, user, password):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        self.driver.close()

    def upsert_file_structure(self, file_path: str, file_hash: str, symbols: list[SymbolMetadata]):
        with self.driver.session() as session:
            session.execute_write(self._upsert_nodes, file_path, file_hash, symbols)

    def get_file_hashes(self, file_paths: list[str]) -> dict[str, str]:
        """Return {path: hash} for all known files in the given list."""
        if not file_paths:
            return {}
        with self.driver.session() as session:
            result = session.run(
                "MATCH (f:File) WHERE f.path IN $paths RETURN f.path AS path, f.hash AS hash",
                paths=file_paths,
            )
            return {r["path"]: r["hash"] for r in result}

    def delete_symbols_for_file(self, file_path: str):
        """Detach-delete all Symbol nodes owned by a File."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path})-[:CONTAINS]->(s:Symbol)
                DETACH DELETE s
            """,
                path=file_path,
            )

    @staticmethod
    def _upsert_nodes(tx, file_path, file_hash, symbols):
        # 1. Создаем/обновляем узел файла
        tx.run(
            """
            MERGE (f:File {path: $path})
            SET f.hash = $hash, f.last_indexed = timestamp()
        """,
            path=file_path,
            hash=file_hash,
        )

        # 2. Обновляем символы и связи CONTAINS
        for s in symbols:
            tx.run(
                """
                MATCH (f:File {path: $file_path})
                MERGE (s:Symbol {uid: $uid})
                SET s.name = $name,
                    s.kind = $kind,
                    s.hash = $content_hash,
                    s.range = [$start, $end],
                    s.token_estimate = $token_estimate
                MERGE (f)-[:CONTAINS]->(s)
            """,
                file_path=file_path,
                uid=s.uid,
                name=s.name,
                kind=s.kind,
                content_hash=s.content_hash,
                start=s.start_line,
                end=s.end_line,
                token_estimate=s.token_estimate,
            )

    def link_calls(self, calls: list[dict]):
        with self.driver.session() as session:
            session.execute_write(self._create_call_relations, calls)

    @staticmethod
    def _create_call_relations(tx, calls):
        for call in calls:
            # Ищем вызываемого (callee) по имени.
            # Это упрощенная логика, которую мы позже заменим на разрешение импортов.
            tx.run(
                """
                MATCH (caller:Symbol {uid: $caller_uid})
                MATCH (callee:Symbol {name: $callee_name})
                WHERE caller <> callee
                MERGE (caller)-[:CALLS]->(callee)
            """,
                caller_uid=call["caller_uid"],
                callee_name=call["callee_name"],
            )

    def link_imports(self, imports: list[ImportEdge]):
        with self.driver.session() as session:
            session.execute_write(self._create_import_relations, imports)

    @staticmethod
    def _create_import_relations(tx, imports):
        for imp in imports:
            tx.run(
                """
                MATCH (source:File {path: $source_file})
                MATCH (target:File)
                WHERE target.path CONTAINS $target_module
                MERGE (source)-[:IMPORTS {type: $import_type}]->(target)
            """,
                source_file=imp.source_file,
                target_module=imp.target_module_name,
                import_type=imp.import_type,
            )

    def link_inheritance(self, inheritance_edges: list[InheritanceEdge]):
        with self.driver.session() as session:
            session.execute_write(self._create_inheritance_relations, inheritance_edges)

    @staticmethod
    def _create_inheritance_relations(tx, inheritance_edges):
        for edge in inheritance_edges:
            tx.run(
                """
                MATCH (subclass:Symbol {uid: $subclass_uid})
                MATCH (superclass:Symbol {name: $superclass_name})
                MERGE (subclass)-[:DEPENDS_ON {is_interface: $is_interface}]->(superclass)
            """,
                subclass_uid=edge.subclass_uid,
                superclass_name=edge.superclass_name,
                is_interface=edge.is_interface,
            )
