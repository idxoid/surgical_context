"""Side-by-side runner for the baseline vs fast indexer, with tqdm progress.

Examples:

    # Compare both indexers on the current project (fresh workspaces).
    python -m sidecar.indexer.fast.bench

    # Point at a specific repo.
    python -m sidecar.indexer.fast.bench /path/to/repo

    # Run only one of them against the default workspace.
    python -m sidecar.indexer.fast.bench --only fast
    python -m sidecar.indexer.fast.bench --only baseline --workspace my_ws

Notes:
    Each indexer runs against its own synthetic workspace id by default
    (``bench_<suffix>_<timestamp>``), so both see a cold "all files changed"
    path and the numbers are comparable. Pass ``--workspace`` to override
    both, or ``--baseline-workspace`` / ``--fast-workspace`` individually.

    The baseline indexer in ``sidecar.indexer.code`` is *not* modified —
    this runner reuses its public building blocks (``_collect_files``,
    ``hash_file``, ``index_file``, ``resolve_pending_anchors``) inside a
    local loop so tqdm can wrap it.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
)

from tqdm import tqdm

from sidecar.indexer.fast.pipeline import ProgressReporter, run_fast_indexing


# ---------------------------------------------------------------------------
# tqdm-backed progress reporter for the fast pipeline.
# ---------------------------------------------------------------------------


class TqdmReporter:
    """ProgressReporter implementation backed by tqdm.

    One tqdm instance per stage; closed when the stage ends. No overlap:
    the pipeline runs stages sequentially, so we never hold more than one
    bar at a time.
    """

    # Short, fixed-width stage labels so bars align across stages.
    _LABELS = {
        "hash":    "hash    ",
        "parse":   "parse   ",
        "graph":   "graph   ",
        "embed":   "embed   ",
        "affects": "affects ",
        "docs":    "docs    ",
    }

    def __init__(self, prefix: str = ""):
        self._prefix = prefix
        self._bar: tqdm | None = None
        self._current_stage: str | None = None

    def stage_start(self, stage: str, total: int) -> None:
        desc = f"{self._prefix}{self._LABELS.get(stage, stage)}"
        # total=0 phases (e.g. affects with no changed uids) still get a
        # zero-length bar so the user sees the stage was touched.
        self._bar = tqdm(
            total=max(total, 0),
            desc=desc,
            leave=True,
            unit="file" if stage in {"hash", "parse", "graph"} else "item",
            dynamic_ncols=True,
            disable=False,
        )
        self._current_stage = stage

    def step(self, stage: str, n: int = 1) -> None:
        if self._bar is not None and self._current_stage == stage:
            self._bar.update(n)

    def stage_end(self, stage: str) -> None:
        if self._bar is not None:
            self._bar.close()
            self._bar = None
            self._current_stage = None


# ---------------------------------------------------------------------------
# Baseline runner — reuses existing code.py building blocks, adds tqdm.
# ---------------------------------------------------------------------------


def _run_baseline(project_path: str, workspace_id: str) -> dict:
    """Replay ``sidecar.indexer.code.run_indexing`` with tqdm progress.

    We do not modify the baseline module. Instead we call its public
    helpers (``_collect_files``, ``hash_file``, ``index_file``) inside
    our own loop so tqdm can wrap the per-file work. Semantics match
    ``run_indexing`` exactly.
    """
    from sidecar.database.lancedb_client import LanceDBClient
    from sidecar.database.neo4j_client import Neo4jClient
    from sidecar.indexer.anchor import resolve_pending_anchors
    from sidecar.indexer.code import _collect_files, hash_file, index_file
    from sidecar.indexer.job_log import IndexJobLog
    from sidecar.parser.extractor import SymbolExtractor
    from sidecar.workspace import WorkspaceResolver

    workspace_id = (
        workspace_id or WorkspaceResolver().from_project_path(project_path).id
    )

    db = Neo4jClient(
        os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        os.getenv("NEO4J_USER", "neo4j"),
        os.getenv("NEO4J_PASSWORD", "password"),
    )
    lance = LanceDBClient()
    extractor = SymbolExtractor()
    job_log = IndexJobLog()

    stats: dict = {
        "indexer": "baseline",
        "project_path": project_path,
        "workspace_id": workspace_id,
        "collected": 0,
        "changed": 0,
        "timings_sec": {},
    }

    t0 = time.perf_counter()
    print(f"🐢 Baseline indexing: {project_path} ({workspace_id})")

    try:
        # Stage 1: collect (same prefilter as sidecar.indexer.code)
        t_stage = time.perf_counter()
        files = _collect_files(project_path)
        stats["collected"] = len(files)
        stats["timings_sec"]["collect"] = round(time.perf_counter() - t_stage, 3)
        if not files:
            print(f"❌ No files found at {project_path}")
            return stats

        # Stage 2: sequential hash + diff (baseline has no parallelism here)
        t_stage = time.perf_counter()
        current_hashes: dict[str, str] = {}
        for path in tqdm(files, desc="base hash    ", unit="file", dynamic_ncols=True):
            current_hashes[path] = hash_file(path)
        stored_hashes = db.get_file_hashes(files, workspace_id=workspace_id)
        changed_files = [
            p for p in files if current_hashes[p] != stored_hashes.get(p)
        ]
        stats["changed"] = len(changed_files)
        stats["timings_sec"]["hash"] = round(time.perf_counter() - t_stage, 3)

        if not changed_files:
            print("✅ All files up-to-date, nothing to re-index.")
            return stats

        # Stage 3: per-file index_file (baseline does everything here —
        # parse, graph writes, embeddings, and AFFECTS rebuild per file).
        t_stage = time.perf_counter()
        for path in tqdm(
            changed_files, desc="base index   ", unit="file", dynamic_ncols=True
        ):
            with job_log.track_file_job(path, file_hash=current_hashes[path]):
                index_file(path, db, lance, extractor, workspace_id=workspace_id)
        stats["timings_sec"]["index"] = round(time.perf_counter() - t_stage, 3)

        # Stage 4: resolve pending DocAnchors (baseline tail).
        t_stage = time.perf_counter()
        resolve_pending_anchors(
            db,
            lance,
            workspace_id=workspace_id,
            allowed_prefixes=[project_path],
        )
        stats["timings_sec"]["docs"] = round(time.perf_counter() - t_stage, 3)

    finally:
        db.close()

    stats["timings_sec"]["total"] = round(time.perf_counter() - t0, 3)
    print(f"🐢 Baseline done in {stats['timings_sec']['total']}s")
    return stats


# ---------------------------------------------------------------------------
# Fast runner — thin wrapper that wires tqdm reporter.
# ---------------------------------------------------------------------------


def _run_fast(
    project_path: str,
    workspace_id: str,
    *,
    hash_workers: int | None,
    parse_workers: int | None,
) -> dict:
    stats = run_fast_indexing(
        project_path,
        workspace_id=workspace_id,
        hash_workers=hash_workers,
        parse_workers=parse_workers,
        reporter=TqdmReporter(prefix="fast "),
    )
    stats["indexer"] = "fast"
    return stats


# ---------------------------------------------------------------------------
# Comparison output.
# ---------------------------------------------------------------------------


def _print_comparison(baseline: dict | None, fast: dict | None):
    """Print a side-by-side table of stage timings and key counters."""
    print()
    print("=" * 64)
    print("Benchmark summary")
    print("=" * 64)

    def _fmt_sec(d: dict | None, key: str) -> str:
        if d is None:
            return "    -"
        v = d.get("timings_sec", {}).get(key)
        if v is None:
            return "    -"
        return f"{v:>8.3f}"

    def _fmt_int(d: dict | None, key: str) -> str:
        if d is None:
            return "    -"
        v = d.get(key)
        if v is None:
            return "    -"
        return f"{v:>8d}"

    def _speedup(base_key: str, fast_key: str) -> str:
        if not baseline or not fast:
            return ""
        b = baseline.get("timings_sec", {}).get(base_key)
        f = fast.get("timings_sec", {}).get(fast_key)
        if not b or not f:
            return ""
        ratio = b / f if f > 0 else 0
        return f"  x{ratio:.2f}"

    header = f"{'stage':<12} {'baseline':>10} {'fast':>10}   speedup"
    print(header)
    print("-" * 64)
    # Baseline lumps parse+graph+embed+affects into one per-file loop (index),
    # so for the stage-by-stage line-up we compare:
    #   baseline.collect  <-> fast.collect
    #   baseline.hash     <-> fast.hash
    #   baseline.index    <-> fast.parse + graph + embed + affects  (summed below)
    #   baseline.docs     <-> fast.docs
    print(f"{'collect':<12} {_fmt_sec(baseline, 'collect')} {_fmt_sec(fast, 'collect')}"
          f"{_speedup('collect', 'collect')}")
    print(f"{'hash':<12} {_fmt_sec(baseline, 'hash')} {_fmt_sec(fast, 'hash')}"
          f"{_speedup('hash', 'hash')}")

    # Sum fast's core stages for comparison with baseline's monolithic "index".
    fast_core = None
    if fast:
        fast_core = sum(
            fast.get("timings_sec", {}).get(k, 0) or 0
            for k in ("parse", "graph", "embed", "affects")
        )
    fast_core_str = f"{fast_core:>8.3f}" if fast_core is not None else "    -"
    b_index = baseline.get("timings_sec", {}).get("index") if baseline else None
    speedup_core = ""
    if b_index and fast_core:
        speedup_core = f"  x{b_index / fast_core:.2f}"
    print(f"{'index*':<12} {_fmt_sec(baseline, 'index')} {fast_core_str}{speedup_core}")

    if fast:
        print(f"  ├─ parse   {'':>10} {_fmt_sec(fast, 'parse')}")
        print(f"  ├─ graph   {'':>10} {_fmt_sec(fast, 'graph')}")
        print(f"  ├─ embed   {'':>10} {_fmt_sec(fast, 'embed')}")
        print(f"  └─ affects {'':>10} {_fmt_sec(fast, 'affects')}")

    print(f"{'docs':<12} {_fmt_sec(baseline, 'docs')} {_fmt_sec(fast, 'docs')}"
          f"{_speedup('docs', 'docs')}")
    print("-" * 64)
    print(f"{'total':<12} {_fmt_sec(baseline, 'total')} {_fmt_sec(fast, 'total')}"
          f"{_speedup('total', 'total')}")
    print("-" * 64)
    print(f"{'collected':<12} {_fmt_int(baseline, 'collected')} {_fmt_int(fast, 'collected')}")
    print(f"{'changed':<12} {_fmt_int(baseline, 'changed')} {_fmt_int(fast, 'changed')}")
    if fast:
        print(f"{'encoded':<12} {'':>10} {_fmt_int(fast, 'symbols_encoded')}")
        print(f"{'affects_rbd':<12} {'':>10} {_fmt_int(fast, 'affects_rebuilt')}")
    print("=" * 64)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _default_workspace(suffix: str, run_id: str) -> str:
    return f"bench_{suffix}_{run_id}"


def main():
    from sidecar.indexer.fast.collector import ROOT

    parser = argparse.ArgumentParser(
        description="Run baseline + fast indexers with progress bars and compare"
    )
    parser.add_argument("path", nargs="?", default=ROOT, help="Project path to index")
    parser.add_argument(
        "--only",
        choices=("baseline", "fast", "both"),
        default="both",
        help="Which indexer to run (default: both)",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Shared workspace id (overrides baseline/fast workspace flags)",
    )
    parser.add_argument(
        "--baseline-workspace", default=None, help="Workspace id for baseline run"
    )
    parser.add_argument(
        "--fast-workspace", default=None, help="Workspace id for fast run"
    )
    parser.add_argument(
        "--hash-workers", type=int, default=None, help="Fast indexer hash pool size"
    )
    parser.add_argument(
        "--parse-workers", type=int, default=None, help="Fast indexer parse pool size"
    )

    args = parser.parse_args()
    project_path = os.path.abspath(args.path)

    run_id = time.strftime("%Y%m%d_%H%M%S")
    baseline_ws = (
        args.workspace
        or args.baseline_workspace
        or _default_workspace("baseline", run_id)
    )
    fast_ws = (
        args.workspace or args.fast_workspace or _default_workspace("fast", run_id)
    )

    baseline_stats: dict | None = None
    fast_stats: dict | None = None

    if args.only in ("baseline", "both"):
        baseline_stats = _run_baseline(project_path, baseline_ws)

    if args.only in ("fast", "both"):
        fast_stats = _run_fast(
            project_path,
            fast_ws,
            hash_workers=args.hash_workers,
            parse_workers=args.parse_workers,
        )

    _print_comparison(baseline_stats, fast_stats)

    if args.only == "both" and not args.workspace:
        print()
        print(f"Benchmark workspaces created: {baseline_ws!r}, {fast_ws!r}")
        print("(remove them manually from Neo4j if you don't want the extra state)")


if __name__ == "__main__":
    main()
