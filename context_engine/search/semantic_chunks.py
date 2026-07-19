"""AST-aligned, overlapping chunks owned by indexed symbols.

Chunks are retrieval artefacts, not graph nodes.  Each hit resolves back to
``owner_uid`` and carries an honest absolute source interval.  Python AST
statement boundaries are preferred; unparsable/oversized regions fall back to
overlapping line windows, so partial editor states and generated code remain
indexable.
"""

from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass


@dataclass(frozen=True)
class SemanticChunk:
    chunk_index: int
    start_line: int
    end_line: int
    text: str
    embedding_text: str


def _ast_statement_intervals(code: str) -> list[tuple[int, int]]:
    try:
        tree = ast.parse(textwrap.dedent(code))
    except (SyntaxError, ValueError, TypeError):
        return []
    intervals: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        start = int(getattr(node, "lineno", 0) or 0)
        end = int(getattr(node, "end_lineno", start) or start)
        if start > 0 and end >= start:
            intervals.add((start - 1, end - 1))
    # Prefer the smallest interval that starts on a line; outer function/class
    # spans otherwise collapse the whole symbol back into one giant chunk.
    by_start: dict[int, int] = {}
    for start, end in intervals:
        by_start[start] = min(end, by_start.get(start, end))
    return sorted(by_start.items())


def _fallback_windows(
    start: int,
    end: int,
    *,
    target_lines: int,
    overlap_lines: int,
) -> list[tuple[int, int]]:
    if end < start:
        return []
    width = max(4, target_lines)
    overlap = min(max(0, overlap_lines), width - 1)
    stride = width - overlap
    windows: list[tuple[int, int]] = []
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + width - 1)
        windows.append((cursor, window_end))
        if window_end >= end:
            break
        cursor += stride
    return windows


def _statement_aligned_windows(
    line_count: int,
    intervals: list[tuple[int, int]],
    *,
    target_lines: int,
    overlap_lines: int,
) -> list[tuple[int, int]]:
    if line_count <= 0:
        return []
    if not intervals:
        return _fallback_windows(
            0,
            line_count - 1,
            target_lines=target_lines,
            overlap_lines=overlap_lines,
        )

    ends = sorted({min(line_count - 1, end) for _start, end in intervals})
    windows: list[tuple[int, int]] = []
    start = 0
    while start < line_count:
        desired_end = min(line_count - 1, start + target_lines - 1)
        eligible = [end for end in ends if start <= end <= desired_end]
        end = max(eligible) if eligible else desired_end
        if end - start + 1 > target_lines * 2:
            windows.extend(
                _fallback_windows(
                    start,
                    end,
                    target_lines=target_lines,
                    overlap_lines=overlap_lines,
                )
            )
        else:
            windows.append((start, end))
        if end >= line_count - 1:
            break
        next_start = max(start + 1, end - overlap_lines + 1)
        # Align the next start to a nearby statement when possible.
        statement_starts = [s for s, _e in intervals if next_start <= s <= end + 1]
        start = min(statement_starts) if statement_starts else next_start
    return windows


def build_semantic_chunks(
    symbol: dict,
    *,
    target_lines: int = 24,
    overlap_lines: int = 4,
    min_symbol_lines: int = 10,
) -> list[SemanticChunk]:
    """Build source-attributed chunks for one symbol index payload."""
    code = str(symbol.get("code") or "")
    lines = code.splitlines()
    if len(lines) < max(1, min_symbol_lines):
        return []
    symbol_start = int(symbol.get("start_line") or 0)
    if symbol_start <= 0:
        return []
    intervals = _ast_statement_intervals(code)
    windows = _statement_aligned_windows(
        len(lines),
        intervals,
        target_lines=max(4, target_lines),
        overlap_lines=max(0, overlap_lines),
    )
    qualified_name = str(symbol.get("qualified_name") or symbol.get("name") or "")
    signature = ""
    for line in lines:
        signature = f"{signature}\n{line}".strip("\n")
        if line.rstrip().endswith(":"):
            break
        if len(signature.splitlines()) >= 6:
            break

    chunks: list[SemanticChunk] = []
    seen: set[tuple[int, int]] = set()
    for chunk_index, (local_start, local_end) in enumerate(windows):
        key = (local_start, local_end)
        if key in seen:
            continue
        seen.add(key)
        text = "\n".join(lines[local_start : local_end + 1]).strip("\n")
        if not text.strip():
            continue
        embedding_text = "\n".join(
            part for part in (qualified_name, signature, text) if part.strip()
        )
        chunks.append(
            SemanticChunk(
                chunk_index=chunk_index,
                start_line=symbol_start + local_start,
                end_line=symbol_start + local_end,
                text=text,
                embedding_text=embedding_text,
            )
        )
    return chunks


__all__ = ["SemanticChunk", "build_semantic_chunks"]
