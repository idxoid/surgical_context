"""Bootstrap: must be imported before LanceDB / SentenceTransformer to ensure
the environment and stderr filter are in place before those modules load."""

import sys
from pathlib import Path


def apply(*, caller_file: str | None = None) -> None:
    """Run env-load + stderr-filter.  Call once, before any heavy import."""
    if caller_file is not None and __package__ is None:
        # Direct-execution fallback: add repo root to sys.path.
        sys.path.insert(0, str(Path(caller_file).resolve().parent.parent))

    from context_engine.env_loader import load_repo_dotenv

    load_repo_dotenv()

    from context_engine.silence import install

    install()
