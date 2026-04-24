#!/usr/bin/env python3
"""
Evaluation harness for Phase 2.5 — retrieval quality and token metrics.

Usage:
    python QA/qa_benchmark.py [--report out.json] [--questions QUESTIONS_YAML]
    python QA/qa_benchmark.py --no-index (skip re-indexing if DBs already populated)

Requires:
    - Neo4j running at bolt://localhost:7687
    - LanceDB at ./data/lancedb
    - Golden fixture at tests/fixtures/sample_project/ (auto-indexed on first run)
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

import tiktoken
import yaml

_WORKSPACE_EDGE_TYPES = [
    "CALLS",
    "CALLS_DIRECT",
    "CALLS_SCOPED",
    "CALLS_IMPORTED",
    "CALLS_DYNAMIC",
    "CALLS_INFERRED",
    "CALLS_GUESS",
    "DEPENDS_ON",
    "IMPLEMENTS",
    "OVERRIDES",
    "AFFECTS",
]


def _expected_file_matches(expected: str, retrieved_files: set[str]) -> bool:
    """True iff any retrieved absolute path matches the expected path hint.

    ``expected_files`` in the real-repo question pack are relative hints
    (``fastapi/routing.py``) or plain subdirectory names (``pydantic``,
    ``tests``, ``packages/toolkit/src``). The retrieved file_paths coming
    back from the ContextArbitrator are absolute
    (``/.../QA/repos/fastapi/fastapi/routing.py``), so a naive set
    intersection is always empty.

    Matching rule: expected matches a retrieved path iff the retrieved
    path ends with ``"/" + expected`` (file-form hint) or contains
    ``"/" + expected + "/"`` (directory-form hint). This guards against
    partial-name collisions (``fast`` vs ``fastapi``) by only matching on
    full path components.
    """
    e = expected.strip().strip("/").replace("\\", "/")
    if not e:
        return False
    end_form = "/" + e
    mid_form = "/" + e + "/"
    for rf in retrieved_files:
        if not rf:
            continue
        norm = rf.replace("\\", "/")
        if norm.endswith(end_form):
            return True
        if mid_form in norm + "/":
            return True
    return False


def _compute_file_recall(expected_files: set[str], retrieved_files: set[str]) -> float:
    """Fraction of expected_files for which at least one retrieved path matches."""
    if not expected_files:
        return 0.0
    matched = sum(
        1 for expected in expected_files if _expected_file_matches(expected, retrieved_files)
    )
    return matched / len(expected_files)


class _LineProgressReporter:
    """Simple console reporter for fast indexing when tqdm is unavailable."""

    def __init__(self, prefix: str = ""):
        self._prefix = prefix
        self._stage = ""
        self._total = 0
        self._done = 0
        self._last_percent = -1

    def stage_start(self, stage: str, total: int) -> None:
        self._stage = stage
        self._total = max(0, total)
        self._done = 0
        self._last_percent = -1
        print(f"{self._prefix}[{stage}] 0/{self._total}")

    def step(self, stage: str, n: int = 1) -> None:
        if stage != self._stage:
            return
        self._done += n
        if self._total <= 0:
            return
        percent = min(100, int((self._done / self._total) * 100))
        if percent == 100 or percent // 10 > self._last_percent // 10:
            print(f"{self._prefix}[{stage}] {min(self._done, self._total)}/{self._total} ({percent}%)")
            self._last_percent = percent

    def stage_end(self, stage: str) -> None:
        if stage != self._stage:
            return
        if self._total == 0:
            print(f"{self._prefix}[{stage}] done")
        elif self._done < self._total:
            print(f"{self._prefix}[{stage}] {self._total}/{self._total} (100%)")
        self._stage = ""
        self._total = 0
        self._done = 0
        self._last_percent = -1


def _make_progress_reporter(prefix: str = ""):
    try:
        from sidecar.indexer.fast.bench import TqdmReporter

        return TqdmReporter(prefix=prefix)
    except Exception:
        return _LineProgressReporter(prefix=prefix)


def _empty_indexing_summary(*, skipped: bool = False) -> dict[str, Any]:
    return {
        "performed": False if skipped else True,
        "skipped": skipped,
        "skip_affects": False,
        "collected": 0,
        "changed": 0,
        "parsed": 0,
        "symbols_encoded": 0,
        "symbols_removed": 0,
        "affects_rebuilt": 0,
        "docs_files_indexed": 0,
        "docs_chunks_indexed": 0,
        "timings_sec": {},
        "docs_timings_sec": {},
    }


def _normalize_cleanup_prefixes(*paths: str | None) -> list[str]:
    prefixes: list[str] = []
    for path in paths:
        if not path:
            continue
        resolved = str(Path(path).resolve())
        prefixes.append(resolved)
    return sorted(set(prefixes))


def _path_matches_prefix(path: str | None, prefixes: list[str]) -> bool:
    if not path:
        return False
    resolved = str(Path(path).resolve())
    return any(
        resolved == prefix or resolved.startswith(f"{prefix}{os.sep}")
        for prefix in prefixes
    )


def _quote_lancedb(value: str) -> str:
    return value.replace("'", "''")


def reset_index_state(*, workspace_id: str, project_path: str, docs_path: str | None = None):
    """Remove graph/vector rows for the indexed project before a fresh run."""
    from sidecar.database.lancedb_client import LanceDBClient
    from sidecar.database.neo4j_client import Neo4jClient
    from sidecar.indexer.fast.schema import ensure_fast_indexes

    prefixes = _normalize_cleanup_prefixes(project_path, docs_path)
    if not prefixes:
        return

    neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "password")

    db = Neo4jClient(neo4j_uri, neo4j_user, neo4j_password)
    with db.driver.session() as session:
        session.run(
            """
            MATCH (f:File {workspace_id: $workspace_id})
            WHERE any(prefix IN $prefixes
                WHERE f.path = prefix OR f.path STARTS WITH prefix + '/')
            WITH collect(DISTINCT f) AS files
            UNWIND files AS file
            DETACH DELETE file
            """,
            workspace_id=workspace_id,
            prefixes=prefixes,
        )
        session.run(
            """
            MATCH (a:DocAnchor {workspace_id: $workspace_id})
            WHERE NOT EXISTS { MATCH (a)-[:FROM]->(:File {workspace_id: $workspace_id}) }
            DETACH DELETE a
            """,
            workspace_id=workspace_id,
        )
        session.run(
            """
            MATCH (w:Workspace {id: $workspace_id})<-[iw:IN_WORKSPACE]-(s:Symbol)
            WHERE NOT EXISTS { MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s) }
            OPTIONAL MATCH (s)-[r]-(other:Symbol)
            WHERE type(r) IN $edge_types
              AND coalesce(r.workspace_id, $workspace_id) = $workspace_id
            DELETE iw, r
            """,
            workspace_id=workspace_id,
            edge_types=_WORKSPACE_EDGE_TYPES,
        )
        session.run(
            """
            MATCH (s:Symbol)
            WHERE NOT EXISTS { MATCH (:File)-[:CONTAINS]->(s) }
              AND NOT EXISTS { MATCH (s)-[:IN_WORKSPACE]->(:Workspace) }
            DETACH DELETE s
            """
        )
        session.run(
            """
            MATCH (w:Workspace {id: $workspace_id})
            WHERE NOT EXISTS { MATCH (:File {workspace_id: $workspace_id}) }
              AND NOT EXISTS { MATCH (:DocAnchor {workspace_id: $workspace_id}) }
              AND NOT EXISTS { MATCH (:Symbol)-[:IN_WORKSPACE]->(w) }
            DETACH DELETE w
            """,
            workspace_id=workspace_id,
        )
    ensure_fast_indexes(db)
    db.close()

    vector_db = LanceDBClient()
    doc_rows = vector_db._table.to_pandas()
    doc_ids = [
        row["id"]
        for _, row in doc_rows.iterrows()
        if _path_matches_prefix(row.get("file_path"), prefixes)
    ]
    for row_id in doc_ids:
        try:
            vector_db._table.delete(f"id = '{_quote_lancedb(row_id)}'")
        except Exception:
            pass

    symbol_rows = vector_db._sym_table.to_pandas()
    symbol_uids = [
        row["uid"]
        for _, row in symbol_rows.iterrows()
        if _path_matches_prefix(row.get("file_path"), prefixes)
    ]
    if symbol_uids:
        vector_db.delete_symbol_embeddings(symbol_uids)


def setup_fixture_db(*, skip_affects: bool = False) -> tuple[str, dict[str, Any]]:
    """Index the golden fixture project into Neo4j + LanceDB (idempotent)."""
    from sidecar.indexer.docs import index_docs
    from sidecar.indexer.fast import run_fast_indexing
    from sidecar.workspace import DEFAULT_WORKSPACE_ID

    fixture_path = Path(__file__).parent.parent / "tests" / "fixtures" / "sample_project"
    docs_path = Path(__file__).parent.parent / "docs"
    reset_index_state(
        workspace_id=DEFAULT_WORKSPACE_ID,
        project_path=str(fixture_path),
        docs_path=str(docs_path) if docs_path.exists() else None,
    )
    print(f"\n[1/2] Indexing fixture: {fixture_path}")
    stats = run_fast_indexing(
        str(fixture_path),
        workspace_id=DEFAULT_WORKSPACE_ID,
        skip_affects=skip_affects,
        reporter=_make_progress_reporter(prefix="fixture "),
    )

    if docs_path.exists():
        print(f"[2/2] Indexing docs: {docs_path}")
        docs_stats = index_docs(str(docs_path))
        stats["docs_files_indexed"] = docs_stats["files_indexed"]
        stats["docs_chunks_indexed"] = docs_stats["chunks_indexed"]
        stats["docs_timings_sec"] = docs_stats["timings_sec"]
    stats["docs_indexed_path"] = str(docs_path) if docs_path.exists() else ""
    return DEFAULT_WORKSPACE_ID, stats


def load_question_pack(questions_path: str) -> dict:
    """Load a question pack from YAML.

    Supports:
    - legacy fixture format: top-level list[question]
    - real-repo pack format: {repositories: [...], questions: [...]}
    """
    with open(questions_path) as f:
        payload = yaml.safe_load(f) or []

    if isinstance(payload, list):
        return {
            "repositories": [],
            "questions": payload,
            "kind": "fixture",
        }
    if isinstance(payload, dict):
        return {
            "repositories": payload.get("repositories", []),
            "questions": payload.get("questions", []),
            "kind": "real_repo" if payload.get("repositories") else "fixture",
        }
    raise ValueError(f"Unsupported question pack format in {questions_path}")


def load_questions(
    questions_path: str,
    *,
    repo: str | None = None,
    core12_only: bool = False,
) -> list:
    """Load question set from YAML, with optional filters."""
    pack = load_question_pack(questions_path)
    questions = pack["questions"]
    if repo:
        questions = [question for question in questions if question.get("repo") == repo]
    if core12_only:
        questions = [question for question in questions if question.get("core12", False)]
    return questions


def load_repository_meta(questions_path: str, repo: str) -> dict[str, Any] | None:
    """Return repository metadata from a real-repo question pack."""
    pack = load_question_pack(questions_path)
    for item in pack["repositories"]:
        if item.get("id") == repo:
            return item
    return None


def default_repo_checkout_path(repo: str, *, repos_root: str | None = None) -> Path:
    root = Path(repos_root) if repos_root else Path(__file__).parent / "repos"
    return root / repo


def resolve_repo_docs_path(
    project_path: str,
    *,
    docs_path: str | None = None,
    preferred_locale: str = "en",
) -> str | None:
    """Resolve a benchmark docs path, preferring one canonical locale when present."""
    if docs_path:
        return str(Path(docs_path).resolve())

    docs_root = Path(project_path) / "docs"
    if not docs_root.exists():
        return None

    preferred = docs_root / preferred_locale / "docs"
    if preferred.exists():
        return str(preferred.resolve())

    return str(docs_root.resolve())


def ensure_repo_checkout(
    questions_path: str,
    repo: str,
    *,
    project_path: str | None = None,
    repos_root: str | None = None,
) -> str:
    """Resolve or clone a repository checkout for a real-repo benchmark pack."""
    if project_path:
        return str(Path(project_path).resolve())

    repo_meta = load_repository_meta(questions_path, repo)
    if repo_meta is None:
        raise ValueError(f"Repository '{repo}' is not defined in {questions_path}")

    checkout_path = default_repo_checkout_path(repo, repos_root=repos_root).resolve()
    if checkout_path.exists():
        return str(checkout_path)

    clone_url = repo_meta.get("clone_url")
    if not clone_url:
        raise ValueError(f"Repository '{repo}' does not define clone_url in {questions_path}")

    checkout_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[checkout] Cloning {repo_meta.get('name', repo)} into {checkout_path}")
    subprocess.run(
        ["git", "clone", "--depth", "1", clone_url, str(checkout_path)],
        check=True,
        text=True,
    )
    return str(checkout_path)


def count_tokens(text: str) -> int:
    """Count tokens using cl100k_base encoding."""
    enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text))


def setup_real_repo_db(
    project_path: str,
    *,
    workspace_id: str | None,
    docs_path: str | None,
    skip_affects: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Index a real repository checkout and optional docs path."""
    from sidecar.indexer.docs import index_docs
    from sidecar.indexer.fast import run_fast_indexing
    from sidecar.workspace import WorkspaceResolver

    workspace = WorkspaceResolver().from_project_path(project_path, value=workspace_id)
    resolved_docs_path = resolve_repo_docs_path(project_path, docs_path=docs_path)

    reset_index_state(
        workspace_id=workspace.id,
        project_path=project_path,
        docs_path=resolved_docs_path,
    )
    print(f"\n[1/2] Indexing real repository: {project_path}")
    stats = run_fast_indexing(
        project_path,
        workspace_id=workspace.id,
        skip_affects=skip_affects,
        reporter=_make_progress_reporter(prefix=f"{Path(project_path).name} "),
    )

    if resolved_docs_path and Path(resolved_docs_path).exists():
        print(f"[2/2] Indexing repository docs: {resolved_docs_path}")
        docs_stats = index_docs(resolved_docs_path, workspace_id=workspace.id)
        stats["docs_files_indexed"] = docs_stats["files_indexed"]
        stats["docs_chunks_indexed"] = docs_stats["chunks_indexed"]
        stats["docs_timings_sec"] = docs_stats["timings_sec"]
        stats["docs_indexed_path"] = resolved_docs_path
    else:
        print("[2/2] No repository docs path detected, skipping doc indexing.")
        stats["docs_indexed_path"] = ""
        stats["docs_files_indexed"] = 0
        stats["docs_chunks_indexed"] = 0
        stats["docs_timings_sec"] = {}
    return workspace.id, stats


