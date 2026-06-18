import re
import time
from dataclasses import dataclass
from pathlib import Path

from context_engine.database.lancedb_client import LanceDBClient
from context_engine.database.neo4j_client import Neo4jClient
from context_engine.workspace import DEFAULT_WORKSPACE_ID

SIMILARITY_THRESHOLD = 1.5
IDENTIFIER_LINK_SKIP_THRESHOLD = 2
DOC_LINK_BATCH_SIZE = 128
ANCHOR_TYPES = {"definition", "example", "reference", "warning", "deprecated"}

_IDENTIFIER_RE = re.compile(
    r"\b([A-Z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*|[A-Z][A-Z0-9_]*_[A-Z0-9_]+|[a-z][a-z0-9]*_[a-z0-9_]+)\b"
)

# Matches markdown links and bare .md filenames that reference project docs
_DOC_REF_RE = re.compile(
    r"\]\(((?:docs/)?[\w_]+\.md)\)|(?<!\w)((?:docs/)?spec_[\w_]+\.md|architectura\.md|concept\.md|idea_[\w_]+\.md)"
)

_DOC_TYPE_MAP = {
    "spec_": "spec",
    "architectura": "architecture",
    "concept": "concept",
    "idea_": "idea",
    "road_map": "roadmap",
    "review_": "review",
}


@dataclass
class _LineProgress:
    total: int
    desc: str
    unit: str = "item"
    done: int = 0
    _last_bucket: int = -1

    def __post_init__(self):
        print(f"{self.desc}: 0/{self.total} {self.unit}")

    def update(self, n: int = 1):
        self.done += n
        if self.total <= 0:
            return
        percent = min(100, int((self.done / self.total) * 100))
        bucket = percent // 10
        if percent == 100 or bucket > self._last_bucket:
            print(f"{self.desc}: {min(self.done, self.total)}/{self.total} ({percent}%)")
            self._last_bucket = bucket

    def close(self):
        if self.total == 0:
            print(f"{self.desc}: done")
        elif self.done < self.total:
            print(f"{self.desc}: {self.total}/{self.total} (100%)")


def _make_progress(total: int, desc: str, unit: str = "item"):
    try:
        from tqdm import tqdm

        return tqdm(total=total, desc=desc, unit=unit, dynamic_ncols=True, leave=True)
    except Exception:
        return _LineProgress(total=total, desc=desc, unit=unit)


def _classify_doc_type(file_path: str) -> str:
    name = file_path.split("/")[-1]
    for prefix, doc_type in _DOC_TYPE_MAP.items():
        if prefix in name:
            return doc_type
    return "documentation"


