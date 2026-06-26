import re
import time
from pathlib import Path

from context_engine.database.lancedb_client import LanceDBClient
from context_engine.database.neo4j_client import Neo4jClient
from context_engine.indexer.progress import make_progress as _make_progress
from context_engine.workspace import DEFAULT_WORKSPACE_ID

SIMILARITY_THRESHOLD = 1.5
IDENTIFIER_LINK_SKIP_THRESHOLD = 2
DOC_LINK_BATCH_SIZE = 128
ANCHOR_TYPES = {"definition", "example", "reference", "warning", "deprecated"}

_CAMEL_CASE_IDENTIFIER = re.compile(r"\b([A-Z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*)\b")
_SCREAMING_SNAKE_IDENTIFIER = re.compile(r"\b([A-Z][A-Z0-9_]*_[A-Z0-9_]+)\b")
_SNAKE_CASE_IDENTIFIER = re.compile(r"\b([a-z][a-z0-9]*_[a-z0-9_]+)\b")
_IDENTIFIER_PATTERNS = (
    _CAMEL_CASE_IDENTIFIER,
    _SCREAMING_SNAKE_IDENTIFIER,
    _SNAKE_CASE_IDENTIFIER,
)

# Matches markdown links and bare .md filenames that reference project docs
_DOC_LINK_TARGET_RE = re.compile(r"\]\(((?:docs/)?\w+\.md)\)")
_DOC_BARE_SPEC_REF_RE = re.compile(r"(?<!\w)((?:docs/)?spec_\w+\.md)")
_DOC_BARE_ARCHITECTURA_REF_RE = re.compile(r"(?<!\w)(architectura\.md)")
_DOC_BARE_CONCEPT_REF_RE = re.compile(r"(?<!\w)(concept\.md)")
_DOC_BARE_IDEA_REF_RE = re.compile(r"(?<!\w)(idea_\w+\.md)")
_DOC_REF_PATTERNS = (
    _DOC_LINK_TARGET_RE,
    _DOC_BARE_SPEC_REF_RE,
    _DOC_BARE_ARCHITECTURA_REF_RE,
    _DOC_BARE_CONCEPT_REF_RE,
    _DOC_BARE_IDEA_REF_RE,
)

_DOC_TYPE_MAP = {
    "spec_": "spec",
    "architectura": "architecture",
    "concept": "concept",
    "idea_": "idea",
    "road_map": "roadmap",
    "review_": "review",
}

_DEPRECATED_WORDS = frozenset(
    {"deprecated", "deprecation", "migration", "migrate", "removed", "renamed"}
)
_WARNING_WORDS = frozenset({"warning", "caution", "danger", "security", "important", "avoid"})
_DEFINITION_HEADING_WORDS = frozenset(
    {
        "api",
        "reference",
        "parameter",
        "parameters",
        "return",
        "returns",
        "class",
        "function",
        "method",
    }
)
_DEFINITION_BODY_WORDS = frozenset(
    {
        "argument",
        "arguments",
        "parameter",
        "parameters",
        "return",
        "returns",
        "raises",
        "signature",
    }
)


def _classify_doc_type(file_path: str) -> str:
    name = file_path.split("/")[-1]
    for prefix, doc_type in _DOC_TYPE_MAP.items():
        if prefix in name:
            return doc_type
    return "documentation"


def _contains_any_alpha_word(text: str, words: frozenset[str]) -> bool:
    """Return True when *text* contains a whole alphabetic token from *words*."""
    i = 0
    text_len = len(text)
    while i < text_len:
        while i < text_len and not text[i].isalpha():
            i += 1
        if i >= text_len:
            return False
        j = i
        while j < text_len and text[j].isalpha():
            j += 1
        if text[i:j] in words:
            return True
        i = j
    return False


def _markdown_heading_has_keyword(line: str, keywords: frozenset[str]) -> bool:
    """Match markdown headings `#`..`####` whose title contains a keyword."""
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return False
    level = 0
    while level < len(stripped) and stripped[level] == "#":
        level += 1
    if level < 1 or level > 4:
        return False
    body = stripped[level:]
    if not body or body[0] not in " \t":
        return False
    return _contains_any_alpha_word(body.lstrip().lower(), keywords)


def _classify_anchor_type(chunk_text: str, file_path: str = "") -> str:
    """Classify how strongly a doc chunk is meant to describe a symbol."""
    text = chunk_text.lower()
    path = file_path.lower()
    if _contains_any_alpha_word(text, _DEPRECATED_WORDS):
        return "deprecated"
    if _contains_any_alpha_word(text, _WARNING_WORDS):
        return "warning"
    if "```" in chunk_text or "/examples/" in path or "/tutorial/" in path:
        return "example"
    if (
        "/reference/" in path
        or any(
            _markdown_heading_has_keyword(line, _DEFINITION_HEADING_WORDS)
            for line in chunk_text.splitlines()
        )
        or _contains_any_alpha_word(text, _DEFINITION_BODY_WORDS)
    ):
        return "definition"
    return "reference"


