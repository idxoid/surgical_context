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
        --pack tests/fixtures/real_repo_question_pack.yaml \\
        --out /tmp/axis_benchmark

    # Comparison with a previous run:
    python -m QA.axis_benchmark --pack ... --out ... \\
        --compare /tmp/axis_benchmark_previous/summary.json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sidecar.axis.context_builder import build_context_for_candidates
from sidecar.axis.cross_role_boost import intersect_by_cross_role_proximity
from sidecar.axis.impact_traversal import expand_impact_neighbourhood
from sidecar.axis.intent_classifier import classify_intent
from sidecar.axis.role_lookahead import expand_candidates_via_neighbourhood
from sidecar.axis.role_retrieval import find_symbols_by_role
from sidecar.axis.structural_neighbours import expand_structural_neighbours
from sidecar.database.lancedb_client import LanceDBClient
from sidecar.database.neo4j_client import Neo4jClient
from sidecar.index_profile import AXIS_PYTHON_V1_PROFILE
from sidecar.indexer.fast.pipeline import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER


# Map ``repo`` from the question pack to the axis-profile workspace_id
# we actually indexed. Repos not listed here are skipped with reason.
REPO_TO_WORKSPACE: dict[str, str] = {
    "fastapi": "qa_repo/fastapi@axis-v4+axis_python_v1",
    "flask":   "qa_repo/flask@axis-v4+axis_python_v1",
    "celery":  "qa_repo/celery@axis-v4+axis_python_v1",
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
            "intent_top_role": self.intent_top_role,
            "intent_top_similarity": self.intent_top_similarity,
            "intent_matches": [
                {"role": r, "similarity": s} for r, s in self.intent_matches
            ],
            "skipped_reason": self.skipped_reason,
            "candidate_count": self.candidate_count,
        }


def _load_pack(pack_path: Path) -> list[dict[str, Any]]:
    payload = yaml.safe_load(pack_path.read_text(encoding="utf-8"))
    questions: list[dict[str, Any]] = []
    for entry in payload.get("questions", []) or []:
        if not isinstance(entry, dict):
            continue
        questions.append(entry)
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

    raw_by_role: dict[str, list] = {}
    for match in intent:
        raw_by_role[match.role] = find_symbols_by_role(
            workspace_id,
            match.role,
            query_text=result.question,
            embed_fn=_embed,
            limit=per_role_limit,
        )

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

    # File-level structural-neighbour pass via undirected AFFECTS.
    # Capped tightly — see ``expand_structural_neighbours`` docstring.
    existing_pool_for_struct = [
        c
        for role, cands in raw_by_role.items()
        if role not in {"impact_analysis", "structural_neighbour"}
        for c in cands
    ]
    if existing_pool_for_struct:
        raw_by_role["structural_neighbour"] = expand_structural_neighbours(
            existing_pool_for_struct, db=db, workspace_id=workspace_id,
        )

    # Impact-analysis pass — anchored on the existing candidate pool,
    # walks reverse-CALLS, forward-AFFECTS, structural inheritors and
    # API carriers. Fires only when the intent classifier explicitly
    # gestures at "what's affected if this changes?".
    if any(m.role == "impact_analysis" for m in intent):
        existing_pool = [
            c
            for role, cands in raw_by_role.items()
            if role != "impact_analysis"
            for c in cands
        ]
        if existing_pool:
            raw_by_role["impact_analysis"] = expand_impact_neighbourhood(
                existing_pool, db=db, workspace_id=workspace_id,
            )

    # Multi-role *intersection* pass — when ≥2 intents fire we use the
    # weaker signals as structural constraints, dropping primary
    # candidates that have no graph proximity to any secondary
    # candidate. ``fallback_on_empty`` keeps the original primary list
    # if no candidate intersects, so a single-role question never
    # silently zeros out.
    if len(intent) >= 2:
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

    by_repo: defaultdict[str, list[QuestionResult]] = defaultdict(list)
    for r in scored:
        by_repo[r.repo].append(r)
    by_repo_summary = {
        repo: {
            "questions": len(items),
            "mean_recall": sum(r.file_recall for r in items) / len(items),
            "full_recall": sum(1 for r in items if r.file_recall >= 1.0 - 1e-9),
            "zero_recall": sum(1 for r in items if r.file_recall == 0.0),
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
        f"- overall mean file_recall: **{summary['overall_mean_recall']:.3f}**",
        f"- full-recall questions: {summary['full_recall_questions']}",
        f"- zero-recall questions: {summary['zero_recall_questions']}",
        "",
        "## Per-repo",
        "",
        "| repo | questions | mean_recall | full | zero |",
        "|---|---|---|---|---|",
    ]
    for repo, info in summary["per_repo"].items():
        lines.append(
            f"| {repo} | {info['questions']} | {info['mean_recall']:.3f} | "
            f"{info['full_recall']} | {info['zero_recall']} |"
        )
    lines.extend([
        "",
        "## Intent classifier — top role distribution",
        "",
        f"`{json.dumps(summary['intent_top_role_counts'], sort_keys=True)}`",
        "",
        "## Per-question detail",
        "",
        "| id | repo | recall | matched/expected | intent | candidates |",
        "|---|---|---|---|---|---|",
    ])
    for r in sorted(results, key=lambda x: (x.repo, x.question_id)):
        if r.skipped_reason:
            lines.append(
                f"| {r.question_id} | {r.repo} | — | — | — | "
                f"skipped: {r.skipped_reason} |"
            )
            continue
        intent_str = (
            f"{r.intent_top_role}({r.intent_top_similarity:.2f})"
            if r.intent_top_role
            else "(none)"
        )
        lines.append(
            f"| {r.question_id} | {r.repo} | {r.file_recall:.2f} | "
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
        f"\noverall mean file_recall: {prev_recall:.3f} → {curr_recall:.3f} "
        f"({arrow} {delta:+.3f})"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Axis pipeline benchmark over the real_repo question pack",
    )
    parser.add_argument(
        "--pack",
        default="tests/fixtures/real_repo_question_pack.yaml",
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
    args = parser.parse_args()

    questions = _load_pack(args.pack)
    if not questions:
        print(f"no questions in pack {args.pack}")
        return

    db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    lance = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)

    results: list[QuestionResult] = []
    for entry in questions:
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
