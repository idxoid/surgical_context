import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from sidecar.silence import install as _silence; _silence()
from sidecar.database.lancedb_client import LanceDBClient
from sidecar.database.neo4j_client import Neo4jClient
from sidecar.parser.extractor import SymbolExtractor
from sidecar.parser.registry import REGISTRY

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_INDEXED_EXTENSIONS = {ext for adapter in REGISTRY.supported_adapters() for ext in adapter.file_extensions}


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
            dirs[:] = [
                d for d in dirs
                if not spec.match_file(os.path.join(rel_root, d))
            ]
        for name in filenames:
            _, ext = os.path.splitext(name)
            if ext not in _INDEXED_EXTENSIONS or name.startswith('.'):
                continue
            full = os.path.join(root, name)
            if spec:
                rel = os.path.relpath(full, ROOT)
                if spec.match_file(rel):
                    continue
            files.append(full)
    return files


def run_indexing(project_path: str):
    db = Neo4jClient("bolt://localhost:7687", "neo4j", "password")
    lance = LanceDBClient()
    extractor = SymbolExtractor()

    print(f"🚀 Indexing project: {project_path}")

    files_to_index = _collect_files(project_path)
    if not files_to_index:
        print(f"❌ No files found at {project_path}")
        return

    # Phase 1: symbol nodes
    for file_path in files_to_index:
        print(f"📄 Symbols: {file_path}")
        symbols = extractor.extract(file_path)
        # Compute token_estimate for each symbol (empirical: ~8 tokens/line)
        for sym in symbols:
            line_count = sym.end_line - sym.start_line + 1
            sym.token_estimate = max(1, line_count * 8)
        with open(file_path, 'rb') as f:
            file_hash = f.read().hex()
        db.upsert_file_structure(file_path, file_hash, symbols)

    # Phase 2: CALLS edges
    for file_path in files_to_index:
        print(f"🔗 Calls: {file_path}")
        calls = extractor.extract_calls(file_path)
        if calls:
            db.link_calls(calls)

    # Phase 3: symbol body embeddings
    print("🧠 Embedding symbol bodies...")
    all_symbols = []
    for file_path in files_to_index:
        with open(file_path, encoding='utf-8') as f:
            source = f.read()
        lines = source.splitlines()
        for s in extractor.extract_from_source(source, file_path):
            all_symbols.append({
                "uid": s.uid,
                "name": s.name,
                "file_path": s.file_path,
                "code": "\n".join(lines[s.start_line - 1:s.end_line]),
            })
    lance.upsert_symbol_embeddings(all_symbols)

    # Phase 4: resolve pending DocAnchors
    from sidecar.indexer.anchor import resolve_pending_anchors
    resolve_pending_anchors(db, lance)

    # Phase 5: IMPORTS edges (File → File)
    print("📂 Imports: extracting cross-module dependencies...")
    for file_path in files_to_index:
        imports = extractor.extract_imports(file_path)
        if imports:
            db.link_imports(imports)

    # Phase 6: DEPENDS_ON edges (Symbol → Symbol)
    print("🔗 Dependencies: extracting type/interface usage...")
    for file_path in files_to_index:
        inheritance = extractor.extract_inheritance(file_path)
        if inheritance:
            db.link_inheritance(inheritance)

    db.close()
    print("✅ Indexing complete.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Index a project into Neo4j")
    parser.add_argument("path", nargs="?", default=ROOT, help="Project path to index")
    args = parser.parse_args()
    run_indexing(args.path)
