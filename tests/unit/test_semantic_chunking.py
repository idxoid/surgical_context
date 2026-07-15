from __future__ import annotations

import os
import subprocess
import sys

from context_engine.search.semantic_chunks import build_semantic_chunks


def test_semantic_chunk_index_is_opt_in_by_default():
    env = os.environ.copy()
    env.pop("AXIS_SEMANTIC_CHUNK_INDEX", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from context_engine.database.lancedb_client import "
                "AXIS_SEMANTIC_CHUNK_INDEX; "
                "print(int(AXIS_SEMANTIC_CHUNK_INDEX))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.stdout.strip() == "0"


def test_semantic_chunks_are_overlapping_and_source_attributed():
    code = "\n".join(
        [
            "def solve(value):",
            "    prepare(value)",
            "    if value:",
            "        first(value)",
            "        second(value)",
            "    normalize(value)",
            "    persist(value)",
            "    publish(value)",
            "    audit(value)",
            "    return value",
        ]
    )
    chunks = build_semantic_chunks(
        {
            "uid": "solve",
            "name": "solve",
            "qualified_name": "service.solve",
            "code": code,
            "start_line": 40,
        },
        target_lines=6,
        overlap_lines=2,
        min_symbol_lines=1,
    )
    assert len(chunks) >= 2
    assert chunks[0].start_line == 40
    assert chunks[0].end_line >= chunks[1].start_line
    assert chunks[-1].end_line == 49
    assert "service.solve" in chunks[0].embedding_text


def test_semantic_chunks_require_honest_symbol_start_line():
    assert (
        build_semantic_chunks(
            {"uid": "x", "name": "x", "code": "def x():\n    return 1"},
            min_symbol_lines=1,
        )
        == []
    )
