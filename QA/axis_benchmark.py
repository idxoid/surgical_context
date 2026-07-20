"""A/B benchmark for the axis pipeline (read-side).

Replays ``QA/fixtures/questions_python.yaml`` against the
axis pipeline (intent → role retrieval → context expansion) and
measures file_recall: how many of each question's ``expected_files``
appear in the retrieved file_paths. The legacy ``/ask`` cascade is
unaffected; this tool is the A/B baseline for the axis side so the
two can be compared by a separate harness or by eye.

Alongside recall, each layer also reports a REPORT-ONLY precision
(``expected_files`` is a recall gold set, not an exhaustive relevance
set — see ``_compute_precision``) plus a token split of the rendered
bundle (expected-file tokens vs everything else), so noise growth is
visible even while recall sits at 1.0. None of these gate P7.

We only score questions whose repository is indexed under the
axis_python_v1 profile. Others are recorded as ``skipped`` with the
reason so the report is honest about coverage.

Usage::

    python -m QA.axis_benchmark \\
        --pack QA/fixtures/questions_python.yaml \\
        --out /tmp/axis_benchmark

    # Comparison with a previous run:
    python -m QA.axis_benchmark --pack ... --out ... \\
        --compare /tmp/axis_benchmark_previous/summary.json

    # Cap sweep (per-role seed limit / impact blast radius):
    python -m QA.axis_benchmark --pack ... --out /tmp/cap_8_35 \\
        --per-role-limit 8 --max-impacted 35 --token-budget 6000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import yaml

from context_engine.axis.axis_profiles import AXIS_EDGES, axes_for_kinds
from context_engine.axis.axis_ranking import intent_axes
from context_engine.axis.pipeline import AxisRetrievalConfig, run_axis_retrieval
from context_engine.axis.role_retrieval import retrieval_channel_families
from context_engine.database.lancedb_client import LanceDBClient
from context_engine.database.neo4j_client import Neo4jClient
from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE, resolve_index_profile
from context_engine.indexer.fast.pipeline import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER
from context_engine.observability.metrics import estimate_text_tokens


class _StageTimer:
    """Minimal tracer for the pipeline's ``trace.stage(name)`` protocol —
    accumulates wall-time per stage so the benchmark can read the
    post-processing cost (the ``context`` stage = graph expansion + code
    fetch) without the full observability trace."""

    def __init__(self) -> None:
        self.durations: dict[str, float] = {}

    @contextmanager
    def stage(self, name: str):
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.durations[name] = self.durations.get(name, 0.0) + (time.monotonic() - t0)


# Map ``repo`` from the question pack to the axis-profile workspace_id we
# actually indexed. Env-driven so a new index can be built while an old one is
# kept for A/B: ``AXIS_BENCH_TENANT`` (default ``qa_repo``) / ``AXIS_BENCH_REF``
# (default ``main``) compose ``{tenant}/{repo}@{ref}``; the active index profile
# then appends its ``+axis_python_v1`` suffix. The legacy manual base was
# ``@axis-v4`` — set ``AXIS_BENCH_REF=axis-v4`` to point back at it.
# Repos not present in the DB are skipped with reason.
_BENCH_TENANT = os.getenv("AXIS_BENCH_TENANT", "qa_repo")
_BENCH_REF = os.getenv("AXIS_BENCH_REF", "main")
_BENCH_COMMIT_TENANT = os.getenv("AXIS_BENCH_COMMIT_TENANT", "contextbench")
_BENCH_PROFILE = resolve_index_profile(AXIS_PYTHON_V1_PROFILE)
_BENCH_REPOS = (
    "fastapi",
    "flask",
    "celery",
    "click",
    "pydantic",
    "sqlalchemy",
    "django",
    "dathund",
    "surgical_context",
    # Non-Python benchmark pack (QA/fixtures/questions_non_python.yaml)
    "express",
    "nestjs",
    "redux_toolkit",
    "vue",
)
REPO_TO_WORKSPACE: dict[str, str] = {
    repo: _BENCH_PROFILE.workspace_id(f"{_BENCH_TENANT}/{repo}@{_BENCH_REF}")
    for repo in _BENCH_REPOS
    if repo != "surgical_context"
}
# Dogfood repo is indexed under the same qa_repo tenant as the benchmark
# checkouts on this box. Keep a dedicated override for CI/manual local runs
# that intentionally index it elsewhere.
_SC_WS = os.getenv(
    "AXIS_SURGICAL_CONTEXT_WORKSPACE",
    f"{_BENCH_TENANT}/surgical_context@{_BENCH_REF}",
)
REPO_TO_WORKSPACE["surgical_context"] = _BENCH_PROFILE.workspace_id(_SC_WS)

_TOP_CANDIDATE_AUDIT_LIMIT = 15


@dataclass
class QuestionResult:
    question_id: str
    repo: str
    workspace_id: str | None
    question: str
    mechanism: str
    expected_files: list[str]
    base_commit: str = ""
    expected_symbols: list[str] = field(default_factory=list)
    expected_spans: list[dict[str, Any]] = field(default_factory=list)
    retrieved_files: list[str] = field(default_factory=list)
    matched_files: list[str] = field(default_factory=list)
    file_recall: float = 0.0
    # Three nested retrieval layers, each a recall metric over file paths:
    #   seed_recall   — pure vector/role retrieval (no graph walk).
    #   pool_recall   — after the pool expander (graph-walk passes).
    #   file_recall   — after per-candidate context expansion (the bundle).
    # Each layer can only add files, so seed ≤ pool ≤ bundle. Where a
    # higher layer is full but a lower one is not, that layer is MASKING a
    # retrieval miss the layer below it actually has.
    seed_files: list[str] = field(default_factory=list)
    seed_matched: list[str] = field(default_factory=list)
    seed_recall: float = 0.0
    pool_files: list[str] = field(default_factory=list)
    pool_matched: list[str] = field(default_factory=list)
    pool_recall: float = 0.0
    # Report-only precision mirror of the recall ladder: the share of each
    # layer's files that match an expected entry. ``expected_files`` is a
    # recall gold set (non-exhaustive), so a non-expected file is NOT
    # necessarily noise — read these as trends/deltas, never as P7 gates.
    seed_precision: float = 0.0
    pool_precision: float = 0.0
    bundle_precision: float = 0.0
    intent_top_role: str | None = None
    intent_top_similarity: float | None = None
    intent_matches: list[tuple[str, float]] = field(default_factory=list)
    skipped_reason: str | None = None
    candidate_count: int = 0
    # Post-processing cost (the expensive part — see the budget cost model):
    # ``context_seconds`` is the build_context graph-expansion + code-fetch
    # stage; ``rendered_tokens`` is the estimated token volume of the bundle
    # code actually handed to the prompt.
    context_seconds: float = 0.0
    rendered_tokens: int = 0
    # Token split of ``rendered_tokens`` (the packing-density view file
    # recall is blind to): tokens rendered from expected files vs everything
    # else. Same non-exhaustive-gold caveat as the precision fields above.
    expected_tokens: int = 0
    other_tokens: int = 0
    token_precision: float = 0.0
    # Candidate-level precision audit. These fields do not affect scoring;
    # they make the existing seed/pool/bundle report explain *why* noisy
    # candidates reached the pool or prompt.
    candidate_relation_histogram: dict[str, int] = field(default_factory=dict)
    bundle_relation_histogram: dict[str, int] = field(default_factory=dict)
    top_candidates: list[dict[str, Any]] = field(default_factory=list)
    top_rendered_symbols: list[dict[str, Any]] = field(default_factory=list)
    seed_selection: dict[str, Any] = field(default_factory=dict)
    lexical_span_probe: dict[str, Any] = field(default_factory=dict)
    lexical_span_probe_seconds: float = 0.0
    lexical_span_score_audit: dict[str, Any] = field(default_factory=dict)
    gold_rank_audit: dict[str, Any] = field(default_factory=dict)
    candidate_cohort_audit: dict[str, Any] = field(default_factory=dict)
    expected_file_layers: list[dict[str, Any]] = field(default_factory=list)
    seed_symbol_recall: float = 0.0
    pool_symbol_recall: float = 0.0
    bundle_symbol_recall: float = 0.0
    # Span gold has two independent failure modes. Owner recall asks whether
    # the exact (file, symbol) pair survived each layer; line recall asks how
    # much of the gold interval is actually anchored/rendered once it did.
    seed_span_owner_recall: float = 0.0
    pool_span_owner_recall: float = 0.0
    bundle_span_owner_recall: float = 0.0
    seed_span_recall: float = 0.0
    pool_span_recall: float = 0.0
    bundle_span_recall: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "repo": self.repo,
            "workspace_id": self.workspace_id,
            "question": self.question,
            "mechanism": self.mechanism,
            "expected_files": self.expected_files,
            "base_commit": self.base_commit,
            "expected_symbols": self.expected_symbols,
            "expected_spans": self.expected_spans,
            "retrieved_files": self.retrieved_files,
            "matched_files": self.matched_files,
            "file_recall": self.file_recall,
            "seed_files": self.seed_files,
            "seed_matched": self.seed_matched,
            "seed_recall": self.seed_recall,
            "pool_files": self.pool_files,
            "pool_matched": self.pool_matched,
            "pool_recall": self.pool_recall,
            "seed_precision": self.seed_precision,
            "pool_precision": self.pool_precision,
            "bundle_precision": self.bundle_precision,
            "intent_top_role": self.intent_top_role,
            "intent_top_similarity": self.intent_top_similarity,
            "intent_matches": [{"role": r, "similarity": s} for r, s in self.intent_matches],
            "skipped_reason": self.skipped_reason,
            "candidate_count": self.candidate_count,
            "context_seconds": self.context_seconds,
            "rendered_tokens": self.rendered_tokens,
            "expected_tokens": self.expected_tokens,
            "other_tokens": self.other_tokens,
            "token_precision": self.token_precision,
            "candidate_relation_histogram": self.candidate_relation_histogram,
            "bundle_relation_histogram": self.bundle_relation_histogram,
            "top_candidates": self.top_candidates,
            "top_rendered_symbols": self.top_rendered_symbols,
            "seed_selection": self.seed_selection,
            "lexical_span_probe": self.lexical_span_probe,
            "lexical_span_probe_seconds": self.lexical_span_probe_seconds,
            "lexical_span_score_audit": self.lexical_span_score_audit,
            "gold_rank_audit": self.gold_rank_audit,
            "candidate_cohort_audit": self.candidate_cohort_audit,
            "expected_file_layers": self.expected_file_layers,
            "seed_symbol_recall": self.seed_symbol_recall,
            "pool_symbol_recall": self.pool_symbol_recall,
            "bundle_symbol_recall": self.bundle_symbol_recall,
            "seed_span_owner_recall": self.seed_span_owner_recall,
            "pool_span_owner_recall": self.pool_span_owner_recall,
            "bundle_span_owner_recall": self.bundle_span_owner_recall,
            "seed_span_recall": self.seed_span_recall,
            "pool_span_recall": self.pool_span_recall,
            "bundle_span_recall": self.bundle_span_recall,
        }


def _load_pack(pack_path: Path, *, _seen: set[Path] | None = None) -> list[dict[str, Any]]:
    path = pack_path.resolve()
    seen = _seen if _seen is not None else set()
    if path in seen:
        raise ValueError(f"Circular include in question packs: {path}")
    seen.add(path)

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if not isinstance(payload, dict):
        raise ValueError(f"Unsupported question pack format in {path}")

    questions: list[dict[str, Any]] = []
    for entry in payload.get("questions", []) or []:
        if not isinstance(entry, dict):
            continue
        questions.append(entry)
    for include in payload.get("includes", []) or []:
        questions.extend(_load_pack(path.parent / str(include), _seen=seen))
    return questions


def _file_matches(retrieved: str, expected: str) -> bool:
    """An expected entry matches a retrieved path when either:

      * the entry's last segment has an extension (looks like a file) —
        the retrieved path ends with ``/<expected>`` (or equals it), OR
      * the entry's last segment has no extension (looks like a
        directory or a directory tree marker — e.g. ``tests``,
        ``fastapi/dependencies``) — the retrieved path *contains* the
        entry as a directory component.

    The directory case is critical for impact-style questions where
    the question pack lists a *region* of the codebase ("tests",
    "fastapi/dependencies") as the expected answer surface rather
    than a specific file. Without it, ``recall`` undercounts whenever
    the answer set is "any file under this directory".
    """
    r = retrieved.replace("\\", "/").rstrip("/")
    e = expected.replace("\\", "/").strip("/")
    if not r or not e:
        return False
    last_segment = e.rsplit("/", 1)[-1]
    if "." in last_segment:
        return r.endswith("/" + e) or r == e
    # Directory entry — match if ``/<expected>/`` is a path component.
    needle = "/" + e + "/"
    return needle in (r + "/")


def _compute_recall(expected: list[str], retrieved: list[str]) -> tuple[float, list[str]]:
    if not expected:
        return 0.0, []
    matched: list[str] = []
    for exp in expected:
        for ret in retrieved:
            if _file_matches(ret, exp):
                matched.append(exp)
                break
    return len(matched) / len(expected), matched


def _compute_precision(expected: list[str], retrieved: list[str]) -> float:
    """Share of retrieved files that match an expected entry.

    REPORT-ONLY. ``expected_files`` is a recall gold set — files that MUST
    be present — not an exhaustive relevance set, so this number is biased
    low and a non-expected file is not necessarily noise. Gating P7 on it
    would create pressure to under-retrieve; read it as a trend/delta
    between runs, and report token pairs next to it, not the bare ratio.
    """
    paths = _ordered_unique_paths(retrieved)
    if not paths:
        return 0.0
    matched = sum(1 for ret in paths if any(_file_matches(ret, exp) for exp in expected))
    return matched / len(paths)


def _normalise_expected_spans(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Accept future span gold without forcing one fixture representation."""
    output: list[dict[str, Any]] = []

    def add(raw: Any, *, default_symbol: str = "", default_file: str = "") -> None:
        if isinstance(raw, int):
            start = end = raw
            symbol = default_symbol
            file_path = default_file
        elif isinstance(raw, (list, tuple)) and len(raw) == 2:
            start, end = raw
            symbol = default_symbol
            file_path = default_file
        elif isinstance(raw, dict):
            start = int(raw.get("start_line", raw.get("start", raw.get("line", 0))) or 0)
            end = int(raw.get("end_line", raw.get("end", start)) or start)
            symbol = str(raw.get("symbol") or default_symbol)
            file_path = str(raw.get("file_path") or raw.get("file") or default_file)
        else:
            return
        try:
            start_line = int(start)
            end_line = int(end)
        except (TypeError, ValueError):
            return
        if start_line <= 0 or end_line < start_line:
            return
        output.append(
            {
                "symbol": symbol,
                "file_path": file_path,
                "start_line": start_line,
                "end_line": end_line,
            }
        )

    default_symbol = str(entry.get("symbol") or "")
    default_files = [str(path) for path in (entry.get("expected_files") or [])]
    default_file = default_files[0] if len(default_files) == 1 else ""
    for raw in entry.get("expected_spans") or []:
        add(raw, default_symbol=default_symbol, default_file=default_file)

    expected_lines = entry.get("expected_lines") or []
    if isinstance(expected_lines, dict):
        for owner, lines in expected_lines.items():
            owner_text = str(owner)
            owner_is_file = "/" in owner_text or owner_text.endswith(".py")
            owner_file = owner_text if owner_is_file else default_file
            owner_symbol = default_symbol if owner_is_file else owner_text
            for raw in lines if isinstance(lines, list) else [lines]:
                add(raw, default_symbol=owner_symbol, default_file=owner_file)
    else:
        for raw in expected_lines if isinstance(expected_lines, list) else [expected_lines]:
            add(raw, default_symbol=default_symbol, default_file=default_file)
    return output


def _item_owner_views(item: Any) -> list[tuple[str, str, str]]:
    views = [
        (
            str(getattr(item, "file_path", "") or ""),
            str(getattr(item, "name", "") or ""),
            str(getattr(item, "qualified_name", "") or ""),
        )
    ]
    for owner in tuple(getattr(item, "represented_owners", ()) or ()):
        if isinstance(owner, dict):
            file_path = str(owner.get("file_path") or "")
            name = str(owner.get("name") or "")
            qualified_name = str(owner.get("qualified_name") or "")
        else:
            file_path = str(getattr(owner, "file_path", "") or "")
            name = str(getattr(owner, "name", "") or "")
            qualified_name = str(getattr(owner, "qualified_name", "") or "")
        view = (file_path, name, qualified_name)
        if view not in views:
            views.append(view)
    return views


def _owner_symbol_matches(name: str, qualified_name: str, expected_symbol: str) -> bool:
    expected = expected_symbol.strip().lower()
    if not expected:
        return False
    name = name.lower()
    qualified = qualified_name.lower()
    return (
        name == expected
        or qualified == expected
        or qualified.endswith(f".{expected}")
        or qualified.endswith(f":{expected}")
    )


def _symbol_matches(item: Any, expected_symbol: str) -> bool:
    return any(
        _owner_symbol_matches(name, qualified_name, expected_symbol)
        for _file_path, name, qualified_name in _item_owner_views(item)
    )


def _compute_symbol_recall(expected: list[str], items: list[Any]) -> float:
    if not expected:
        return 0.0
    matched = sum(1 for symbol in expected if any(_symbol_matches(item, symbol) for item in items))
    return matched / len(expected)


def _item_matches_span_owner(item: Any, gold: dict[str, Any]) -> bool:
    symbol = str(gold.get("symbol") or "")
    file_path = str(gold.get("file_path") or "")
    return any(
        (not symbol or _owner_symbol_matches(name, qualified_name, symbol))
        and (not file_path or _file_matches(owner_file, file_path))
        for owner_file, name, qualified_name in _item_owner_views(item)
    )


