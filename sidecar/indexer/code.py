import hashlib
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sidecar.database.lancedb_client import LanceDBClient
from sidecar.database.neo4j_client import Neo4jClient
from sidecar.parser.extractor import SymbolExtractor
from sidecar.parser.registry import REGISTRY
from sidecar.silence import install as _silence

_silence()

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_INDEXED_EXTENSIONS = {
    ext for adapter in REGISTRY.supported_adapters() for ext in adapter.file_extensions
}


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
            _, ext = os.path.splitext(name)
            if ext not in _INDEXED_EXTENSIONS or name.startswith("."):
                continue
            full = os.path.join(root, name)
            if spec:
                rel = os.path.relpath(full, ROOT)
                if spec.match_file(rel):
                    continue
            files.append(full)
    return files


def index_file(file_path: str, db: Neo4jClient, lance: LanceDBClient, extractor: SymbolExtractor):
    """Index a single file: symbols → calls → embeddings → imports → inheritance."""
    with open(file_path, "rb") as f:
        file_hash = hashlib.sha256(f.read()).hexdigest()
    symbols = extractor.extract(file_path)
    for sym in symbols:
        line_count = sym.end_line - sym.start_line + 1
        sym.token_estimate = max(1, line_count * 8)
    db.upsert_file_structure(file_path, file_hash, symbols)

    calls = extractor.extract_calls(file_path)
    if calls:
        db.link_calls(calls)

    with open(file_path, encoding="utf-8") as f:
        source = f.read()
    lines = source.splitlines()
    symbol_docs = [
        {
            "uid": s.uid,
            "name": s.name,
            "file_path": s.file_path,
            "code": "\n".join(lines[s.start_line - 1 : s.end_line]),
        }
        for s in extractor.extract_from_source(source, file_path)
    ]
    lance.upsert_symbol_embeddings(symbol_docs)

    imports = extractor.extract_imports(file_path)
    if imports:
        db.link_imports(imports)

    inheritance = extractor.extract_inheritance(file_path)
    if inheritance:
        db.link_inheritance(inheritance)


def run_indexing(project_path: str):
    db = Neo4jClient("bolt://localhost:7687", "neo4j", "password")
    lance = LanceDBClient()
    extractor = SymbolExtractor()

    print(f"🚀 Indexing project: {project_path}")

    files_to_index = _collect_files(project_path)
    if not files_to_index:
        print(f"❌ No files found at {project_path}")
        db.close()
        return

    # Compute current file hashes
    current_hashes = {}
    for file_path in files_to_index:
        with open(file_path, "rb") as f:
            current_hashes[file_path] = hashlib.sha256(f.read()).hexdigest()

    # Query stored hashes from Neo4j
    stored_hashes = db.get_file_hashes(files_to_index)

    # Filter to changed files only
    changed_files = [p for p in files_to_index if current_hashes[p] != stored_hashes.get(p)]

    if not changed_files:
        print("✅ All files up-to-date, nothing to re-index.")
        db.close()
        return

    print(f"🔄 {len(changed_files)}/{len(files_to_index)} files changed, re-indexing...")

    # Delete stale symbols for changed files
    for file_path in changed_files:
        db.delete_symbols_for_file(file_path)

    # Re-index only changed files
    for file_path in changed_files:
        print(f"📄 Indexing: {file_path}")
        index_file(file_path, db, lance, extractor)

    # Resolve pending DocAnchors (runs over entire DB)
    from sidecar.indexer.anchor import resolve_pending_anchors

    resolve_pending_anchors(db, lance)

    db.close()
    print("✅ Indexing complete.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Index a project into Neo4j")
    parser.add_argument("path", nargs="?", default=ROOT, help="Project path to index")
    args = parser.parse_args()
    run_indexing(args.path)
