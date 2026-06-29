import hashlib
import os
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import logging

from context_engine.database.lancedb_client import LanceDBClient
from context_engine.database.neo4j_client import Neo4jClient
from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE
from context_engine.parser.extractor import SymbolExtractor
from context_engine.parser.registry import REGISTRY
from context_engine.silence import install as _silence
from context_engine.workspace import DEFAULT_WORKSPACE_ID

_silence()

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_INDEXED_EXTENSIONS = {
    ext for adapter in REGISTRY.supported_adapters() for ext in adapter.file_extensions
}


def is_indexable_file(file_path: str) -> bool:
    _, ext = os.path.splitext(file_path)
    return ext.lower() in _INDEXED_EXTENSIONS


def hash_file(file_path: str) -> str:
    with open(file_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _symbol_needs_upsert(sym, existing: dict | None) -> bool:
    if existing is None:
        return True
    return bool(
        existing.get("hash") != sym.content_hash
        or int(existing.get("start_line") or 0) != sym.start_line
        or int(existing.get("end_line") or 0) != sym.end_line
    )


def _index_file_symbol_delta(
    file_path: str,
    extractor: SymbolExtractor,
    db: Neo4jClient,
    workspace_id: str,
) -> tuple[str, list, list, list[str], list[str], list[str], dict, str]:
    file_hash = hash_file(file_path)
    symbols = extractor.extract(file_path)
    for sym in symbols:
        line_count = sym.end_line - sym.start_line + 1
        sym.token_estimate = max(1, line_count * 8)

    get_symbol_index = getattr(db, "get_symbol_index_for_file", None)
    existing_symbols = (
        get_symbol_index(file_path, workspace_id=workspace_id) if callable(get_symbol_index) else {}
    )
    current_uids = [s.uid for s in symbols]
    changed_symbols = [s for s in symbols if _symbol_needs_upsert(s, existing_symbols.get(s.uid))]
    changed_uids = [s.uid for s in changed_symbols]
    edge_refresh_uids = changed_uids or current_uids
    removed_uids = sorted(set(existing_symbols) - set(current_uids))
    with open(file_path, encoding="utf-8") as f:
        source = f.read()
    return (
        file_hash,
        symbols,
        changed_symbols,
        changed_uids,
        edge_refresh_uids,
        removed_uids,
        existing_symbols,
        source,
    )


def _maybe_delete_for_file(
    db: Neo4jClient, method_name: str, file_path: str, workspace_id: str
) -> None:
    delete_fn = getattr(db, method_name, None)
    if callable(delete_fn):
        delete_fn(file_path, workspace_id=workspace_id)


def _relink_extracted_file_edges(
    file_path: str,
    *,
    db: Neo4jClient,
    workspace_id: str,
    delete_method: str,
    link_method: str,
    extract_items,
) -> None:
    _maybe_delete_for_file(db, delete_method, file_path, workspace_id)
    link_fn = getattr(db, link_method, None)
    if not callable(link_fn):
        return
    items = extract_items()
    if items:
        link_fn(items, workspace_id=workspace_id)


def _refresh_file_structure_and_calls(
    file_path: str,
    *,
    db: Neo4jClient,
    workspace_id: str,
    file_hash: str,
    changed_symbols: list,
    current_uids: list[str],
    edge_refresh_uids: list[str],
    calls: list,
) -> None:
    db.upsert_file_structure(file_path, file_hash, changed_symbols, workspace_id=workspace_id)
    prune_symbols = getattr(db, "prune_symbols_for_file", None)
    if callable(prune_symbols):
        prune_symbols(file_path, keep_uids=current_uids, workspace_id=workspace_id)
    clear_edges = getattr(db, "clear_outgoing_symbol_edges", None)
    if callable(clear_edges):
        clear_edges(edge_refresh_uids, workspace_id=workspace_id)
    if calls and edge_refresh_uids:
        db.link_calls(calls, workspace_id=workspace_id)


def _refresh_http_endpoints(
    file_path: str,
    *,
    db: Neo4jClient,
    extractor: SymbolExtractor,
    workspace_id: str,
) -> None:
    delete_http_endpoints = getattr(db, "delete_http_endpoints_for_file", None)
    link_http_endpoints = getattr(db, "link_http_endpoints", None)
    extract_http_endpoints = getattr(extractor, "extract_http_endpoints", None)
    if callable(delete_http_endpoints):
        delete_http_endpoints(file_path, workspace_id=workspace_id)
    if not callable(link_http_endpoints) or not callable(extract_http_endpoints):
        return
    http_endpoints = extract_http_endpoints(file_path)
    if http_endpoints:
        link_http_endpoints(http_endpoints, workspace_id=workspace_id)


def _refresh_imports_and_external_boundary(
    file_path: str,
    *,
    db: Neo4jClient,
    extractor: SymbolExtractor,
    workspace_id: str,
    imports: list,
    calls: list,
    source: str,
) -> None:
    _maybe_delete_for_file(db, "delete_imports_for_file", file_path, workspace_id)
    if imports:
        db.link_imports(imports, workspace_id=workspace_id)

    from context_engine.indexer.external_boundary import build_project_boundary
    from context_engine.indexer.external_facts import apply_external_boundary_for_file

    project_root = getattr(extractor, "project_root", None) or str(Path(file_path).resolve().parent)
    boundary = build_project_boundary(project_root, file_paths=(file_path,))
    apply_external_boundary_for_file(
        db,
        file_path=file_path,
        source_code=source,
        calls=calls,
        boundary=boundary,
        workspace_id=workspace_id,
    )


def _refresh_proxy_bindings(
    file_path: str,
    *,
    db: Neo4jClient,
    extractor: SymbolExtractor,
    workspace_id: str,
    calls: list,
) -> list[str]:
    proxy_uids: list[str] = []
    delete_proxies = getattr(db, "delete_proxy_bindings_for_file", None)
    link_proxies = getattr(db, "link_proxy_bindings", None)
    resolve_proxies = getattr(db, "resolve_proxy_calls", None)
    if callable(delete_proxies):
        delete_proxies(file_path, workspace_id=workspace_id)
    if callable(link_proxies):
        proxy_bindings = extractor.extract_proxy_bindings(file_path)
        if proxy_bindings:
            link_proxies(proxy_bindings, workspace_id=workspace_id)
            proxy_uids = [str(b["proxy_uid"]) for b in proxy_bindings if b.get("proxy_uid")]
    if callable(resolve_proxies):
        proxy_calls = [
            {
                "caller_uid": c.get("caller_uid"),
                "callee_qualified_name": c.get("callee_qualified_name"),
                "call_site_line": c.get("call_site_line"),
            }
            for c in calls
            if c.get("callee_qualified_name")
        ]
        if proxy_calls:
            resolve_proxies(proxy_calls, workspace_id=workspace_id)
    return proxy_uids


def _index_file_refresh_graph_edges(
    file_path: str,
    *,
    db: Neo4jClient,
    extractor: SymbolExtractor,
    workspace_id: str,
    file_hash: str,
    changed_symbols: list,
    current_uids: list[str],
    edge_refresh_uids: list[str],
    calls: list,
    imports: list,
    inheritance: list,
    source: str,
) -> list[str]:
    _refresh_file_structure_and_calls(
        file_path,
        db=db,
        workspace_id=workspace_id,
        file_hash=file_hash,
        changed_symbols=changed_symbols,
        current_uids=current_uids,
        edge_refresh_uids=edge_refresh_uids,
        calls=calls,
    )
    _refresh_http_endpoints(file_path, db=db, extractor=extractor, workspace_id=workspace_id)
    _refresh_imports_and_external_boundary(
        file_path,
        db=db,
        extractor=extractor,
        workspace_id=workspace_id,
        imports=imports,
        calls=calls,
        source=source,
    )
    if inheritance and edge_refresh_uids:
        db.link_inheritance(inheritance, workspace_id=workspace_id)
    proxy_uids = _refresh_proxy_bindings(
        file_path,
        db=db,
        extractor=extractor,
        workspace_id=workspace_id,
        calls=calls,
    )
    _relink_extracted_file_edges(
        file_path,
        db=db,
        workspace_id=workspace_id,
        delete_method="delete_decorators_for_file",
        link_method="link_decorators",
        extract_items=lambda: extractor.extract_decorators(file_path),
    )
    _relink_extracted_file_edges(
        file_path,
        db=db,
        workspace_id=workspace_id,
        delete_method="delete_type_references_for_file",
        link_method="link_type_references",
        extract_items=lambda: extractor.extract_type_references(file_path),
    )
    _relink_extracted_file_edges(
        file_path,
        db=db,
        workspace_id=workspace_id,
        delete_method="delete_injections_for_file",
        link_method="link_injections",
        extract_items=lambda: extractor.extract_injections(file_path),
    )
    return proxy_uids


def _delete_symbol_embeddings_safe(
    lance: LanceDBClient,
    uids: list[str],
    workspace_id: str,
) -> None:
    delete_symbol_embeddings = getattr(lance, "delete_symbol_embeddings", None)
    if not callable(delete_symbol_embeddings) or not uids:
        return
    try:
        delete_symbol_embeddings(uids, workspace_id=workspace_id)
    except TypeError:
        delete_symbol_embeddings(uids)


def _axis_facts_for_file(
    file_path: str,
    source: str,
    symbols: list,
    project_root: str,
    include_axis_facts: bool,
) -> list | None:
    if not include_axis_facts:
        return None
    try:
        adapter = REGISTRY.get_adapter(REGISTRY.detect_language(file_path))
        return adapter.extract_axis_facts(
            source,
            file_path,
            symbols=symbols,
            project_root=project_root or None,
        )
    except ValueError:
        return []


def _index_file_refresh_embeddings(
    file_path: str,
    *,
    lance: LanceDBClient,
    db: Neo4jClient,
    extractor: SymbolExtractor,
    workspace_id: str,
    symbols: list,
    changed_uids: list[str],
    removed_uids: list[str],
    calls: list,
    imports: list,
    inheritance: list,
    source: str,
    file_hash: str,
) -> None:
    include_axis_facts = getattr(lance, "index_profile_name", "") == AXIS_PYTHON_V1_PROFILE
    changed_uid_set = set(changed_uids)
    if not changed_uid_set:
        _delete_symbol_embeddings_safe(lance, removed_uids, workspace_id)
        return

    from context_engine.indexer.fast.extractor import ExtractedFile
    from context_engine.indexer.fast.pipeline import build_symbol_docs_for_extracted

    project_root = getattr(extractor, "project_root", None) or str(Path(file_path).resolve().parent)
    axis_facts = _axis_facts_for_file(
        file_path,
        source,
        symbols,
        project_root,
        include_axis_facts,
    )
    extracted = ExtractedFile(
        file_path,
        source,
        file_hash,
        symbols,
        calls,
        imports,
        inheritance,
        axis_facts=axis_facts,
    )
    graph_probe = None
    if include_axis_facts:
        from context_engine.axis.graph_probe import Neo4jGraphContextProbe

        graph_probe = Neo4jGraphContextProbe(db, workspace_id)
    symbol_docs = build_symbol_docs_for_extracted(
        extracted,
        changed_uids=changed_uid_set,
        workspace_id=workspace_id,
        project_path=project_root,
        graph_probe=graph_probe,
        include_axis_facts=include_axis_facts,
    )
    try:
        lance.upsert_symbol_embeddings(symbol_docs, workspace_id=workspace_id)
    except TypeError:
        lance.upsert_symbol_embeddings(symbol_docs)

    _delete_symbol_embeddings_safe(lance, removed_uids, workspace_id)


def _index_file_finalize(
    *,
    db: Neo4jClient,
    lance: LanceDBClient,
    workspace_id: str,
    extractor: SymbolExtractor,
    file_path: str,
    changed_uids: list[str],
    edge_refresh_uids: list[str],
    removed_uids: list[str],
    pre_neighbor_uids: list[str],
    proxy_uids: list[str],
    skip_affects: bool,
    collected_adjacency_seeds: set[str] | None,
) -> list[str]:
    recompute_degree = getattr(db, "recompute_degree_for_closure", None)
    degree_seeds: list[str] = []
    if callable(recompute_degree):
        removed_set = set(removed_uids)
        degree_seeds = sorted(
            (set(edge_refresh_uids) | set(pre_neighbor_uids) | set(proxy_uids)) - removed_set
        )
        if degree_seeds:
            recompute_degree(degree_seeds, workspace_id=workspace_id)
    if collected_adjacency_seeds is not None and degree_seeds:
        collected_adjacency_seeds.update(degree_seeds)

    if changed_uids and not skip_affects:
        from context_engine.indexer.affects import AFFECTSIndexer

        AFFECTSIndexer(db).rebuild_affects(changed_uids, workspace_id=workspace_id)

    include_axis_facts = getattr(lance, "index_profile_name", "") == AXIS_PYTHON_V1_PROFILE
    if not skip_affects and include_axis_facts:
        from context_engine.indexer.fast.pipeline import run_axis_incremental_finalize

        project_root = getattr(extractor, "project_root", None) or str(
            Path(file_path).resolve().parent
        )
        run_axis_incremental_finalize(
            db,
            lance,
            workspace_id,
            seed_uids=set(degree_seeds) | set(changed_uids),
            project_path=project_root,
        )
    return degree_seeds


def index_file(
    file_path: str,
    db: Neo4jClient,
    lance: LanceDBClient,
    extractor: SymbolExtractor,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    *,
    skip_affects: bool = False,
    collected_adjacency_seeds: set[str] | None = None,
) -> list[str]:
    """Index a single file: symbols → calls → embeddings → imports → inheritance → AFFECTS rebuild.

    Returns the list of changed symbol UIDs so batch callers can collect them
    and run a single AFFECTS rebuild after all files are processed.
    Pass ``skip_affects=True`` when the caller will do that itself; batch
    callers should then invoke ``run_axis_incremental_finalize`` once with the
    union of ``collected_adjacency_seeds`` from each file.
    """
    from context_engine.indexer.git_committed import should_index_file

    if not should_index_file(file_path):
        logger.info("Skipping index for uncommitted or untracked file: %s", file_path)
        return []

    (
        file_hash,
        symbols,
        changed_symbols,
        changed_uids,
        edge_refresh_uids,
        removed_uids,
        _existing,
        source,
    ) = _index_file_symbol_delta(file_path, extractor, db, workspace_id)
    current_uids = [s.uid for s in symbols]
    calls = extractor.extract_calls(file_path)
    imports = extractor.extract_imports(file_path)
    inheritance = extractor.extract_inheritance(file_path)

    degree_neighbors = getattr(db, "degree_neighbor_uids", None)
    pre_neighbor_uids: list[str] = []
    if callable(degree_neighbors):
        seed_for_neighbors = sorted(set(edge_refresh_uids) | set(removed_uids))
        pre_neighbor_uids = degree_neighbors(seed_for_neighbors, workspace_id=workspace_id)

    proxy_uids = _index_file_refresh_graph_edges(
        file_path,
        db=db,
        extractor=extractor,
        workspace_id=workspace_id,
        file_hash=file_hash,
        changed_symbols=changed_symbols,
        current_uids=current_uids,
        edge_refresh_uids=edge_refresh_uids,
        calls=calls,
        imports=imports,
        inheritance=inheritance,
        source=source,
    )
    _index_file_refresh_embeddings(
        file_path,
        lance=lance,
        db=db,
        extractor=extractor,
        workspace_id=workspace_id,
        symbols=symbols,
        changed_uids=changed_uids,
        removed_uids=removed_uids,
        calls=calls,
        imports=imports,
        inheritance=inheritance,
        source=source,
        file_hash=file_hash,
    )
    _index_file_finalize(
        db=db,
        lance=lance,
        workspace_id=workspace_id,
        extractor=extractor,
        file_path=file_path,
        changed_uids=changed_uids,
        edge_refresh_uids=edge_refresh_uids,
        removed_uids=removed_uids,
        pre_neighbor_uids=pre_neighbor_uids,
        proxy_uids=proxy_uids,
        skip_affects=skip_affects,
        collected_adjacency_seeds=collected_adjacency_seeds,
    )
    return changed_uids


def run_indexing(project_path: str, workspace_id: str | None = None):
    """Whole-project index pass.

    Delegates to ``context_engine.indexer.fast.run_fast_indexing``: parallel hash +
    parse, global embedding batch, single AFFECTS rebuild. The return value
    (stats dict) is ignored by all current callers, which matches the old
    ``None``-returning contract.

    The single-file hot path (``index_file`` below, used by ``/overlay``
    and ``/index/file``) is intentionally left on the per-file flow so the
    IDE save path keeps synchronous AFFECTS semantics.
    """
    from context_engine.indexer.fast import run_fast_indexing

    return run_fast_indexing(project_path, workspace_id=workspace_id)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Index a project into Neo4j")
    parser.add_argument("path", nargs="?", default=ROOT, help="Project path to index")
    args = parser.parse_args()
    run_indexing(args.path)
