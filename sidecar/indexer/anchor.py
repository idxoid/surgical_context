import re

from sidecar.database.lancedb_client import LanceDBClient
from sidecar.database.neo4j_client import Neo4jClient
from sidecar.workspace import DEFAULT_WORKSPACE_ID

SIMILARITY_THRESHOLD = 1.5

_IDENTIFIER_RE = re.compile(
    r"\b([A-Z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*|[A-Z][A-Z0-9_]*_[A-Z0-9_]+|[a-z][a-z0-9]*_[a-z0-9_]+)\b"
)

# Matches markdown links and bare .md filenames that reference project docs
_DOC_REF_RE = re.compile(r'\]\(((?:docs/)?[\w_]+\.md)\)|(?<!\w)((?:docs/)?spec_[\w_]+\.md|architectura\.md|concept\.md|idea_[\w_]+\.md)')

_DOC_TYPE_MAP = {
    "spec_": "spec",
    "architectura": "architecture",
    "concept": "concept",
    "idea_": "idea",
    "road_map": "roadmap",
    "review_": "review",
}


def _classify_doc_type(file_path: str) -> str:
    name = file_path.split("/")[-1]
    for prefix, doc_type in _DOC_TYPE_MAP.items():
        if prefix in name:
            return doc_type
    return "documentation"


def _extract_doc_references(chunk_text: str) -> list[str]:
    """Extract doc filenames referenced in a chunk (markdown links or bare filenames)."""
    refs = []
    for match in _DOC_REF_RE.finditer(chunk_text):
        ref = match.group(1) or match.group(2)
        if ref:
            refs.append(ref.split("/")[-1])  # basename only; matched against File.path ENDS WITH
    return list(set(refs))


def _write_anchor(tx, chunk_id: str, file_path: str, workspace_id: str):
    doc_type = _classify_doc_type(file_path)
    tx.run(
        """
        MERGE (w:Workspace {id: $workspace_id})
        MERGE (a:DocAnchor {chunk_id: $chunk_id, workspace_id: $workspace_id})
        MERGE (a)-[:IN_WORKSPACE]->(w)
        MERGE (f:File {path: $file_path, workspace_id: $workspace_id})
        SET f.doc_type = $doc_type
        MERGE (f)-[:IN_WORKSPACE]->(w)
        MERGE (a)-[:FROM {type: "doc"}]->(f)
        """,
        chunk_id=chunk_id,
        file_path=file_path,
        doc_type=doc_type,
        workspace_id=workspace_id,
    )


def _add_covers_edge(tx, chunk_id: str, uid: str, workspace_id: str):
    """Create COVERS edge and a FROM {type: "code"} edge to the symbol's containing file."""
    tx.run(
        """
        MATCH (a:DocAnchor {chunk_id: $chunk_id, workspace_id: $workspace_id})
        MATCH (s:Symbol {uid: $uid})
        MERGE (a)-[:COVERS {workspace_id: $workspace_id}]->(s)
        WITH a, s
        MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(s)
        SET f.doc_type = coalesce(f.doc_type, 'code')
        MERGE (a)-[:FROM {type: "code"}]->(f)
        """,
        chunk_id=chunk_id,
        uid=uid,
        workspace_id=workspace_id,
    )


def _link_related_docs(tx, chunk_id: str, chunk_text: str, workspace_id: str):
    """Create FROM {type: <doc_type>} edges to any project docs referenced in the chunk."""
    refs = _extract_doc_references(chunk_text)
    if not refs:
        return
    for ref in refs:
        ref_type = _classify_doc_type(ref)
        tx.run(
            """
            MATCH (a:DocAnchor {chunk_id: $chunk_id, workspace_id: $workspace_id})
            MATCH (related:File {workspace_id: $workspace_id})
            WHERE related.path ENDS WITH $ref AND related.path <> ''
            SET related.doc_type = coalesce(related.doc_type, $ref_type)
            MERGE (a)-[:FROM {type: $ref_type}]->(related)
            """,
            chunk_id=chunk_id,
            ref=ref,
            ref_type=ref_type,
            workspace_id=workspace_id,
        )


def _extract_identifiers(text: str) -> list[str]:
    return list(set(_IDENTIFIER_RE.findall(text)))


def link_docs_to_symbols(
    neo4j: Neo4jClient,
    lance: LanceDBClient,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
):
    rows = lance._table.to_pandas()
    if rows.empty:
        return

    with neo4j.driver.session() as session:
        result = session.run(
            """
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
            RETURN s.uid AS uid, s.name AS name
            """,
            workspace_id=workspace_id,
        )
        name_to_uid = {r["name"]: r["uid"] for r in result}

    for _, row in rows.iterrows():
        chunk_id = row["id"]
        chunk_text = row["chunk"]
        file_path = row["file_path"]

        hits = lance.search_symbols(chunk_text, limit=5, threshold=SIMILARITY_THRESHOLD)
        matched_names = {h["name"] for h in hits}

        with neo4j.driver.session() as session:
            session.execute_write(_write_anchor, chunk_id, file_path, workspace_id)
            for hit in hits:
                session.execute_write(_add_covers_edge, chunk_id, hit["uid"], workspace_id)
            session.execute_write(_link_related_docs, chunk_id, chunk_text, workspace_id)

        pending = []
        for name in _extract_identifiers(chunk_text):
            if name in matched_names:
                continue
            if name in name_to_uid:
                with neo4j.driver.session() as session:
                    session.execute_write(
                        _add_covers_edge, chunk_id, name_to_uid[name], workspace_id
                    )
            else:
                pending.append(name)

        lance.set_pending(chunk_id, pending)

    print(f"DocAnchor: processed {len(rows)} chunks.")


def resolve_pending_anchors(
    neo4j: Neo4jClient,
    lance: LanceDBClient,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
):
    pending_store = lance.get_pending()
    if not pending_store:
        return

    with neo4j.driver.session() as session:
        result = session.run(
            """
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
            RETURN s.uid AS uid, s.name AS name
            """,
            workspace_id=workspace_id,
        )
        name_to_uid = {r["name"]: r["uid"] for r in result}

    resolved_total = 0
    for chunk_id, names in pending_store.items():
        still_pending = []
        for name in names:
            if name in name_to_uid:
                with neo4j.driver.session() as session:
                    session.execute_write(
                        _add_covers_edge, chunk_id, name_to_uid[name], workspace_id
                    )
                resolved_total += 1
            else:
                still_pending.append(name)
        lance.set_pending(chunk_id, still_pending)

    print(f"DocAnchor: resolved {resolved_total} pending links.")
