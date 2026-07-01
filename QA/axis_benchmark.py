"""A/B benchmark for the axis pipeline (read-side).

Replays ``QA/fixtures/questions_python.yaml`` against the
axis pipeline (intent → role retrieval → context expansion) and
measures file_recall: how many of each question's ``expected_files``
appear in the retrieved file_paths. The legacy ``/ask`` cascade is
unaffected; this tool is the A/B baseline for the axis side so the
two can be compared by a separate harness or by eye.

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

from context_engine.axis.pipeline import AxisRetrievalConfig, run_axis_retrieval
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
    # Candidate-level precision audit. These fields do not affect scoring;
    # they make the existing seed/pool/bundle report explain *why* noisy
    # candidates reached the pool or prompt.
    candidate_relation_histogram: dict[str, int] = field(default_factory=dict)
    bundle_relation_histogram: dict[str, int] = field(default_factory=dict)
    top_candidates: list[dict[str, Any]] = field(default_factory=list)
    top_rendered_symbols: list[dict[str, Any]] = field(default_factory=list)
    expected_file_layers: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "repo": self.repo,
            "workspace_id": self.workspace_id,
            "question": self.question,
            "mechanism": self.mechanism,
            "expected_files": self.expected_files,
            "retrieved_files": self.retrieved_files,
            "matched_files": self.matched_files,
            "file_recall": self.file_recall,
            "seed_files": self.seed_files,
            "seed_matched": self.seed_matched,
            "seed_recall": self.seed_recall,
            "pool_files": self.pool_files,
            "pool_matched": self.pool_matched,
            "pool_recall": self.pool_recall,
            "intent_top_role": self.intent_top_role,
            "intent_top_similarity": self.intent_top_similarity,
            "intent_matches": [{"role": r, "similarity": s} for r, s in self.intent_matches],
            "skipped_reason": self.skipped_reason,
            "candidate_count": self.candidate_count,
            "context_seconds": self.context_seconds,
            "rendered_tokens": self.rendered_tokens,
            "candidate_relation_histogram": self.candidate_relation_histogram,
            "bundle_relation_histogram": self.bundle_relation_histogram,
            "top_candidates": self.top_candidates,
            "top_rendered_symbols": self.top_rendered_symbols,
            "expected_file_layers": self.expected_file_layers,
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


def _sorted_counter_dict(counter: Counter[str]) -> dict[str, int]:
    return {
        key: count
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    }


def _candidate_relation(candidate: Any) -> str:
    return str(
        getattr(candidate, "role", "")
        or getattr(candidate, "edge_type", "")
        or "(none)"
    )


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
    rendered_symbols = _unique_rendered_symbols(getattr(retrieval, "bundles", []) or [])

    result.candidate_relation_histogram = _sorted_counter_dict(
        Counter(_candidate_relation(candidate) for candidate in candidates)
    )
    result.bundle_relation_histogram = _sorted_counter_dict(
        Counter(_rendered_symbol_relation(symbol) for symbol in rendered_symbols)
    )
    result.top_candidates = [
        _candidate_audit_row(candidate, rank)
        for rank, candidate in enumerate(candidates[:top_limit], start=1)
    ]
    result.top_rendered_symbols = [
        _rendered_symbol_audit_row(symbol, rank)
        for rank, symbol in enumerate(rendered_symbols[:top_limit], start=1)
    ]


def _question_result_from_entry(question_entry: dict[str, Any]) -> QuestionResult:
    return QuestionResult(
        question_id=str(question_entry.get("id") or ""),
        repo=str(question_entry.get("repo") or ""),
        workspace_id=None,
        question=str(question_entry.get("question") or ""),
        mechanism=str(question_entry.get("mechanism") or ""),
        expected_files=[str(p) for p in (question_entry.get("expected_files") or [])],
    )


def _resolve_question_workspace(
    repo: str,
    result: QuestionResult,
    workspace_overrides: dict[str, str] | None,
) -> str | None:
    overrides = workspace_overrides or {}
    workspace_id = overrides.get(repo) or REPO_TO_WORKSPACE.get(repo)
    if workspace_id is None:
        result.skipped_reason = f"repo {repo!r} not indexed under axis_python_v1"
        return None
    result.workspace_id = workspace_id
    return workspace_id


def _count_rendered_tokens(bundles: Any) -> int:
    seen: set[str] = set()
    total = 0
    for bundle in bundles:
        for sym in bundle.all_symbols():
            if sym.uid in seen:
                continue
            seen.add(sym.uid)
            total += estimate_text_tokens(sym.code or "")
    return total


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
    context_seeds_per_role: int | None = None,
    intent_budget: bool = True,
    base_token_budget: int = 6000,
    render_mode_override: str | None = None,
    ignore_anchor: bool = False,
    hook_transparency: bool = False,
    workspace_overrides: dict[str, str] | None = None,
) -> QuestionResult:
    result = _question_result_from_entry(question_entry)
    workspace_id = _resolve_question_workspace(result.repo, result, workspace_overrides)
    if workspace_id is None:
        return result

    # The whole read-side pipeline is the canonical ``run_axis_retrieval``
    # — the same function the ``/ask/axis`` endpoint runs, so this
    # benchmark validates that exact code. ``context_seeds_per_role=None``
    # feeds the entire pool into context expansion (the historical
    # benchmark behaviour); the seed / pool / bundle recall layers below
    # read straight off the layered result.
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
    )
    # Post-processing cost: the ``context`` stage is the build_context graph
    # expansion + per-uid code fetch; rendered_tokens is the token volume of
    # the DEDUPED bundle code (after any signature trim / budget cut) — i.e.
    # the prompt the adapter actually hands the LLM, deduped by uid exactly as
    # ``axis_bundles_to_prompt_context`` does (no double-counting shared
    # neighbours across bundles).
    result.context_seconds = round(timer.durations.get("context", 0.0), 4)
    result.rendered_tokens = _count_rendered_tokens(retrieval.bundles)
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
    context_seeds_per_role: int | None = None,
    intent_budget: bool = True,
    base_token_budget: int = 6000,
    render_mode_override: str | None = None,
    ignore_anchor: bool = False,
    hook_transparency: bool = True,
    workspace_overrides: dict[str, str] | None = None,
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
        "masked_by_pool_expander": masked_by_pool_expander,
        "masked_by_context_expander": masked_by_context_expander,
        "per_repo": by_repo_summary,
        "intent_top_role_counts": dict(by_intent),
        "candidate_relation_totals": _sorted_counter_dict(candidate_relation_totals),
        "bundle_relation_totals": _sorted_counter_dict(bundle_relation_totals),
        "skipped_reasons": Counter(r.skipped_reason for r in skipped),
        # Post-processing cost (the expensive part of the budget cost model).
        "overall_mean_context_seconds": _mean("context_seconds"),
        "max_context_seconds": max((r.context_seconds for r in scored), default=0.0),
        "overall_mean_rendered_tokens": _mean("rendered_tokens"),
        "max_rendered_tokens": max((r.rendered_tokens for r in scored), default=0),
        "per_question": [
            {
                "question_id": r.question_id,
                "repo": r.repo,
                "file_recall": round(r.file_recall, 4),
                "seed_recall": round(r.seed_recall, 4),
                "pool_recall": round(r.pool_recall, 4),
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
        "",
        "## Per-repo (seed → pool → bundle)",
        "",
        "| repo | q | seed | pool | bundle | seed_full | pool_full | bundle_full |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for repo, info in summary["per_repo"].items():
        lines.append(
            f"| {repo} | {info['questions']} | "
            f"{info.get('seed_mean_recall', 0.0):.3f} | "
            f"{info.get('pool_mean_recall', 0.0):.3f} | "
            f"{info['mean_recall']:.3f} | "
            f"{info.get('seed_full_recall', 0)} | "
            f"{info.get('pool_full_recall', 0)} | {info['full_recall']} |"
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
            "| id | repo | seed | pool | bundle | matched/expected | intent | cand | top candidate relations |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for r in sorted(results, key=lambda x: (x.repo, x.question_id)):
        if r.skipped_reason:
            lines.append(
                f"| {r.question_id} | {r.repo} | — | — | — | — | — | skipped: {r.skipped_reason} |"
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
        default=None,
        nargs="?",
        const=2,
        metavar="N",
        help="Optional latency A/B cap for context seeds per intent role. "
        "Omit for the production/full-pool path; pass alone for legacy cap 2.",
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
    args = parser.parse_args()

    questions = _load_pack(args.pack)
    if args.repo:
        questions = [q for q in questions if q.get("repo") == args.repo]
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
        "context_seeds_per_role": args.context_seeds_per_role,
        "intent_budget": args.intent_budget,
    }
    if args.repo:
        summary["repo_filter"] = args.repo

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

    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    print(f"\nfull report → {args.out}/")
    print(f"Report JSON: {summary_path}")

    if args.compare and args.compare.exists():
        prev = json.loads(args.compare.read_text(encoding="utf-8"))
        _print_comparison(prev, summary)


if __name__ == "__main__":
    main()