def _clamp(value: float, low: float = 0.05, high: float = 1.0) -> float:
    return min(high, max(low, value))


def _symbol_name_in_heading(symbol_name: str, chunk_text: str) -> bool:
    if not symbol_name:
        return False
    pattern = re.compile(rf"^#{{1,6}}\s+.*\b{re.escape(symbol_name)}\b", re.M)
    return bool(pattern.search(chunk_text))


def _symbol_name_has_code_mention(symbol_name: str, chunk_text: str) -> bool:
    if not symbol_name:
        return False
    return f"`{symbol_name}`" in chunk_text or f"{symbol_name}(" in chunk_text


def _anchor_confidence(
    chunk_text: str,
    file_path: str,
    symbol_name: str,
    *,
    resolver: str,
    semantic_score: float = 0.0,
) -> float:
    """Score a COVERS edge without storing chunk content in Neo4j."""
    anchor_type = _classify_anchor_type(chunk_text, file_path)
    if resolver == "identifier":
        confidence = 0.68
    elif resolver == "pending_identifier":
        confidence = 0.62
    else:
        confidence = max(0.25, min(0.72, float(semantic_score or 0.0)))

    if symbol_name and re.search(rf"\b{re.escape(symbol_name)}\b", chunk_text):
        confidence += 0.14
    if _symbol_name_in_heading(symbol_name, chunk_text):
        confidence += 0.10
    if _symbol_name_has_code_mention(symbol_name, chunk_text):
        confidence += 0.08
    if anchor_type in {"definition", "warning", "deprecated"}:
        confidence += 0.05
    elif anchor_type == "example":
        confidence -= 0.04
    return round(_clamp(confidence), 3)


def _cover_link(
    uid: str,
    symbol_name: str = "",
    chunk_text: str = "",
    file_path: str = "",
    *,
    resolver: str = "identifier",
    semantic_score: float = 0.0,
    link_count: int = 1,
) -> dict:
    anchor_type = _classify_anchor_type(chunk_text, file_path)
    primary_bias = 1.0 if link_count <= 1 else 0.65
    if symbol_name and _symbol_name_in_heading(symbol_name, chunk_text):
        primary_bias = max(primary_bias, 0.9)
    return {
        "uid": uid,
        "anchor_type": anchor_type,
        "confidence": _anchor_confidence(
            chunk_text,
            file_path,
            symbol_name,
            resolver=resolver,
            semantic_score=semantic_score,
        ),
        "primary_bias": round(primary_bias, 3),
        "resolver": resolver,
    }


def _merge_cover_link(links_by_uid: dict[str, dict], link: dict) -> None:
    existing = links_by_uid.get(link["uid"])
    if existing is None or float(link.get("confidence", 0.0)) > float(
        existing.get("confidence", 0.0)
    ):
        links_by_uid[link["uid"]] = link


def _normalize_cover_links(
    *,
    uids: list[str] | None = None,
    links: list[dict] | None = None,
    resolver: str = "identifier",
) -> list[dict]:
    normalized: list[dict] = []
    for link in links or []:
        uid = link.get("uid")
        if not uid:
            continue
        anchor_type = (
            link.get("anchor_type") if link.get("anchor_type") in ANCHOR_TYPES else "reference"
        )
        normalized.append(
            {
                "uid": uid,
                "anchor_type": anchor_type,
                "confidence": float(link.get("confidence", 0.6)),
                "primary_bias": float(link.get("primary_bias", 0.6)),
                "resolver": link.get("resolver") or resolver,
            }
        )
    for uid in uids or []:
        normalized.append(
            {
                "uid": uid,
                "anchor_type": "reference",
                "confidence": 0.6,
                "primary_bias": 1.0 if len(uids or []) <= 1 else 0.65,
                "resolver": resolver,
            }
        )
    deduped: dict[str, dict] = {}
    for link in normalized:
        _merge_cover_link(deduped, link)
    return sorted(deduped.values(), key=lambda item: item["uid"])


def _extract_doc_references(chunk_text: str) -> list[str]:
    """Extract doc filenames referenced in a chunk (markdown links or bare filenames)."""
    refs: set[str] = set()
    for pattern in _DOC_REF_PATTERNS:
        for match in pattern.finditer(chunk_text):
            refs.add(match.group(1).split("/")[-1])
    return list(refs)


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


def _write_anchors(tx, rows: list[dict], workspace_id: str):
    if not rows:
        return
    tx.run(
        """
        MERGE (w:Workspace {id: $workspace_id})
        WITH w
        UNWIND $rows AS row
        MERGE (a:DocAnchor {chunk_id: row.chunk_id, workspace_id: $workspace_id})
        MERGE (a)-[:IN_WORKSPACE]->(w)
        MERGE (f:File {path: row.file_path, workspace_id: $workspace_id})
        SET f.doc_type = row.doc_type
        MERGE (f)-[:IN_WORKSPACE]->(w)
        MERGE (a)-[:FROM {type: "doc"}]->(f)
        """,
        rows=rows,
        workspace_id=workspace_id,
    )


