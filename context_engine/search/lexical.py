"""Small fielded BM25 index for exact code-symbol retrieval.

The axis index currently runs on LanceDB 0.6.2.  That release has no native
hybrid/RRF query path and its optional Tantivy FTS index is not incrementally
maintained by this project.  This module therefore keeps a compact, immutable
BM25 view beside the already cached workspace symbol scan.  Rebuilding it is
cheap compared with loading the embedding matrix and it is invalidated by the
same scan-cache lifecycle.

Only high-signal metadata is indexed, plus (optionally) each symbol body's
RARE identifier tokens — presence-only, capped by a document-frequency
ceiling, so error codes and message fragments quoted verbatim in questions
become findable without duplicating the source column's bulk in memory.
Exact identifier and qualified-name matches receive explicit boosts; BM25
supplies the softer abbreviation/path-token channel.
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


# Body tokens join the index only while they stay RARE across the workspace
# (document frequency at or below this ceiling). Rare body tokens are the
# whole point — error codes, command names, message fragments quoted verbatim
# in a question ("models.E028", "inspectdb") that never appear in symbol
# metadata. Common body tokens (self, request, model, …) carry no lexical
# signal and would multiply the posting list by orders of magnitude — the
# memory concern that originally kept bodies out of this cache.
_BODY_TOKEN_DF_CEILING = 32
_BODY_TOKEN_MIN_LENGTH = 3


def body_identifier_tokens(text: str) -> frozenset[str]:
    """Unique identifier-shaped tokens of a symbol body (full + dotted parts).

    No camel/snake splitting: sub-word parts of body identifiers are exactly
    the common-token noise the df ceiling exists to keep out, while a quoted
    ``models.E028`` must match a query's ``e028`` part — hence the dot split.
    """
    tokens: set[str] = set()
    for match in _IDENTIFIER_RE.finditer(text or ""):
        raw = match.group(0).lower()
        if len(raw) >= _BODY_TOKEN_MIN_LENGTH:
            tokens.add(raw)
        for part in re.split(r"[.$:/-]+", raw):
            if len(part) >= _BODY_TOKEN_MIN_LENGTH:
                tokens.add(part)
    return frozenset(tokens)


class FieldedBM25Index:
    """Immutable field-weighted BM25 over symbol metadata rows.

    ``bodies`` (optional, aligned with ``rows``) adds each symbol's RARE body
    tokens as presence-weighted terms (tf=1, weight below any metadata field)
    — see ``_BODY_TOKEN_DF_CEILING``.
    """

    def __init__(
        self,
        rows: Sequence[Mapping[str, object]],
        bodies: Sequence[str | None] | None = None,
    ) -> None:
        documents: list[Counter[str]] = []
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        lengths: list[int] = []
        names: list[str] = []
        qualified_names: list[str] = []

        rare_body_tokens: list[frozenset[str]] | None = None
        rare_body_postings: dict[str, tuple[int, ...]] = {}
        if bodies is not None:
            body_token_sets = [body_identifier_tokens(body or "") for body in bodies]
            body_df: Counter[str] = Counter()
            for token_set in body_token_sets:
                body_df.update(token_set)
            rare_body_tokens = [
                frozenset(token for token in token_set if body_df[token] <= _BODY_TOKEN_DF_CEILING)
                for token_set in body_token_sets
            ]
            rare_rows: dict[str, list[int]] = defaultdict(list)
            for index, token_set in enumerate(rare_body_tokens):
                for token in token_set:
                    rare_rows[token].append(index)
            rare_body_postings = {token: tuple(rows) for token, rows in rare_rows.items()}

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
            if rare_body_tokens is not None and index < len(rare_body_tokens):
                for token in rare_body_tokens[index]:
                    if token not in counts:
                        counts[token] = 1
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
        self._rare_body_postings = rare_body_postings

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

        if self._rare_body_postings:
            # A rare body token quoted in the question (an error code, a
            # message fragment) is a near-unique pointer at its symbol.
            # Additive bonuses cannot compete with tf-weighted name matches
            # (raw top scores run 10-100× the flat exact boosts), so lift the
            # few best body-exact rows to just under the current top score —
            # high enough to survive the channel's ceiling normalisation and
            # downstream seed selection, low enough that metadata identity
            # still outranks them.
            body_exact: dict[int, int] = {}
            for token in query_tokens:
                for row_index in self._rare_body_postings.get(token, ()):
                    if allowed is not None and row_index not in allowed:
                        continue
                    body_exact[row_index] = body_exact.get(row_index, 0) + 1
            if body_exact:
                top_score = max(scores.values(), default=0.0) or 1.0
                lifted = sorted(
                    body_exact.items(),
                    key=lambda item: (item[1], scores.get(item[0], 0.0)),
                    reverse=True,
                )[:4]
                for row_index, _matched in lifted:
                    scores[row_index] = max(scores[row_index], top_score * 0.9)

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
    "body_identifier_tokens",
    "exact_identifier_terms",
    "identifier_tokens",
]
