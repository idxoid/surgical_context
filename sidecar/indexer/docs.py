import glob
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from sidecar.database.lancedb_client import LanceDBClient
from sidecar.database.neo4j_client import Neo4jClient
from sidecar.indexer.anchor import link_docs_to_symbols
from sidecar.workspace import DEFAULT_WORKSPACE_ID

CHUNK_SIZE = 400
CHUNK_OVERLAP = 80

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

_HEADING_RE = re.compile(r"^#{1,3} .+", re.MULTILINE)


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


def index_docs(docs_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID) -> dict[str, Any]:
    lance = LanceDBClient()
    neo4j = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)

    md_files = sorted(glob.glob(os.path.join(docs_path, "**/*.md"), recursive=True))
    if not md_files:
        print(f"No markdown files found in {docs_path}")
        neo4j.close()
        return {
            "docs_path": docs_path,
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
            workspace_id=workspace_id,
            progress_callback=lambda msg: print(f"[docs upsert] {msg}"),
        )
    else:
        for path, chunks in file_chunks:
            lance.upsert_chunks(path, chunks, workspace_id=workspace_id)
    upsert_seconds += time.perf_counter() - t_stage
    upsert_progress.update(1)
    upsert_progress.close()

    link_progress = _make_progress(1, "docs finalize", unit="step")
    t_stage = time.perf_counter()
    link_stats = link_docs_to_symbols(
        neo4j,
        lance,
        workspace_id=workspace_id,
        allowed_prefixes=[docs_path],
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
        "docs_path": docs_path,
        "files_indexed": len(md_files),
        "chunks_indexed": total_chunks,
        "link_stats": link_stats or {},
        "timings_sec": timings,
    }


if __name__ == "__main__":
    import sys

    index_docs(sys.argv[1] if len(sys.argv) > 1 else "./docs")
