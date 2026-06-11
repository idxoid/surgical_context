"""A/B benchmark for the axis pipeline (read-side).

Replays ``tests/fixtures/real_repo_question_pack.yaml`` against the
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
        --pack tests/fixtures/questions_python.yaml \\
        --out /tmp/axis_benchmark

    # Comparison with a previous run:
    python -m QA.axis_benchmark --pack ... --out ... \\
        --compare /tmp/axis_benchmark_previous/summary.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import yaml

from sidecar.axis.axis_phased import expand_phased
from sidecar.axis.axis_ranking import apply_intent_axis_boost
from sidecar.axis.context_builder import build_context_for_candidates
from sidecar.axis.cross_role_boost import intersect_by_cross_role_proximity
from sidecar.axis.impact_traversal import expand_impact_neighbourhood
from sidecar.axis.inheritance_ancestors import expand_inheritance_ancestors
from sidecar.axis.intent_classifier import classify_intent
from sidecar.axis.role_lookahead import expand_candidates_via_neighbourhood
from sidecar.axis.role_retrieval import (
    find_seeds_by_vector,
    find_symbols_by_roles,
    scan_workspace_rows,
)
from sidecar.axis.structural_neighbours import expand_structural_neighbours
from sidecar.axis.trace_traversal import expand_trace_neighbourhood
from sidecar.database.lancedb_client import LanceDBClient
from sidecar.database.neo4j_client import Neo4jClient
from sidecar.index_profile import AXIS_PYTHON_V1_PROFILE
from sidecar.indexer.fast.pipeline import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER

# Map ``repo`` from the question pack to the axis-profile workspace_id
# we actually indexed. Repos not listed here are skipped with reason.
REPO_TO_WORKSPACE: dict[str, str] = {
    "fastapi":    "qa_repo/fastapi@axis-v4+axis_python_v1",
    "flask":      "qa_repo/flask@axis-v4+axis_python_v1",
    "celery":     "qa_repo/celery@axis-v4+axis_python_v1",
    "click":      "qa_repo/click@axis-v4+axis_python_v1",
    "pydantic":   "qa_repo/pydantic@axis-v4+axis_python_v1",
    "sqlalchemy": "qa_repo/sqlalchemy@axis-v4+axis_python_v1",
    "django":     "qa_repo/django@axis-v4+axis_python_v1",
    "dathund":    "qa_repo/dathund@axis-v4+axis_python_v1",
}


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
            "intent_matches": [
                {"role": r, "similarity": s} for r, s in self.intent_matches
            ],
            "skipped_reason": self.skipped_reason,
            "candidate_count": self.candidate_count,
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


def run_question(
    question_entry: dict[str, Any],
    *,
    db: Neo4jClient,
    lance: LanceDBClient,
    top_roles: int,
    per_role_limit: int,
    intent_threshold: float,
    context_per_seed: int,
) -> QuestionResult:
    repo = str(question_entry.get("repo") or "")
    qid = str(question_entry.get("id") or "")
    result = QuestionResult(
        question_id=qid,
        repo=repo,
        workspace_id=None,
        question=str(question_entry.get("question") or ""),
        mechanism=str(question_entry.get("mechanism") or ""),
        expected_files=[str(p) for p in (question_entry.get("expected_files") or [])],
    )

    workspace_id = REPO_TO_WORKSPACE.get(repo)
    if workspace_id is None:
        result.skipped_reason = f"repo {repo!r} not indexed under axis_python_v1"
        return result
    result.workspace_id = workspace_id

    def _embed(text: str):
        return lance._embed([text])[0]  # noqa: SLF001

    intent = classify_intent(
        result.question,
        _embed,
        top_k=top_roles,
        threshold=intent_threshold,
    )
    if intent:
        result.intent_top_role = intent[0].role
        result.intent_top_similarity = intent[0].similarity
        result.intent_matches = [(m.role, m.similarity) for m in intent]

    # One workspace-scoped Lance scan serves every role retrieval AND
    # the role-agnostic vector seeds — workspace predicate pushdown +
    # parse-once instead of a full-table scan per role.
    scanned = scan_workspace_rows(workspace_id)
    raw_by_role: dict[str, list] = find_symbols_by_roles(
        workspace_id,
        [m.role for m in intent],
        query_text=result.question,
        embed_fn=_embed,
        limit=per_role_limit,
        prescanned=scanned,
    )

    # Base-retrieval (seed) files — pure vector/role similarity, captured
    # BEFORE any graph-walk pool expansion (lookahead is itself a pool
    # pass). seed_recall is the honest embedding-only number; the gap to
    # pool_recall is everything the pool expander adds (and may mask).
    base_seed_files: set[str] = {
        getattr(c, "file_path", "") or ""
        for cands in raw_by_role.values()
        for c in cands
    }

    # Cross-role *lookahead*: walk K hops from each role's seed
    # candidates and inject neighbours whose container_kinds back any
    # *other* intent role. Restores recall when the intent classifier
    # picks the right theme but the answer's role has shallow vector
    # retrieval (e.g. flask ``current_app`` question primes
    # proxy_mechanism, but the mechanism's implementation lives in
    # ``dispatch_surface``-tagged dispatchers).
    if len(intent) >= 2 and any(raw_by_role.values()):
        raw_by_role = expand_candidates_via_neighbourhood(
            [m.role for m in intent],
            raw_by_role,
            db=db,
            lance=lance,
            workspace_id=workspace_id,
        )

    # Role-AGNOSTIC vector seeds — added AFTER lookahead (which rebuilds
    # the dict around intent roles only and would drop a non-intent
    # key). The intent classifier no longer gates structure selection:
    # ``find_symbols_by_role`` discards the right nodes when intent
    # picks the wrong role, pure similarity does not. These seeds join
    # the pool and anchor the phased walk, so a misrouted intent (click
    # parse_args → proxy) still reaches its answer by topology. Intent
    # stays a resource manager (ranking + depth), out of structure.
    raw_by_role["vector_seed"] = find_seeds_by_vector(
        workspace_id,
        result.question,
        embed_fn=_embed,
        limit=per_role_limit,
        prescanned=scanned,
    )
    base_seed_files |= {
        getattr(c, "file_path", "") or ""
        for c in raw_by_role.get("vector_seed", [])
    }

    # File-level structural-neighbour pass via undirected AFFECTS.
    # Capped tightly — see ``expand_structural_neighbours`` docstring.
    existing_pool_for_struct = [
        c
        for role, cands in raw_by_role.items()
        if role not in {"impact_analysis", "structural_neighbour"}
        for c in cands
    ]
    if existing_pool_for_struct:
        affects_pool = expand_structural_neighbours(
            existing_pool_for_struct, db=db, workspace_id=workspace_id,
        )
        # Upward inheritance walk via DEPENDS_ON — abstract bases of
        # the concrete implementations in the pool.
        ancestor_pool = expand_inheritance_ancestors(
            existing_pool_for_struct,
            db=db,
            workspace_id=workspace_id,
            exclude_uids=[c.uid for c in affects_pool],
        )
        # Reactive phased walk (REGISTRY*→CONTROL) — start axis chosen
        # by the seeds' kinds, not intent. Closes the case where intent
        # picked the wrong role but the seeds still sit on a registry /
        # router whose topology leads to the answer.
        already = {
            c.uid
            for c in (list(affects_pool) + list(ancestor_pool))
        }
        phased_pool = expand_phased(
            existing_pool_for_struct,
            db=db,
            lance=lance,
            workspace_id=workspace_id,
            exclude_uids=already,
            prescanned=scanned,
        )
        raw_by_role["structural_neighbour"] = (
            list(affects_pool)
            + list(ancestor_pool)
            + list(phased_pool)
        )

    # Mode passes — anchored on the existing candidate pool, but kept
    # semantically separate: impact_analysis walks blast-radius edges;
    # trace_dependency walks CALLS_* callers/callees only.
    mode_intents_present = {
        m.role
        for m in intent
        if m.role in {"impact_analysis", "trace_dependency"}
    }
    if mode_intents_present:
        existing_pool = [
            c
            for role, cands in raw_by_role.items()
            if role not in {"impact_analysis", "trace_dependency"}
            for c in cands
        ]
        if existing_pool:
            if "impact_analysis" in mode_intents_present:
                raw_by_role["impact_analysis"] = expand_impact_neighbourhood(
                    existing_pool, db=db, workspace_id=workspace_id,
                )
            if "trace_dependency" in mode_intents_present:
                raw_by_role["trace_dependency"] = expand_trace_neighbourhood(
                    existing_pool, db=db, workspace_id=workspace_id,
                )

    # Multi-role *intersection* pass — when ≥2 intents fire we use the
    # weaker signals as structural constraints, dropping primary
    # candidates that have no graph proximity to any secondary
    # candidate. ``fallback_on_empty`` keeps the original primary list
    # if no candidate intersects, so a single-role question never
    # silently zeros out.
    #
    # Mode intents (``impact_analysis`` / ``trace_dependency``) signal
    # broad exploratory traversal. Their secondary effect on
    # intersection is harmful: an impact question is exactly the case
    # where the "right" answer file may have no graph proximity to
    # the other tangential intent candidates (e.g. celery's
    # ``app/amqp.py`` is the publisher answer for an apply_async
    # impact question, but has no proximity to the loaders/timer
    # candidates the other weak intents nominate). Skip intersection
    # when any mode intent is present so the wider pool reaches the
    # impact / call-chain traversals downstream.
    has_mode_intent = any(
        m.role in {"impact_analysis", "trace_dependency"} for m in intent
    )
    if len(intent) >= 2 and not has_mode_intent:
        for i, match in enumerate(intent):
            primary = raw_by_role.get(match.role) or []
            secondary = {
                other.role: raw_by_role.get(other.role) or []
                for j, other in enumerate(intent)
                if j != i
            }
            raw_by_role[match.role] = intersect_by_cross_role_proximity(
                primary, secondary, db=db, workspace_id=workspace_id,
            )

    # Intent-axis ranking — intent as a ranker (not a selector). Boost
    # candidates whose kind-axes match the intent's axes; pools re-sort.
    # Role-agnostic seeds (no kinds) pass through untouched.
    raw_by_role = apply_intent_axis_boost(raw_by_role, [m.role for m in intent])

    # Iterate over every key in ``raw_by_role`` — the lookahead may
    # have *promoted* a non-intent role into its own pool. Skipping those
    # would discard the graph-evidenced candidates the lookahead produced.
    candidates_for_context: list = []
    seen_role_keys: set[str] = set()
    intent_role_keys = [m.role for m in intent]
    for role_key in intent_role_keys + [
        r for r in raw_by_role if r not in set(intent_role_keys)
    ]:
        if role_key in seen_role_keys:
            continue
        seen_role_keys.add(role_key)
        candidates_for_context.extend(raw_by_role.get(role_key) or [])

    result.candidate_count = len(candidates_for_context)

    # Layer 1 — seed (pure retrieval) recall, captured before any pool pass.
    result.seed_files = sorted(f for f in base_seed_files if f)
    seed_recall, seed_matched = _compute_recall(
        result.expected_files, result.seed_files
    )
    result.seed_recall = seed_recall
    result.seed_matched = seed_matched

    # Layer 2 — pool (engine) recall: the candidate POOL's files BEFORE
    # per-candidate context expansion. This is what the retrieval + the
    # reactive pool passes actually selected; the gap to the bundle metric
    # below is what the per-seed traversal in build_context_for_candidates
    # adds (and may be masking).
    pool_files_ordered: list[str] = []
    seen_pool: set[str] = set()
    for cand in candidates_for_context:
        path = getattr(cand, "file_path", "") or ""
        if path and path not in seen_pool:
            seen_pool.add(path)
            pool_files_ordered.append(path)
    result.pool_files = pool_files_ordered
    pool_recall, pool_matched = _compute_recall(
        result.expected_files, result.pool_files
    )
    result.pool_recall = pool_recall
    result.pool_matched = pool_matched

    bundles = build_context_for_candidates(
        candidates_for_context,
        workspace_id=workspace_id,
        db=db,
        lance=lance,
        max_per_seed=context_per_seed,
    )

    retrieved_paths_ordered: list[str] = []
    seen: set[str] = set()
    for bundle in bundles:
        for sym in bundle.all_symbols():
            path = sym.file_path or ""
            if path and path not in seen:
                seen.add(path)
                retrieved_paths_ordered.append(path)
    result.retrieved_files = retrieved_paths_ordered

    recall, matched = _compute_recall(result.expected_files, result.retrieved_files)
    result.file_recall = recall
    result.matched_files = matched
    return result


def summarise(results: list[QuestionResult]) -> dict[str, Any]:
    scored = [r for r in results if r.skipped_reason is None]
    skipped = [r for r in results if r.skipped_reason is not None]
    overall_recall = (
        sum(r.file_recall for r in scored) / len(scored) if scored else 0.0
    )
    full_recall_count = sum(1 for r in scored if r.file_recall >= 1.0 - 1e-9)
    zero_recall_count = sum(1 for r in scored if r.file_recall == 0.0)

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
    seed_zero_count = sum(1 for r in scored if r.seed_recall == 0.0)
    overall_pool_recall = _mean("pool_recall")
    pool_full_count = sum(1 for r in scored if r.pool_recall >= 1.0 - 1e-9)
    pool_zero_count = sum(1 for r in scored if r.pool_recall == 0.0)

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
            "zero_recall": sum(1 for r in items if r.file_recall == 0.0),
            "seed_mean_recall": sum(r.seed_recall for r in items) / len(items),
            "seed_full_recall": sum(
                1 for r in items if r.seed_recall >= 1.0 - 1e-9
            ),
            "pool_mean_recall": sum(r.pool_recall for r in items) / len(items),
            "pool_full_recall": sum(
                1 for r in items if r.pool_recall >= 1.0 - 1e-9
            ),
        }
        for repo, items in sorted(by_repo.items())
    }

    by_intent: Counter[str] = Counter()
    for r in scored:
        by_intent[r.intent_top_role or "(no_role)"] += 1

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
        "skipped_reasons": Counter(r.skipped_reason for r in skipped),
    }


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
        lines.extend([
            "",
            f"## {title}",
            "",
            f"{upper} covered these; {lower} missed them. The real engine "
            f"gaps a collapse of this layer would expose — move the gap "
            f"down a layer before trimming.",
            "",
            f"| id | repo | {lower} | {upper} | files only this layer found |",
            "|---|---|---|---|---|",
        ])
        for m in sorted(rows, key=lambda x: (x["repo"], x["question_id"])):
            files = ", ".join(m["added_files"]) or "—"
            lo = m.get(f"{lower}_recall", 0.0)
            up = m.get(f"{upper}_recall", 0.0)
            lines.append(
                f"| {m['question_id']} | {m['repo']} | {lo:.2f} | "
                f"{up:.2f} | {files} |"
            )

    _masking_section(
        "Masked by the pool expander", "seed", "pool",
        summary.get("masked_by_pool_expander", []),
    )
    _masking_section(
        "Masked by per-candidate context expansion", "pool", "bundle",
        summary.get("masked_by_context_expander", []),
    )
    lines.extend([
        "",
        "## Intent classifier — top role distribution",
        "",
        f"`{json.dumps(summary['intent_top_role_counts'], sort_keys=True)}`",
        "",
        "## Per-question detail",
        "",
        "`p⚠` = pool expander masks a seed miss · `c⚠` = context expander "
        "masks a pool miss",
        "",
        "| id | repo | seed | pool | bundle | matched/expected | intent | cand |",
        "|---|---|---|---|---|---|---|---|",
    ])
    for r in sorted(results, key=lambda x: (x.repo, x.question_id)):
        if r.skipped_reason:
            lines.append(
                f"| {r.question_id} | {r.repo} | — | — | — | — | — | "
                f"skipped: {r.skipped_reason} |"
            )
            continue
        intent_str = (
            f"{r.intent_top_role}({r.intent_top_similarity:.2f})"
            if r.intent_top_role
            else "(none)"
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
            f"{intent_str} | {r.candidate_count} |"
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
        default="tests/fixtures/questions_python.yaml",
        type=Path,
    )
    parser.add_argument("--out", default="/tmp/axis_benchmark", type=Path)
    parser.add_argument("--top-roles", type=int, default=3)
    parser.add_argument("--per-role-limit", type=int, default=8)
    parser.add_argument("--intent-threshold", type=float, default=0.20)
    parser.add_argument("--context-per-seed", type=int, default=6)
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
    args = parser.parse_args()

    questions = _load_pack(args.pack)
    if not questions:
        print(f"no questions in pack {args.pack}")
        return

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
            f"[axis] pack={args.pack} questions={total_questions} out={args.out}",
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
            intent_threshold=args.intent_threshold,
            context_per_seed=args.context_per_seed,
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
            if res.file_recall == 0.0:
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

    if args.compare and args.compare.exists():
        prev = json.loads(args.compare.read_text(encoding="utf-8"))
        _print_comparison(prev, summary)


if __name__ == "__main__":
    main()