def run_benchmark(
    questions_path: str = None,
    no_index: bool = False,
    repo: str | None = None,
    core12_only: bool = False,
    project_path: str | None = None,
    docs_path: str | None = None,
    workspace_id: str | None = None,
    repos_root: str | None = None,
    skip_affects: bool = False,
) -> dict:
    """Run the benchmark suite and return metrics dict."""
    if not questions_path:
        questions_path = str(
            Path(__file__).parent.parent / "tests" / "fixtures" / "sample_project" / "questions.yaml"
        )

    question_pack = load_question_pack(questions_path)
    is_real_repo_pack = question_pack["kind"] == "real_repo"

    print("="*70)
    print("EVALUATION HARNESS — Phase 2.5")
    print("="*70)

    active_workspace_id = workspace_id or ""
    active_project_path = project_path
    indexing_summary = _empty_indexing_summary(skipped=True)

    if is_real_repo_pack and repo:
        active_project_path = ensure_repo_checkout(
            questions_path,
            repo,
            project_path=project_path,
            repos_root=repos_root,
        )

    if not no_index and not is_real_repo_pack:
        active_workspace_id, indexing_summary = setup_fixture_db(skip_affects=skip_affects)
    elif not no_index and is_real_repo_pack:
        if active_project_path:
            active_workspace_id, indexing_summary = setup_real_repo_db(
                active_project_path,
                workspace_id=workspace_id,
                docs_path=docs_path,
                skip_affects=skip_affects,
            )
        else:
            print("\n[info] Real-repository question pack detected.")
            print("[info] Automatic sample fixture indexing skipped.")
            print("[info] Pass --repo or --project-path to index and benchmark a real checkout.\n")

    from sidecar.context.arbitrator import ContextArbitrator
    from sidecar.database.neo4j_client import Neo4jClient
    from sidecar.workspace import DEFAULT_WORKSPACE_ID, WorkspaceResolver

    neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "password")

    db = Neo4jClient(neo4j_uri, neo4j_user, neo4j_password)
    if not active_workspace_id and active_project_path:
        active_workspace_id = WorkspaceResolver().from_project_path(active_project_path).id
    arb = ContextArbitrator(db, workspace_id=active_workspace_id or DEFAULT_WORKSPACE_ID)

    questions = load_questions(questions_path, repo=repo, core12_only=core12_only)
    results = []

    print(f"\n{'-'*70}")
    print(f"Running {len(questions)} questions...")
    print(f"{'-'*70}\n")

    for q in questions:
        symbol = q.get("symbol")
        question_text = q.get("question")
        expected_symbols = set(q.get("expected_symbols", []))
        difficulty = q.get("difficulty", "unknown")
        intent = q.get("intent", "unknown")
        expected_files = set(q.get("expected_files", []))

        # Measure assembly time
        start_ms = time.time()
        ctx = arb.get_context_for_symbol(symbol, question=question_text)
        end_ms = time.time()

        assembly_ms = (end_ms - start_ms) * 1000

        # Handle error case
        if isinstance(ctx, str):
            print(f"  ❌ {q['id']}: {symbol} — {ctx}")
            results.append({
                "id": q["id"],
                "repo": q.get("repo", ""),
                "symbol": symbol,
                "question": question_text,
                "status": "error",
                "error": ctx,
                "assembly_ms": assembly_ms,
            })
            continue

        # Extract retrieved symbols
        retrieved_symbols = {dep.symbol for dep in ctx.graph_context}
        primary_symbol = {ctx.primary_source.symbol}
        all_retrieved = retrieved_symbols | primary_symbol
        retrieved_files = {
            file_path
            for file_path in [
                ctx.primary_source.file_path,
                *[dep.file_path for dep in ctx.graph_context],
                *[doc.source_file for doc in ctx.documentation],
            ]
            if file_path
        }

        # Compute recall@k and precision@k
        intersection = all_retrieved & expected_symbols
        recall_at_k = len(intersection) / len(expected_symbols) if expected_symbols else 0.0
        precision_at_k = len(intersection) / len(all_retrieved) if all_retrieved else 0.0
        file_recall = _compute_file_recall(expected_files, retrieved_files)

        # Token counts
        tokens_surgical = ctx.token_count()

        # Carpet-bomb baseline: all files containing any expected symbol
        # (For now, estimate as sum of file token counts containing expected symbols)
        tokens_carpet_bomb = tokens_surgical * 2  # Placeholder; improve later with actual file union

        # Calculate reduction ratio
        reduction_ratio = 1 - (tokens_surgical / tokens_carpet_bomb) if tokens_carpet_bomb > 0 else 0.0

        status = "pass" if recall_at_k >= 0.8 and precision_at_k >= 0.6 else "warn"
        status_emoji = "✅" if status == "pass" else "⚠️"

        print(
            f"  {status_emoji} {q['id']}: {symbol:20} "
            f"| recall={recall_at_k:.2f} | precision={precision_at_k:.2f} "
            f"| files={file_recall:.2f} | {tokens_surgical}t"
        )

        results.append({
            "id": q["id"],
            "repo": q.get("repo", ""),
            "symbol": symbol,
            "question": question_text,
            "difficulty": difficulty,
            "intent": intent,
            "status": status,
            "retrieved_symbols": sorted(list(all_retrieved)),
            "expected_symbols": sorted(list(expected_symbols)),
            "retrieved_files": sorted(list(retrieved_files)),
            "expected_files": sorted(list(expected_files)),
            "recall_at_k": recall_at_k,
            "precision_at_k": precision_at_k,
            "file_recall": file_recall,
            "tokens_surgical": tokens_surgical,
            "tokens_carpet_bomb": tokens_carpet_bomb,
            "reduction_ratio": reduction_ratio,
            "assembly_ms": assembly_ms,
        })

    db.close()

    # Aggregate metrics
    passes = sum(1 for r in results if r.get("status") == "pass")
    total = len(results)
    avg_recall = sum(r.get("recall_at_k", 0) for r in results) / total if total > 0 else 0.0
    avg_precision = sum(r.get("precision_at_k", 0) for r in results) / total if total > 0 else 0.0
    avg_file_recall = sum(r.get("file_recall", 0) for r in results) / total if total > 0 else 0.0
    total_tokens_surgical = sum(r.get("tokens_surgical", 0) for r in results)
    total_tokens_carpet = sum(r.get("tokens_carpet_bomb", 0) for r in results)
    avg_assembly_ms = sum(r.get("assembly_ms", 0) for r in results) / total if total > 0 else 0.0

    metrics = {
        "timestamp": time.time(),
        "question_pack": {
            "path": questions_path,
            "kind": question_pack["kind"],
            "repo_filter": repo or "",
            "core12_only": core12_only,
            "project_path": active_project_path or "",
            "docs_path": docs_path or "",
            "workspace_id": active_workspace_id,
            "repos_root": str(Path(repos_root).resolve()) if repos_root else "",
            "skip_affects": skip_affects,
        },
        "indexing": indexing_summary,
        "summary": {
            "total_questions": total,
            "pass_count": passes,
            "pass_rate": passes / total if total > 0 else 0.0,
            "recall_at_5": avg_recall,
            "precision_at_5": avg_precision,
            "file_recall": avg_file_recall,
            "tokens_surgical": total_tokens_surgical,
            "tokens_carpet_bomb": total_tokens_carpet,
            "reduction_ratio": 1 - (total_tokens_surgical / total_tokens_carpet)
            if total_tokens_carpet > 0
            else 0.0,
            "assembly_ms_avg": avg_assembly_ms,
        },
        "results": results,
    }

    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"Pass rate:       {metrics['summary']['pass_rate']:.1%} ({passes}/{total})")
    print(f"Recall@5:        {metrics['summary']['recall_at_5']:.2f}")
    print(f"Precision@5:     {metrics['summary']['precision_at_5']:.2f}")
    print(f"File recall:     {metrics['summary']['file_recall']:.2f}")
    print(f"Tokens (surgical): {metrics['summary']['tokens_surgical']:,}")
    print(f"Tokens (carpet):   {metrics['summary']['tokens_carpet_bomb']:,}")
    print(f"Reduction:       {metrics['summary']['reduction_ratio']:.1%}")
    print(f"Avg assembly:    {metrics['summary']['assembly_ms_avg']:.1f}ms")
    if metrics["indexing"]["skipped"]:
        print("Indexing:        skipped")
    else:
        print(
            "Indexing:        "
            f"collected={metrics['indexing']['collected']} "
            f"changed={metrics['indexing']['changed']} "
            f"parsed={metrics['indexing']['parsed']}"
        )
        print(
            "Index timings:   "
            f"{metrics['indexing']['timings_sec']}"
        )
        if metrics["indexing"].get("docs_timings_sec"):
            print(
                "Docs timings:    "
                f"{metrics['indexing']['docs_timings_sec']}"
            )
    print(f"{'='*70}\n")

    return metrics