def _compute_span_owner_recall(
    expected_spans: list[dict[str, Any]],
    items: list[Any],
) -> float:
    """Recall of unique exact (file, symbol) owners in span gold.

    A question may name several disjoint answer intervals inside one large
    function. Count that owner once here; the line metric below retains the
    interval-level weighting.
    """
    owners: dict[tuple[str, str], dict[str, Any]] = {}
    for gold in expected_spans:
        symbol = str(gold.get("symbol") or "").strip()
        file_path = str(gold.get("file_path") or "").replace("\\", "/").strip("/")
        if not symbol and not file_path:
            continue
        owners.setdefault((file_path, symbol.lower()), gold)
    if not owners:
        return 0.0
    matched = sum(
        1 for gold in owners.values() if any(_item_matches_span_owner(item, gold) for item in items)
    )
    return matched / len(owners)


def _compute_span_recall(
    expected_spans: list[dict[str, Any]],
    items: list[Any],
    *,
    span_getter,
) -> float:
    gold_lines = {
        (gold_index, line)
        for gold_index, gold in enumerate(expected_spans)
        for line in range(int(gold["start_line"]), int(gold["end_line"]) + 1)
    }
    if not gold_lines:
        return 0.0
    covered: set[tuple[int, int]] = set()
    for gold_index, gold in enumerate(expected_spans):
        for item in items:
            if not _item_matches_span_owner(item, gold):
                continue
            for start_line, end_line in span_getter(item):
                start = int(start_line)
                end = int(end_line)
                for line in range(
                    max(start, int(gold["start_line"])), min(end, int(gold["end_line"])) + 1
                ):
                    covered.add((gold_index, line))
    return len(covered) / len(gold_lines)


def _lexical_span_score_audit(
    expected_spans: list[dict[str, Any]],
    items: list[Any],
) -> dict[str, Any]:
    unique: dict[str, Any] = {}
    for index, item in enumerate(items):
        uid = str(getattr(item, "uid", "") or f"__missing_uid__:{index}")
        unique.setdefault(uid, item)
    gold_items = [
        item
        for item in unique.values()
        if any(_item_matches_span_owner(item, gold) for gold in expected_spans)
    ]
    other_items = [item for item in unique.values() if item not in gold_items]

    def score(item: Any) -> float:
        value = getattr(item, "lexical_span_score", None)
        return float(value) if value is not None else 0.0

    gold_scores = [score(item) for item in gold_items]
    other_scores = [score(item) for item in other_items]
    pair_count = len(gold_scores) * len(other_scores)
    auc = None
    if pair_count:
        wins = sum(
            1.0 if gold > other else 0.5 if gold == other else 0.0
            for gold in gold_scores
            for other in other_scores
        )
        auc = wins / pair_count
    ranked = sorted(
        unique.values(),
        key=lambda item: (score(item), str(getattr(item, "uid", "") or "")),
        reverse=True,
    )
    cutoff = len(gold_items)
    top = ranked[:cutoff]
    top_gold = sum(item in gold_items for item in top)
    return {
        "candidate_count": len(unique),
        "gold_owner_candidates": len(gold_items),
        "other_candidates": len(other_items),
        "scored_gold_owner_candidates": sum(
            getattr(item, "lexical_span_score", None) is not None for item in gold_items
        ),
        "scored_other_candidates": sum(
            getattr(item, "lexical_span_score", None) is not None for item in other_items
        ),
        "mean_gold_owner_score": (sum(gold_scores) / len(gold_scores) if gold_scores else 0.0),
        "mean_other_score": (sum(other_scores) / len(other_scores) if other_scores else 0.0),
        "auc": auc,
        "gold_precision_at_owner_count": top_gold / cutoff if cutoff else 0.0,
    }


_GOLD_RANK_CUTOFFS = (1, 3, 5, 10, 20, 40, 80, 160)
_RANK_SPEND_BUCKETS = (
    ("1", 1, 1),
    ("2-3", 2, 3),
    ("4-5", 4, 5),
    ("6-10", 6, 10),
    ("11-20", 11, 20),
    ("21-40", 21, 40),
    ("41+", 41, None),
)


def _first_matching_rank(items: list[Any], predicate) -> int | None:
    return next(
        (rank for rank, item in enumerate(items, start=1) if predicate(item)),
        None,
    )


def _candidate_rank_spend_audit(
    ranked_candidates: list[Any],
    budget_trace: Any | None,
) -> dict[str, Any]:
    """Relate Token Credit spend to the pre-budget candidate rank.

    Coverage is intentionally reported separately from upgrades: a wide cheap
    signature floor can be recall-safe while body/rich-render spend should be
    concentrated in the ranked head. Transactions are keyed by bundle seed
    UID, which is also the stable identity in ``ranked_candidates``.
    """
    rank_by_uid: dict[str, int] = {}
    for rank, candidate in enumerate(ranked_candidates, start=1):
        uid = str(getattr(candidate, "uid", "") or "")
        if uid:
            rank_by_uid.setdefault(uid, rank)

    bucket_rows = {
        label: {
            "coverage_tokens": 0,
            "upgrade_tokens": 0,
            "other_tokens": 0,
            "transactions": 0,
        }
        for label, _lower, _upper in _RANK_SPEND_BUCKETS
    }
    bucket_rows["unranked"] = {
        "coverage_tokens": 0,
        "upgrade_tokens": 0,
        "other_tokens": 0,
        "transactions": 0,
    }
    totals = {"coverage_tokens": 0, "upgrade_tokens": 0, "other_tokens": 0}
    upgrade_tokens_at = {str(cutoff): 0 for cutoff in _GOLD_RANK_CUTOFFS}
    upgrade_attribution: dict[str, Counter[str]] = {
        "scope": Counter(),
        "evidence": Counter(),
        "edge_type": Counter(),
        "depth": Counter(),
        "scope_evidence": Counter(),
    }

    def bucket_for(rank: int | None) -> str:
        if rank is None:
            return "unranked"
        for label, lower, upper in _RANK_SPEND_BUCKETS:
            if rank >= lower and (upper is None or rank <= upper):
                return label
        return "unranked"

    for transaction in list(getattr(budget_trace, "transactions", ()) or ()):
        # A fold swap can theoretically reduce first-wins printed size. This
        # audit measures tokens bought, not net render compaction, so only
        # positive deltas count as spend.
        tokens = max(0, int(getattr(transaction, "delta_tokens", 0) or 0))
        phase = str(getattr(transaction, "phase", "") or "")
        if phase == "coverage":
            field = "coverage_tokens"
        elif phase.startswith("upgrade_"):
            field = "upgrade_tokens"
        else:
            field = "other_tokens"
        uid = str(getattr(transaction, "uid", "") or "")
        spend_rank = rank_by_uid.get(uid)
        row = bucket_rows[bucket_for(spend_rank)]
        row[field] += tokens
        row["transactions"] += 1
        totals[field] += tokens
        if field == "upgrade_tokens":
            for attribution in list(getattr(transaction, "attribution", ()) or ()):
                attributed_tokens = max(0, int(getattr(attribution, "delta_tokens", 0) or 0))
                scope = str(getattr(attribution, "scope", "unknown") or "unknown")
                evidence = str(getattr(attribution, "evidence", "unknown") or "unknown")
                edge_type = str(getattr(attribution, "edge_type", "") or "(none)")
                depth = str(max(0, int(getattr(attribution, "depth", 0) or 0)))
                upgrade_attribution["scope"][scope] += attributed_tokens
                upgrade_attribution["evidence"][evidence] += attributed_tokens
                upgrade_attribution["edge_type"][edge_type] += attributed_tokens
                upgrade_attribution["depth"][depth] += attributed_tokens
                upgrade_attribution["scope_evidence"][f"{scope}|{evidence}"] += attributed_tokens
        if field == "upgrade_tokens" and spend_rank is not None:
            for cutoff in _GOLD_RANK_CUTOFFS:
                if spend_rank <= cutoff:
                    upgrade_tokens_at[str(cutoff)] += tokens

    upgrade_total = totals["upgrade_tokens"]
    return {
        "candidate_count": len(rank_by_uid),
        "allocation_mode": str(getattr(budget_trace, "allocation_mode", "legacy")),
        "coverage_share": float(getattr(budget_trace, "coverage_share", 0.0) or 0.0),
        "rank_decay_coverage_threshold": float(
            getattr(budget_trace, "rank_decay_coverage_threshold", 0.0) or 0.0
        ),
        "graph_fanout": {
            "base_limit": int(getattr(budget_trace, "graph_fanout_base_limit", 0) or 0),
            "tier_counts": dict(getattr(budget_trace, "graph_fanout_tier_counts", {}) or {}),
            "limit_counts": {
                str(limit): count
                for limit, count in dict(
                    getattr(budget_trace, "graph_fanout_limit_counts", {}) or {}
                ).items()
            },
        },
        **totals,
        "upgrade_attribution": {
            dimension: dict(counter.most_common())
            for dimension, counter in upgrade_attribution.items()
        },
        "upgrade_tokens_at": upgrade_tokens_at,
        "upgrade_share_at": {
            cutoff: tokens / upgrade_total if upgrade_total else 0.0
            for cutoff, tokens in upgrade_tokens_at.items()
        },
        "by_rank": bucket_rows,
    }