def _add_covers_edge(tx, chunk_id: str, uid: str, workspace_id: str):
    """Create COVERS edge and a FROM {type: "code"} edge to the symbol's containing file."""
    links = _normalize_cover_links(uids=[uid])
    tx.run(
        """
        MATCH (a:DocAnchor {chunk_id: $chunk_id, workspace_id: $workspace_id})
        UNWIND $links AS link
        MATCH (s:Symbol {uid: link.uid})
        MERGE (a)-[r:COVERS {workspace_id: $workspace_id}]->(s)
        SET r.anchor_type = coalesce(link.anchor_type, 'reference'),
            r.confidence = coalesce(link.confidence, 0.6),
            r.primary_bias = coalesce(link.primary_bias, 0.6),
            r.resolver = coalesce(link.resolver, 'identifier')
        WITH a, collect(DISTINCT s) AS symbols
        UNWIND symbols AS sym
        MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(sym)
        SET f.doc_type = coalesce(f.doc_type, 'code')
        MERGE (a)-[:FROM {type: "code"}]->(f)
        """,
        chunk_id=chunk_id,
        links=links,
        workspace_id=workspace_id,
    )


def _add_covers_edges(tx, chunk_id: str, uids: list[str], workspace_id: str):
    links = _normalize_cover_links(uids=uids)
    if not links:
        return
    tx.run(
        """
        MATCH (a:DocAnchor {chunk_id: $chunk_id, workspace_id: $workspace_id})
        UNWIND $links AS link
        MATCH (s:Symbol {uid: link.uid})
        MERGE (a)-[r:COVERS {workspace_id: $workspace_id}]->(s)
        SET r.anchor_type = coalesce(link.anchor_type, 'reference'),
            r.confidence = coalesce(link.confidence, 0.6),
            r.primary_bias = coalesce(link.primary_bias, 0.6),
            r.resolver = coalesce(link.resolver, 'identifier')
        WITH a, collect(DISTINCT s) AS symbols
        UNWIND symbols AS sym
        MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(sym)
        SET f.doc_type = coalesce(f.doc_type, 'code')
        MERGE (a)-[:FROM {type: "code"}]->(f)
        """,
        chunk_id=chunk_id,
        links=links,
        workspace_id=workspace_id,
    )


def _add_covers_edges_batch(tx, rows: list[dict], workspace_id: str):
    if not rows:
        return
    normalized_rows = [
        {
            "chunk_id": row["chunk_id"],
            "links": _normalize_cover_links(
                uids=row.get("uids"),
                links=row.get("links"),
                resolver=row.get("resolver", "identifier"),
            ),
        }
        for row in rows
    ]
    tx.run(
        """
        UNWIND $rows AS row
        MATCH (a:DocAnchor {chunk_id: row.chunk_id, workspace_id: $workspace_id})
        UNWIND row.links AS link
        MATCH (s:Symbol {uid: link.uid})
        MERGE (a)-[r:COVERS {workspace_id: $workspace_id}]->(s)
        SET r.anchor_type = coalesce(link.anchor_type, 'reference'),
            r.confidence = coalesce(link.confidence, 0.6),
            r.primary_bias = coalesce(link.primary_bias, 0.6),
            r.resolver = coalesce(link.resolver, 'identifier')
        WITH a, collect(DISTINCT s) AS symbols
        UNWIND symbols AS sym
        MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(sym)
        SET f.doc_type = coalesce(f.doc_type, 'code')
        MERGE (a)-[:FROM {type: "code"}]->(f)
        """,
        rows=normalized_rows,
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


def _link_related_docs_batch(tx, rows: list[dict], workspace_id: str):
    if not rows:
        return
    tx.run(
        """
        UNWIND $rows AS row
        MATCH (a:DocAnchor {chunk_id: row.chunk_id, workspace_id: $workspace_id})
        UNWIND row.refs AS ref
        MATCH (related:File {workspace_id: $workspace_id})
        WHERE related.path ENDS WITH ref.ref AND related.path <> ''
        SET related.doc_type = coalesce(related.doc_type, ref.ref_type)
        MERGE (a)-[:FROM {type: ref.ref_type}]->(related)
        """,
        rows=rows,
        workspace_id=workspace_id,
    )


def _extract_identifiers(text: str) -> list[str]:
    found: set[str] = set()
    for pattern in _IDENTIFIER_PATTERNS:
        found.update(pattern.findall(text))
    return list(found)


def _normalize_pending(value) -> list[str]:
    if value is None:
        return []
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, str):
        return [value] if value else []
    try:
        return list(value)
    except TypeError:
        return [value]


