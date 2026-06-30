import glob
import os
import re
import time
from typing import Any

from context_engine.database.lancedb_client import LanceDBClient
from context_engine.database.provider import get_database_provider
from context_engine.index_profile import (
    IndexProfile,
    active_index_profile,
    effective_index_workspace_id,
    resolve_index_profile,
)
from context_engine.indexer.anchor import link_docs_to_symbols
from context_engine.indexer.progress import make_progress as _make_progress
from context_engine.workspace import DEFAULT_WORKSPACE_ID
from context_engine.workspace_paths import WorkspaceRootNotAllowedError, resolve_cli_directory

CHUNK_SIZE = 400
CHUNK_OVERLAP = 80

_HEADING_RE = re.compile(r"^#{1,3} .+", re.MULTILINE)


def _split_by_sections(text: str) -> list[str]:
    boundaries = [m.start() for m in _HEADING_RE.finditer(text)]
    if not boundaries:
        return [text]
    sections = []
    for i, start in enumerate(boundaries):
        end = boundaries[i + 1] if i + 1 < len(boundaries) else len(text)
        sections.append(text[start:end].strip())
    return [s for s in sections if s]


def _word_split_chunk(text: str) -> list[str]:
    words = text.split()
    if len(words) <= CHUNK_SIZE:
        return [text]
    chunks, i = [], 0
    while i < len(words):
        chunks.append(" ".join(words[i : i + CHUNK_SIZE]))
        i += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def _chunk_text(text: str) -> list[str]:
    chunks = []
    for section in _split_by_sections(text):
        chunks.extend(_word_split_chunk(section))
    return chunks


def index_docs(
    docs_path: str,
    workspace_id: str | None = None,
    *,
    index_profile: str | IndexProfile | None = None,
    user_id: str = "anonymous",
) -> dict[str, Any]:
    # Resolve the profile once and derive BOTH the physical workspace namespace
    # and the LanceDBClient's tables from it. Threading the suffix via the
    # workspace string while letting the client read the profile from env is the
    # split that lets the two drift (docs written to one table, read from
    # another). ``workspace_id`` here is the client-facing (base) id.
    if isinstance(index_profile, IndexProfile):
        profile = index_profile
    elif index_profile:
        profile = resolve_index_profile(index_profile)
    else:
        profile = active_index_profile()
    index_workspace_id = effective_index_workspace_id(
        workspace_id or DEFAULT_WORKSPACE_ID, profile=profile
    )
    resolved_docs_path = str(resolve_cli_directory(docs_path))
    lance = LanceDBClient(index_profile=profile)
    # Mint a request-scoped view over the process-wide Neo4j driver (audit-
    # tagged by user_id) instead of opening a second raw driver — same shared
    # driver the request path and run_fast_indexing use. close() is a no-op on
    # this view, so the shared driver outlives the call.
    neo4j = get_database_provider().client_for(user_id)

    md_files = sorted(glob.glob(os.path.join(resolved_docs_path, "**/*.md"), recursive=True))
    if not md_files:
        print(f"No markdown files found in {resolved_docs_path}")
        neo4j.close()
        return {
            "docs_path": resolved_docs_path,
            "files_indexed": 0,
            "chunks_indexed": 0,
            "link_stats": {},
            "timings_sec": {
                "chunking": 0.0,
                "upsert": 0.0,
                "link_prepare": 0.0,
                "link_neo_write": 0.0,
                "link": 0.0,
                "total": 0.0,
            },
        }

    progress = _make_progress(len(md_files), "docs files", unit="file")
    total_chunks = 0
    chunking_seconds = 0.0
    upsert_seconds = 0.0
    file_chunks: list[tuple[str, list[str]]] = []
    t_total = time.perf_counter()
    for path in md_files:
        t_stage = time.perf_counter()
        with open(path, encoding="utf-8") as f:
            text = f.read()
        chunks = _chunk_text(text)
        chunking_seconds += time.perf_counter() - t_stage
        file_chunks.append((path, chunks))
        total_chunks += len(chunks)
        progress.update(1)
    progress.close()

    upsert_progress = _make_progress(1, "docs upsert", unit="step")
    t_stage = time.perf_counter()
    if hasattr(lance, "upsert_chunk_batches"):
        lance.upsert_chunk_batches(
            file_chunks,
            workspace_id=index_workspace_id,
            progress_callback=lambda msg: print(f"[docs upsert] {msg}"),
        )
    else:
        for path, chunks in file_chunks:
            lance.upsert_chunks(path, chunks, workspace_id=index_workspace_id)
    upsert_seconds += time.perf_counter() - t_stage
    upsert_progress.update(1)
    upsert_progress.close()

    link_progress = _make_progress(1, "docs finalize", unit="step")
    t_stage = time.perf_counter()
    link_stats = link_docs_to_symbols(
        neo4j,
        lance,
        workspace_id=index_workspace_id,
        allowed_prefixes=[resolved_docs_path],
    )
    link_seconds = time.perf_counter() - t_stage
    link_progress.update(1)
    link_progress.close()
    neo4j.close()
    total_seconds = time.perf_counter() - t_total
    timings = {
        "chunking": round(chunking_seconds, 3),
        "upsert": round(upsert_seconds, 3),
        "link_prepare": round((link_stats or {}).get("prepare_sec", 0.0), 3),
        "link_neo_write": round((link_stats or {}).get("neo_write_sec", 0.0), 3),
        "link": round(link_seconds, 3),
        "total": round(total_seconds, 3),
    }
    print(f"Doc indexing complete. files={len(md_files)} chunks={total_chunks} timings={timings}")
    return {
        "docs_path": resolved_docs_path,
        "files_indexed": len(md_files),
        "chunks_indexed": total_chunks,
        "link_stats": link_stats or {},
        "timings_sec": timings,
    }


if __name__ == "__main__":
    import sys

    from context_engine.database.provider import close_database_provider

    raw_docs_path = sys.argv[1] if len(sys.argv) > 1 else "./docs"
    try:
        index_docs(raw_docs_path)
    except (FileNotFoundError, WorkspaceRootNotAllowedError) as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        close_database_provider()