def main():
    parser = argparse.ArgumentParser(
        description="Evaluation harness for Phase 2.5",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--report",
        help="Output metrics to JSON file",
        default=None,
    )
    parser.add_argument(
        "--questions",
        help="Path to questions.yaml",
        default=None,
    )
    parser.add_argument(
        "--repo",
        help="Filter a real-repo question pack to one repository id",
        default=None,
    )
    parser.add_argument(
        "--core12",
        action="store_true",
        help="Run only questions marked core12: true",
    )
    parser.add_argument(
        "--project-path",
        help="Path to a checked out real repository to index/benchmark",
        default=None,
    )
    parser.add_argument(
        "--docs-path",
        help="Optional docs path to index for a real repository benchmark",
        default=None,
    )
    parser.add_argument(
        "--workspace-id",
        help="Optional explicit workspace id override",
        default=None,
    )
    parser.add_argument(
        "--repos-root",
        help="Directory for auto-cloned benchmark repositories (default: QA/repos)",
        default=None,
    )
    parser.add_argument(
        "--skip-affects",
        action="store_true",
        help="Skip AFFECTS rebuild during indexing to compare raw retrieval/index speed",
    )
    parser.add_argument(
        "--no-index",
        action="store_true",
        help="Skip re-indexing (use existing DB)",
    )
    args = parser.parse_args()

    metrics = run_benchmark(
        questions_path=args.questions,
        no_index=args.no_index,
        repo=args.repo,
        core12_only=args.core12,
        project_path=args.project_path,
        docs_path=args.docs_path,
        workspace_id=args.workspace_id,
        repos_root=args.repos_root,
        skip_affects=args.skip_affects,
    )

    if args.report:
        with open(args.report, "w") as f:
            json.dump(metrics, f, indent=2)
        print(f"Metrics saved to: {args.report}")

        # Append to baselines.jsonl for historical tracking
        baseline_file = Path(__file__).parent / "baselines.jsonl"
        with open(baseline_file, "a") as f:
            f.write(json.dumps(metrics["summary"]) + "\n")
        print(f"Baseline appended to: {baseline_file}")

    return 0 if metrics["summary"]["pass_rate"] >= 0.8 else 1


if __name__ == "__main__":
    sys.exit(main())