def _build_symbol_vector_index(lance, workspace_id: str):
    if getattr(lance, "_sym_table", None) is None:
        return None
    try:
        import numpy as np
    except Exception:
        return None
    scan = getattr(lance, "scan_symbols_workspace", None)
    if not callable(scan):
        return None
    try:
        row_dicts = scan(workspace_id)
    except Exception:
        return None
    if not row_dicts:
        return None

    uids = []
    names = []
    file_paths = []
    vectors = []
    for row in row_dicts:
        vector = row.get("vector")
        if vector is None:
            continue
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        if len(vector) == 0:
            continue
        uids.append(row["uid"])
        names.append(row["name"])
        file_paths.append(row["file_path"])
        vectors.append(vector)
    if not vectors:
        return None

    matrix = np.asarray(vectors, dtype=np.float32)
    return {
        "uids": uids,
        "names": names,
        "file_paths": file_paths,
        "matrix": matrix,
        "norm_sq": np.einsum("ij,ij->i", matrix, matrix),
        "name_to_uid": {name: uid for name, uid in zip(names, uids, strict=False)},
    }


def _search_symbol_vectors_locally(
    index,
    vectors: list[list[float]],
    *,
    limit: int = 5,
    threshold: float = 0.4,
) -> list[list[dict]]:
    if not vectors or index is None:
        return [[] for _ in vectors]
    try:
        import numpy as np
    except Exception:
        return [[] for _ in vectors]

    query_matrix = np.asarray(
        [vector.tolist() if hasattr(vector, "tolist") else vector for vector in vectors],
        dtype=np.float32,
    )
    if query_matrix.size == 0:
        return [[] for _ in vectors]

    symbol_matrix = index["matrix"]
    symbol_count = symbol_matrix.shape[0]
    top_k = min(limit, symbol_count)
    if top_k <= 0:
        return [[] for _ in vectors]

    dots = query_matrix @ symbol_matrix.T
    query_norm_sq = np.einsum("ij,ij->i", query_matrix, query_matrix)[:, None]
    distance_sq = query_norm_sq + index["norm_sq"][None, :] - (2.0 * dots)
    np.maximum(distance_sq, 0.0, out=distance_sq)

    top_indices = np.argpartition(distance_sq, kth=top_k - 1, axis=1)[:, :top_k]
    results: list[list[dict]] = []
    for row_idx, candidate_indices in enumerate(top_indices):
        sorted_indices = candidate_indices[np.argsort(distance_sq[row_idx, candidate_indices])]
        row_hits = []
        for col_idx in sorted_indices:
            distance = float(np.sqrt(distance_sq[row_idx, col_idx]))
            if distance > threshold:
                continue
            row_hits.append(
                {
                    "uid": index["uids"][col_idx],
                    "name": index["names"][col_idx],
                    "file_path": index["file_paths"][col_idx],
                    "distance": distance,
                    "score": max(0.0, 1.0 - distance),
                }
            )
        results.append(row_hits)
    return results


def _bump_graph_version(neo4j: Neo4jClient, workspace_id: str):
    with neo4j.driver.session() as session:
        session.run(
            """
            MATCH (w:Workspace {id: $workspace_id})
            SET w.graph_version = coalesce(w.graph_version, 0) + 1
            """,
            workspace_id=workspace_id,
        )


def _normalize_allowed_prefixes(prefixes: list[str] | None) -> list[str]:
    if not prefixes:
        return []
    normalized = []
    for prefix in prefixes:
        if not prefix:
            continue
        normalized.append(str(Path(prefix).resolve()))
    return sorted(set(normalized))


def _matches_allowed_prefix(file_path: str | None, prefixes: list[str]) -> bool:
    if not prefixes:
        return True
    if not file_path:
        return False
    path = Path(file_path)
    candidates: list[str] = []
    if path.is_absolute():
        candidates.append(str(path.resolve()))
    else:
        for prefix in prefixes:
            candidates.append(str((Path(prefix) / path).resolve()))
        candidates.append(str(path.resolve()))
    for resolved in candidates:
        for prefix in prefixes:
            if resolved == prefix or resolved.startswith(f"{prefix}/"):
                return True
    return False


def _load_name_to_uid(
    neo4j: Neo4jClient,
    symbol_vector_index,
    workspace_id: str,
) -> dict[str, str]:
    if symbol_vector_index is not None:
        return dict(symbol_vector_index["name_to_uid"])
    with neo4j.driver.session() as session:
        result = session.run(
            """
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
            RETURN s.uid AS uid, s.name AS name
            """,
            workspace_id=workspace_id,
        )
        return {r["name"]: r["uid"] for r in result}


def _empty_doc_prepare_stats(**overrides) -> dict:
    stats = {
        "chunks": 0,
        "batches": [],
        "anchors": 0,
        "covers": 0,
        "related": 0,
        "pending_updates": 0,
        "prepare_sec": 0.0,
    }
    stats.update(overrides)
    return stats


def _resolve_identifier_links(
    chunk_text: str,
    file_path: str,
    name_to_uid: dict[str, str],
) -> tuple[set[str], set[str], dict[str, dict], list[str]]:
    identifier_matches: dict[str, str] = {}
    pending: list[str] = []
    for name in _extract_identifiers(chunk_text):
        if name in name_to_uid:
            identifier_matches[name] = name_to_uid[name]
        else:
            pending.append(name)

    resolved_uids = set(identifier_matches.values())
    matched_names = set(identifier_matches)
    link_count = max(1, len(resolved_uids))
    links_by_uid: dict[str, dict] = {}
    for name, uid in identifier_matches.items():
        _merge_cover_link(
            links_by_uid,
            _cover_link(
                uid,
                name,
                chunk_text,
                file_path,
                resolver="identifier",
                link_count=link_count,
            ),
        )
    return resolved_uids, matched_names, links_by_uid, pending


