import re

from sidecar.database.lancedb_client import LanceDBClient
from sidecar.database.neo4j_client import Neo4jClient

SIMILARITY_THRESHOLD = 1.5

# CamelCase, UPPER_CASE_WITH_UNDERSCORE, snake_case_with_underscore
_IDENTIFIER_RE = re.compile(
    r"\b([A-Z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*|[A-Z][A-Z0-9_]*_[A-Z0-9_]+|[a-z][a-z0-9]*_[a-z0-9_]+)\b"
)


def _extract_identifiers(text: str) -> list[str]:
    return list(set(_IDENTIFIER_RE.findall(text)))


def _write_anchor(tx, chunk_id: str, file_path: str):
    tx.run(
        """
        MERGE (a:DocAnchor {chunk_id: $chunk_id})
        WITH a
        MERGE (f:File {path: $file_path})
        MERGE (a)-[:FROM]->(f)
    """,
        chunk_id=chunk_id,
        file_path=file_path,
    )


def _add_covers_edge(tx, chunk_id: str, uid: str):
    tx.run(
        """
        MATCH (a:DocAnchor {chunk_id: $chunk_id})
        MATCH (s:Symbol {uid: $uid})
        MERGE (a)-[:COVERS]->(s)
    """,
        chunk_id=chunk_id,
        uid=uid,
    )


def link_docs_to_symbols(neo4j: Neo4jClient, lance: LanceDBClient):
    rows = lance._table.to_pandas()
    if rows.empty:
        return

    with neo4j.driver.session() as session:
        result = session.run("MATCH (s:Symbol) RETURN s.uid AS uid, s.name AS name")
        name_to_uid = {r["name"]: r["uid"] for r in result}

    for _, row in rows.iterrows():
        chunk_id = row["id"]
        chunk_text = row["chunk"]
        file_path = row["file_path"]

        hits = lance.search_symbols(chunk_text, limit=5, threshold=SIMILARITY_THRESHOLD)
        matched_names = {h["name"] for h in hits}

        with neo4j.driver.session() as session:
            session.execute_write(_write_anchor, chunk_id, file_path)
            for hit in hits:
                session.execute_write(_add_covers_edge, chunk_id, hit["uid"])

        pending = []
        for name in _extract_identifiers(chunk_text):
            if name in matched_names:
                continue
            if name in name_to_uid:
                with neo4j.driver.session() as session:
                    session.execute_write(_add_covers_edge, chunk_id, name_to_uid[name])
            else:
                pending.append(name)

        lance.set_pending(chunk_id, pending)

    print(f"DocAnchor: processed {len(rows)} chunks.")


def resolve_pending_anchors(neo4j: Neo4jClient, lance: LanceDBClient):
    pending_store = lance.get_pending()
    if not pending_store:
        return

    with neo4j.driver.session() as session:
        result = session.run("MATCH (s:Symbol) RETURN s.uid AS uid, s.name AS name")
        name_to_uid = {r["name"]: r["uid"] for r in result}

    resolved_total = 0
    for chunk_id, names in pending_store.items():
        still_pending = []
        for name in names:
            if name in name_to_uid:
                with neo4j.driver.session() as session:
                    session.execute_write(_add_covers_edge, chunk_id, name_to_uid[name])
                resolved_total += 1
            else:
                still_pending.append(name)
        lance.set_pending(chunk_id, still_pending)

    print(f"DocAnchor: resolved {resolved_total} pending links.")
