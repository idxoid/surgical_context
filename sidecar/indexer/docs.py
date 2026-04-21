import glob
import os
import re

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


def index_docs(docs_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID):
    lance = LanceDBClient()
    neo4j = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)

    md_files = glob.glob(os.path.join(docs_path, "**/*.md"), recursive=True)
    if not md_files:
        print(f"No markdown files found in {docs_path}")
        neo4j.close()
        return

    for path in md_files:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        chunks = _chunk_text(text)
        lance.upsert_chunks(path, chunks)
        print(f"Indexed {len(chunks)} chunks from {path}")

    link_docs_to_symbols(neo4j, lance, workspace_id=workspace_id)
    neo4j.close()
    print("Doc indexing complete.")


if __name__ == "__main__":
    import sys

    index_docs(sys.argv[1] if len(sys.argv) > 1 else "./docs")
