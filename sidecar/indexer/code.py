import hashlib
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sidecar.database.lancedb_client import LanceDBClient
from sidecar.database.neo4j_client import Neo4jClient
from sidecar.parser.extractor import SymbolExtractor
from sidecar.parser.registry import REGISTRY
from sidecar.silence import install as _silence
from sidecar.workspace import DEFAULT_WORKSPACE_ID

_silence()

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_INDEXED_EXTENSIONS = {
    ext for adapter in REGISTRY.supported_adapters() for ext in adapter.file_extensions
}


def is_indexable_file(file_path: str) -> bool:
    _, ext = os.path.splitext(file_path)
    return ext.lower() in _INDEXED_EXTENSIONS


def _load_gitignore(root: str):
    """Return a pathspec matcher for the nearest .gitignore, or None."""
    import pathspec

    gitignore = os.path.join(root, ".gitignore")
    if not os.path.exists(gitignore):
        gitignore = os.path.join(ROOT, ".gitignore")
    if not os.path.exists(gitignore):
        return None
    with open(gitignore) as f:
        return pathspec.PathSpec.from_lines("gitwildmatch", f)


def _collect_files(project_path: str) -> list[str]:
    spec = _load_gitignore(project_path)
    files = []
    for root, dirs, filenames in os.walk(project_path):
        if spec:
            rel_root = os.path.relpath(root, ROOT)
            dirs[:] = [d for d in dirs if not spec.match_file(os.path.join(rel_root, d))]
        for name in filenames:
            if name.startswith(".") or not is_indexable_file(name):
                continue
            full = os.path.join(root, name)
            if spec:
                rel = os.path.relpath(full, ROOT)
                if spec.match_file(rel):
                    continue
            files.append(full)
    return files


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


def index_file(
    file_path: str,
    db: Neo4jClient,
    lance: LanceDBClient,
    extractor: SymbolExtractor,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    *,
    skip_affects: bool = False,
) -> list[str]:
    """Index a single file: symbols → calls → embeddings → imports → inheritance → AFFECTS rebuild.

    Returns the list of changed symbol UIDs so batch callers can collect them
    and run a single AFFECTS rebuild after all files are processed.
    Pass ``skip_affects=True`` when the caller will do that itself.
    """
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

    # Snapshot degree-edge neighbors of symbols whose edges are about to change or
    # be deleted, BEFORE mutation: a removed symbol's neighbor still needs its
    # degree corrected, but becomes unreachable once the symbol is detached.
    degree_neighbors = getattr(db, "degree_neighbor_uids", None)
    pre_neighbor_uids: list[str] = []
    if callable(degree_neighbors):
        seed_for_neighbors = sorted(set(edge_refresh_uids) | set(removed_uids))
        pre_neighbor_uids = degree_neighbors(seed_for_neighbors, workspace_id=workspace_id)

    # Always update File.hash, but preserve unchanged Symbol nodes and embeddings.
    db.upsert_file_structure(file_path, file_hash, changed_symbols, workspace_id=workspace_id)

    prune_symbols = getattr(db, "prune_symbols_for_file", None)
    if callable(prune_symbols):
        prune_symbols(file_path, keep_uids=current_uids, workspace_id=workspace_id)

    clear_edges = getattr(db, "clear_outgoing_symbol_edges", None)
    if callable(clear_edges):
        clear_edges(edge_refresh_uids, workspace_id=workspace_id)

    calls = extractor.extract_calls(file_path)
    if calls and edge_refresh_uids:
        db.link_calls(calls, workspace_id=workspace_id)

    with open(file_path, encoding="utf-8") as f:
        source = f.read()
    lines = source.splitlines()
    changed_uid_set = set(changed_uids)
    symbol_docs = [
        {
            "uid": s.uid,
            "name": s.name,
            "file_path": s.file_path,
            "workspace_id": workspace_id,
            "code": "\n".join(lines[s.start_line - 1 : s.end_line]),
        }
        for s in symbols
        if s.uid in changed_uid_set
    ]
    try:
        lance.upsert_symbol_embeddings(symbol_docs, workspace_id=workspace_id)
    except TypeError:
        lance.upsert_symbol_embeddings(symbol_docs)
    delete_symbol_embeddings = getattr(lance, "delete_symbol_embeddings", None)
    if callable(delete_symbol_embeddings):
        try:
            delete_symbol_embeddings(removed_uids, workspace_id=workspace_id)
        except TypeError:
            delete_symbol_embeddings(removed_uids)

    imports = extractor.extract_imports(file_path)
    delete_imports = getattr(db, "delete_imports_for_file", None)
    if callable(delete_imports):
        delete_imports(file_path, workspace_id=workspace_id)
    if imports:
        db.link_imports(imports, workspace_id=workspace_id)

    inheritance = extractor.extract_inheritance(file_path)
    if inheritance and edge_refresh_uids:
        db.link_inheritance(inheritance, workspace_id=workspace_id)

    # Lazy-proxy bindings: drop stale ProxyBinding nodes for this file, recreate,
    # then forward this file's proxy-var calls through PROXY_OF to the real type.
    delete_proxies = getattr(db, "delete_proxy_bindings_for_file", None)
    link_proxies = getattr(db, "link_proxy_bindings", None)
    resolve_proxies = getattr(db, "resolve_proxy_calls", None)
    proxy_uids: list[str] = []
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

    # Refresh materialized degree over the affected closure now that edges are
    # relinked. Removed symbols are excluded (they no longer exist); their former
    # neighbors come from the pre-mutation snapshot.
    recompute_degree = getattr(db, "recompute_degree_for_closure", None)
    if callable(recompute_degree):
        removed_set = set(removed_uids)
        degree_seeds = sorted(
            (set(edge_refresh_uids) | set(pre_neighbor_uids) | set(proxy_uids)) - removed_set
        )
        if degree_seeds:
            recompute_degree(degree_seeds, workspace_id=workspace_id)

    if changed_uids and not skip_affects:
        from sidecar.indexer.affects import AFFECTSIndexer

        AFFECTSIndexer(db).rebuild_affects(changed_uids, workspace_id=workspace_id)

    return changed_uids


def run_indexing(project_path: str, workspace_id: str | None = None):
    """Whole-project index pass.

    Delegates to ``sidecar.indexer.fast.run_fast_indexing``: parallel hash +
    parse, global embedding batch, single AFFECTS rebuild. The return value
    (stats dict) is ignored by all current callers, which matches the old
    ``None``-returning contract.

    The single-file hot path (``index_file`` below, used by ``/overlay``
    and ``/index/file``) is intentionally left on the per-file flow so the
    IDE save path keeps synchronous AFFECTS semantics.
    """
    from sidecar.indexer.fast import run_fast_indexing

    return run_fast_indexing(project_path, workspace_id=workspace_id)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Index a project into Neo4j")
    parser.add_argument("path", nargs="?", default=ROOT, help="Project path to index")
    args = parser.parse_args()
    run_indexing(args.path)
