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
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

import tiktoken
import yaml

from sidecar.context.role_taxonomy import normalize_roles

_PASS_GATES = {
    "explain_behavior": {"role_recall": 0.70, "file_recall": 0.50},
    "trace_dependency": {"role_recall": 0.80, "file_recall": 0.70},
    "impact_analysis": {"role_recall": 0.60, "file_recall": 0.50},
}

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


def _compute_role_recall(required_roles: list[str], ctx_missing_roles: list[str]) -> float:
    """Fraction of required_roles the ranker fulfilled (not in ctx.missing_roles).

    Both inputs are normalized into the canonical cross-framework taxonomy
    before comparison so legacy pack role names and ranker-native names share
    one scale.
    """
    required = normalize_roles(required_roles)
    if not required:
        return 1.0
    missing_set = set(normalize_roles(ctx_missing_roles))
    fulfilled = sum(1 for r in required if r not in missing_set)
    return fulfilled / len(required)


def _compute_file_recall(expected_files: set[str], retrieved_files: set[str]) -> float:
    """Fraction of expected_files for which at least one retrieved path matches."""
    if not expected_files:
        return 0.0
    matched = sum(
        1 for expected in expected_files if _expected_file_matches(expected, retrieved_files)
    )
    return matched / len(expected_files)


def _format_roles_for_column(roles: list[str], *, max_len: int = 72) -> str:
    """Compact role list for console columns."""
    if not roles:
        return "-"
    text = ",".join(roles)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


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
            print(
                f"{self._prefix}[{stage}] {min(self._done, self._total)}/{self._total} ({percent}%)"
            )
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
    from sidecar.indexer.repository_profile import build_empty_repository_profile

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
        "repository_profile": build_empty_repository_profile(
            reason="benchmark_indexing_skipped" if skipped else "not_built"
        ),
        "repository_profile_store": "",
    }


def default_report_output_path(
    *,
    repo: str | None = None,
    project_path: str | None = None,
    core12_only: bool = False,
    now: float | None = None,
) -> str:
    """Return a stable default JSON report path for ad-hoc benchmark runs."""
    if repo:
        label = repo
    elif project_path:
        label = Path(project_path).name or "project"
    else:
        label = "fixture"
    label = re.sub(r"[^a-zA-Z0-9_.-]+", "_", label).strip("_") or "benchmark"
    timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now or time.time()))
    suffix = "_core12" if core12_only else ""
    filename = f"qa_benchmark_{label}{suffix}_{timestamp}.json"
    return str((Path(tempfile.gettempdir()) / filename).resolve())


def default_snapshot_manifest_path() -> str:
    """Return the local JSONL registry path for benchmark report snapshots."""
    return str((Path(__file__).parent / "benchmark_runs.jsonl").resolve())


def write_metrics_report(metrics: dict[str, Any], report_path: str) -> str:
    """Write metrics JSON and return the resolved absolute path."""
    resolved = Path(report_path).resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(metrics)
    payload["report_path"] = str(resolved)
    with open(resolved, "w") as f:
        json.dump(payload, f, indent=2)
    metrics["report_path"] = str(resolved)
    return str(resolved)