def _append_chunk_link_outputs(
    row: dict,
    chunk_text: str,
    file_path: str,
    *,
    resolved_uids: set[str],
    links_by_uid: dict[str, dict],
    pending: list[str],
    anchor_rows: list[dict],
    cover_rows: list[dict],
    related_rows: list[dict],
    pending_updates: list[tuple[dict, list[str]]],
) -> None:
    chunk_id = row["id"]
    current_pending = _normalize_pending(row.get("pending"))
    if pending != current_pending:
        pending_updates.append((row, pending))
    anchor_rows.append(
        {
            "chunk_id": chunk_id,
            "file_path": file_path,
            "doc_type": _classify_doc_type(file_path),
        }
    )
    if resolved_uids:
        cover_rows.append(
            {
                "chunk_id": chunk_id,
                "links": sorted(links_by_uid.values(), key=lambda item: item["uid"]),
            }
        )
    refs = _extract_doc_references(chunk_text)
    if refs:
        related_rows.append(
            {
                "chunk_id": chunk_id,
                "refs": [{"ref": ref, "ref_type": _classify_doc_type(ref)} for ref in refs],
            }
        )


def _search_symbols_by_vector(lance, vector, *, workspace_id: str) -> list[dict]:
    try:
        return lance.search_symbols_by_vector(
            vector,
            limit=5,
            threshold=SIMILARITY_THRESHOLD,
            workspace_id=workspace_id,
        )
    except TypeError:
        return lance.search_symbols_by_vector(
            vector,
            limit=5,
            threshold=SIMILARITY_THRESHOLD,
        )


def _search_symbols_by_text(lance, chunk_text: str, *, workspace_id: str) -> list[dict]:
    try:
        return lance.search_symbols(
            chunk_text,
            limit=5,
            threshold=SIMILARITY_THRESHOLD,
            workspace_id=workspace_id,
        )
    except TypeError:
        return lance.search_symbols(
            chunk_text,
            limit=5,
            threshold=SIMILARITY_THRESHOLD,
        )


def _semantic_hits_for_row(state: dict, lance, *, workspace_id: str) -> list[dict]:
    vector = state["row"].get("vector")
    if vector is not None:
        return _search_symbols_by_vector(lance, vector, workspace_id=workspace_id)
    return _search_symbols_by_text(lance, state["chunk_text"], workspace_id=workspace_id)


def _semantic_hits_for_states(
    semantic_states: list[dict],
    symbol_vector_index,
    lance,
    *,
    workspace_id: str,
) -> list[list[dict]]:
    if symbol_vector_index is not None and all(
        state["row"].get("vector") is not None for state in semantic_states
    ):
        vectors = [state["row"].get("vector") for state in semantic_states]
        return _search_symbol_vectors_locally(
            symbol_vector_index,
            vectors,
            limit=5,
            threshold=SIMILARITY_THRESHOLD,
        )
    return [
        _semantic_hits_for_row(state, lance, workspace_id=workspace_id) for state in semantic_states
    ]


def _apply_semantic_hits(state: dict, hits: list[dict]) -> list[str]:
    row = state["row"]
    chunk_text = state["chunk_text"]
    file_path = row["file_path"]
    pending = list(state["pending"])
    resolved_uids = set(state["resolved_uids"])
    matched_names = set(state["matched_names"])
    links_by_uid = dict(state["links_by_uid"])

    matched_names.update(h["name"] for h in hits)
    resolved_uids.update(h["uid"] for h in hits)
    link_count = max(1, len(resolved_uids))
    for hit in hits:
        _merge_cover_link(
            links_by_uid,
            _cover_link(
                hit["uid"],
                hit.get("name", ""),
                chunk_text,
                file_path,
                resolver="semantic",
                semantic_score=float(hit.get("score") or 0.0),
                link_count=link_count,
            ),
        )
    state["resolved_uids"] = resolved_uids
    state["matched_names"] = matched_names
    state["links_by_uid"] = links_by_uid
    return [name for name in pending if name not in matched_names]


