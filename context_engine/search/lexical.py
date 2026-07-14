"""Small fielded BM25 index for exact code-symbol retrieval.

The axis index currently runs on LanceDB 0.6.2.  That release has no native
hybrid/RRF query path and its optional Tantivy FTS index is not incrementally
maintained by this project.  This module therefore keeps a compact, immutable
BM25 view beside the already cached workspace symbol scan.  Rebuilding it is
cheap compared with loading the embedding matrix and it is invalidated by the
same scan-cache lifecycle.

Only high-signal metadata is indexed.  Symbol bodies stay out of this cache so
lexical retrieval does not duplicate the (often much larger) source column in
memory.  Exact identifier and qualified-name matches receive explicit boosts;
BM25 supplies the softer abbreviation/path-token channel.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.$:-]*")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_QUOTED_IDENTIFIER_RE = re.compile(r"`+([A-Za-z_][A-Za-z0-9_.$:-]*)`+")


def identifier_tokens(text: str) -> tuple[str, ...]:
    """Tokenise code-ish text while retaining full identifiers and parts."""
    tokens: list[str] = []
    for match in _IDENTIFIER_RE.finditer(text or ""):
        raw = match.group(0)
        lowered = raw.lower()
        tokens.append(lowered)
        for dotted in re.split(r"[.$:/-]+", raw):
            for snake in dotted.split("_"):
                for part in _CAMEL_BOUNDARY_RE.split(snake):
                    part = part.strip().lower()
                    if part and part != lowered:
                        tokens.append(part)
    return tuple(tokens)


def exact_identifier_terms(query: str) -> frozenset[str]:
    """Identifiers worth applying exact name/qname boosts to.

    Backtick mentions are always exact.  Unquoted tokens containing code
    punctuation, underscores, capitals or digits are also treated as explicit
    symbol-shaped mentions; ordinary prose words remain BM25-only.
    """
    exact = {match.group(1).lower() for match in _QUOTED_IDENTIFIER_RE.finditer(query or "")}
    for match in _IDENTIFIER_RE.finditer(query or ""):
        raw = match.group(0)
        if (
            any(ch in raw for ch in "_.$:-")
            or any(ch.isupper() for ch in raw[1:])
            or any(ch.isdigit() for ch in raw)
        ):
            exact.add(raw.lower())
    return frozenset(exact)


@dataclass(frozen=True)
class LexicalHit:
    row_index: int
    score: float
    exact: bool = False


class FieldedBM25Index:
    """Immutable field-weighted BM25 over symbol metadata rows."""

    def __init__(self, rows: Sequence[Mapping[str, object]]) -> None:
        documents: list[Counter[str]] = []
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        lengths: list[int] = []
        names: list[str] = []
        qualified_names: list[str] = []

        for index, row in enumerate(rows):
            name = str(row.get("name") or "")
            qualified_name = str(row.get("qualified_name") or "")
            file_path = str(row.get("file_path") or "")
            symbol_kind = str(row.get("symbol_kind") or "")
            names.append(name.lower())
            qualified_names.append(qualified_name.lower())

            weighted: list[str] = []
            weighted.extend(identifier_tokens(name) * 5)
            weighted.extend(identifier_tokens(qualified_name) * 3)
            weighted.extend(identifier_tokens(file_path))
            weighted.extend(identifier_tokens(symbol_kind))
            counts = Counter(weighted)
            documents.append(counts)
            length = sum(counts.values())
            lengths.append(length)
            for token, frequency in counts.items():
                postings[token].append((index, frequency))

        self._documents = documents
        self._postings = dict(postings)
        self._lengths = lengths
        self._average_length = sum(lengths) / max(1, len(lengths))
        self._names = names
        self._qualified_names = qualified_names

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        allowed_indices: Iterable[int] | None = None,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> list[LexicalHit]:
        query_tokens = Counter(identifier_tokens(query))
        if not query_tokens or limit <= 0 or not self._documents:
            return []
        allowed = set(allowed_indices) if allowed_indices is not None else None
        scores: dict[int, float] = defaultdict(float)
        document_count = len(self._documents)
        average_length = max(1.0, self._average_length)
        for token, query_frequency in query_tokens.items():
            posting = self._postings.get(token) or ()
            if not posting:
                continue
            document_frequency = len(posting)
            idf = math.log(
                1.0 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            for row_index, frequency in posting:
                if allowed is not None and row_index not in allowed:
                    continue
                length = self._lengths[row_index]
                norm = frequency + k1 * (1.0 - b + b * length / average_length)
                scores[row_index] += query_frequency * idf * frequency * (k1 + 1.0) / norm

        exact_terms = exact_identifier_terms(query)
        exact_rows: set[int] = set()
        if exact_terms:
            candidate_indices = allowed if allowed is not None else range(document_count)
            for row_index in candidate_indices:
                name = self._names[row_index]
                qualified_name = self._qualified_names[row_index]
                exact_name = name in exact_terms
                exact_qname = any(
                    qualified_name == term
                    or qualified_name.endswith(f".{term}")
                    or qualified_name.endswith(f":{term}")
                    for term in exact_terms
                )
                if exact_name or exact_qname:
                    # A boost expressed on the same positive scale as BM25.
                    # Exact name wins over exact qualified-name suffix.
                    scores[row_index] += 12.0 if exact_name else 9.0
                    exact_rows.add(row_index)

        ranked = sorted(scores.items(), key=lambda item: (item[1], -item[0]), reverse=True)
        return [
            LexicalHit(row_index=row_index, score=float(score), exact=row_index in exact_rows)
            for row_index, score in ranked[:limit]
            if score > 0.0
        ]


__all__ = [
    "FieldedBM25Index",
    "LexicalHit",
    "exact_identifier_terms",
    "identifier_tokens",
]