def _git_output(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=Path(__file__).parent.parent,
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return ""
    return result.stdout.strip()


def build_snapshot_manifest_row(
    metrics: dict[str, Any],
    report_path: str,
    *,
    git_commit: str | None = None,
    git_branch: str | None = None,
) -> dict[str, Any]:
    """Build a compact JSONL row pointing to a full benchmark report."""
    summary = metrics.get("summary", {})
    question_pack = metrics.get("question_pack", {})
    indexing = metrics.get("indexing", {})
    profile = indexing.get("repository_profile") or {}
    capabilities = profile.get("capabilities") or {}
    strategy = profile.get("strategy_profile") or {}
    return {
        "timestamp": metrics.get("timestamp"),
        "report_path": str(Path(report_path).resolve()),
        "git_commit": git_commit if git_commit is not None else _git_output("rev-parse", "HEAD"),
        "git_branch": git_branch
        if git_branch is not None
        else _git_output("branch", "--show-current"),
        "repo": question_pack.get("repo_filter") or "",
        "core12_only": bool(question_pack.get("core12_only")),
        "workspace_id": question_pack.get("workspace_id") or "",
        "question_pack": question_pack.get("path") or "",
        "indexing_skipped": bool(indexing.get("skipped")),
        "repository_readiness": profile.get("retrieval_readiness", ""),
        "repository_indexability": profile.get("indexability", ""),
        "repository_profile_store": indexing.get("repository_profile_store", ""),
        "selected_strategy": strategy.get("selected_strategy", ""),
        "impact_readiness": capabilities.get("impact_analysis", ""),
        "total_questions": summary.get("total_questions", 0),
        "pass_count": summary.get("pass_count", 0),
        "pass_rate": summary.get("pass_rate", 0.0),
        "precision_at_5": summary.get("precision_at_5", summary.get("precision", 0.0)),
        "file_recall": summary.get("file_recall", 0.0),
        "role_recall": summary.get("role_recall", 0.0),
        "tokens_surgical": summary.get("tokens_surgical", 0),
        "reduction_ratio": summary.get("reduction_ratio", 0.0),
        "assembly_ms_avg": summary.get("assembly_ms_avg", 0.0),
    }


def append_snapshot_manifest(
    metrics: dict[str, Any],
    report_path: str,
    manifest_path: str | None = None,
) -> str:
    """Append one compact benchmark snapshot row and return the manifest path."""
    resolved = Path(manifest_path or default_snapshot_manifest_path()).resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    row = build_snapshot_manifest_row(metrics, report_path)
    with open(resolved, "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
    return str(resolved)


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
        resolved == prefix or resolved.startswith(f"{prefix}{os.sep}") for prefix in prefixes
    )


def _quote_lancedb(value: str) -> str:
    return value.replace("'", "''")


def reset_index_state(
    *,
    workspace_id: str,
    project_path: str,
    docs_path: str | None = None,
    wipe_workspace: bool = False,
):
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
        if wipe_workspace:
            session.run(
                """
                MATCH (a:DocAnchor {workspace_id: $workspace_id})
                DETACH DELETE a
                """,
                workspace_id=workspace_id,
            )
            session.run(
                """
                MATCH (f:File {workspace_id: $workspace_id})
                DETACH DELETE f
                """,
                workspace_id=workspace_id,
            )
        else:
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


def setup_fixture_db(
    *, skip_affects: bool = False, skip_docs: bool = False
) -> tuple[str, dict[str, Any]]:
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
        wipe_workspace=True,
    )
    print(f"\n[1/2] Indexing fixture: {fixture_path}")
    stats = run_fast_indexing(
        str(fixture_path),
        workspace_id=DEFAULT_WORKSPACE_ID,
        skip_affects=skip_affects,
        reporter=_make_progress_reporter(prefix="fixture "),
    )

    if docs_path.exists() and not skip_docs:
        print(f"[2/2] Indexing docs: {docs_path}")
        docs_stats = index_docs(str(docs_path))
        stats["docs_files_indexed"] = docs_stats["files_indexed"]
        stats["docs_chunks_indexed"] = docs_stats["chunks_indexed"]
        stats["docs_timings_sec"] = docs_stats["timings_sec"]
    elif skip_docs:
        print("[2/2] Skipping docs indexing (--skip-docs)")
    stats["docs_indexed_path"] = str(docs_path) if (docs_path.exists() and not skip_docs) else ""
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


def resolve_questions_path(
    questions_path: str | None,
    *,
    repo: str | None = None,
    project_path: str | None = None,
) -> str:
    """Pick the default question pack for fixture vs real-repo workflows."""
    if questions_path:
        return str(Path(questions_path).resolve())

    root = Path(__file__).parent.parent
    if repo or project_path:
        return str((root / "tests" / "fixtures" / "real_repo_question_pack.yaml").resolve())
    return str((root / "tests" / "fixtures" / "sample_project" / "questions.yaml").resolve())


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


