
from neo4j import GraphDatabase

from sidecar.parser.protocol import SymbolMetadata


class Neo4jClient:
    def __init__(self, uri, user, password):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self):
        self.driver.close()

    def upsert_file_structure(self, file_path: str, file_hash: str, symbols: list[SymbolMetadata]):
        with self.driver.session() as session:
            session.execute_write(self._upsert_nodes, file_path, file_hash, symbols)

    @staticmethod
    def _upsert_nodes(tx, file_path, file_hash, symbols):
        # 1. Создаем/обновляем узел файла
        tx.run("""
            MERGE (f:File {path: $path})
            SET f.hash = $hash, f.last_indexed = timestamp()
        """, path=file_path, hash=file_hash)

        # 2. Обновляем символы и связи CONTAINS
        for s in symbols:
            tx.run("""
                MATCH (f:File {path: $file_path})
                MERGE (s:Symbol {uid: $uid})
                SET s.name = $name,
                    s.kind = $kind,
                    s.hash = $content_hash,
                    s.range = [$start, $end]
                MERGE (f)-[:CONTAINS]->(s)
            """, 
            file_path=file_path, uid=s.uid, name=s.name, 
            kind=s.kind, content_hash=s.content_hash, 
            start=s.start_line, end=s.end_line)

    def link_calls(self, calls: list[dict]):
        with self.driver.session() as session:
            session.execute_write(self._create_call_relations, calls)

    @staticmethod
    def _create_call_relations(tx, calls):
        for call in calls:
            # Ищем вызываемого (callee) по имени. 
            # Это упрощенная логика, которую мы позже заменим на разрешение импортов.
            tx.run("""
                MATCH (caller:Symbol {uid: $caller_uid})
                MATCH (callee:Symbol {name: $callee_name})
                WHERE caller <> callee
                MERGE (caller)-[:CALLS]->(callee)
            """, caller_uid=call['caller_uid'], callee_name=call['callee_name'])