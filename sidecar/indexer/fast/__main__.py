"""CLI entry point for the fast indexer.

Usage:
    python -m sidecar.indexer.fast [path] [--workspace ID]
                                   [--hash-workers N] [--parse-workers N]

Mirrors ``python -m sidecar.indexer.code`` but runs the alternative
parallel pipeline. The baseline indexer is untouched.
"""

import argparse
import os

from sidecar.indexer.fast.collector import ROOT
from sidecar.indexer.fast.pipeline import run_fast_indexing


def main():
    parser = argparse.ArgumentParser(
        description="Fast (parallel) indexer — alternative to sidecar.indexer.code"
    )
    parser.add_argument("path", nargs="?", default=ROOT, help="Project path to index")
    parser.add_argument("--workspace", default=None, help="Workspace id override")
    parser.add_argument(
        "--hash-workers",
        type=int,
        default=None,
        help="Thread pool size for sha256 hashing",
    )
    parser.add_argument(
        "--parse-workers",
        type=int,
        default=None,
        help="Thread pool size for tree-sitter parsing",
    )

    args = parser.parse_args()
    project_path = os.path.abspath(args.path)

    run_fast_indexing(
        project_path,
        workspace_id=args.workspace,
        hash_workers=args.hash_workers,
        parse_workers=args.parse_workers,
    )


if __name__ == "__main__":
    main()