_TOKEN_CACHE: dict[str, int] = {}


def _expected_file_tokens(path: Path) -> int:
    """Cached token count for a single file. Returns 0 if unreadable."""
    key = str(path)
    cached = _TOKEN_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        _TOKEN_CACHE[key] = 0
        return 0
    count = count_tokens(text)
    _TOKEN_CACHE[key] = count
    return count


def compute_carpet_bomb_tokens(
    expected_files: set[str],
    baseline_root: str | None,
) -> int:
    """Sum token counts of every expected file, resolved under baseline_root.

    ``expected_files`` may contain file-form hints (``fastapi/routing.py``)
    or directory-form hints (``pydantic``, ``packages/toolkit/src``). For
    directory hints we recurse and count every file that survives a simple
    build-artifact prefilter. For file hints we count that single file.

    Returns 0 when no hints resolve. That marks the baseline as
    "unavailable for this question" so downstream code can ignore it
    instead of dividing by a fake number.
    """
    if not expected_files or not baseline_root:
        return 0

    _SKIP_DIRS = {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        ".pytest_cache",
        ".mypy_cache",
    }
    _CODE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".rst", ".yaml", ".yml", ".json"}

    root = Path(baseline_root)
    total = 0

    for hint in expected_files:
        normalized = hint.strip().strip("/").replace("\\", "/")
        if not normalized:
            continue
        resolved = root / normalized
        if resolved.is_file():
            total += _expected_file_tokens(resolved)
            continue
        if resolved.is_dir():
            for entry in resolved.rglob("*"):
                if not entry.is_file():
                    continue
                if any(part in _SKIP_DIRS for part in entry.parts):
                    continue
                if entry.suffix.lower() not in _CODE_EXTS:
                    continue
                total += _expected_file_tokens(entry)

    return total


def _build_ready_context_payload(ctx, token_count: int) -> dict[str, Any]:
    """Serialize the fully assembled context that would be sent to the model."""
    return {
        "token_count": token_count,
        "contract": ctx.to_dict(),
        "system_prompt": ctx.to_system_prompt(),
    }