def _classify_anchor_type(chunk_text: str, file_path: str = "") -> str:
    """Classify how strongly a doc chunk is meant to describe a symbol."""
    text = chunk_text.lower()
    path = file_path.lower()
    if re.search(r"\b(deprecated|deprecation|migration|migrate|removed|renamed)\b", text):
        return "deprecated"
    if re.search(r"\b(warning|caution|danger|security|important|avoid)\b", text):
        return "warning"
    if "```" in chunk_text or "/examples/" in path or "/tutorial/" in path:
        return "example"
    if (
        "/reference/" in path
        or re.search(
            r"^#{1,4}\s+.*\b(api|reference|parameters?|returns?|class|function|method)\b",
            text,
            re.M,
        )
        or re.search(r"\b(arguments?|parameters?|returns?|raises|signature)\b", text)
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
    return list(set(_IDENTIFIER_RE.findall(text)))


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
    resolved = str(Path(file_path).resolve())
    return any(resolved == prefix or resolved.startswith(f"{prefix}/") for prefix in prefixes)


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


def _prepare_doc_link_batches(
    neo4j: Neo4jClient,
    lance: LanceDBClient,
    workspace_id: str,
    allowed_prefixes: list[str] | None = None,
):
    scan_docs = getattr(lance, "scan_docs_workspace", None)
    if not callable(scan_docs):
        return {
            "chunks": 0,
            "batches": [],
            "anchors": 0,
            "covers": 0,
            "related": 0,
            "pending_updates": 0,
            "prepare_sec": 0.0,
        }
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
        return {
            "chunks": 0,
            "batches": [],
            "anchors": 0,
            "covers": 0,
            "related": 0,
            "pending_updates": 0,
            "prepare_sec": 0.0,
        }
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
        anchor_rows = []
        cover_rows = []
        related_rows = []
        pending_updates = []
        semantic_states = []

        for row in batch:
            chunk_id = row["id"]
            chunk_text = row["chunk"]
            file_path = row["file_path"]
            identifier_matches: dict[str, str] = {}
            pending = []
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
            else:
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
                            "links": sorted(
                                links_by_uid.values(),
                                key=lambda item: item["uid"],
                            ),
                        }
                    )

                refs = _extract_doc_references(chunk_text)
                if refs:
                    related_rows.append(
                        {
                            "chunk_id": chunk_id,
                            "refs": [
                                {"ref": ref, "ref_type": _classify_doc_type(ref)} for ref in refs
                            ],
                        }
                    )

        if semantic_states:
            if symbol_vector_index is not None and all(
                state["row"].get("vector") is not None for state in semantic_states
            ):
                semantic_vectors = [state["row"].get("vector") for state in semantic_states]
                semantic_hits = _search_symbol_vectors_locally(
                    symbol_vector_index,
                    semantic_vectors,
                    limit=5,
                    threshold=SIMILARITY_THRESHOLD,
                )
            else:
                semantic_hits = []
                for state in semantic_states:
                    vector = state["row"].get("vector")
                    if vector is not None:
                        try:
                            hits = lance.search_symbols_by_vector(
                                vector,
                                limit=5,
                                threshold=SIMILARITY_THRESHOLD,
                                workspace_id=workspace_id,
                            )
                        except TypeError:
                            hits = lance.search_symbols_by_vector(
                                vector,
                                limit=5,
                                threshold=SIMILARITY_THRESHOLD,
                            )
                    else:
                        try:
                            hits = lance.search_symbols(
                                state["chunk_text"],
                                limit=5,
                                threshold=SIMILARITY_THRESHOLD,
                                workspace_id=workspace_id,
                            )
                        except TypeError:
                            hits = lance.search_symbols(
                                state["chunk_text"],
                                limit=5,
                                threshold=SIMILARITY_THRESHOLD,
                            )
                    semantic_hits.append(hits)

            for state, hits in zip(semantic_states, semantic_hits, strict=False):
                row = state["row"]
                chunk_id = row["id"]
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
                pending = [name for name in pending if name not in matched_names]

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
                            "links": sorted(
                                links_by_uid.values(),
                                key=lambda item: item["uid"],
                            ),
                        }
                    )

                refs = _extract_doc_references(chunk_text)
                if refs:
                    related_rows.append(
                        {
                            "chunk_id": chunk_id,
                            "refs": [
                                {"ref": ref, "ref_type": _classify_doc_type(ref)} for ref in refs
                            ],
                        }
                    )

        batches.append(
            {
                "count": len(batch),
                "anchor_rows": anchor_rows,
                "cover_rows": cover_rows,
                "related_rows": related_rows,
                "pending_updates": pending_updates,
            }
        )
        total_anchors += len(anchor_rows)
        total_covers += len(cover_rows)
        total_related += len(related_rows)
        total_pending_updates += len(pending_updates)
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
        return {
            "chunks": 0,
            "anchors": 0,
            "covers": 0,
            "related": 0,
            "pending_updates": 0,
            "prepare_sec": 0.0,
            "neo_write_sec": 0.0,
            "total_sec": 0.0,
        }

    progress = _make_progress(prepared["chunks"], "docs link", unit="chunk")
    # Per-row lance.set_pending_row is O(table) due to delete+add per call.
    # Prefer the bulk helper when the client supports it; fall back to the
    # per-row loop for test fakes that only implement set_pending_row.
    bulk_set_pending = getattr(lance, "set_pending_rows_batch", None)
    t0 = time.perf_counter()
    with neo4j.driver.session() as session:
        for batch in prepared["batches"]:
            session.execute_write(_write_anchors, batch["anchor_rows"], workspace_id)
            if batch["cover_rows"]:
                session.execute_write(_add_covers_edges_batch, batch["cover_rows"], workspace_id)
            if batch["related_rows"]:
                session.execute_write(_link_related_docs_batch, batch["related_rows"], workspace_id)
            if batch["pending_updates"]:
                if callable(bulk_set_pending):
                    bulk_set_pending(batch["pending_updates"])
                else:
                    for row, pending in batch["pending_updates"]:
                        lance.set_pending_row(row, pending)
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
        chunk_id = row["id"]
        chunk_text = row.get("chunk", "")
        file_path = row.get("file_path", "")
        names = _normalize_pending(row.get("pending"))
        links_by_uid: dict[str, dict] = {}
        still_pending = []
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
                resolved_total += 1
            else:
                still_pending.append(name)
        if links_by_uid:
            cover_rows.append(
                {
                    "chunk_id": chunk_id,
                    "links": sorted(links_by_uid.values(), key=lambda item: item["uid"]),
                    "resolver": "pending_identifier",
                }
            )
        if still_pending != names:
            pending_updates.append((row, still_pending))
        progress.update(1)
    progress.close()

    if cover_rows:
        with neo4j.driver.session() as session:
            session.execute_write(_add_covers_edges_batch, cover_rows, workspace_id)

    if pending_updates:
        bulk_set_pending = getattr(lance, "set_pending_rows_batch", None)
        if callable(bulk_set_pending):
            bulk_set_pending(pending_updates)
        else:
            for row, still_pending in pending_updates:
                lance.set_pending_row(row, still_pending)

    print(f"DocAnchor: resolved {resolved_total} pending links.")
    if resolved_total:
        _bump_graph_version(neo4j, workspace_id)
