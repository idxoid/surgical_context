"""Load repo-root .env into os.environ (stdlib only; no python-dotenv dependency)."""

from __future__ import annotations

import os
from pathlib import Path


def load_repo_dotenv(*, override: bool = False, path: Path | None = None) -> bool:
    """Parse ``.env`` at the repo root. Returns True if the file was read."""
    if path is None:
        path = Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return False

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not override and key in os.environ:
            continue
        os.environ[key] = value
    return True