def _process_doc_link_batch(
    batch: list[dict],
    *,
    name_to_uid: dict[str, str],
    symbol_vector_index,
    lance,
    workspace_id: str,
) -> dict:
    anchor_rows: list[dict] = []
    cover_rows: list[dict] = []
    related_rows: list[dict] = []
    pending_updates: list[tuple[dict, list[str]]] = []
    semantic_states: list[dict] = []

    for row in batch:
        chunk_text = row["chunk"]
        file_path = row["file_path"]
        resolved_uids, matched_names, links_by_uid, pending = _resolve_identifier_links(
            chunk_text,
            file_path,
            name_to_uid,
        )
        if len(resolved_uids) < IDENTIFIER_LINK_SKIP_THRESHOLD:
            semantic_states.append(
                {
                    "row": row,
                    "chunk_text": chunk_text,
                    "pending": pending,
                    "resolved_uids": resolved_uids,
                    "matched_names": matched_names,
                    "links_by_uid": links_by_uid,
                }
            )
            continue
        _append_chunk_link_outputs(
            row,
            chunk_text,
            file_path,
            resolved_uids=resolved_uids,
            links_by_uid=links_by_uid,
            pending=pending,
            anchor_rows=anchor_rows,
            cover_rows=cover_rows,
            related_rows=related_rows,
            pending_updates=pending_updates,
        )

    if semantic_states:
        semantic_hits = _semantic_hits_for_states(
            semantic_states,
            symbol_vector_index,
            lance,
            workspace_id=workspace_id,
        )
        for state, hits in zip(semantic_states, semantic_hits, strict=False):
            pending = _apply_semantic_hits(state, hits)
            row = state["row"]
            _append_chunk_link_outputs(
                row,
                state["chunk_text"],
                row["file_path"],
                resolved_uids=set(state["resolved_uids"]),
                links_by_uid=state["links_by_uid"],
                pending=pending,
                anchor_rows=anchor_rows,
                cover_rows=cover_rows,
                related_rows=related_rows,
                pending_updates=pending_updates,
            )

    return {
        "count": len(batch),
        "anchor_rows": anchor_rows,
        "cover_rows": cover_rows,
        "related_rows": related_rows,
        "pending_updates": pending_updates,
    }


def _apply_lance_pending_updates(lance, pending_updates: list[tuple[dict, list[str]]]) -> None:
    if not pending_updates:
        return
    bulk_set_pending = getattr(lance, "set_pending_rows_batch", None)
    if callable(bulk_set_pending):
        bulk_set_pending(pending_updates)
        return
    for row, pending in pending_updates:
        lance.set_pending_row(row, pending)


def _prepare_doc_link_batches(
    neo4j: Neo4jClient,
    lance: LanceDBClient,
    workspace_id: str,
    allowed_prefixes: list[str] | None = None,
):
    scan_docs = getattr(lance, "scan_docs_workspace", None)
    if not callable(scan_docs):
        return _empty_doc_prepare_stats()
    try:
        all_rows = scan_docs(workspace_id)
    except Exception:
        all_rows = []

    t0 = time.perf_counter()
    prefixes = _normalize_allowed_prefixes(allowed_prefixes)
    row_records = [
        row for row in all_rows if _matches_allowed_prefix(row.get("file_path"), prefixes)
    ]
    if not row_records:
        return _empty_doc_prepare_stats()

    symbol_vector_index = _build_symbol_vector_index(lance, workspace_id)
    name_to_uid = _load_name_to_uid(neo4j, symbol_vector_index, workspace_id)

    batches = []
    total_anchors = 0
    total_covers = 0
    total_related = 0
    total_pending_updates = 0

    progress = _make_progress(len(row_records), "docs prepare", unit="chunk")
    batch_size = max(1, DOC_LINK_BATCH_SIZE)
    for start in range(0, len(row_records), batch_size):
        batch = row_records[start : start + batch_size]
        prepared = _process_doc_link_batch(
            batch,
            name_to_uid=name_to_uid,
            symbol_vector_index=symbol_vector_index,
            lance=lance,
            workspace_id=workspace_id,
        )
        batches.append(prepared)
        total_anchors += len(prepared["anchor_rows"])
        total_covers += len(prepared["cover_rows"])
        total_related += len(prepared["related_rows"])
        total_pending_updates += len(prepared["pending_updates"])
        progress.update(len(batch))
    progress.close()

    return {
        "chunks": len(row_records),
        "batches": batches,
        "anchors": total_anchors,
        "covers": total_covers,
        "related": total_related,
        "pending_updates": total_pending_updates,
        "prepare_sec": round(time.perf_counter() - t0, 3),
    }


def _empty_doc_link_result(**overrides) -> dict:
    stats = {
        "chunks": 0,
        "anchors": 0,
        "covers": 0,
        "related": 0,
        "pending_updates": 0,
        "prepare_sec": 0.0,
        "neo_write_sec": 0.0,
        "total_sec": 0.0,
    }
    stats.update(overrides)
    return stats


def _write_doc_link_batch(
    session,
    batch: dict,
    *,
    lance,
    workspace_id: str,
) -> None:
    session.execute_write(_write_anchors, batch["anchor_rows"], workspace_id)
    if batch["cover_rows"]:
        session.execute_write(_add_covers_edges_batch, batch["cover_rows"], workspace_id)
    if batch["related_rows"]:
        session.execute_write(_link_related_docs_batch, batch["related_rows"], workspace_id)
    _apply_lance_pending_updates(lance, batch["pending_updates"])