def _gold_rank_funnel(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def stage(field: str) -> dict[str, Any]:
        ranks = [int(row[field]) for row in rows if row.get(field) is not None]
        return {
            "present": len(ranks),
            "recall": len(ranks) / len(rows) if rows else 0.0,
            "recall_at": {
                str(cutoff): (sum(rank <= cutoff for rank in ranks) / len(rows) if rows else 0.0)
                for cutoff in _GOLD_RANK_CUTOFFS
            },
        }

    return {
        "gold_count": len(rows),
        "utility": stage("utility_rank"),
        "coverage": stage("coverage_rank"),
        "final": stage("final_rank"),
        "retrieval_missing_but_final": sum(
            row.get("utility_rank") is None and row.get("final_rank") is not None for row in rows
        ),
        "utility_not_coverage_but_final": sum(
            row.get("utility_rank") is not None
            and row.get("coverage_rank") is None
            and row.get("final_rank") is not None
            for row in rows
        ),
        "coverage_not_final": sum(
            row.get("coverage_rank") is not None and row.get("final_rank") is None for row in rows
        ),
    }


def _gold_rank_audit(
    expected_spans: list[dict[str, Any]],
    expected_symbols: list[str],
    ranked_candidates: list[Any],
    rendered_symbols: list[Any],
    budget_trace: Any | None,
) -> dict[str, Any]:
    coverage_transactions = [
        transaction
        for transaction in list(getattr(budget_trace, "transactions", ()) or ())
        if str(getattr(transaction, "phase", "") or "") == "coverage"
    ]
    coverage_uids = [
        str(getattr(transaction, "uid", "") or "") for transaction in coverage_transactions
    ]

    owners: dict[tuple[str, str], dict[str, Any]] = {}
    for gold in expected_spans:
        file_path = str(gold.get("file_path") or "").replace("\\", "/").strip("/")
        symbol = str(gold.get("symbol") or "").strip()
        key = (file_path, symbol.lower())
        if key != ("", ""):
            owners.setdefault(key, gold)

    owner_rows: list[dict[str, Any]] = []
    for (file_path, symbol_key), gold in owners.items():

        def matches_owner(item: Any, expected: dict[str, Any] = gold) -> bool:
            return _item_matches_span_owner(item, expected)

        matching_uids = {
            str(getattr(candidate, "uid", "") or "")
            for candidate in ranked_candidates
            if matches_owner(candidate)
        }
        utility_rank = _first_matching_rank(ranked_candidates, matches_owner)
        ranked_match = ranked_candidates[utility_rank - 1] if utility_rank is not None else None
        owner_rows.append(
            {
                "file_path": file_path,
                "symbol": str(gold.get("symbol") or symbol_key),
                "utility_rank": utility_rank,
                "coverage_rank": next(
                    (
                        rank
                        for rank, uid in enumerate(coverage_uids, start=1)
                        if uid in matching_uids
                    ),
                    None,
                ),
                "final_rank": _first_matching_rank(rendered_symbols, matches_owner),
                "lexical_span_score": _float_or_none(
                    getattr(ranked_match, "lexical_span_score", None)
                ),
            }
        )

    symbol_rows: list[dict[str, Any]] = []
    for expected_symbol in expected_symbols:
        symbol = str(expected_symbol or "").strip()
        if not symbol:
            continue

        def matches_symbol(item: Any, expected: str = symbol) -> bool:
            return _symbol_matches(item, expected)

        matching_uids = {
            str(getattr(candidate, "uid", "") or "")
            for candidate in ranked_candidates
            if matches_symbol(candidate)
        }
        utility_rank = _first_matching_rank(ranked_candidates, matches_symbol)
        ranked_match = ranked_candidates[utility_rank - 1] if utility_rank is not None else None
        symbol_rows.append(
            {
                "symbol": symbol,
                "utility_rank": utility_rank,
                "coverage_rank": next(
                    (
                        rank
                        for rank, uid in enumerate(coverage_uids, start=1)
                        if uid in matching_uids
                    ),
                    None,
                ),
                "final_rank": _first_matching_rank(rendered_symbols, matches_symbol),
                "lexical_span_score": _float_or_none(
                    getattr(ranked_match, "lexical_span_score", None)
                ),
            }
        )

    return {
        "candidate_count": len(ranked_candidates),
        "coverage_count": len(coverage_transactions),
        "rendered_symbol_count": len(rendered_symbols),
        "candidate_rank_spend": _candidate_rank_spend_audit(
            ranked_candidates,
            budget_trace,
        ),
        "owners": owner_rows,
        "symbols": symbol_rows,
        "owner_funnel": _gold_rank_funnel(owner_rows),
        "symbol_funnel": _gold_rank_funnel(symbol_rows),
    }


def _sorted_counter_dict(counter: Counter[str]) -> dict[str, int]:
    return {
        key: count for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    }


def _candidate_relation(candidate: Any) -> str:
    return str(getattr(candidate, "role", "") or getattr(candidate, "edge_type", "") or "(none)")


def _rendered_symbol_relation(symbol: Any) -> str:
    return str(
        getattr(symbol, "role", "")
        or getattr(symbol, "expansion_step", "")
        or getattr(symbol, "edge_type", "")
        or "(none)"
    )


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _candidate_audit_row(candidate: Any, rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "uid": str(getattr(candidate, "uid", "") or ""),
        "name": str(getattr(candidate, "name", "") or ""),
        "qualified_name": str(getattr(candidate, "qualified_name", "") or ""),
        "file_path": str(getattr(candidate, "file_path", "") or ""),
        "relation": _candidate_relation(candidate),
        "role": str(getattr(candidate, "role", "") or ""),
        "score": _float_or_none(getattr(candidate, "score", None)),
        "utility_score": _float_or_none(getattr(candidate, "utility_score", None)),
        "depth": getattr(candidate, "depth", None),
        "edge_type": str(getattr(candidate, "edge_type", "") or ""),
        "satisfying_contracts": list(getattr(candidate, "satisfying_contracts", ()) or ()),
        "satisfying_kinds": list(getattr(candidate, "satisfying_kinds", ()) or ()),
        "retrieval_channels": list(getattr(candidate, "retrieval_channels", ()) or ()),
        "retrieval_spans": list(getattr(candidate, "retrieval_spans", ()) or ()),
        "exact_symbol_match": bool(getattr(candidate, "exact_symbol_match", False)),
        "lexical_span_score": _float_or_none(getattr(candidate, "lexical_span_score", None)),
        "supporting_roles": list(getattr(candidate, "supporting_roles", ()) or ()),
        "selection_reasons": list(getattr(candidate, "selection_reasons", ()) or ()),
        "role_consensus_bonus": _float_or_none(getattr(candidate, "role_consensus_bonus", None)),
        "channel_consensus_bonus": _float_or_none(
            getattr(candidate, "channel_consensus_bonus", None)
        ),
        "exact_symbol_bonus": _float_or_none(getattr(candidate, "exact_symbol_bonus", None)),
    }


def _edge_axes(edge_type: str) -> frozenset[str]:
    """Return traversal axes directly evidenced by a candidate edge."""
    edge = str(edge_type or "").upper()
    if not edge:
        return frozenset()
    wildcard_prefix = edge[:-1] if edge.endswith("*") else ""
    return frozenset(
        axis
        for axis, relationships in AXIS_EDGES.items()
        if any(
            relationship == edge or (wildcard_prefix and relationship.startswith(wildcard_prefix))
            for relationship in relationships
        )
    )


def _candidate_axis_signature(candidate: Any) -> tuple[str, str]:
    """Classify a candidate by its strongest available structural axis.

    Kind evidence is strongest, followed by an explicit traversal edge.  A
    role profile is only a fallback for candidates whose producer discarded
    both; keeping the basis separate prevents an intent-shaped role label from
    masquerading as direct structural evidence in the audit.
    """
    kind_axes = axes_for_kinds(frozenset(getattr(candidate, "satisfying_kinds", ()) or ()))
    if kind_axes:
        return "+".join(sorted(kind_axes)), "kind"
    edge_axes = _edge_axes(str(getattr(candidate, "edge_type", "") or ""))
    if edge_axes:
        return "+".join(sorted(edge_axes)), "edge"
    roles = tuple(getattr(candidate, "supporting_roles", ()) or ()) or (
        str(getattr(candidate, "role", "") or ""),
    )
    profiled_axes = intent_axes(role for role in roles if role)
    if profiled_axes:
        return "+".join(sorted(profiled_axes)), "role_profile"
    return "axisless", "none"


def _candidate_gold_class(
    candidate: Any,
    *,
    expected_files: list[str],
    expected_symbols: list[str],
    expected_spans: list[dict[str, Any]],
) -> str:
    if any(_item_matches_span_owner(candidate, span) for span in expected_spans):
        return "owner"
    if any(_symbol_matches(candidate, symbol) for symbol in expected_symbols):
        return "symbol"
    path = str(getattr(candidate, "file_path", "") or "")
    if any(_file_matches(path, expected_file) for expected_file in expected_files):
        return "file"
    return "other"


def _new_candidate_cohort_row() -> dict[str, int | float]:
    return {
        "candidates": 0,
        "owner_gold": 0,
        "symbol_gold": 0,
        "file_gold": 0,
        "other": 0,
        "top5": 0,
        "top10": 0,
        "rank_sum": 0,
        "exact_gold_top5": 0,
        "exact_gold_top10": 0,
        "exact_gold_rank_sum": 0,
        "labelled_gold_top10": 0,
        "labelled_gold_rank_sum": 0,
        "coverage_candidates": 0,
        "upgraded_candidates": 0,
        "coverage_tokens": 0,
        "upgrade_tokens": 0,
    }


def _finalise_candidate_cohort_row(
    row: dict[str, Any],
) -> dict[str, Any]:
    candidates = int(row.get("candidates", 0))
    labelled_gold = sum(
        int(row.get(field, 0)) for field in ("owner_gold", "symbol_gold", "file_gold")
    )
    exact_gold = sum(int(row.get(field, 0)) for field in ("owner_gold", "symbol_gold"))
    row["labelled_gold"] = labelled_gold
    row["exact_gold"] = exact_gold
    row["labelled_gold_rate"] = labelled_gold / candidates if candidates else 0.0
    row["exact_gold_rate"] = exact_gold / candidates if candidates else 0.0
    row["mean_rank"] = float(row.get("rank_sum", 0)) / candidates if candidates else 0.0
    row["mean_exact_gold_rank"] = (
        float(row.get("exact_gold_rank_sum", 0)) / exact_gold if exact_gold else None
    )
    row["exact_gold_top10_share"] = (
        int(row.get("exact_gold_top10", 0)) / exact_gold if exact_gold else 0.0
    )
    row["labelled_gold_top10_share"] = (
        int(row.get("labelled_gold_top10", 0)) / labelled_gold if labelled_gold else 0.0
    )
    row["coverage_rate"] = (
        int(row.get("coverage_candidates", 0)) / candidates if candidates else 0.0
    )
    return row


def _candidate_cohort_audit(
    ranked_candidates: list[Any],
    *,
    expected_files: list[str],
    expected_symbols: list[str],
    expected_spans: list[dict[str, Any]],
    intent_matches: list[tuple[str, float]],
    budget_trace: Any | None,
) -> dict[str, Any]:
    """Explain the complete pre-budget pool by role, axis and intent fit."""
    intent_roles = tuple(role for role, _similarity in intent_matches)
    question_axes = intent_axes(intent_roles)
    coverage_uids: set[str] = set()
    upgraded_uids: set[str] = set()
    spend_by_uid: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for transaction in list(getattr(budget_trace, "transactions", ()) or ()):
        uid = str(getattr(transaction, "uid", "") or "")
        if not uid:
            continue
        phase = str(getattr(transaction, "phase", "") or "")
        tokens = max(0, int(getattr(transaction, "delta_tokens", 0) or 0))
        if phase == "coverage":
            coverage_uids.add(uid)
            spend_by_uid[uid]["coverage_tokens"] += tokens
        elif phase.startswith("upgrade_"):
            upgraded_uids.add(uid)
            spend_by_uid[uid]["upgrade_tokens"] += tokens

    groups: dict[str, defaultdict[str, dict[str, int | float]]] = {
        dimension: defaultdict(_new_candidate_cohort_row)
        for dimension in (
            "by_role",
            "by_role_signature",
            "by_role_intent_alignment",
            "by_axis",
            "by_axis_basis",
            "by_intent_alignment",
            "by_channel",
            "by_channel_signature",
            "by_channel_count",
            "by_exact_symbol_prior",
        )
    }
    cross: defaultdict[str, dict[str, int | float]] = defaultdict(_new_candidate_cohort_row)
    total = _new_candidate_cohort_row()
    multi_role_candidates = 0

    for rank, candidate in enumerate(ranked_candidates, start=1):
        uid = str(getattr(candidate, "uid", "") or "")
        primary_role = str(getattr(candidate, "role", "") or "(none)")
        supporting_roles = frozenset(
            str(value)
            for value in (getattr(candidate, "supporting_roles", ()) or (primary_role,))
            if value
        )
        role_signature = "+".join(sorted(supporting_roles)) or "(none)"
        channel_families = retrieval_channel_families(
            getattr(candidate, "retrieval_channels", ()) or ()
        )
        channel_signature = "+".join(channel_families) or "(none)"
        channel_count = str(len(channel_families))
        exact_symbol_prior = (
            "exact_symbol" if bool(getattr(candidate, "exact_symbol_match", False)) else "non_exact"
        )
        if len(supporting_roles) > 1:
            multi_role_candidates += 1
        axis, axis_basis = _candidate_axis_signature(candidate)
        candidate_axes = frozenset() if axis == "axisless" else frozenset(axis.split("+"))
        role_match = bool(supporting_roles.intersection(intent_roles))
        axis_match = bool(candidate_axes.intersection(question_axes))
        if role_match and axis_match:
            alignment = "role+axis"
        elif role_match:
            alignment = "role_only"
        elif axis_match:
            alignment = "axis_only"
        else:
            alignment = "none"
        gold_class = _candidate_gold_class(
            candidate,
            expected_files=expected_files,
            expected_symbols=expected_symbols,
            expected_spans=expected_spans,
        )

        def update(
            row: dict[str, int | float],
            *,
            gold_class: str = gold_class,
            rank: int = rank,
            uid: str = uid,
        ) -> None:
            row["candidates"] += 1
            row[f"{gold_class}_gold" if gold_class != "other" else "other"] += 1
            row["rank_sum"] += rank
            row["top5"] += int(rank <= 5)
            row["top10"] += int(rank <= 10)
            if gold_class != "other":
                row["labelled_gold_rank_sum"] += rank
                row["labelled_gold_top10"] += int(rank <= 10)
            if gold_class in {"owner", "symbol"}:
                row["exact_gold_rank_sum"] += rank
                row["exact_gold_top5"] += int(rank <= 5)
                row["exact_gold_top10"] += int(rank <= 10)
            row["coverage_candidates"] += int(uid in coverage_uids)
            row["upgraded_candidates"] += int(uid in upgraded_uids)
            row["coverage_tokens"] += spend_by_uid[uid]["coverage_tokens"]
            row["upgrade_tokens"] += spend_by_uid[uid]["upgrade_tokens"]

        for dimension, key in (
            ("by_role_signature", role_signature),
            ("by_axis", axis),
            ("by_axis_basis", axis_basis),
            ("by_intent_alignment", alignment),
            ("by_channel_signature", channel_signature),
            ("by_channel_count", channel_count),
            ("by_exact_symbol_prior", exact_symbol_prior),
        ):
            update(groups[dimension][key])
        for channel in channel_families or ("(none)",):
            update(groups["by_channel"][channel])
        for supporting_role in supporting_roles or {"(none)"}:
            update(groups["by_role"][supporting_role])
            update(groups["by_role_intent_alignment"][f"{supporting_role}|{alignment}"])
        update(cross[f"{role_signature}|{axis}|{alignment}"])
        update(total)

    def finalise_group(
        rows: dict[str, dict[str, int | float]],
    ) -> dict[str, dict[str, int | float]]:
        return {
            key: _finalise_candidate_cohort_row(dict(row))
            for key, row in sorted(
                rows.items(),
                key=lambda item: (-int(item[1].get("candidates", 0)), item[0]),
            )
        }

    return {
        "candidate_count": len(ranked_candidates),
        "multi_role_candidates": multi_role_candidates,
        "intent_roles": list(intent_roles),
        "intent_axes": sorted(question_axes),
        "totals": _finalise_candidate_cohort_row(total),
        **{dimension: finalise_group(rows) for dimension, rows in groups.items()},
        "by_role_axis_intent": finalise_group(cross),
    }


def _rendered_symbol_audit_row(symbol: Any, rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "uid": str(getattr(symbol, "uid", "") or ""),
        "name": str(getattr(symbol, "name", "") or ""),
        "qualified_name": str(getattr(symbol, "qualified_name", "") or ""),
        "file_path": str(getattr(symbol, "file_path", "") or ""),
        "relation": _rendered_symbol_relation(symbol),
        "distance_from_seed": getattr(symbol, "distance_from_seed", None),
        "expansion_step": str(getattr(symbol, "expansion_step", "") or ""),
        "edge_type": str(getattr(symbol, "edge_type", "") or ""),
        "relevance_score": _float_or_none(getattr(symbol, "relevance_score", None)),
        "utility_score": _float_or_none(getattr(symbol, "utility_score", None)),
        "retrieval_spans": list(getattr(symbol, "retrieval_spans", ()) or ()),
        "rendered_spans": list(symbol.effective_rendered_spans()),
    }


def _unique_rendered_symbols(bundles: Any) -> list[Any]:
    seen: set[str] = set()
    out: list[Any] = []
    for bundle in bundles:
        for symbol in bundle.all_symbols():
            uid = str(getattr(symbol, "uid", "") or "")
            key = uid or "::".join(
                (
                    str(getattr(symbol, "file_path", "") or ""),
                    str(getattr(symbol, "name", "") or ""),
                    str(getattr(symbol, "distance_from_seed", "") or ""),
                )
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(symbol)
    return out


def _layer_presence(expected_file: str, paths: list[str]) -> bool:
    return any(_file_matches(path, expected_file) for path in paths)


def _expected_file_layers(result: QuestionResult) -> list[dict[str, Any]]:
    layers: list[dict[str, Any]] = []
    for expected in result.expected_files:
        present = {
            "seed": _layer_presence(expected, result.seed_files),
            "pool": _layer_presence(expected, result.pool_files),
            "bundle": _layer_presence(expected, result.retrieved_files),
        }
        first_layer = next((name for name in ("seed", "pool", "bundle") if present[name]), None)
        lost_after: list[str] = []
        if present["seed"] and not present["pool"]:
            lost_after.append("seed")
        if present["pool"] and not present["bundle"]:
            lost_after.append("pool")
        layers.append(
            {
                "expected_file": expected,
                **present,
                "first_layer": first_layer or "missing",
                "lost_after": lost_after,
            }
        )
    return layers


def _populate_candidate_audit(
    result: QuestionResult,
    retrieval: Any,
    *,
    top_limit: int = _TOP_CANDIDATE_AUDIT_LIMIT,
) -> None:
    candidates = list(getattr(retrieval, "candidates_for_context", []) or [])
    ranked_candidates = list(getattr(retrieval, "budget_ranked_candidates", []) or candidates)
    rendered_symbols = _unique_rendered_symbols(getattr(retrieval, "bundles", []) or [])

    result.candidate_relation_histogram = _sorted_counter_dict(
        Counter(_candidate_relation(candidate) for candidate in candidates)
    )
    result.bundle_relation_histogram = _sorted_counter_dict(
        Counter(_rendered_symbol_relation(symbol) for symbol in rendered_symbols)
    )
    result.top_candidates = [
        _candidate_audit_row(candidate, rank)
        for rank, candidate in enumerate(ranked_candidates[:top_limit], start=1)
    ]
    result.top_rendered_symbols = [
        _rendered_symbol_audit_row(symbol, rank)
        for rank, symbol in enumerate(rendered_symbols[:top_limit], start=1)
    ]
    selection_trace = getattr(retrieval, "seed_selection_trace", None)
    result.seed_selection = selection_trace.to_dict() if selection_trace is not None else {}
    result.gold_rank_audit = _gold_rank_audit(
        result.expected_spans,
        result.expected_symbols,
        ranked_candidates,
        rendered_symbols,
        getattr(retrieval, "budget_trace", None),
    )
    result.candidate_cohort_audit = _candidate_cohort_audit(
        ranked_candidates,
        expected_files=result.expected_files,
        expected_symbols=result.expected_symbols,
        expected_spans=result.expected_spans,
        intent_matches=result.intent_matches,
        budget_trace=getattr(retrieval, "budget_trace", None),
    )


def _question_result_from_entry(question_entry: dict[str, Any]) -> QuestionResult:
    return QuestionResult(
        question_id=str(question_entry.get("id") or ""),
        repo=str(question_entry.get("repo") or ""),
        workspace_id=None,
        question=str(question_entry.get("question") or ""),
        mechanism=str(question_entry.get("mechanism") or ""),
        expected_files=[str(p) for p in (question_entry.get("expected_files") or [])],
        base_commit=str(question_entry.get("base_commit") or ""),
        expected_symbols=[str(p) for p in (question_entry.get("expected_symbols") or [])],
        expected_spans=_normalise_expected_spans(question_entry),
    )


def _resolve_question_workspace(
    question_entry: dict[str, Any],
    result: QuestionResult,
    workspace_overrides: dict[str, str] | None,
    *,
    use_base_commit_workspace: bool,
    commit_workspace_tenant: str,
) -> str | None:
    repo = result.repo
    overrides = workspace_overrides or {}
    explicit_workspace = str(question_entry.get("workspace_id") or "").strip()
    workspace_id = explicit_workspace or overrides.get(result.question_id) or overrides.get(repo)
    if workspace_id:
        workspace_id = _BENCH_PROFILE.workspace_id(workspace_id)
    elif use_base_commit_workspace and result.base_commit:
        workspace_id = _BENCH_PROFILE.workspace_id(
            f"{commit_workspace_tenant}/{repo}@{result.base_commit[:12]}"
        )
    else:
        workspace_id = REPO_TO_WORKSPACE.get(repo)
    if workspace_id is None:
        result.skipped_reason = f"repo {repo!r} not indexed under axis_python_v1"
        return None
    result.workspace_id = workspace_id
    return workspace_id


def _split_rendered_tokens(bundles: Any, expected: list[str]) -> tuple[int, int]:
    """Token split of the deduped rendered bundle: (expected, other).

    Same uid-dedupe as the old total counter (``rendered_tokens`` is the sum
    of the two halves), attributed by whether the symbol's file matches an
    expected entry. Carries ``_compute_precision``'s caveat: "other" is not
    synonymous with noise, the gold set is recall-oriented.
    """
    seen: set[str] = set()
    match_cache: dict[str, bool] = {}
    expected_total = 0
    other_total = 0
    for bundle in bundles:
        for sym in bundle.all_symbols():
            if sym.uid in seen:
                continue
            seen.add(sym.uid)
            path = sym.file_path or ""
            hit = match_cache.get(path)
            if hit is None:
                hit = any(_file_matches(path, exp) for exp in expected)
                match_cache[path] = hit
            tokens = estimate_text_tokens(sym.code or "")
            if hit:
                expected_total += tokens
            else:
                other_total += tokens
    return expected_total, other_total


def _apply_intent(result: QuestionResult, intent: Any) -> None:
    if not intent:
        return
    result.intent_top_role = intent[0].role
    result.intent_top_similarity = intent[0].similarity
    result.intent_matches = [(m.role, m.similarity) for m in intent]


def _ordered_unique_paths(paths: Any) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path and path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def _populate_recall_layers(result: QuestionResult, retrieval: Any) -> None:
    result.seed_files = retrieval.seed_files
    result.seed_recall, result.seed_matched = _compute_recall(
        result.expected_files, result.seed_files
    )

    result.pool_files = _ordered_unique_paths(
        getattr(cand, "file_path", "") or "" for cand in retrieval.candidates_for_context
    )
    result.pool_recall, result.pool_matched = _compute_recall(
        result.expected_files, result.pool_files
    )

    result.retrieved_files = _ordered_unique_paths(
        sym.file_path or "" for bundle in retrieval.bundles for sym in bundle.all_symbols()
    )
    result.file_recall, result.matched_files = _compute_recall(
        result.expected_files, result.retrieved_files
    )

    result.seed_precision = _compute_precision(result.expected_files, result.seed_files)
    result.pool_precision = _compute_precision(result.expected_files, result.pool_files)
    result.bundle_precision = _compute_precision(result.expected_files, result.retrieved_files)

    seed_candidates = list(getattr(retrieval, "seed_candidates", []) or [])
    pool_candidates = list(getattr(retrieval, "candidates_for_context", []) or [])
    rendered_symbols = _unique_rendered_symbols(getattr(retrieval, "bundles", []) or [])
    result.seed_symbol_recall = _compute_symbol_recall(
        result.expected_symbols,
        seed_candidates,
    )
    result.pool_symbol_recall = _compute_symbol_recall(
        result.expected_symbols,
        pool_candidates,
    )
    result.bundle_symbol_recall = _compute_symbol_recall(
        result.expected_symbols,
        rendered_symbols,
    )
    result.seed_span_owner_recall = _compute_span_owner_recall(
        result.expected_spans,
        seed_candidates,
    )
    result.pool_span_owner_recall = _compute_span_owner_recall(
        result.expected_spans,
        pool_candidates,
    )
    result.bundle_span_owner_recall = _compute_span_owner_recall(
        result.expected_spans,
        rendered_symbols,
    )
    result.seed_span_recall = _compute_span_recall(
        result.expected_spans,
        seed_candidates,
        span_getter=lambda candidate: getattr(candidate, "retrieval_spans", ()) or (),
    )
    result.pool_span_recall = _compute_span_recall(
        result.expected_spans,
        pool_candidates,
        span_getter=lambda candidate: getattr(candidate, "retrieval_spans", ()) or (),
    )
    result.bundle_span_recall = _compute_span_recall(
        result.expected_spans,
        rendered_symbols,
        span_getter=lambda symbol: symbol.effective_rendered_spans(),
    )
    result.lexical_span_score_audit = _lexical_span_score_audit(
        result.expected_spans,
        pool_candidates,
    )


def _run_axis_retrieval_for_question(
    *,
    result: QuestionResult,
    question_entry: dict[str, Any],
    workspace_id: str,
    db: Neo4jClient,
    lance: LanceDBClient,
    timer: _StageTimer,
    top_roles: int,
    per_role_limit: int,
    max_impacted: int,
    intent_threshold: float,
    context_per_seed: int,
    context_seeds_per_role: int | None,
    intent_budget: bool,
    base_token_budget: int,
    render_mode_override: str | None,
    ignore_anchor: bool,
    hook_transparency: bool,
    query_node_rerank: bool,
    query_node_semantic_weight: float,
    query_node_mode_semantic_weight: float,
    query_node_ordering_mode: str,
    query_node_blend_alpha: float,
    query_node_mode_blend_alpha: float,
    query_node_rrf_weight: float,
    query_node_mode_rrf_weight: float,
    query_node_rrf_k: int,
    weighted_intent_role_axis_boost: bool,
    intent_role_axis_boost: float,
    intent_axis_only_share: float,
    role_consensus_score_boost: float,
    role_consensus_max_extra_roles: int,
    role_consensus_min_effective_tokens: int,
    channel_consensus_score_boost: float,
    channel_consensus_max_extra_families: int,
    exact_symbol_score_boost: float,
    channel_consensus_min_effective_tokens: int,
    non_intent_structural_role_soft_cap: int | None,
    token_credit_min_utility_per_token: float | None,
    token_credit_upgrade_min_utility_per_token: float | None,
    token_credit_freeze_at_plateau: bool,
    token_credit_plateau_upgrade_reserve_share: float,
    node_semantic_utility_weight: float,
    rank_decay_body_allocation: bool,
    rank_decay_head_share: float,
    rank_decay_rate: float,
    rank_decay_max_coverage_share: float,
    decoupled_symbol_body_allocation: bool,
    decoupled_seed_span_reserve_share: float,
    span_line_rerank: bool,
    span_line_rerank_on_explicit_line_hints: bool,
    span_rank_max_symbols: int,
    span_rank_max_candidates_per_symbol: int,
    span_rank_max_body_lines: int,
    context_semantic_expansion: bool,
    context_semantic_expansion_alpha: float,
    context_semantic_expansion_structural_reserve: int,
    evidence_graph_fanout: bool,
    evidence_graph_fanout_min: int,
    evidence_graph_fanout_protected_head: int,
    lexical_retrieval: bool,
    semantic_chunk_retrieval: bool,
    hybrid_seed_limit: int,
    pregraph_lexical_span_probe: bool,
    lexical_span_probe_max_symbols: int,
    lexical_span_probe_max_windows_per_symbol: int,
    lexical_span_probe_window_lines: int,
    lexical_span_utility_weight: float,
) -> Any:
    return run_axis_retrieval(
        result.question,
        workspace_id=workspace_id,
        db=db,
        lance=lance,
        config=AxisRetrievalConfig(
            top_roles=top_roles,
            per_role_limit=per_role_limit,
            max_impacted=max_impacted,
            intent_threshold=intent_threshold,
            with_context=True,
            context_per_seed=context_per_seed,
            context_seeds_per_role=context_seeds_per_role,
            intent_budget=intent_budget,
            base_token_budget=base_token_budget,
            render_mode_override=render_mode_override,
            anchor_path=(
                None if ignore_anchor else (str(question_entry.get("anchor") or "") or None)
            ),
            anchor_symbol=(str(question_entry.get("symbol") or "") or None),
            hook_transparency=hook_transparency,
            query_node_rerank=query_node_rerank,
            query_node_semantic_weight=query_node_semantic_weight,
            query_node_mode_semantic_weight=query_node_mode_semantic_weight,
            query_node_ordering_mode=query_node_ordering_mode,
            query_node_blend_alpha=query_node_blend_alpha,
            query_node_mode_blend_alpha=query_node_mode_blend_alpha,
            query_node_rrf_weight=query_node_rrf_weight,
            query_node_mode_rrf_weight=query_node_mode_rrf_weight,
            query_node_rrf_k=query_node_rrf_k,
            weighted_intent_role_axis_boost=weighted_intent_role_axis_boost,
            intent_role_axis_boost=intent_role_axis_boost,
            intent_axis_only_share=intent_axis_only_share,
            role_consensus_score_boost=role_consensus_score_boost,
            role_consensus_max_extra_roles=role_consensus_max_extra_roles,
            role_consensus_min_effective_tokens=(role_consensus_min_effective_tokens),
            channel_consensus_score_boost=channel_consensus_score_boost,
            channel_consensus_max_extra_families=(channel_consensus_max_extra_families),
            exact_symbol_score_boost=exact_symbol_score_boost,
            channel_consensus_min_effective_tokens=(channel_consensus_min_effective_tokens),
            non_intent_structural_role_soft_cap=(non_intent_structural_role_soft_cap),
            # Report-only transaction capture. TokenCreditTrace records
            # accepted purchases but never changes selection.
            capture_budget_trace=True,
            token_credit_min_utility_per_token=token_credit_min_utility_per_token,
            token_credit_upgrade_min_utility_per_token=(token_credit_upgrade_min_utility_per_token),
            token_credit_freeze_at_plateau=token_credit_freeze_at_plateau,
            token_credit_plateau_upgrade_reserve_share=(token_credit_plateau_upgrade_reserve_share),
            node_semantic_utility_weight=node_semantic_utility_weight,
            rank_decay_body_allocation=rank_decay_body_allocation,
            rank_decay_head_share=rank_decay_head_share,
            rank_decay_rate=rank_decay_rate,
            rank_decay_max_coverage_share=rank_decay_max_coverage_share,
            decoupled_symbol_body_allocation=decoupled_symbol_body_allocation,
            decoupled_seed_span_reserve_share=decoupled_seed_span_reserve_share,
            span_line_rerank=span_line_rerank,
            span_line_rerank_on_explicit_line_hints=(span_line_rerank_on_explicit_line_hints),
            span_rank_max_symbols=span_rank_max_symbols,
            span_rank_max_candidates_per_symbol=span_rank_max_candidates_per_symbol,
            span_rank_max_body_lines=span_rank_max_body_lines,
            context_semantic_expansion=context_semantic_expansion,
            context_semantic_expansion_alpha=context_semantic_expansion_alpha,
            context_semantic_expansion_structural_reserve=(
                context_semantic_expansion_structural_reserve
            ),
            evidence_graph_fanout=evidence_graph_fanout,
            evidence_graph_fanout_min=evidence_graph_fanout_min,
            evidence_graph_fanout_protected_head=(evidence_graph_fanout_protected_head),
            lexical_retrieval=lexical_retrieval,
            semantic_chunk_retrieval=semantic_chunk_retrieval,
            hybrid_seed_limit=hybrid_seed_limit,
            pregraph_lexical_span_probe=pregraph_lexical_span_probe,
            lexical_span_probe_max_symbols=lexical_span_probe_max_symbols,
            lexical_span_probe_max_windows_per_symbol=(lexical_span_probe_max_windows_per_symbol),
            lexical_span_probe_window_lines=lexical_span_probe_window_lines,
            lexical_span_utility_weight=lexical_span_utility_weight,
            trace=timer,
        ),
    )


def run_question(
    question_entry: dict[str, Any],
    *,
    db: Neo4jClient,
    lance: LanceDBClient,
    top_roles: int,
    per_role_limit: int,
    max_impacted: int,
    intent_threshold: float,
    context_per_seed: int,
    context_seeds_per_role: int | None = 7,
    intent_budget: bool = True,
    base_token_budget: int = 6000,
    render_mode_override: str | None = None,
    ignore_anchor: bool = False,
    hook_transparency: bool = False,
    query_node_rerank: bool = True,
    query_node_semantic_weight: float = 0.20,
    query_node_mode_semantic_weight: float = 0.05,
    query_node_ordering_mode: str = "calibrated_blend",
    query_node_blend_alpha: float = 0.40,
    query_node_mode_blend_alpha: float = 0.10,
    query_node_rrf_weight: float = 1.0,
    query_node_mode_rrf_weight: float = 0.25,
    query_node_rrf_k: int = 60,
    weighted_intent_role_axis_boost: bool = False,
    intent_role_axis_boost: float = 0.15,
    intent_axis_only_share: float = 0.25,
    role_consensus_score_boost: float = 0.05,
    role_consensus_max_extra_roles: int = 2,
    role_consensus_min_effective_tokens: int = 10_000,
    channel_consensus_score_boost: float = 0.0,
    channel_consensus_max_extra_families: int = 2,
    exact_symbol_score_boost: float = 0.0,
    channel_consensus_min_effective_tokens: int = 10_000,
    non_intent_structural_role_soft_cap: int | None = None,
    token_credit_min_utility_per_token: float | None = None,
    token_credit_upgrade_min_utility_per_token: float | None = 0.00025,
    token_credit_freeze_at_plateau: bool = False,
    token_credit_plateau_upgrade_reserve_share: float = 0.0,
    node_semantic_utility_weight: float = 0.0,
    rank_decay_body_allocation: bool = True,
    rank_decay_head_share: float = 0.15,
    rank_decay_rate: float = 0.75,
    rank_decay_max_coverage_share: float = 0.65,
    decoupled_symbol_body_allocation: bool = True,
    decoupled_seed_span_reserve_share: float = 0.10,
    span_line_rerank: bool = False,
    span_line_rerank_on_explicit_line_hints: bool = False,
    span_rank_max_symbols: int = 48,
    span_rank_max_candidates_per_symbol: int = 24,
    span_rank_max_body_lines: int = 6,
    context_semantic_expansion: bool = True,
    context_semantic_expansion_alpha: float = 0.70,
    context_semantic_expansion_structural_reserve: int = 1,
    evidence_graph_fanout: bool = True,
    evidence_graph_fanout_min: int = 2,
    evidence_graph_fanout_protected_head: int = 5,
    lexical_retrieval: bool = True,
    semantic_chunk_retrieval: bool = True,
    hybrid_seed_limit: int = 12,
    pregraph_lexical_span_probe: bool = True,
    lexical_span_probe_max_symbols: int = 96,
    lexical_span_probe_max_windows_per_symbol: int = 3,
    lexical_span_probe_window_lines: int = 6,
    lexical_span_utility_weight: float = 0.15,
    workspace_overrides: dict[str, str] | None = None,
    use_base_commit_workspace: bool = True,
    commit_workspace_tenant: str = _BENCH_COMMIT_TENANT,
) -> QuestionResult:
    result = _question_result_from_entry(question_entry)
    workspace_id = _resolve_question_workspace(
        question_entry,
        result,
        workspace_overrides,
        use_base_commit_workspace=use_base_commit_workspace,
        commit_workspace_tenant=commit_workspace_tenant,
    )
    if workspace_id is None:
        return result
    if use_base_commit_workspace and result.base_commit:
        count_symbols = getattr(lance, "count_symbols_workspace", None)
        if callable(count_symbols) and int(count_symbols(workspace_id)) <= 0:
            result.skipped_reason = f"exact-commit workspace {workspace_id!r} has no symbol rows"
            return result

    # The whole read-side pipeline is the canonical ``run_axis_retrieval``
    # — the same function the ``/ask/axis`` endpoint runs, so this
    # benchmark validates that exact code. The production default is the
    # evidence-aware soft cap of seven; ``context_seeds_per_role=None`` remains
    # the explicit historical/full-pool arm. The seed / pool / bundle recall
    # layers below read straight off the layered result.
    #
    # The default benchmark path measures production /ask budgeting: full
    # ranked scope, then the echelon-2 marginal token-credit packer. The seed /
    # pool layers are unaffected (budgeting lives in context expansion); only
    # the bundle layer reflects the cost.
    timer = _StageTimer()
    retrieval = _run_axis_retrieval_for_question(
        result=result,
        question_entry=question_entry,
        workspace_id=workspace_id,
        db=db,
        lance=lance,
        timer=timer,
        top_roles=top_roles,
        per_role_limit=per_role_limit,
        max_impacted=max_impacted,
        intent_threshold=intent_threshold,
        context_per_seed=context_per_seed,
        context_seeds_per_role=context_seeds_per_role,
        intent_budget=intent_budget,
        base_token_budget=base_token_budget,
        render_mode_override=render_mode_override,
        ignore_anchor=ignore_anchor,
        hook_transparency=hook_transparency,
        query_node_rerank=query_node_rerank,
        query_node_semantic_weight=query_node_semantic_weight,
        query_node_mode_semantic_weight=query_node_mode_semantic_weight,
        query_node_ordering_mode=query_node_ordering_mode,
        query_node_blend_alpha=query_node_blend_alpha,
        query_node_mode_blend_alpha=query_node_mode_blend_alpha,
        query_node_rrf_weight=query_node_rrf_weight,
        query_node_mode_rrf_weight=query_node_mode_rrf_weight,
        query_node_rrf_k=query_node_rrf_k,
        weighted_intent_role_axis_boost=weighted_intent_role_axis_boost,
        intent_role_axis_boost=intent_role_axis_boost,
        intent_axis_only_share=intent_axis_only_share,
        role_consensus_score_boost=role_consensus_score_boost,
        role_consensus_max_extra_roles=role_consensus_max_extra_roles,
        role_consensus_min_effective_tokens=role_consensus_min_effective_tokens,
        channel_consensus_score_boost=channel_consensus_score_boost,
        channel_consensus_max_extra_families=(channel_consensus_max_extra_families),
        exact_symbol_score_boost=exact_symbol_score_boost,
        channel_consensus_min_effective_tokens=(channel_consensus_min_effective_tokens),
        non_intent_structural_role_soft_cap=(non_intent_structural_role_soft_cap),
        token_credit_min_utility_per_token=token_credit_min_utility_per_token,
        token_credit_upgrade_min_utility_per_token=(token_credit_upgrade_min_utility_per_token),
        token_credit_freeze_at_plateau=token_credit_freeze_at_plateau,
        token_credit_plateau_upgrade_reserve_share=(token_credit_plateau_upgrade_reserve_share),
        node_semantic_utility_weight=node_semantic_utility_weight,
        rank_decay_body_allocation=rank_decay_body_allocation,
        rank_decay_head_share=rank_decay_head_share,
        rank_decay_rate=rank_decay_rate,
        rank_decay_max_coverage_share=rank_decay_max_coverage_share,
        decoupled_symbol_body_allocation=decoupled_symbol_body_allocation,
        decoupled_seed_span_reserve_share=decoupled_seed_span_reserve_share,
        span_line_rerank=span_line_rerank,
        span_line_rerank_on_explicit_line_hints=(span_line_rerank_on_explicit_line_hints),
        span_rank_max_symbols=span_rank_max_symbols,
        span_rank_max_candidates_per_symbol=span_rank_max_candidates_per_symbol,
        span_rank_max_body_lines=span_rank_max_body_lines,
        context_semantic_expansion=context_semantic_expansion,
        context_semantic_expansion_alpha=context_semantic_expansion_alpha,
        context_semantic_expansion_structural_reserve=(
            context_semantic_expansion_structural_reserve
        ),
        evidence_graph_fanout=evidence_graph_fanout,
        evidence_graph_fanout_min=evidence_graph_fanout_min,
        evidence_graph_fanout_protected_head=evidence_graph_fanout_protected_head,
        lexical_retrieval=lexical_retrieval,
        semantic_chunk_retrieval=semantic_chunk_retrieval,
        hybrid_seed_limit=hybrid_seed_limit,
        pregraph_lexical_span_probe=pregraph_lexical_span_probe,
        lexical_span_probe_max_symbols=lexical_span_probe_max_symbols,
        lexical_span_probe_max_windows_per_symbol=(lexical_span_probe_max_windows_per_symbol),
        lexical_span_probe_window_lines=lexical_span_probe_window_lines,
        lexical_span_utility_weight=lexical_span_utility_weight,
    )
    # Post-processing cost: the ``context`` stage is the build_context graph
    # expansion + per-uid code fetch; rendered_tokens is the token volume of
    # the DEDUPED bundle code (after any signature trim / budget cut) — i.e.
    # the prompt the adapter actually hands the LLM, deduped by uid exactly as
    # ``axis_bundles_to_prompt_context`` does (no double-counting shared
    # neighbours across bundles).
    result.context_seconds = round(timer.durations.get("context", 0.0), 4)
    result.lexical_span_probe_seconds = round(
        timer.durations.get("pregraph_lexical_span_probe", 0.0), 4
    )
    result.lexical_span_probe = (
        retrieval.lexical_span_probe_trace.to_dict()
        if retrieval.lexical_span_probe_trace is not None
        else {}
    )
    result.expected_tokens, result.other_tokens = _split_rendered_tokens(
        retrieval.bundles, result.expected_files
    )
    result.rendered_tokens = result.expected_tokens + result.other_tokens
    result.token_precision = (
        result.expected_tokens / result.rendered_tokens if result.rendered_tokens else 0.0
    )
    _apply_intent(result, retrieval.intent)
    result.candidate_count = len(retrieval.candidates_for_context)
    _populate_recall_layers(result, retrieval)
    _populate_candidate_audit(result, retrieval)
    result.expected_file_layers = _expected_file_layers(result)
    return result


def run_axis_pack(
    questions: list[dict[str, Any]],
    *,
    db: Neo4jClient | None = None,
    lance: LanceDBClient | None = None,
    top_roles: int = 3,
    per_role_limit: int = 7,
    max_impacted: int = 35,
    intent_threshold: float = 0.20,
    context_per_seed: int = 6,
    context_seeds_per_role: int | None = 7,
    intent_budget: bool = True,
    base_token_budget: int = 6000,
    render_mode_override: str | None = None,
    ignore_anchor: bool = False,
    hook_transparency: bool = True,
    workspace_overrides: dict[str, str] | None = None,
    use_base_commit_workspace: bool = True,
    commit_workspace_tenant: str = _BENCH_COMMIT_TENANT,
) -> list[QuestionResult]:
    """Run the axis benchmark over an in-memory question list."""
    owned_db = db is None
    active_db = db if db is not None else Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    active_lance = (
        lance if lance is not None else LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)
    )
    results: list[QuestionResult] = []
    try:
        for entry in questions:
            results.append(
                run_question(
                    entry,
                    db=active_db,
                    lance=active_lance,
                    top_roles=top_roles,
                    per_role_limit=per_role_limit,
                    max_impacted=max_impacted,
                    intent_threshold=intent_threshold,
                    context_per_seed=context_per_seed,
                    context_seeds_per_role=context_seeds_per_role,
                    intent_budget=intent_budget,
                    base_token_budget=base_token_budget,
                    render_mode_override=render_mode_override,
                    ignore_anchor=ignore_anchor,
                    hook_transparency=hook_transparency,
                    workspace_overrides=workspace_overrides,
                    use_base_commit_workspace=use_base_commit_workspace,
                    commit_workspace_tenant=commit_workspace_tenant,
                )
            )
    finally:
        if owned_db:
            active_db.close()
    return results


def assert_p7_baseline(summary: dict[str, Any], baseline: dict[str, Any]) -> None:
    """Raise AssertionError when an axis summary regresses below the P7 gate."""
    expected_scored = int(baseline["scored"])
    actual_scored = int(summary.get("scored", 0))
    assert actual_scored == expected_scored, (
        f"expected {expected_scored} scored questions, got {actual_scored} "
        f"(skipped={summary.get('skipped', 0)})"
    )

    def _check_min(key: str, label: str) -> None:
        floor = float(baseline[f"min_{key}"])
        actual = float(summary.get(key, 0.0))
        assert actual + 1e-9 >= floor, (
            f"{label} {actual:.3f} below P7 floor {floor:.3f}. "
            "Refresh QA/fixtures/baselines/p7_surgical_context_axis.json "
            "only after an intentional engine improvement."
        )

    _check_min("overall_mean_recall", "bundle recall")
    _check_min("overall_seed_mean_recall", "seed recall")
    _check_min("overall_pool_mean_recall", "pool recall")

    max_zero = int(baseline["max_zero_recall_count"])
    zero_count = int(summary.get("zero_recall_questions", 0))
    assert zero_count <= max_zero, (
        f"{zero_count} questions had zero bundle recall (max allowed {max_zero})"
    )

    min_full = int(baseline["min_full_recall_count"])
    full_count = int(summary.get("full_recall_questions", 0))
    assert full_count >= min_full, (
        f"{full_count} questions reached full bundle recall (min required {min_full})"
    )

    per_question = baseline.get("per_question_min_file_recall") or {}
    by_id = {r["question_id"]: r for r in summary.get("per_question", [])}
    for qid, floor in per_question.items():
        row = by_id.get(qid)
        assert row is not None, f"missing per-question row for {qid!r} in summary"
        actual = float(row.get("file_recall", 0.0))
        assert actual + 1e-9 >= float(floor), (
            f"{qid} bundle recall {actual:.3f} below floor {float(floor):.3f}"
        )


def _rank_percentile(values: list[int], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * min(1.0, max(0.0, quantile))
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _aggregate_gold_rank_funnel(
    results: list[QuestionResult],
    *,
    row_key: str,
) -> dict[str, Any]:
    audits = [result.gold_rank_audit for result in results if result.gold_rank_audit]
    rows = [row for audit in audits for row in list(audit.get(row_key, []) or [])]

    def stage(field: str) -> dict[str, Any]:
        ranks = [int(row[field]) for row in rows if row.get(field) is not None]
        return {
            "present": len(ranks),
            "recall": len(ranks) / len(rows) if rows else 0.0,
            "mean_rank": sum(ranks) / len(ranks) if ranks else None,
            "median_rank": _rank_percentile(ranks, 0.50),
            "p90_rank": _rank_percentile(ranks, 0.90),
            "recall_at": {
                str(cutoff): (sum(rank <= cutoff for rank in ranks) / len(rows) if rows else 0.0)
                for cutoff in _GOLD_RANK_CUTOFFS
            },
        }

    return {
        "gold_count": len(rows),
        "candidate_count": sum(int(audit.get("candidate_count", 0)) for audit in audits),
        "coverage_count": sum(int(audit.get("coverage_count", 0)) for audit in audits),
        "rendered_symbol_count": sum(
            int(audit.get("rendered_symbol_count", 0)) for audit in audits
        ),
        "utility": stage("utility_rank"),
        "coverage": stage("coverage_rank"),
        "final": stage("final_rank"),
        "retrieval_missing_but_final": sum(
            row.get("utility_rank") is None and row.get("final_rank") is not None for row in rows
        ),
        "utility_not_coverage_but_final": sum(
            row.get("utility_rank") is not None
            and row.get("coverage_rank") is None
            and row.get("final_rank") is not None
            for row in rows
        ),
        "coverage_not_final": sum(
            row.get("coverage_rank") is not None and row.get("final_rank") is None for row in rows
        ),
    }


def _aggregate_candidate_rank_spend(results: list[QuestionResult]) -> dict[str, Any]:
    audits = [
        spend
        for result in results
        if result.gold_rank_audit and (spend := result.gold_rank_audit.get("candidate_rank_spend"))
    ]
    totals = {
        field: sum(int(audit.get(field, 0)) for audit in audits)
        for field in ("coverage_tokens", "upgrade_tokens", "other_tokens")
    }
    bucket_labels = [label for label, _lower, _upper in _RANK_SPEND_BUCKETS] + ["unranked"]
    by_rank: dict[str, dict[str, int | float]] = {}
    for label in bucket_labels:
        row: dict[str, int | float] = {
            field: sum(
                int((audit.get("by_rank", {}).get(label, {}) or {}).get(field, 0))
                for audit in audits
            )
            for field in (
                "coverage_tokens",
                "upgrade_tokens",
                "other_tokens",
                "transactions",
            )
        }
        row["upgrade_share"] = (
            int(row["upgrade_tokens"]) / totals["upgrade_tokens"]
            if totals["upgrade_tokens"]
            else 0.0
        )
        by_rank[label] = row

    upgrade_tokens_at = {
        str(cutoff): sum(
            int((audit.get("upgrade_tokens_at", {}) or {}).get(str(cutoff), 0)) for audit in audits
        )
        for cutoff in _GOLD_RANK_CUTOFFS
    }
    fanout_tiers: Counter[str] = Counter()
    fanout_limits: Counter[str] = Counter()
    fanout_questions = 0
    upgrade_attribution: dict[str, Counter[str]] = {
        "scope": Counter(),
        "evidence": Counter(),
        "edge_type": Counter(),
        "depth": Counter(),
        "scope_evidence": Counter(),
    }
    for audit in audits:
        for dimension, counter in upgrade_attribution.items():
            counter.update((audit.get("upgrade_attribution", {}) or {}).get(dimension, {}) or {})
        fanout = audit.get("graph_fanout", {}) or {}
        if not fanout.get("base_limit"):
            continue
        fanout_questions += 1
        fanout_tiers.update(fanout.get("tier_counts", {}) or {})
        fanout_limits.update(fanout.get("limit_counts", {}) or {})
    return {
        "questions": len(audits),
        "allocation_mode_counts": dict(
            Counter(str(audit.get("allocation_mode") or "legacy") for audit in audits)
        ),
        "mean_coverage_share": (
            sum(float(audit.get("coverage_share", 0.0)) for audit in audits) / len(audits)
            if audits
            else 0.0
        ),
        "mean_rank_decay_coverage_threshold": (
            sum(float(audit.get("rank_decay_coverage_threshold", 0.0)) for audit in audits)
            / len(audits)
            if audits
            else 0.0
        ),
        "graph_fanout": {
            "questions": fanout_questions,
            "tier_counts": dict(fanout_tiers),
            "limit_counts": dict(fanout_limits),
        },
        "upgrade_attribution": {
            dimension: dict(counter.most_common())
            for dimension, counter in upgrade_attribution.items()
        },
        **totals,
        "upgrade_tokens_at": upgrade_tokens_at,
        "upgrade_share_at": {
            cutoff: tokens / totals["upgrade_tokens"] if totals["upgrade_tokens"] else 0.0
            for cutoff, tokens in upgrade_tokens_at.items()
        },
        "by_rank": by_rank,
    }


_CANDIDATE_COHORT_ADDITIVE_FIELDS = (
    "candidates",
    "owner_gold",
    "symbol_gold",
    "file_gold",
    "other",
    "top5",
    "top10",
    "rank_sum",
    "exact_gold_top5",
    "exact_gold_top10",
    "exact_gold_rank_sum",
    "labelled_gold_top10",
    "labelled_gold_rank_sum",
    "coverage_candidates",
    "upgraded_candidates",
    "coverage_tokens",
    "upgrade_tokens",
)


def _aggregate_candidate_cohorts(results: list[QuestionResult]) -> dict[str, Any]:
    audits = [result.candidate_cohort_audit for result in results if result.candidate_cohort_audit]

    def merge_row(target: dict[str, int | float], source: dict[str, Any]) -> None:
        for metric in _CANDIDATE_COHORT_ADDITIVE_FIELDS:
            target[metric] += int(source.get(metric, 0) or 0)

    def merge_group(
        target: defaultdict[str, dict[str, int | float]],
        source: dict[str, Any],
        *,
        prefix: str = "",
    ) -> None:
        for key, row in source.items():
            merge_row(target[f"{prefix}{key}"], row)

    dimensions = (
        "by_role",
        "by_role_signature",
        "by_role_intent_alignment",
        "by_axis",
        "by_axis_basis",
        "by_intent_alignment",
        "by_channel",
        "by_channel_signature",
        "by_channel_count",
        "by_exact_symbol_prior",
        "by_role_axis_intent",
    )
    groups: dict[str, defaultdict[str, dict[str, int | float]]] = {
        dimension: defaultdict(_new_candidate_cohort_row) for dimension in dimensions
    }
    by_top_intent: defaultdict[str, dict[str, int | float]] = defaultdict(_new_candidate_cohort_row)
    by_top_intent_role: defaultdict[str, dict[str, int | float]] = defaultdict(
        _new_candidate_cohort_row
    )
    by_top_intent_axis: defaultdict[str, dict[str, int | float]] = defaultdict(
        _new_candidate_cohort_row
    )
    totals = _new_candidate_cohort_row()
    intent_role_questions: Counter[str] = Counter()
    intent_axis_questions: Counter[str] = Counter()

    for audit in audits:
        for dimension in dimensions:
            merge_group(groups[dimension], audit.get(dimension, {}) or {})
        merge_row(totals, audit.get("totals", {}) or {})
        intent_roles_for_question = list(audit.get("intent_roles", []) or [])
        top_intent = str(intent_roles_for_question[0]) if intent_roles_for_question else "(none)"
        intent_role_questions.update(intent_roles_for_question or ["(none)"])
        intent_axis_questions.update(list(audit.get("intent_axes", []) or ["axisless"]))
        merge_row(by_top_intent[top_intent], audit.get("totals", {}) or {})
        merge_group(
            by_top_intent_role,
            audit.get("by_role", {}) or {},
            prefix=f"{top_intent}|",
        )
        merge_group(
            by_top_intent_axis,
            audit.get("by_axis", {}) or {},
            prefix=f"{top_intent}|",
        )

    def finalise_group(
        rows: dict[str, dict[str, int | float]],
    ) -> dict[str, dict[str, int | float]]:
        return {
            key: _finalise_candidate_cohort_row(dict(row))
            for key, row in sorted(
                rows.items(),
                key=lambda item: (-int(item[1].get("candidates", 0)), item[0]),
            )
        }

    return {
        "questions": len(audits),
        "multi_role_candidates": sum(
            int(audit.get("multi_role_candidates", 0)) for audit in audits
        ),
        "intent_role_question_counts": _sorted_counter_dict(intent_role_questions),
        "intent_axis_question_counts": _sorted_counter_dict(intent_axis_questions),
        "totals": _finalise_candidate_cohort_row(totals),
        **{dimension: finalise_group(rows) for dimension, rows in groups.items()},
        "by_top_intent": finalise_group(by_top_intent),
        "by_top_intent_role": finalise_group(by_top_intent_role),
        "by_top_intent_axis": finalise_group(by_top_intent_axis),
    }


def summarise(results: list[QuestionResult]) -> dict[str, Any]:
    scored = [r for r in results if r.skipped_reason is None]
    skipped = [r for r in results if r.skipped_reason is not None]
    overall_recall = sum(r.file_recall for r in scored) / len(scored) if scored else 0.0
    full_recall_count = sum(1 for r in scored if r.file_recall >= 1.0 - 1e-9)
    zero_recall_count = sum(1 for r in scored if r.file_recall <= 1e-9)

    # Three-layer aggregates: seed (pure retrieval) ≤ pool (after the
    # pool expander) ≤ bundle (after per-candidate context expansion).
    # Two masking lists, one per expander layer — a question is "masked"
    # by a layer when that layer scores it higher than the layer below,
    # i.e. the layer is covering a miss the cheaper layer below actually
    # has. They name exactly the files (and questions) a collapse of that
    # layer would expose, so the gap can be moved down a layer first.
    def _mean(attr: str) -> float:
        return sum(getattr(r, attr) for r in scored) / len(scored) if scored else 0.0

    def _mean_with_gold(attr: str, gold_attr: str) -> float:
        eligible = [result for result in scored if getattr(result, gold_attr)]
        return (
            sum(float(getattr(result, attr)) for result in eligible) / len(eligible)
            if eligible
            else 0.0
        )

    overall_seed_recall = _mean("seed_recall")
    seed_full_count = sum(1 for r in scored if r.seed_recall >= 1.0 - 1e-9)
    seed_zero_count = sum(1 for r in scored if r.seed_recall <= 1e-9)
    overall_pool_recall = _mean("pool_recall")
    pool_full_count = sum(1 for r in scored if r.pool_recall >= 1.0 - 1e-9)
    pool_zero_count = sum(1 for r in scored if r.pool_recall <= 1e-9)

    masked_by_pool_expander = [
        {
            "question_id": r.question_id,
            "repo": r.repo,
            "seed_recall": round(r.seed_recall, 3),
            "pool_recall": round(r.pool_recall, 3),
            "added_files": sorted(set(r.pool_matched) - set(r.seed_matched)),
        }
        for r in scored
        if r.pool_recall > r.seed_recall + 1e-9
    ]
    masked_by_context_expander = [
        {
            "question_id": r.question_id,
            "repo": r.repo,
            "pool_recall": round(r.pool_recall, 3),
            "bundle_recall": round(r.file_recall, 3),
            "added_files": sorted(set(r.matched_files) - set(r.pool_matched)),
        }
        for r in scored
        if r.file_recall > r.pool_recall + 1e-9
    ]

    by_repo: defaultdict[str, list[QuestionResult]] = defaultdict(list)
    for r in scored:
        by_repo[r.repo].append(r)

    def _items_mean_with_gold(items: list[QuestionResult], attr: str, gold_attr: str) -> float:
        eligible = [result for result in items if getattr(result, gold_attr)]
        return (
            sum(float(getattr(result, attr)) for result in eligible) / len(eligible)
            if eligible
            else 0.0
        )

    by_repo_summary = {
        repo: {
            "questions": len(items),
            "mean_recall": sum(r.file_recall for r in items) / len(items),
            "full_recall": sum(1 for r in items if r.file_recall >= 1.0 - 1e-9),
            "zero_recall": sum(1 for r in items if r.file_recall <= 1e-9),
            "seed_mean_recall": sum(r.seed_recall for r in items) / len(items),
            "seed_full_recall": sum(1 for r in items if r.seed_recall >= 1.0 - 1e-9),
            "pool_mean_recall": sum(r.pool_recall for r in items) / len(items),
            "pool_full_recall": sum(1 for r in items if r.pool_recall >= 1.0 - 1e-9),
            "mean_precision": sum(r.bundle_precision for r in items) / len(items),
            "mean_token_precision": sum(r.token_precision for r in items) / len(items),
            "mean_expected_tokens": sum(r.expected_tokens for r in items) / len(items),
            "mean_other_tokens": sum(r.other_tokens for r in items) / len(items),
            "symbol_gold_questions": sum(1 for r in items if r.expected_symbols),
            "span_gold_questions": sum(1 for r in items if r.expected_spans),
            "seed_symbol_recall": _items_mean_with_gold(
                items, "seed_symbol_recall", "expected_symbols"
            ),
            "pool_symbol_recall": _items_mean_with_gold(
                items, "pool_symbol_recall", "expected_symbols"
            ),
            "bundle_symbol_recall": _items_mean_with_gold(
                items, "bundle_symbol_recall", "expected_symbols"
            ),
            "seed_span_owner_recall": _items_mean_with_gold(
                items, "seed_span_owner_recall", "expected_spans"
            ),
            "pool_span_owner_recall": _items_mean_with_gold(
                items, "pool_span_owner_recall", "expected_spans"
            ),
            "bundle_span_owner_recall": _items_mean_with_gold(
                items, "bundle_span_owner_recall", "expected_spans"
            ),
            "seed_span_recall": _items_mean_with_gold(items, "seed_span_recall", "expected_spans"),
            "pool_span_recall": _items_mean_with_gold(items, "pool_span_recall", "expected_spans"),
            "bundle_span_recall": _items_mean_with_gold(
                items, "bundle_span_recall", "expected_spans"
            ),
        }
        for repo, items in sorted(by_repo.items())
    }

    by_intent: Counter[str] = Counter()
    for r in scored:
        by_intent[r.intent_top_role or "(no_role)"] += 1

    candidate_relation_totals: Counter[str] = Counter()
    bundle_relation_totals: Counter[str] = Counter()
    for r in scored:
        candidate_relation_totals.update(r.candidate_relation_histogram)
        bundle_relation_totals.update(r.bundle_relation_histogram)

    lexical_score_audits = [
        result.lexical_span_score_audit for result in scored if result.lexical_span_score_audit
    ]
    lexical_score_auc_values = [
        float(audit["auc"]) for audit in lexical_score_audits if audit.get("auc") is not None
    ]
    owner_gold_rank_funnel = _aggregate_gold_rank_funnel(scored, row_key="owners")
    symbol_gold_rank_funnel = _aggregate_gold_rank_funnel(scored, row_key="symbols")
    candidate_rank_spend = _aggregate_candidate_rank_spend(scored)
    candidate_cohorts = _aggregate_candidate_cohorts(scored)

    return {
        "scored": len(scored),
        "skipped": len(skipped),
        "overall_mean_recall": overall_recall,
        "full_recall_questions": full_recall_count,
        "zero_recall_questions": zero_recall_count,
        "overall_seed_mean_recall": overall_seed_recall,
        "seed_full_recall_questions": seed_full_count,
        "seed_zero_recall_questions": seed_zero_count,
        "overall_pool_mean_recall": overall_pool_recall,
        "pool_full_recall_questions": pool_full_count,
        "pool_zero_recall_questions": pool_zero_count,
        "overall_seed_symbol_recall": _mean_with_gold("seed_symbol_recall", "expected_symbols"),
        "overall_pool_symbol_recall": _mean_with_gold("pool_symbol_recall", "expected_symbols"),
        "overall_bundle_symbol_recall": _mean_with_gold("bundle_symbol_recall", "expected_symbols"),
        "overall_seed_span_owner_recall": _mean_with_gold(
            "seed_span_owner_recall", "expected_spans"
        ),
        "overall_pool_span_owner_recall": _mean_with_gold(
            "pool_span_owner_recall", "expected_spans"
        ),
        "overall_bundle_span_owner_recall": _mean_with_gold(
            "bundle_span_owner_recall", "expected_spans"
        ),
        "overall_seed_span_recall": _mean_with_gold("seed_span_recall", "expected_spans"),
        "overall_pool_span_recall": _mean_with_gold("pool_span_recall", "expected_spans"),
        "overall_bundle_span_recall": _mean_with_gold("bundle_span_recall", "expected_spans"),
        "symbol_gold_questions": sum(1 for result in scored if result.expected_symbols),
        "span_gold_questions": sum(1 for result in scored if result.expected_spans),
        # Report-only precision telemetry (never a P7 gate — the gold set is
        # recall-oriented; see ``_compute_precision``).
        "overall_seed_mean_precision": _mean("seed_precision"),
        "overall_pool_mean_precision": _mean("pool_precision"),
        "overall_mean_precision": _mean("bundle_precision"),
        "overall_mean_token_precision": _mean("token_precision"),
        "overall_mean_expected_tokens": _mean("expected_tokens"),
        "overall_mean_other_tokens": _mean("other_tokens"),
        "masked_by_pool_expander": masked_by_pool_expander,
        "masked_by_context_expander": masked_by_context_expander,
        "per_repo": by_repo_summary,
        "intent_top_role_counts": dict(by_intent),
        "candidate_relation_totals": _sorted_counter_dict(candidate_relation_totals),
        "bundle_relation_totals": _sorted_counter_dict(bundle_relation_totals),
        "gold_rank_funnel": {
            "owners": owner_gold_rank_funnel,
            "symbols": symbol_gold_rank_funnel,
        },
        "candidate_rank_token_spend": candidate_rank_spend,
        "candidate_cohorts": candidate_cohorts,
        "skipped_reasons": Counter(r.skipped_reason for r in skipped),
        # Post-processing cost (the expensive part of the budget cost model).
        "overall_mean_context_seconds": _mean("context_seconds"),
        "max_context_seconds": max((r.context_seconds for r in scored), default=0.0),
        "overall_mean_lexical_span_probe_seconds": _mean("lexical_span_probe_seconds"),
        "max_lexical_span_probe_seconds": max(
            (r.lexical_span_probe_seconds for r in scored), default=0.0
        ),
        "overall_lexical_span_score_auc": (
            sum(lexical_score_auc_values) / len(lexical_score_auc_values)
            if lexical_score_auc_values
            else 0.0
        ),
        "overall_mean_lexical_span_gold_owner_score": (
            sum(float(audit["mean_gold_owner_score"]) for audit in lexical_score_audits)
            / len(lexical_score_audits)
            if lexical_score_audits
            else 0.0
        ),
        "overall_mean_lexical_span_other_score": (
            sum(float(audit["mean_other_score"]) for audit in lexical_score_audits)
            / len(lexical_score_audits)
            if lexical_score_audits
            else 0.0
        ),
        "overall_mean_rendered_tokens": _mean("rendered_tokens"),
        "max_rendered_tokens": max((r.rendered_tokens for r in scored), default=0),
        "per_question": [
            {
                "question_id": r.question_id,
                "repo": r.repo,
                "file_recall": round(r.file_recall, 4),
                "seed_recall": round(r.seed_recall, 4),
                "pool_recall": round(r.pool_recall, 4),
                "seed_symbol_recall": round(r.seed_symbol_recall, 4),
                "pool_symbol_recall": round(r.pool_symbol_recall, 4),
                "bundle_symbol_recall": round(r.bundle_symbol_recall, 4),
                "seed_span_owner_recall": round(r.seed_span_owner_recall, 4),
                "pool_span_owner_recall": round(r.pool_span_owner_recall, 4),
                "bundle_span_owner_recall": round(r.bundle_span_owner_recall, 4),
                "seed_span_recall": round(r.seed_span_recall, 4),
                "pool_span_recall": round(r.pool_span_recall, 4),
                "bundle_span_recall": round(r.bundle_span_recall, 4),
                "bundle_precision": round(r.bundle_precision, 4),
                "token_precision": round(r.token_precision, 4),
                "expected_tokens": r.expected_tokens,
                "other_tokens": r.other_tokens,
                "lexical_span_probe_seconds": r.lexical_span_probe_seconds,
                "lexical_span_probe": r.lexical_span_probe,
                "lexical_span_score_audit": r.lexical_span_score_audit,
                "gold_rank_audit": r.gold_rank_audit,
            }
            for r in sorted(scored, key=lambda x: x.question_id)
        ],
    }


def _short_report_text(text: str, *, limit: int = 80) -> str:
    compacted = " ".join(str(text or "").split())
    if len(compacted) <= limit:
        return compacted
    return compacted[: limit - 3].rstrip() + "..."


def _format_histogram(histogram: dict[str, int], *, limit: int = 4) -> str:
    if not histogram:
        return "—"
    items = list(histogram.items())[:limit]
    rendered = ", ".join(f"{key}:{count}" for key, count in items)
    if len(histogram) > limit:
        rendered += ", ..."
    return rendered


def _format_top_candidate_rows(rows: list[dict[str, Any]], *, limit: int = 5) -> str:
    if not rows:
        return "—"
    parts: list[str] = []
    for row in rows[:limit]:
        label = row.get("qualified_name") or row.get("name") or row.get("uid") or "?"
        score = row.get("score")
        score_text = f"{score:.2f}" if isinstance(score, int | float) else "?"
        parts.append(
            _short_report_text(
                f"{row.get('relation') or '?'}:{label}({score_text})",
                limit=48,
            )
        )
    if len(rows) > limit:
        parts.append("...")
    return "; ".join(parts)


def _render_markdown(results: list[QuestionResult], summary: dict[str, Any]) -> str:
    lines = [
        "# Axis pipeline — benchmark report",
        "",
        f"- scored questions: **{summary['scored']}**",
        f"- skipped: {summary['skipped']}",
        f"- mean **seed** recall (pure retrieval, no graph walk): "
        f"**{summary.get('overall_seed_mean_recall', 0.0):.3f}** "
        f"({summary.get('seed_full_recall_questions', 0)} full, "
        f"{summary.get('seed_zero_recall_questions', 0)} zero)",
        f"- mean **pool** recall (after pool expander): "
        f"**{summary.get('overall_pool_mean_recall', 0.0):.3f}** "
        f"({summary.get('pool_full_recall_questions', 0)} full, "
        f"{summary.get('pool_zero_recall_questions', 0)} zero)",
        f"- mean **bundle** recall (after context expansion): "
        f"**{summary['overall_mean_recall']:.3f}** "
        f"({summary['full_recall_questions']} full, "
        f"{summary['zero_recall_questions']} zero)",
        f"- masked by **pool expander** (pool>seed): "
        f"**{len(summary.get('masked_by_pool_expander', []))}**  ·  "
        f"masked by **context expander** (bundle>pool): "
        f"**{len(summary.get('masked_by_context_expander', []))}**",
        f"- mean **precision** (report-only; gold is recall-oriented, other ≠ noise): "
        f"seed **{summary.get('overall_seed_mean_precision', 0.0):.3f}** → "
        f"pool **{summary.get('overall_pool_mean_precision', 0.0):.3f}** → "
        f"bundle **{summary.get('overall_mean_precision', 0.0):.3f}**",
        f"- mean **rendered token split**: expected "
        f"**{summary.get('overall_mean_expected_tokens', 0.0):.0f}** vs other "
        f"**{summary.get('overall_mean_other_tokens', 0.0):.0f}** "
        f"(token precision **{summary.get('overall_mean_token_precision', 0.0):.3f}**)",
        f"- mean **exact symbol recall**: seed "
        f"**{summary.get('overall_seed_symbol_recall', 0.0):.3f}** → pool "
        f"**{summary.get('overall_pool_symbol_recall', 0.0):.3f}** → bundle "
        f"**{summary.get('overall_bundle_symbol_recall', 0.0):.3f}**",
        f"- mean **span owner recall** (`file + symbol`): seed "
        f"**{summary.get('overall_seed_span_owner_recall', 0.0):.3f}** → pool "
        f"**{summary.get('overall_pool_span_owner_recall', 0.0):.3f}** → bundle "
        f"**{summary.get('overall_bundle_span_owner_recall', 0.0):.3f}**",
        f"- mean **span line recall**: seed "
        f"**{summary.get('overall_seed_span_recall', 0.0):.3f}** → pool "
        f"**{summary.get('overall_pool_span_recall', 0.0):.3f}** → bundle "
        f"**{summary.get('overall_bundle_span_recall', 0.0):.3f}**",
        "",
        "## Per-repo (seed → pool → bundle)",
        "",
        "| repo | q | seed | pool | bundle | prec | tok exp/other | "
        "seed_full | pool_full | bundle_full |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for repo, info in summary["per_repo"].items():
        lines.append(
            f"| {repo} | {info['questions']} | "
            f"{info.get('seed_mean_recall', 0.0):.3f} | "
            f"{info.get('pool_mean_recall', 0.0):.3f} | "
            f"{info['mean_recall']:.3f} | "
            f"{info.get('mean_precision', 0.0):.3f} | "
            f"{info.get('mean_expected_tokens', 0.0):.0f}/{info.get('mean_other_tokens', 0.0):.0f} | "
            f"{info.get('seed_full_recall', 0)} | "
            f"{info.get('pool_full_recall', 0)} | {info['full_recall']} |"
        )

    rank_funnel = summary.get("gold_rank_funnel", {})
    owner_funnel = rank_funnel.get("owners", {})
    symbol_funnel = rank_funnel.get("symbols", {})
    if owner_funnel.get("gold_count") or symbol_funnel.get("gold_count"):
        lines.extend(
            [
                "",
                "## Gold rank funnel (utility → Token Credit coverage → prompt)",
                "",
                "Ranks are computed over the complete budget-sorted pool and "
                "the complete first-wins prompt, not the top-15 audit sample.",
                "",
                "| gold | stage | present/total | recall | median rank | p90 rank |",
                "|---|---|---|---|---|---|",
            ]
        )
        for label, funnel in (("owner", owner_funnel), ("symbol", symbol_funnel)):
            total = int(funnel.get("gold_count", 0))
            for stage_name in ("utility", "coverage", "final"):
                stage = funnel.get(stage_name, {})
                median = stage.get("median_rank")
                p90 = stage.get("p90_rank")
                lines.append(
                    f"| {label} | {stage_name} | {stage.get('present', 0)}/{total} | "
                    f"{stage.get('recall', 0.0):.3f} | "
                    f"{median if median is not None else '—'} | "
                    f"{p90 if p90 is not None else '—'} |"
                )
        lines.extend(
            [
                "",
                f"Owner flow: retrieval-missing but graph-rescued "
                f"**{owner_funnel.get('retrieval_missing_but_final', 0)}** · "
                f"utility-present, coverage-missed but rescued "
                f"**{owner_funnel.get('utility_not_coverage_but_final', 0)}** · "
                f"coverage-selected but absent after first-wins "
                f"**{owner_funnel.get('coverage_not_final', 0)}**.",
            ]
        )

    rank_spend = summary.get("candidate_rank_token_spend", {})
    if rank_spend.get("questions"):
        lines.extend(
            [
                "",
                "## Token Credit spend by pre-budget candidate rank",
                "",
                "Coverage and body/rich-render upgrades are separated; upgrade "
                "concentration shows whether the ranked head actually receives "
                "the body budget.",
                "",
                "| candidate rank | coverage tokens | upgrade tokens | upgrade share |",
                "|---|---:|---:|---:|",
            ]
        )
        for label, row in (rank_spend.get("by_rank", {}) or {}).items():
            lines.append(
                f"| {label} | {int(row.get('coverage_tokens', 0))} | "
                f"{int(row.get('upgrade_tokens', 0))} | "
                f"{float(row.get('upgrade_share', 0.0)):.3f} |"
            )
        share_at = rank_spend.get("upgrade_share_at", {}) or {}
        allocation_modes = rank_spend.get("allocation_mode_counts", {}) or {}
        lines.extend(
            [
                "",
                f"Allocation modes: **{allocation_modes}** · mean coverage share "
                f"**{float(rank_spend.get('mean_coverage_share', 0.0)):.3f}** · "
                "mean adaptive threshold "
                f"**{float(rank_spend.get('mean_rank_decay_coverage_threshold', 0.0)):.3f}**.",
                "",
                "Upgrade share in ranked prefix: "
                f"@1 **{float(share_at.get('1', 0.0)):.3f}** · "
                f"@3 **{float(share_at.get('3', 0.0)):.3f}** · "
                f"@5 **{float(share_at.get('5', 0.0)):.3f}** · "
                f"@10 **{float(share_at.get('10', 0.0)):.3f}**.",
            ]
        )
        attribution = rank_spend.get("upgrade_attribution", {}) or {}
        upgrade_total = max(1, int(rank_spend.get("upgrade_tokens", 0) or 0))
        lines.extend(
            [
                "",
                "### Upgrade token attribution",
                "",
                "Exact paid upgrade tokens split by the symbol that actually "
                "gained rendered text. Retrieval-backed means the symbol owns "
                "direct exact/channel/span evidence; graph-only means it is "
                "present only through context expansion.",
                "",
                "| dimension | value | tokens | share |",
                "|---|---|---:|---:|",
            ]
        )
        for dimension in ("scope", "evidence", "depth", "edge_type"):
            rows = list((attribution.get(dimension, {}) or {}).items())
            if dimension == "edge_type":
                rows = rows[:12]
            for value, tokens in rows:
                lines.append(
                    f"| {dimension} | {value} | {int(tokens)} | {int(tokens) / upgrade_total:.3f} |"
                )

    candidate_cohorts = summary.get("candidate_cohorts", {}) or {}
    if candidate_cohorts.get("questions"):
        lines.extend(
            [
                "",
                "## Pre-budget candidates by role, axis and intent alignment",
                "",
                "Gold labels are non-exhaustive benchmark evidence: `other` means "
                "unlabelled, not proven noise. Axis basis is kind → edge → role-profile "
                "fallback; alignment uses all classifier intent matches. Role rows are "
                "overlapping memberships from the complete `supporting_roles` set.",
            ]
        )

        def append_cohort_table(
            title: str,
            rows: dict[str, dict[str, Any]],
            *,
            limit: int | None = None,
        ) -> None:
            lines.extend(
                [
                    "",
                    f"### {title}",
                    "",
                    "| cohort | candidates | exact/labelled gold | exact rate | "
                    "exact gold rank | "
                    "coverage/body tokens |",
                    "|---|---:|---:|---:|---:|---:|",
                ]
            )
            items = list(rows.items())
            if limit is not None:
                items = items[:limit]
            for label, row in items:
                lines.append(
                    f"| {label} | {int(row.get('candidates', 0))} | "
                    f"{int(row.get('exact_gold', 0))}/"
                    f"{int(row.get('labelled_gold', 0))} | "
                    f"{float(row.get('exact_gold_rate', 0.0)):.3f} | "
                    f"{float(row.get('mean_exact_gold_rank') or 0.0):.1f} | "
                    f"{int(row.get('coverage_tokens', 0))}/"
                    f"{int(row.get('upgrade_tokens', 0))} |"
                )

        append_cohort_table(
            "Supporting role membership",
            candidate_cohorts.get("by_role", {}),
            limit=15,
        )
        append_cohort_table("Structural axis", candidate_cohorts.get("by_axis", {}))
        append_cohort_table(
            "Intent alignment",
            candidate_cohorts.get("by_intent_alignment", {}),
        )
        append_cohort_table(
            "Independent retrieval-channel count",
            candidate_cohorts.get("by_channel_count", {}),
        )
        append_cohort_table(
            "Retrieval-channel signature",
            candidate_cohorts.get("by_channel_signature", {}),
        )
        append_cohort_table(
            "Exact-symbol prior",
            candidate_cohorts.get("by_exact_symbol_prior", {}),
        )
        append_cohort_table(
            "Top classified intent",
            candidate_cohorts.get("by_top_intent", {}),
        )

    def _masking_section(title: str, lower: str, upper: str, rows: list) -> None:
        if not rows:
            return
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                f"{upper} covered these; {lower} missed them. The real engine "
                f"gaps a collapse of this layer would expose — move the gap "
                f"down a layer before trimming.",
                "",
                f"| id | repo | {lower} | {upper} | files only this layer found |",
                "|---|---|---|---|---|",
            ]
        )
        for m in sorted(rows, key=lambda x: (x["repo"], x["question_id"])):
            files = ", ".join(m["added_files"]) or "—"
            lo = m.get(f"{lower}_recall", 0.0)
            up = m.get(f"{upper}_recall", 0.0)
            lines.append(f"| {m['question_id']} | {m['repo']} | {lo:.2f} | {up:.2f} | {files} |")

    _masking_section(
        "Masked by the pool expander",
        "seed",
        "pool",
        summary.get("masked_by_pool_expander", []),
    )
    _masking_section(
        "Masked by per-candidate context expansion",
        "pool",
        "bundle",
        summary.get("masked_by_context_expander", []),
    )
    lines.extend(
        [
            "",
            "## Intent classifier — top role distribution",
            "",
            f"`{json.dumps(summary['intent_top_role_counts'], sort_keys=True)}`",
            "",
            "## Candidate audit",
            "",
            "| surface | top relations |",
            "|---|---|",
            f"| context candidate pool | "
            f"{_format_histogram(summary.get('candidate_relation_totals', {}), limit=8)} |",
            f"| rendered bundle symbols | "
            f"{_format_histogram(summary.get('bundle_relation_totals', {}), limit=8)} |",
            "",
            "## Per-question detail",
            "",
            "`p⚠` = pool expander masks a seed miss · `c⚠` = context expander masks a pool miss",
            "",
            "| id | repo | seed | pool | bundle | prec | tok exp/other | "
            "matched/expected | intent | cand | top candidate relations |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for r in sorted(results, key=lambda x: (x.repo, x.question_id)):
        if r.skipped_reason:
            lines.append(
                f"| {r.question_id} | {r.repo} | — | — | — | — | — | — | — | — | "
                f"skipped: {r.skipped_reason} |"
            )
            continue
        intent_str = (
            f"{r.intent_top_role}({r.intent_top_similarity:.2f})" if r.intent_top_role else "(none)"
        )
        marks = ""
        if r.pool_recall > r.seed_recall + 1e-9:
            marks += " p⚠"
        if r.file_recall > r.pool_recall + 1e-9:
            marks += " c⚠"
        lines.append(
            f"| {r.question_id} | {r.repo} | {r.seed_recall:.2f} | "
            f"{r.pool_recall:.2f} | {r.file_recall:.2f}{marks} | "
            f"{r.bundle_precision:.2f} | {r.expected_tokens}/{r.other_tokens} | "
            f"{len(r.matched_files)}/{len(r.expected_files)} | "
            f"{intent_str} | {r.candidate_count} | "
            f"{_format_histogram(r.candidate_relation_histogram, limit=3)} |"
        )

    expected_layer_rows = [
        (r, row)
        for r in sorted(results, key=lambda x: (x.repo, x.question_id))
        if not r.skipped_reason
        for row in r.expected_file_layers
        if row.get("first_layer") != "seed" or row.get("lost_after")
    ]
    if expected_layer_rows:
        lines.extend(
            [
                "",
                "## Expected files by layer",
                "",
                "Rows here either missed seed retrieval, appeared only after expansion, "
                "or were lost before the final bundle.",
                "",
                "| id | repo | expected file | seed | pool | bundle | first | lost after |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        for r, row in expected_layer_rows:
            lost = ", ".join(row.get("lost_after") or []) or "—"
            lines.append(
                f"| {r.question_id} | {r.repo} | {row.get('expected_file')} | "
                f"{'yes' if row.get('seed') else 'no'} | "
                f"{'yes' if row.get('pool') else 'no'} | "
                f"{'yes' if row.get('bundle') else 'no'} | "
                f"{row.get('first_layer')} | {lost} |"
            )

    top_candidate_rows = [
        r
        for r in sorted(results, key=lambda x: (x.repo, x.question_id))
        if not r.skipped_reason and (r.file_recall < 1.0 - 1e-9 or r.candidate_count >= 50)
    ]
    if top_candidate_rows:
        lines.extend(
            [
                "",
                "## Top candidates for noisy or non-full questions",
                "",
                "| id | repo | candidate count | top candidates |",
                "|---|---|---|---|",
            ]
        )
        for r in top_candidate_rows:
            lines.append(
                f"| {r.question_id} | {r.repo} | {r.candidate_count} | "
                f"{_format_top_candidate_rows(r.top_candidates)} |"
            )
    return "\n".join(lines) + "\n"


def _ordered_summary_for_console(summary: dict[str, Any]) -> dict[str, Any]:
    """Put overall_/max_ metrics just before the trailing aggregate counts.

    Default ``sort_keys`` dumps ``overall_*`` above the huge ``per_question``
    block; keep those headline numbers next to scored/seed/pool counts at the
    end of the console JSON.
    """
    trailing = [
        "full_recall_questions",
        "pool_full_recall_questions",
        "pool_zero_recall_questions",
        "scored",
        "seed_full_recall_questions",
        "seed_zero_recall_questions",
        "skipped",
        "skipped_reasons",
        "zero_recall_questions",
    ]
    headline = sorted(key for key in summary if key.startswith(("overall_", "max_")))
    trailing_present = [key for key in trailing if key in summary]
    pinned = set(headline) | set(trailing_present)
    rest = sorted(key for key in summary if key not in pinned)
    return {key: summary[key] for key in [*rest, *headline, *trailing_present]}


def _print_per_repo_table(summary: dict[str, Any]) -> None:
    """Print a fixed-width console table: rows = repos (+ ALL), columns = metrics.

    File-recall funnel (seed ≤ pool; bundle may exceed pool via neighbour
    render), then the discriminating telemetry: bundle symbol / span-owner /
    span-line recalls and token_precision (the headline precision), with the
    file-level ``file_prec`` kept as the report-only trend number it is.
    ``full s/p/b`` = questions at full file recall per layer.
    """
    per_repo = summary.get("per_repo") or {}
    if not per_repo:
        return

    headers = (
        "repo",
        "q",
        "seed",
        "pool",
        "bundle",
        "sym",
        "span_own",
        "span",
        "tok_prec",
        "file_prec",
        "tok exp/other",
        "full s/p/b",
    )

    def _gold_cell(value: float, gold_questions: int) -> str:
        # A gold-conditioned mean over zero gold-bearing questions is not a
        # measured 0.000 — the pack simply has no such gold; render a dash.
        return f"{value:.3f}" if gold_questions else "-"

    def _row(
        label: str,
        questions: int,
        seed: float,
        pool: float,
        bundle: float,
        sym: str,
        span_own: str,
        span: str,
        tok_prec: float,
        file_prec: float,
        exp_tok: float,
        other_tok: float,
        seed_full: int,
        pool_full: int,
        bundle_full: int,
    ) -> tuple[str, ...]:
        return (
            label,
            str(questions),
            f"{seed:.3f}",
            f"{pool:.3f}",
            f"{bundle:.3f}",
            sym,
            span_own,
            span,
            f"{tok_prec:.3f}",
            f"{file_prec:.3f}",
            f"{exp_tok:.0f}/{other_tok:.0f}",
            f"{seed_full}/{pool_full}/{bundle_full}",
        )

    rows: list[tuple[str, ...]] = []
    for repo, info in per_repo.items():
        sym_gold = int(info.get("symbol_gold_questions", 0))
        span_gold = int(info.get("span_gold_questions", 0))
        rows.append(
            _row(
                str(repo),
                info["questions"],
                info.get("seed_mean_recall", 0.0),
                info.get("pool_mean_recall", 0.0),
                info["mean_recall"],
                _gold_cell(info.get("bundle_symbol_recall", 0.0), sym_gold),
                _gold_cell(info.get("bundle_span_owner_recall", 0.0), span_gold),
                _gold_cell(info.get("bundle_span_recall", 0.0), span_gold),
                info.get("mean_token_precision", 0.0),
                info.get("mean_precision", 0.0),
                info.get("mean_expected_tokens", 0.0),
                info.get("mean_other_tokens", 0.0),
                info.get("seed_full_recall", 0),
                info.get("pool_full_recall", 0),
                info["full_recall"],
            )
        )
    all_sym_gold = int(summary.get("symbol_gold_questions", 0))
    all_span_gold = int(summary.get("span_gold_questions", 0))
    rows.append(
        _row(
            "ALL",
            int(summary.get("scored", 0)),
            float(summary.get("overall_seed_mean_recall", 0.0)),
            float(summary.get("overall_pool_mean_recall", 0.0)),
            float(summary.get("overall_mean_recall", 0.0)),
            _gold_cell(float(summary.get("overall_bundle_symbol_recall", 0.0)), all_sym_gold),
            _gold_cell(float(summary.get("overall_bundle_span_owner_recall", 0.0)), all_span_gold),
            _gold_cell(float(summary.get("overall_bundle_span_recall", 0.0)), all_span_gold),
            float(summary.get("overall_mean_token_precision", 0.0)),
            float(summary.get("overall_mean_precision", 0.0)),
            float(summary.get("overall_mean_expected_tokens", 0.0)),
            float(summary.get("overall_mean_other_tokens", 0.0)),
            int(summary.get("seed_full_recall_questions", 0)),
            int(summary.get("pool_full_recall_questions", 0)),
            int(summary.get("full_recall_questions", 0)),
        )
    )

    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]

    def _fmt(cells: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    print("\nPer-repo summary")
    print(_fmt(headers))
    print(_fmt(tuple("-" * w for w in widths)))
    for row in rows[:-1]:
        print(_fmt(row))
    print(_fmt(tuple("-" * w for w in widths)))
    print(_fmt(rows[-1]))


def _print_comparison(prev_summary: dict[str, Any], summary: dict[str, Any]) -> None:
    prev_recall = float(prev_summary.get("overall_mean_recall", 0.0))
    curr_recall = float(summary.get("overall_mean_recall", 0.0))
    delta = curr_recall - prev_recall
    arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "·")
    print(
        f"\noverall mean bundle_recall: {prev_recall:.3f} → {curr_recall:.3f} "
        f"({arrow} {delta:+.3f})"
    )

    def _layer(label: str, key: str) -> tuple[float, float]:
        p = float(prev_summary.get(key, 0.0))
        c = float(summary.get(key, 0.0))
        d = c - p
        a = "↑" if d > 0 else ("↓" if d < 0 else "·")
        print(f"overall mean {label:13} {p:.3f} → {c:.3f} ({a} {d:+.3f})")
        return p, c

    _, curr_seed = _layer("seed_recall:", "overall_seed_mean_recall")
    _, curr_pool = _layer("pool_recall:", "overall_pool_mean_recall")
    pool_masks = len(summary.get("masked_by_pool_expander", []))
    ctx_masks = len(summary.get("masked_by_context_expander", []))
    print(
        f"layer gaps: pool−seed {curr_pool - curr_seed:+.3f} "
        f"({pool_masks} masked)  ·  bundle−pool "
        f"{curr_recall - curr_pool:+.3f} ({ctx_masks} masked)"
    )

    # Precision telemetry deltas — only comparable when the previous summary
    # already carries the keys (older runs predate them).
    for label, key in (
        ("bundle_precision:", "overall_mean_precision"),
        ("token_precision:", "overall_mean_token_precision"),
    ):
        if key in prev_summary:
            _layer(label, key)
        elif key in summary:
            print(f"overall mean {label:13} (n/a) → {float(summary[key]):.3f}")


def _format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"

    whole_seconds = int(seconds)
    minutes, secs = divmod(whole_seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"

    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s"


def _compact_progress_text(text: str, *, limit: int = 96) -> str:
    compacted = " ".join(text.split())
    if len(compacted) <= limit:
        return compacted
    return compacted[: limit - 3].rstrip() + "..."


def _print_progress_start(
    *,
    index: int,
    total: int,
    question_entry: dict[str, Any],
    stream: TextIO,
) -> None:
    qid = str(question_entry.get("id") or "(no id)")
    repo = str(question_entry.get("repo") or "(no repo)")
    question = _compact_progress_text(str(question_entry.get("question") or ""))
    print(
        f"[axis {index}/{total}] start {repo}/{qid}: {question}",
        file=stream,
        flush=True,
    )


def _print_progress_done(
    *,
    index: int,
    total: int,
    result: QuestionResult,
    question_seconds: float,
    elapsed_seconds: float,
    scored_count: int,
    skipped_count: int,
    full_count: int,
    zero_count: int,
    recall_sum: float,
    stream: TextIO,
) -> None:
    completed = index
    remaining = max(total - completed, 0)
    eta_seconds = (elapsed_seconds / completed) * remaining if completed else 0.0
    running_mean = recall_sum / scored_count if scored_count else 0.0

    # Line 1 — progress / timing / running aggregate.
    progress_line = (
        f"[axis {index}/{total}] done  {result.repo}/{result.question_id} "
        f"q={_format_duration(question_seconds)} "
        f"elapsed={_format_duration(elapsed_seconds)} "
        f"eta={_format_duration(eta_seconds)} "
        f"mean={running_mean:.3f} full={full_count} zero={zero_count} "
        f"skipped={skipped_count}"
    )

    # Line 2 (new line, indented) — this question's metrics, kept off the
    # progress line so neither crowds the other.
    if result.skipped_reason:
        metrics_line = f"    skipped={result.skipped_reason}"
    else:
        intent = result.intent_top_role or "(none)"
        if result.intent_top_similarity is not None:
            intent = f"{intent}({result.intent_top_similarity:.2f})"
        marks = ""
        if result.pool_recall > result.seed_recall + 1e-9:
            marks += " p⚠"
        if result.file_recall > result.pool_recall + 1e-9:
            marks += " c⚠"
        metrics_line = (
            f"    seed={result.seed_recall:.3f} pool={result.pool_recall:.3f} "
            f"bundle={result.file_recall:.3f}{marks} "
            f"prec={result.bundle_precision:.2f} "
            f"tok={result.expected_tokens}/{result.other_tokens} "
            f"matched={len(result.matched_files)}/{len(result.expected_files)} "
            f"candidates={result.candidate_count} intent={intent}"
        )

    print(f"{progress_line}\n{metrics_line}", file=stream, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Axis pipeline benchmark over the Python question pack",
    )
    parser.add_argument(
        "--pack",
        default="QA/fixtures/questions_python.yaml",
        type=Path,
    )
    parser.add_argument("--out", default="/tmp/axis_benchmark", type=Path)
    parser.add_argument(
        "--base-commit-workspaces",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Resolve questions carrying base_commit against an isolated exact-commit "
        "workspace (default ON). The off arm is approximate and invalidates line/span gold.",
    )
    parser.add_argument(
        "--commit-workspace-tenant",
        default=_BENCH_COMMIT_TENANT,
        help="Workspace tenant for base_commit questions (default: contextbench).",
    )
    parser.add_argument("--top-roles", type=int, default=3)
    parser.add_argument(
        "--per-role-limit", type=int, default=7, help="Seed/pool cap per intent role (default 7)."
    )
    parser.add_argument(
        "--max-impacted",
        type=int,
        default=35,
        help="Impact-analysis traversal cap (default 35). Pair with --per-role-limit for cap sweeps (e.g. 7/35).",
    )
    parser.add_argument("--intent-threshold", type=float, default=0.20)
    parser.add_argument("--context-per-seed", type=int, default=6)
    parser.add_argument(
        "--context-seeds-per-role",
        type=int,
        default=7,
        nargs="?",
        const=2,
        metavar="N",
        help="Evidence-aware soft cap for context seeds per source role "
        "(production default 7; pass alone for legacy cap 2).",
    )
    parser.add_argument(
        "--uncapped-context-seeds",
        action="store_true",
        help="Diagnostic historical arm: feed the full candidate pool to context expansion.",
    )
    budget_group = parser.add_mutually_exclusive_group()
    budget_group.add_argument(
        "--intent-budget",
        dest="intent_budget",
        action="store_true",
        help="Use the production Token Credit budget path (default).",
    )
    budget_group.add_argument(
        "--no-intent-budget",
        dest="intent_budget",
        action="store_false",
        help="Run the legacy unbudgeted render path for A/B comparisons.",
    )
    parser.set_defaults(intent_budget=True)
    parser.add_argument(
        "--token-budget",
        type=int,
        default=6000,
        help="Base token budget for the intent budget path (scaled per intent "
        "profile). Mirrors AskRequest.token_budget. Default 6000.",
    )
    parser.add_argument(
        "--render-mode",
        choices=[
            "full",
            "impact_tiered",
            "impact_surface",
            "signature_only",
            "hybrid",
            "hybrid_compact",
            "fold",
            "fold_compact",
        ],
        default=None,
        help="Override the profile's echelon-2 render mode for intent budgeting "
        "(sweep knob). Unset = use each profile's own render_mode.",
    )
    parser.add_argument(
        "--no-proximity",
        action="store_true",
        help="Ignore each question's `anchor` field (B_proximity OFF) — the "
        "off-arm for an on/off comparison on an anchor pack.",
    )
    parser.add_argument(
        "--no-hook-transparency",
        dest="hook_transparency",
        action="store_false",
        default=True,
        help="Disable hook transparency (default ON). Hook transparency opens "
        "hook-DECLARATION seeds through their registration lifecycle (incoming "
        "HOOK sites -> the registration API they go through) — the "
        "hook->registration archetype chain; inert for non-hook seeds. This "
        "flag is the off-arm for an on/off A/B.",
    )
    parser.add_argument(
        "--no-query-node-rerank",
        dest="query_node_rerank",
        action="store_false",
        default=True,
        help="Disable the final query↔node semantic Pool rerank for an A/B baseline.",
    )
    parser.add_argument(
        "--query-node-weight",
        type=float,
        default=0.20,
        help="Semantic boost weight for ordinary graph-only Pool candidates.",
    )
    parser.add_argument(
        "--query-node-mode-weight",
        type=float,
        default=0.05,
        help="Lower semantic boost weight for impact/trace candidates.",
    )
    parser.add_argument(
        "--query-node-ordering",
        choices=["legacy_boost", "calibrated_blend", "rrf"],
        default="calibrated_blend",
        help="Pool ordering strategy after query↔node annotation.",
    )
    parser.add_argument("--query-node-blend-alpha", type=float, default=0.40)
    parser.add_argument("--query-node-mode-blend-alpha", type=float, default=0.10)
    parser.add_argument("--query-node-rrf-weight", type=float, default=1.0)
    parser.add_argument("--query-node-mode-rrf-weight", type=float, default=0.25)
    parser.add_argument("--query-node-rrf-k", type=int, default=60)
    parser.add_argument(
        "--weighted-intent-role-axis-boost",
        action="store_true",
        help=(
            "Experimental pre-budget rank arm: similarity-scale exact-role "
            "intent evidence and discount broad axis-only overlap."
        ),
    )
    parser.add_argument("--intent-role-axis-boost", type=float, default=0.15)
    parser.add_argument("--intent-axis-only-share", type=float, default=0.25)
    parser.add_argument(
        "--role-consensus-score-boost",
        type=float,
        default=0.05,
        help=(
            "Add this score per extra supporting role after seed deduplication; "
            "pass 0 for the control arm."
        ),
    )
    parser.add_argument("--role-consensus-max-extra-roles", type=int, default=2)
    parser.add_argument(
        "--role-consensus-min-effective-tokens",
        type=int,
        default=10_000,
        help=(
            "Enable the pre-score consensus boost only when the selected "
            "intent profile receives at least this many tokens."
        ),
    )
    parser.add_argument(
        "--channel-consensus-score-boost",
        type=float,
        default=0.0,
        help="Add this score per extra independent retrieval family.",
    )
    parser.add_argument(
        "--channel-consensus-max-extra-families",
        type=int,
        default=2,
    )
    parser.add_argument("--exact-symbol-score-boost", type=float, default=0.0)
    parser.add_argument(
        "--channel-consensus-min-effective-tokens",
        type=int,
        default=10_000,
    )
    parser.add_argument(
        "--non-intent-structural-role-soft-cap",
        type=int,
        default=None,
        help=(
            "Experimental coverage arm: tighter seed cap for profiled "
            "structural roles absent from the active intent."
        ),
    )
    parser.add_argument(
        "--min-utility-per-token",
        type=float,
        default=None,
        help="Experimental Token Credit cutoff; unset preserves full-budget selection.",
    )
    parser.add_argument(
        "--upgrade-min-utility-per-token",
        type=float,
        default=0.00025,
        help=(
            "Paid-upgrade density cutoff; coverage remains unchanged. "
            "Default 0.00025; pass 0 for the unrestricted control."
        ),
    )
    parser.add_argument(
        "--freeze-at-utility-plateau",
        action="store_true",
        help="Leave budget unused after density cutoff; allow only free/reclaim upgrades.",
    )
    parser.add_argument(
        "--plateau-upgrade-reserve-share",
        type=float,
        default=0.0,
        help="Budget share reserved for paid upgrades after a frozen coverage plateau.",
    )
    parser.add_argument(
        "--node-semantic-utility-weight",
        type=float,
        default=0.0,
        help=(
            "Experimental Token Credit weight for request-local related-symbol "
            "query similarity; 0 preserves structural-only utility."
        ),
    )
    parser.add_argument(
        "--rank-decay-body-allocation",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Keep coverage unchanged, then spend "
            "geometrically decaying body credits in pre-budget candidate-rank order."
        ),
    )
    parser.add_argument("--rank-decay-head-share", type=float, default=0.15)
    parser.add_argument("--rank-decay-rate", type=float, default=0.75)
    parser.add_argument(
        "--rank-decay-max-coverage-share",
        type=float,
        default=0.65,
        help=("Coverage-pressure threshold at an 8k envelope; it relaxes linearly to 1.0 by 12k."),
    )
    parser.add_argument(
        "--decoupled-symbol-body-allocation",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Buy seed retrieval spans/body separately and require own evidence "
            "for neighbour bodies."
        ),
    )
    parser.add_argument(
        "--decoupled-seed-span-reserve-share",
        type=float,
        default=0.10,
        help="Bounded global budget share reserved for direct seed retrieval windows.",
    )
    parser.add_argument(
        "--span-line-rerank",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Rank and render query-relevant windows inside each selected symbol.",
    )
    parser.add_argument(
        "--span-line-rerank-on-explicit-line-hints",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Experimental: enable within-symbol reranking when the question names source lines.",
    )
    parser.add_argument("--span-rank-max-symbols", type=int, default=48)
    parser.add_argument("--span-rank-max-candidates-per-symbol", type=int, default=24)
    parser.add_argument("--span-rank-max-body-lines", type=int, default=6)
    parser.add_argument(
        "--lexical-retrieval",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable fielded BM25/exact-symbol seeds before graph expansion.",
    )
    parser.add_argument(
        "--semantic-chunk-retrieval",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable owner-resolved semantic chunk seeds before graph expansion.",
    )
    parser.add_argument("--hybrid-seed-limit", type=int, default=12)
    parser.add_argument(
        "--pregraph-lexical-span-probe",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Fetch a bounded pre-context-graph symbol batch and attach query-matching "
            "lexical source windows without a persistent chunk index."
        ),
    )
    parser.add_argument("--lexical-span-probe-max-symbols", type=int, default=96)
    parser.add_argument("--lexical-span-probe-max-windows-per-symbol", type=int, default=3)
    parser.add_argument("--lexical-span-probe-window-lines", type=int, default=6)
    parser.add_argument("--lexical-span-utility-weight", type=float, default=0.15)
    parser.add_argument(
        "--context-semantic-expansion",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Select dependency-solver context neighbours by query similarity with "
            "a structural reserve."
        ),
    )
    parser.add_argument("--context-semantic-expansion-alpha", type=float, default=0.70)
    parser.add_argument(
        "--context-semantic-expansion-structural-reserve",
        type=int,
        default=1,
    )
    parser.add_argument(
        "--evidence-graph-fanout",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Reduce the batched graph reservoir and final neighbours for weak "
            "tail seeds while preserving retrieval-backed and head seeds."
        ),
    )
    parser.add_argument("--evidence-graph-fanout-min", type=int, default=2)
    parser.add_argument(
        "--evidence-graph-fanout-protected-head",
        type=int,
        default=5,
    )
    parser.add_argument(
        "--compare",
        type=Path,
        default=None,
        help="Previous summary.json to compare against",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable per-question progress output on stderr",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Run only questions whose ``repo`` field matches this id",
    )
    parser.add_argument(
        "--exclude-repo",
        action="append",
        default=[],
        help="Exclude a repository id from the pack; repeat for multiple ids.",
    )
    args = parser.parse_args()
    if args.uncapped_context_seeds:
        args.context_seeds_per_role = None

    questions = _load_pack(args.pack)
    if args.repo:
        questions = [q for q in questions if q.get("repo") == args.repo]
    if args.exclude_repo:
        excluded_repos = set(args.exclude_repo)
        questions = [q for q in questions if q.get("repo") not in excluded_repos]
    if not questions:
        target = f"{args.pack}" + (f" repo={args.repo!r}" if args.repo else "")
        print(f"no questions in {target}")
        raise SystemExit(1)

    db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    lance = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)

    results: list[QuestionResult] = []
    progress_enabled = not args.no_progress
    progress_stream = sys.stderr
    total_questions = len(questions)
    run_started = time.monotonic()
    scored_count = 0
    skipped_count = 0
    full_count = 0
    zero_count = 0
    recall_sum = 0.0

    if progress_enabled:
        print(
            f"[axis] pack={args.pack} questions={total_questions} out={args.out} "
            f"caps={args.per_role_limit}/{args.max_impacted}",
            file=progress_stream,
            flush=True,
        )

    for index, entry in enumerate(questions, start=1):
        if progress_enabled:
            _print_progress_start(
                index=index,
                total=total_questions,
                question_entry=entry,
                stream=progress_stream,
            )
        question_started = time.monotonic()
        res = run_question(
            entry,
            db=db,
            lance=lance,
            top_roles=args.top_roles,
            per_role_limit=args.per_role_limit,
            max_impacted=args.max_impacted,
            intent_threshold=args.intent_threshold,
            context_per_seed=args.context_per_seed,
            context_seeds_per_role=args.context_seeds_per_role,
            intent_budget=args.intent_budget,
            base_token_budget=args.token_budget,
            render_mode_override=args.render_mode,
            ignore_anchor=args.no_proximity,
            hook_transparency=args.hook_transparency,
            query_node_rerank=args.query_node_rerank,
            query_node_semantic_weight=args.query_node_weight,
            query_node_mode_semantic_weight=args.query_node_mode_weight,
            query_node_ordering_mode=args.query_node_ordering,
            query_node_blend_alpha=args.query_node_blend_alpha,
            query_node_mode_blend_alpha=args.query_node_mode_blend_alpha,
            query_node_rrf_weight=args.query_node_rrf_weight,
            query_node_mode_rrf_weight=args.query_node_mode_rrf_weight,
            query_node_rrf_k=args.query_node_rrf_k,
            weighted_intent_role_axis_boost=(args.weighted_intent_role_axis_boost),
            intent_role_axis_boost=args.intent_role_axis_boost,
            intent_axis_only_share=args.intent_axis_only_share,
            role_consensus_score_boost=args.role_consensus_score_boost,
            role_consensus_max_extra_roles=(args.role_consensus_max_extra_roles),
            role_consensus_min_effective_tokens=(args.role_consensus_min_effective_tokens),
            channel_consensus_score_boost=args.channel_consensus_score_boost,
            channel_consensus_max_extra_families=(args.channel_consensus_max_extra_families),
            exact_symbol_score_boost=args.exact_symbol_score_boost,
            channel_consensus_min_effective_tokens=(args.channel_consensus_min_effective_tokens),
            non_intent_structural_role_soft_cap=(args.non_intent_structural_role_soft_cap),
            token_credit_min_utility_per_token=args.min_utility_per_token,
            token_credit_upgrade_min_utility_per_token=(args.upgrade_min_utility_per_token),
            token_credit_freeze_at_plateau=args.freeze_at_utility_plateau,
            token_credit_plateau_upgrade_reserve_share=(args.plateau_upgrade_reserve_share),
            node_semantic_utility_weight=args.node_semantic_utility_weight,
            rank_decay_body_allocation=args.rank_decay_body_allocation,
            rank_decay_head_share=args.rank_decay_head_share,
            rank_decay_rate=args.rank_decay_rate,
            rank_decay_max_coverage_share=args.rank_decay_max_coverage_share,
            decoupled_symbol_body_allocation=(args.decoupled_symbol_body_allocation),
            decoupled_seed_span_reserve_share=(args.decoupled_seed_span_reserve_share),
            span_line_rerank=args.span_line_rerank,
            span_line_rerank_on_explicit_line_hints=(args.span_line_rerank_on_explicit_line_hints),
            span_rank_max_symbols=args.span_rank_max_symbols,
            span_rank_max_candidates_per_symbol=(args.span_rank_max_candidates_per_symbol),
            span_rank_max_body_lines=args.span_rank_max_body_lines,
            context_semantic_expansion=args.context_semantic_expansion,
            context_semantic_expansion_alpha=args.context_semantic_expansion_alpha,
            context_semantic_expansion_structural_reserve=(
                args.context_semantic_expansion_structural_reserve
            ),
            evidence_graph_fanout=args.evidence_graph_fanout,
            evidence_graph_fanout_min=args.evidence_graph_fanout_min,
            evidence_graph_fanout_protected_head=(args.evidence_graph_fanout_protected_head),
            lexical_retrieval=args.lexical_retrieval,
            semantic_chunk_retrieval=args.semantic_chunk_retrieval,
            hybrid_seed_limit=args.hybrid_seed_limit,
            pregraph_lexical_span_probe=args.pregraph_lexical_span_probe,
            lexical_span_probe_max_symbols=args.lexical_span_probe_max_symbols,
            lexical_span_probe_max_windows_per_symbol=(
                args.lexical_span_probe_max_windows_per_symbol
            ),
            lexical_span_probe_window_lines=args.lexical_span_probe_window_lines,
            lexical_span_utility_weight=args.lexical_span_utility_weight,
            use_base_commit_workspace=args.base_commit_workspaces,
            commit_workspace_tenant=args.commit_workspace_tenant,
        )
        results.append(res)
        question_seconds = time.monotonic() - question_started

        if res.skipped_reason:
            skipped_count += 1
        else:
            scored_count += 1
            recall_sum += res.file_recall
            if res.file_recall >= 1.0 - 1e-9:
                full_count += 1
            if res.file_recall <= 1e-9:
                zero_count += 1

        if progress_enabled:
            _print_progress_done(
                index=index,
                total=total_questions,
                result=res,
                question_seconds=question_seconds,
                elapsed_seconds=time.monotonic() - run_started,
                scored_count=scored_count,
                skipped_count=skipped_count,
                full_count=full_count,
                zero_count=zero_count,
                recall_sum=recall_sum,
                stream=progress_stream,
            )

    summary = summarise(results)
    summary["caps"] = {
        "per_role_limit": args.per_role_limit,
        "max_impacted": args.max_impacted,
        "context_per_seed": args.context_per_seed,
        "context_seeds_per_role": args.context_seeds_per_role,
        "intent_budget": args.intent_budget,
        "base_token_budget": args.token_budget,
        "base_commit_workspaces": args.base_commit_workspaces,
        "commit_workspace_tenant": args.commit_workspace_tenant,
        "query_node_rerank": args.query_node_rerank,
        "query_node_semantic_weight": args.query_node_weight,
        "lexical_retrieval": args.lexical_retrieval,
        "semantic_chunk_retrieval": args.semantic_chunk_retrieval,
        "hybrid_seed_limit": args.hybrid_seed_limit,
        "pregraph_lexical_span_probe": args.pregraph_lexical_span_probe,
        "lexical_span_probe_max_symbols": args.lexical_span_probe_max_symbols,
        "lexical_span_probe_max_windows_per_symbol": (
            args.lexical_span_probe_max_windows_per_symbol
        ),
        "lexical_span_probe_window_lines": args.lexical_span_probe_window_lines,
        "lexical_span_utility_weight": args.lexical_span_utility_weight,
        "query_node_mode_semantic_weight": args.query_node_mode_weight,
        "query_node_ordering_mode": args.query_node_ordering,
        "query_node_blend_alpha": args.query_node_blend_alpha,
        "query_node_mode_blend_alpha": args.query_node_mode_blend_alpha,
        "query_node_rrf_weight": args.query_node_rrf_weight,
        "query_node_mode_rrf_weight": args.query_node_mode_rrf_weight,
        "query_node_rrf_k": args.query_node_rrf_k,
        "weighted_intent_role_axis_boost": (args.weighted_intent_role_axis_boost),
        "intent_role_axis_boost": args.intent_role_axis_boost,
        "intent_axis_only_share": args.intent_axis_only_share,
        "role_consensus_score_boost": args.role_consensus_score_boost,
        "role_consensus_max_extra_roles": args.role_consensus_max_extra_roles,
        "role_consensus_min_effective_tokens": (args.role_consensus_min_effective_tokens),
        "channel_consensus_score_boost": args.channel_consensus_score_boost,
        "channel_consensus_max_extra_families": (args.channel_consensus_max_extra_families),
        "exact_symbol_score_boost": args.exact_symbol_score_boost,
        "channel_consensus_min_effective_tokens": (args.channel_consensus_min_effective_tokens),
        "non_intent_structural_role_soft_cap": (args.non_intent_structural_role_soft_cap),
        "token_credit_min_utility_per_token": args.min_utility_per_token,
        "token_credit_upgrade_min_utility_per_token": (args.upgrade_min_utility_per_token),
        "token_credit_freeze_at_plateau": args.freeze_at_utility_plateau,
        "token_credit_plateau_upgrade_reserve_share": (args.plateau_upgrade_reserve_share),
        "node_semantic_utility_weight": args.node_semantic_utility_weight,
        "rank_decay_body_allocation": args.rank_decay_body_allocation,
        "rank_decay_head_share": args.rank_decay_head_share,
        "rank_decay_rate": args.rank_decay_rate,
        "rank_decay_max_coverage_share": args.rank_decay_max_coverage_share,
        "decoupled_symbol_body_allocation": args.decoupled_symbol_body_allocation,
        "decoupled_seed_span_reserve_share": args.decoupled_seed_span_reserve_share,
        "span_line_rerank": args.span_line_rerank,
        "span_line_rerank_on_explicit_line_hints": (args.span_line_rerank_on_explicit_line_hints),
        "span_rank_max_symbols": args.span_rank_max_symbols,
        "span_rank_max_candidates_per_symbol": (args.span_rank_max_candidates_per_symbol),
        "span_rank_max_body_lines": args.span_rank_max_body_lines,
        "context_semantic_expansion": args.context_semantic_expansion,
        "context_semantic_expansion_alpha": args.context_semantic_expansion_alpha,
        "context_semantic_expansion_structural_reserve": (
            args.context_semantic_expansion_structural_reserve
        ),
        "context_semantic_expansion_roles": ["dependency_solver"],
        "evidence_graph_fanout": args.evidence_graph_fanout,
        "evidence_graph_fanout_min": args.evidence_graph_fanout_min,
        "evidence_graph_fanout_protected_head": (args.evidence_graph_fanout_protected_head),
    }
    if args.repo:
        summary["repo_filter"] = args.repo
    if args.exclude_repo:
        summary["excluded_repos"] = sorted(set(args.exclude_repo))

    args.out.mkdir(parents=True, exist_ok=True)
    summary_path = args.out / "summary.json"
    md_path = args.out / "report.md"
    jsonl_path = args.out / "results.jsonl"

    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(_render_markdown(results, summary), encoding="utf-8")
    jsonl_path.write_text(
        "".join(json.dumps(r.to_dict(), sort_keys=True) + "\n" for r in results),
        encoding="utf-8",
    )

    print(json.dumps(_ordered_summary_for_console(summary), indent=2, default=str))
    print(f"\nfull report → {args.out}/")
    print(f"Report JSON: {summary_path}")
    _print_per_repo_table(summary)

    if args.compare and args.compare.exists():
        prev = json.loads(args.compare.read_text(encoding="utf-8"))
        _print_comparison(prev, summary)


if __name__ == "__main__":
    main()