def setup_real_repo_db(
    project_path: str,
    *,
    workspace_id: str | None,
    docs_path: str | None,
    skip_affects: bool = False,
    skip_docs: bool = False,
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
        wipe_workspace=True,
    )
    print(f"\n[1/2] Indexing real repository: {project_path}")
    stats = run_fast_indexing(
        project_path,
        workspace_id=workspace.id,
        skip_affects=skip_affects,
        reporter=_make_progress_reporter(prefix=f"{Path(project_path).name} "),
    )

    if skip_docs:
        print("[2/2] Skipping repository docs indexing (--skip-docs)")
        stats["docs_indexed_path"] = ""
        stats["docs_files_indexed"] = 0
        stats["docs_chunks_indexed"] = 0
        stats["docs_timings_sec"] = {}
        return workspace.id, stats
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
    skip_docs: bool = False,
    ranker_weights=None,
) -> dict:
    """Run the benchmark suite and return metrics dict."""
    questions_path = resolve_questions_path(
        questions_path,
        repo=repo,
        project_path=project_path,
    )

    question_pack = load_question_pack(questions_path)
    is_real_repo_pack = question_pack["kind"] == "real_repo"
    questions = load_questions(questions_path, repo=repo, core12_only=core12_only)

    if not questions:
        filters = []
        if repo:
            filters.append(f"repo={repo}")
        if core12_only:
            filters.append("core12_only=True")
        filter_summary = ", ".join(filters) if filters else "no filters"
        raise ValueError(
            "No benchmark questions matched the selected question pack and filters: "
            f"{questions_path} ({filter_summary})"
        )

    print("=" * 70)
    print("EVALUATION HARNESS — Phase 2.5")
    print("=" * 70)

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

    # Where do we resolve ``expected_files`` hints against for the carpet-bomb
    # baseline? For real-repo packs it's the repo checkout; for the fixture
    # pack the sample_project dir.
    if is_real_repo_pack:
        baseline_root = active_project_path
    else:
        baseline_root = str(Path(__file__).parent.parent / "tests" / "fixtures" / "sample_project")

    if not no_index and not is_real_repo_pack:
        active_workspace_id, indexing_summary = setup_fixture_db(
            skip_affects=skip_affects, skip_docs=skip_docs
        )
    elif not no_index and is_real_repo_pack:
        if active_project_path:
            active_workspace_id, indexing_summary = setup_real_repo_db(
                active_project_path,
                workspace_id=workspace_id,
                docs_path=docs_path,
                skip_affects=skip_affects,
                skip_docs=skip_docs,
            )
        else:
            print("\n[info] Real-repository question pack detected.")
            print("[info] Automatic sample fixture indexing skipped.")
            print("[info] Pass --repo or --project-path to index and benchmark a real checkout.\n")

    from sidecar.context.arbitrator import ContextArbitrator
    from sidecar.database.lancedb_client import LanceDBClient
    from sidecar.database.neo4j_client import Neo4jClient
    from sidecar.workspace import DEFAULT_WORKSPACE_ID, WorkspaceResolver

    neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "password")

    db = Neo4jClient(neo4j_uri, neo4j_user, neo4j_password)
    vector_db = LanceDBClient()
    if not active_workspace_id and active_project_path:
        active_workspace_id = WorkspaceResolver().from_project_path(active_project_path).id
    arb = ContextArbitrator(
        db,
        vector_db=vector_db,
        workspace_id=active_workspace_id or DEFAULT_WORKSPACE_ID,
        ranker_weights=ranker_weights,
    )
    results = []

    print(f"\n{'-' * 70}")
    print(f"Running {len(questions)} questions...")
    print(f"{'-' * 70}\n")

    for q in questions:
        symbol = q.get("symbol")
        question_text = q.get("question")
        expected_symbols = set(q.get("expected_symbols", []))
        difficulty = q.get("difficulty", "unknown")
        intent = q.get("intent", "unknown")
        expected_mode = q.get("expected_mode", "symbol")
        mechanism = q.get("mechanism", "")
        required_roles = q.get("required_roles", [])
        expected_files = set(q.get("expected_files", []))

        # Measure assembly time
        # Benchmark questions can target whole classes / large modules whose
        # own bodies exceed the chat-default 4000 token budget. Run the
        # benchmark at the spec'd default (40k) so retrieval quality —
        # not budget caps — is what we're measuring. Per-question overrides
        # via question_pack ``token_budget`` field stay supported.
        question_budget = int(q.get("token_budget", 4000))
        start_ms = time.time()
        ctx = arb.get_context_for_symbol(
            symbol, question=question_text, token_budget=question_budget
        )
        end_ms = time.time()

        assembly_ms = (end_ms - start_ms) * 1000

        # Handle error case
        if isinstance(ctx, str):
            is_correct_rejection = expected_mode == "workspace" and "not found" in ctx
            status = "pass" if is_correct_rejection else "error"
            gate = "workspace_correct_rejection" if is_correct_rejection else "error"
            emoji = "✅" if status == "pass" else "❌"
            line_suffix = (
                "workspace mode: absent symbol handled as expected" if is_correct_rejection else ctx
            )
            print(f"  {emoji} {q['id']}: {symbol} [{intent}] — {line_suffix}")
            results.append(
                {
                    "id": q["id"],
                    "repo": q.get("repo", ""),
                    "symbol": symbol,
                    "question": question_text,
                    "status": status,
                    "gate": gate,
                    "error": ctx,
                    "assembly_ms": assembly_ms,
                    "mechanism": mechanism,
                    "expected_roles": normalize_roles(required_roles),
                    "missing_expected_roles": normalize_roles(required_roles),
                    "role_recall": 1.0 if is_correct_rejection else 0.0,
                    "precision": 0.0,
                    "ready_context": None,
                }
            )
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

        required_roles_canonical = normalize_roles(required_roles)
        missing_roles_canonical = normalize_roles(ctx.missing_roles)
        missing_expected_roles = [
            role for role in required_roles_canonical if role in set(missing_roles_canonical)
        ]
        ranker_required_roles = normalize_roles(
            getattr(ctx, "ranker_state", {}).get("required_roles", [])
        )
        strategy_profile = getattr(ctx, "ranker_state", {}).get("strategy_profile", {}) or {}

        # Compute recall@k and precision@k
        intersection = all_retrieved & expected_symbols
        recall_at_k = len(intersection) / len(expected_symbols) if expected_symbols else 0.0
        precision_at_k = len(intersection) / len(all_retrieved) if all_retrieved else 0.0
        file_recall = _compute_file_recall(expected_files, retrieved_files)
        role_recall = _compute_role_recall(required_roles_canonical, missing_roles_canonical)

        # Token counts
        tokens_surgical = ctx.token_count()

        # Carpet-bomb baseline: sum of every expected file's token count,
        # resolved under ``baseline_root``. 0 means the question did not
        # declare expected_files or none of them resolved on disk.
        tokens_carpet_bomb = compute_carpet_bomb_tokens(expected_files, baseline_root)

        # Calculate reduction ratio only when baseline is real.
        reduction_ratio = (
            1 - (tokens_surgical / tokens_carpet_bomb) if tokens_carpet_bomb > 0 else 0.0
        )

        # Pass/warn gate.
        # For real-repo packs with mechanism, use intent-stratified gates.
        # For other cases, fall back to legacy gates.
        if is_real_repo_pack and mechanism:
            gate_cfg = _PASS_GATES.get(intent, _PASS_GATES["explain_behavior"])
            rr_ok = role_recall >= gate_cfg["role_recall"]
            fr_ok = file_recall >= gate_cfg["file_recall"]
            if intent == "impact_analysis":
                status = "pass" if (rr_ok or fr_ok) else "warn"
            else:
                status = "pass" if (rr_ok and fr_ok) else "warn"
            gate = f"{intent}(rr>={gate_cfg['role_recall']},fr>={gate_cfg['file_recall']})"
        elif is_real_repo_pack and expected_files:
            status = "pass" if file_recall >= 0.8 else "warn"
            gate = "file_recall_legacy"
        else:
            status = "pass" if recall_at_k >= 0.8 and precision_at_k >= 0.6 else "warn"
            gate = "symbol"
        status_emoji = "✅" if status == "pass" else "⚠️"

        reasoning_info = f" | {ctx.stopped_reason}"
        if ctx.missing_roles:
            reasoning_info += f" | missing: {','.join(ctx.missing_roles)}"

        print(
            f"  {status_emoji} {q['id']}: {symbol:20} [{intent}]"
            f" | precision={precision_at_k:.2f} | role={role_recall:.2f}"
            f" | file={file_recall:.2f} | {tokens_surgical}t"
            f" | expected_roles={_format_roles_for_column(required_roles_canonical)}"
            f" | missing_roles={_format_roles_for_column(missing_expected_roles)}"
            f"{reasoning_info}"
        )

        results.append(
            {
                "id": q["id"],
                "repo": q.get("repo", ""),
                "symbol": symbol,
                "question": question_text,
                "difficulty": difficulty,
                "intent": intent,
                "status": status,
                "gate": gate,
                "retrieved_symbols": sorted(list(all_retrieved)),
                "expected_symbols": sorted(list(expected_symbols)),
                "retrieved_files": sorted(list(retrieved_files)),
                "expected_files": sorted(list(expected_files)),
                "recall_at_k": recall_at_k,
                "precision": precision_at_k,
                "precision_at_k": precision_at_k,
                "file_recall": file_recall,
                "role_recall": role_recall,
                "stopped_reason": ctx.stopped_reason,
                "missing_roles": ctx.missing_roles,
                "missing_roles_canonical": missing_roles_canonical,
                "missing_expected_roles": missing_expected_roles,
                "mechanism": mechanism,
                "expected_roles": required_roles_canonical,
                "required_roles": required_roles,
                "required_roles_canonical": required_roles_canonical,
                "ranker_required_roles": ranker_required_roles,
                "selected_strategy": strategy_profile.get("selected_strategy", ""),
                "strategy_role_plan": strategy_profile.get("role_plan", []),
                "strategy_archetypes": strategy_profile.get("mechanism_archetypes", []),
                "expected_mode": expected_mode,
                "tokens_surgical": tokens_surgical,
                "tokens_carpet_bomb": tokens_carpet_bomb,
                "reduction_ratio": reduction_ratio,
                "assembly_ms": assembly_ms,
                "ready_context": _build_ready_context_payload(ctx, tokens_surgical),
            }
        )

    db.close()

    # Aggregate metrics
    passes = sum(1 for r in results if r.get("status") == "pass")
    total = len(results)

    # Reasoning stats
    reason_counts = {}
    for r in results:
        reason = r.get("stopped_reason", "unknown")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    avg_recall = sum(r.get("recall_at_k", 0) for r in results) / total if total > 0 else 0.0
    avg_precision = sum(r.get("precision_at_k", 0) for r in results) / total if total > 0 else 0.0
    avg_file_recall = sum(r.get("file_recall", 0) for r in results) / total if total > 0 else 0.0
    avg_role_recall = sum(r.get("role_recall", 0) for r in results) / total if total > 0 else 0.0
    total_tokens_surgical = sum(r.get("tokens_surgical", 0) for r in results)
    # Carpet-bomb is only summed for questions that declared a real baseline
    # (expected_files resolved to >0 tokens). Mixing in "0" rows would
    # deflate the ratio artificially.
    scored_results = [r for r in results if r.get("tokens_carpet_bomb", 0) > 0]
    total_tokens_carpet = sum(r.get("tokens_carpet_bomb", 0) for r in scored_results)
    tokens_surgical_scored = sum(r.get("tokens_surgical", 0) for r in scored_results)
    avg_assembly_ms = sum(r.get("assembly_ms", 0) for r in results) / total if total > 0 else 0.0

    if ranker_weights is not None:
        weights_payload = {
            "alpha": ranker_weights.alpha,
            "beta": ranker_weights.beta,
            "gamma": ranker_weights.gamma,
            "delta": ranker_weights.delta,
            "epsilon": ranker_weights.epsilon,
        }
    else:
        from sidecar.context.unified_ranker import DEFAULT_WEIGHTS

        weights_payload = {
            "alpha": DEFAULT_WEIGHTS.alpha,
            "beta": DEFAULT_WEIGHTS.beta,
            "gamma": DEFAULT_WEIGHTS.gamma,
            "delta": DEFAULT_WEIGHTS.delta,
            "epsilon": DEFAULT_WEIGHTS.epsilon,
        }

    metrics = {
        "timestamp": time.time(),
        "ranker_weights": weights_payload,
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
            "precision": avg_precision,
            "precision_at_5": avg_precision,
            "file_recall": avg_file_recall,
            "role_recall": avg_role_recall,
            "tokens_surgical": total_tokens_surgical,
            "tokens_carpet_bomb": total_tokens_carpet,
            "baseline_questions": len(scored_results),
            "reduction_ratio": 1 - (tokens_surgical_scored / total_tokens_carpet)
            if total_tokens_carpet > 0
            else 0.0,
            "assembly_ms_avg": avg_assembly_ms,
        },
        "results": results,
    }

    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print(f"{'=' * 70}")
    print(f"Pass rate:       {metrics['summary']['pass_rate']:.1%} ({passes}/{total})")
    print(f"Recall@5:        {metrics['summary']['recall_at_5']:.2f}")
    print(f"Precision@5:     {metrics['summary']['precision_at_5']:.2f}")
    print(f"File recall:     {metrics['summary']['file_recall']:.2f}")
    print(f"Role recall:     {metrics['summary']['role_recall']:.2f}")
    print(f"Tokens (surgical): {metrics['summary']['tokens_surgical']:,}")
    print(
        f"Tokens (carpet):   {metrics['summary']['tokens_carpet_bomb']:,}"
        f"  ({metrics['summary']['baseline_questions']}/{total} questions with baseline)"
    )
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
        print(f"Index timings:   {metrics['indexing']['timings_sec']}")
        profile = metrics["indexing"].get("repository_profile")
        if profile:
            from sidecar.indexer.repository_profile import summarize_repository_profile

            print(f"Readiness:       {summarize_repository_profile(profile)}")
            if metrics["indexing"].get("repository_profile_store"):
                print(f"Profile store:   {metrics['indexing']['repository_profile_store']}")
        if metrics["indexing"].get("docs_timings_sec"):
            print(f"Docs timings:    {metrics['indexing']['docs_timings_sec']}")
    print(f"{'=' * 70}\n")

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
        "--snapshot-manifest",
        help="Append a compact JSONL row pointing to the report (default: QA/benchmark_runs.jsonl)",
        default=None,
    )
    parser.add_argument(
        "--no-snapshot-manifest",
        action="store_true",
        help="Do not append to the benchmark snapshot manifest",
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
        "--skip-docs",
        action="store_true",
        help="Skip docs indexing (fast iteration during weight tuning)",
    )
    parser.add_argument(
        "--no-index",
        action="store_true",
        help="Skip re-indexing (use existing DB)",
    )
    parser.add_argument(
        "--alpha", type=float, default=None, help="UnifiedRanker α (graph score weight)"
    )
    parser.add_argument(
        "--beta", type=float, default=None, help="UnifiedRanker β (semantic score weight)"
    )
    parser.add_argument(
        "--gamma", type=float, default=None, help="UnifiedRanker γ (intent prior weight)"
    )
    parser.add_argument(
        "--delta", type=float, default=None, help="UnifiedRanker δ (overlap bonus weight)"
    )
    parser.add_argument(
        "--epsilon", type=float, default=None, help="UnifiedRanker ε (token cost penalty)"
    )
    args = parser.parse_args()

    ranker_weights = None
    if any(v is not None for v in (args.alpha, args.beta, args.gamma, args.delta, args.epsilon)):
        from sidecar.context.unified_ranker import DEFAULT_WEIGHTS, RankerWeights

        ranker_weights = RankerWeights(
            alpha=args.alpha if args.alpha is not None else DEFAULT_WEIGHTS.alpha,
            beta=args.beta if args.beta is not None else DEFAULT_WEIGHTS.beta,
            gamma=args.gamma if args.gamma is not None else DEFAULT_WEIGHTS.gamma,
            delta=args.delta if args.delta is not None else DEFAULT_WEIGHTS.delta,
            epsilon=args.epsilon if args.epsilon is not None else DEFAULT_WEIGHTS.epsilon,
        )

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
        skip_docs=args.skip_docs,
        ranker_weights=ranker_weights,
    )

    report_path = write_metrics_report(
        metrics,
        args.report
        or default_report_output_path(
            repo=args.repo,
            project_path=args.project_path,
            core12_only=args.core12,
        ),
    )
    print(f"Report JSON:     {report_path}")

    if not args.no_snapshot_manifest:
        manifest_path = append_snapshot_manifest(
            metrics,
            report_path,
            args.snapshot_manifest,
        )
        print(f"Snapshot index:  {manifest_path}")

    if args.report:
        # Append to baselines.jsonl for historical tracking
        baseline_file = Path(__file__).parent / "baselines.jsonl"
        with open(baseline_file, "a") as f:
            f.write(json.dumps(metrics["summary"]) + "\n")
        print(f"Baseline appended to: {baseline_file}")

    return 0 if metrics["summary"]["pass_rate"] >= 0.8 else 1


if __name__ == "__main__":
    sys.exit(main())