def link_docs_to_symbols(
    neo4j: Neo4jClient,
    lance: LanceDBClient,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    allowed_prefixes: list[str] | None = None,
):
    prepared = _prepare_doc_link_batches(
        neo4j,
        lance,
        workspace_id,
        allowed_prefixes=allowed_prefixes,
    )
    if prepared["chunks"] == 0:
        return _empty_doc_link_result()

    progress = _make_progress(prepared["chunks"], "docs link", unit="chunk")
    t0 = time.perf_counter()
    with neo4j.driver.session() as session:
        for batch in prepared["batches"]:
            _write_doc_link_batch(session, batch, lance=lance, workspace_id=workspace_id)
            progress.update(batch["count"])
    progress.close()

    neo_write_sec = round(time.perf_counter() - t0, 3)
    total_sec = round(prepared["prepare_sec"] + neo_write_sec, 3)
    print(
        "DocAnchor: "
        f"chunks={prepared['chunks']} anchors={prepared['anchors']} "
        f"covers={prepared['covers']} related={prepared['related']} "
        f"pending_updates={prepared['pending_updates']} "
        f"timings={{'prepare': {prepared['prepare_sec']}, 'neo_write': {neo_write_sec}, 'total': {total_sec}}}"
    )
    _bump_graph_version(neo4j, workspace_id)
    return {
        "chunks": prepared["chunks"],
        "anchors": prepared["anchors"],
        "covers": prepared["covers"],
        "related": prepared["related"],
        "pending_updates": prepared["pending_updates"],
        "prepare_sec": prepared["prepare_sec"],
        "neo_write_sec": neo_write_sec,
        "total_sec": total_sec,
    }


def _resolve_pending_chunk(
    row: dict,
    name_to_uid: dict[str, str],
) -> tuple[dict | None, tuple[dict, list[str]] | None, int]:
    chunk_id = row["id"]
    chunk_text = row.get("chunk", "")
    file_path = row.get("file_path", "")
    names = _normalize_pending(row.get("pending"))
    links_by_uid: dict[str, dict] = {}
    still_pending: list[str] = []
    resolved_count = 0
    for name in names:
        if name in name_to_uid:
            uid = name_to_uid[name]
            _merge_cover_link(
                links_by_uid,
                _cover_link(
                    uid,
                    name,
                    chunk_text,
                    file_path,
                    resolver="pending_identifier",
                    link_count=max(1, len(names)),
                ),
            )
            resolved_count += 1
        else:
            still_pending.append(name)
    cover_row = None
    if links_by_uid:
        cover_row = {
            "chunk_id": chunk_id,
            "links": sorted(links_by_uid.values(), key=lambda item: item["uid"]),
            "resolver": "pending_identifier",
        }
    pending_update = (row, still_pending) if still_pending != names else None
    return cover_row, pending_update, resolved_count


def resolve_pending_anchors(
    neo4j: Neo4jClient,
    lance: LanceDBClient,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    allowed_prefixes: list[str] | None = None,
):
    """Resolve previously-unresolved identifier names to symbol UIDs.

    Batches the per-chunk writes so we issue exactly one Cypher transaction
    plus one Lance batch update — instead of one of each per pending row.
    """
    prefixes = _normalize_allowed_prefixes(allowed_prefixes)
    pending_rows = [
        row
        for row in lance.get_pending_rows(workspace_id=workspace_id)
        if _matches_allowed_prefix(row.get("file_path"), prefixes)
    ]
    if not pending_rows:
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
    cover_rows: list[dict] = []
    pending_updates: list[tuple[dict, list[str]]] = []
    progress = _make_progress(len(pending_rows), "docs pending", unit="chunk")
    for row in pending_rows:
        cover_row, pending_update, resolved_count = _resolve_pending_chunk(row, name_to_uid)
        resolved_total += resolved_count
        if cover_row is not None:
            cover_rows.append(cover_row)
        if pending_update is not None:
            pending_updates.append(pending_update)
        progress.update(1)
    progress.close()

    if cover_rows:
        with neo4j.driver.session() as session:
            session.execute_write(_add_covers_edges_batch, cover_rows, workspace_id)

    _apply_lance_pending_updates(lance, pending_updates)

    print(f"DocAnchor: resolved {resolved_total} pending links.")
    if resolved_total:
        _bump_graph_version(neo4j, workspace_id)


def _symbol_docstring_chunk_id(file_path: str, owner_uid: str) -> str:
    return f"{file_path}::doc::{owner_uid}"


def _resolve_indexed_symbol_uid(neo4j: Neo4jClient, workspace_id: str, sym) -> str | None:
    """Map a freshly-parsed symbol to the uid already stored in Neo4j."""
    owner_uid = str(getattr(sym, "uid", "") or "")
    qualified_name = str(getattr(sym, "qualified_name", "") or "")
    name = str(getattr(sym, "name", "") or "")
    file_path = str(getattr(sym, "file_path", "") or "")
    with neo4j.driver.session() as session:
        if owner_uid:
            rec = session.run(
                "MATCH (s:Symbol {workspace_id: $ws, uid: $uid}) RETURN s.uid AS uid LIMIT 1",
                ws=workspace_id,
                uid=owner_uid,
            ).single()
            if rec:
                return str(rec["uid"])
        if qualified_name:
            rec = session.run(
                """
                MATCH (s:Symbol {workspace_id: $ws, qualified_name: $qn})
                RETURN s.uid AS uid
                LIMIT 1
                """,
                ws=workspace_id,
                qn=qualified_name,
            ).single()
            if rec:
                return str(rec["uid"])
        if name and file_path:
            rec = session.run(
                """
                MATCH (s:Symbol {workspace_id: $ws, name: $name})
                WHERE coalesce(s.file_path, '') = $file_path
                   OR s.file_path ENDS WITH $file_path
                RETURN s.uid AS uid
                LIMIT 1
                """,
                ws=workspace_id,
                name=name,
                file_path=file_path,
            ).single()
            if rec:
                return str(rec["uid"])
    return None


