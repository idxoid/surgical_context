"""Safe report/output path resolution for QA CLI tools."""

from __future__ import annotations

import re
from pathlib import Path

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
