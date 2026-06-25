"""Safe report/output path resolution for QA CLI tools."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

QA_DIR = Path(__file__).resolve().parent

DEFAULT_OUTPUT_BASES: tuple[Path, ...] = (
    Path("/tmp").resolve(),
    (QA_DIR / "reports").resolve(),
)

_FILENAME_SAFE = re.compile(r"[^\w.\-]+")


def sanitize_filename_part(value: str) -> str:
    cleaned = _FILENAME_SAFE.sub("_", value.strip())
    return cleaned or "report"


def resolve_output_path(
    raw: str | None,
    *,
    default_name: str,
    allowed_bases: tuple[Path, ...] = DEFAULT_OUTPUT_BASES,
) -> Path:
    """Resolve a CLI report path and ensure it stays under allowed directories."""
    bases = tuple(base.resolve() for base in allowed_bases)
    default_name = sanitize_filename_part(default_name)

    if raw:
        candidate = Path(raw).expanduser()
        candidate = (
            candidate.resolve() if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
        )
    else:
        candidate = (bases[0] / default_name).resolve()

    if candidate.exists() and candidate.is_dir():
        raise SystemExit(f"report path is a directory: {candidate}")

    for base in bases:
        try:
            candidate.relative_to(base)
            break
        except ValueError:
            continue
    else:
        allowed = ", ".join(str(base) for base in bases)
        raise SystemExit(
            f"report path must live under an allowed directory ({allowed}), got: {candidate}"
        )

    return candidate


def resolve_output_directory(
    dir_name: str,
    *,
    allowed_bases: tuple[Path, ...] = DEFAULT_OUTPUT_BASES,
) -> Path:
    """Resolve a directory path under allowed bases (creates it if missing)."""
    bases = tuple(base.resolve() for base in allowed_bases)
    safe_name = sanitize_filename_part(dir_name)
    candidate = (bases[0] / safe_name).resolve()

    for base in bases:
        try:
            candidate.relative_to(base)
            break
        except ValueError:
            continue
    else:
        allowed = ", ".join(str(base) for base in bases)
        raise SystemExit(
            f"output directory must live under an allowed directory ({allowed}), got: {candidate}"
        )

    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def require_allowed_choice(value: str, allowed: frozenset[str], *, label: str = "value") -> str:
    if value not in allowed:
        raise SystemExit(f"unknown {label}: {value}")
    return value


def lookup_allowed_repo_checkout(
    repo: str,
    *,
    allowed: frozenset[str],
    checkouts: dict[str, Path],
) -> Path:
    """Return a pre-mapped repo checkout path selected from an allowlist."""
    safe_repo = require_allowed_choice(repo, allowed, label="repo")
    try:
        checkout = checkouts[safe_repo].resolve()
    except KeyError as exc:
        raise SystemExit(f"missing checkout mapping for repo: {safe_repo}") from exc
    return checkout


def resolve_repo_checkout(qa_dir: Path, repo: str, allowed: frozenset[str]) -> Path:
    """Map a repo slug to an on-disk checkout without path traversal."""
    repos_base = (qa_dir / "repos").resolve()
    checkouts = {name: (repos_base / name).resolve() for name in sorted(allowed)}
    checkout = lookup_allowed_repo_checkout(repo, allowed=allowed, checkouts=checkouts)
    try:
        checkout.relative_to(repos_base)
    except ValueError as exc:
        raise SystemExit(f"repo checkout escapes repos base: {checkout}") from exc
    return checkout


def default_report_basename(prefix: str, repo: str, allowed: frozenset[str]) -> str:
    safe_repo = require_allowed_choice(repo, allowed, label="repo")
    return sanitize_filename_part(f"{prefix}_{safe_repo}.json")


def write_json_report(
    payload: Any,
    raw_path: str | None,
    *,
    default_name: str,
    allowed_bases: tuple[Path, ...] = DEFAULT_OUTPUT_BASES,
) -> Path:
    """Resolve a safe report path and write JSON without path injection."""
    out = resolve_output_path(raw_path, default_name=default_name, allowed_bases=allowed_bases)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out


def resolve_benchmark_workspace(repo: str, allowed: frozenset[str]) -> Path:
    safe_repo = require_allowed_choice(repo, allowed, label="repo")
    return resolve_output_directory(f"axis_benchmark_{safe_repo}")