def _docstring_rows_for_symbol(sym, owner_uid: str) -> tuple[dict, dict, dict]:
    file_path = str(getattr(sym, "file_path", "") or "")
    docstring = str(getattr(sym, "docstring", "") or "").strip()
    chunk_id = _symbol_docstring_chunk_id(file_path, owner_uid)
    lance_row = {
        "id": chunk_id,
        "file_path": file_path,
        "chunk": docstring,
        "owner_uid": owner_uid,
    }
    anchor_row = {
        "chunk_id": chunk_id,
        "file_path": file_path,
        "doc_type": "code",
    }
    cover_row = {
        "chunk_id": chunk_id,
        "links": [
            {
                "uid": owner_uid,
                "anchor_type": "definition",
                "confidence": 1.0,
                "primary_bias": 1.0,
                "resolver": "definition",
            }
        ],
        "resolver": "definition",
    }
    return lance_row, anchor_row, cover_row


def _docstring_symbol_filter(
    sym,
    *,
    neo4j: Neo4jClient,
    workspace_id: str,
    prefixes: list[str],
    tiers: dict[str, str],
) -> tuple[str | None, bool]:
    from context_engine.indexer.file_tier import classify_file_tier, is_doc_anchor_indexable_tier

    docstring = str(getattr(sym, "docstring", "") or "").strip()
    file_path = str(getattr(sym, "file_path", "") or "")
    if not docstring or not file_path:
        return None, False
    if not _matches_allowed_prefix(file_path, prefixes):
        return None, False
    tier = tiers.get(file_path) or classify_file_tier(file_path)
    if not is_doc_anchor_indexable_tier(tier):
        return None, True
    return _resolve_indexed_symbol_uid(neo4j, workspace_id, sym), False


def ingest_symbol_docstrings(
    neo4j: Neo4jClient,
    lance: LanceDBClient,
    symbols: list,
    *,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    allowed_prefixes: list[str] | None = None,
    removed_owner_uids: list[str] | None = None,
    file_tier_by_path: dict[str, str] | None = None,
) -> dict[str, int]:
    """Index in-code docstrings as doc-anchor rows + direct COVERS edges.

    Each symbol with a non-empty ``docstring`` gets a Lance doc-chunk row
    (``owner_uid`` set for Stage-1 resolution) and a ``DocAnchor`` with
    ``COVERS→owner_uid`` at ``resolver='definition'``, ``confidence=1.0``.

    Symbols in non-core file tiers (``test``, ``example``, ``stub``, …) are
    skipped — doc-anchor seed targets library code, not structural noise.
    """
    prefixes = _normalize_allowed_prefixes(allowed_prefixes)
    delete_by_owner = getattr(lance, "delete_doc_anchors_by_owner_uids", None)
    if callable(delete_by_owner) and removed_owner_uids:
        delete_by_owner(removed_owner_uids, workspace_id=workspace_id)

    lance_rows: list[dict] = []
    anchor_rows: list[dict] = []
    cover_rows: list[dict] = []
    skipped_noise = 0
    tiers = file_tier_by_path or {}

    for sym in symbols:
        owner_uid, skipped = _docstring_symbol_filter(
            sym,
            neo4j=neo4j,
            workspace_id=workspace_id,
            prefixes=prefixes,
            tiers=tiers,
        )
        if skipped:
            skipped_noise += 1
            continue
        if not owner_uid:
            continue
        lance_row, anchor_row, cover_row = _docstring_rows_for_symbol(sym, owner_uid)
        lance_rows.append(lance_row)
        anchor_rows.append(anchor_row)
        cover_rows.append(cover_row)

    if not lance_rows:
        return {"anchors": 0, "covers": 0, "rows": 0, "skipped_noise": skipped_noise}

    upsert = getattr(lance, "upsert_symbol_docstring_rows", None)
    if not callable(upsert):
        return {"anchors": 0, "covers": 0, "rows": 0, "skipped_noise": skipped_noise}
    upsert(lance_rows, workspace_id=workspace_id)

    with neo4j.driver.session() as session:
        session.execute_write(_write_anchors, anchor_rows, workspace_id)
        session.execute_write(_add_covers_edges_batch, cover_rows, workspace_id)
    _bump_graph_version(neo4j, workspace_id)
    return {
        "anchors": len(anchor_rows),
        "covers": len(cover_rows),
        "rows": len(lance_rows),
        "skipped_noise": skipped_noise,
    }
