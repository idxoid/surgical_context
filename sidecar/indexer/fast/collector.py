"""File collection with directory prefilter.

The baseline collector in sidecar.indexer.code walks the entire project tree
and then asks .gitignore whether each file should be dropped. On repos with
node_modules / .venv / target / dist, that descent costs seconds per pass.
The prefilter below prunes obvious build and cache dirs before gitignore is
ever consulted. gitignore matching still runs on survivors so project-specific
exclusions are honored.
"""

import os

from sidecar.parser.registry import REGISTRY

# Directories we never want to descend into, regardless of .gitignore.
# Matched by basename only. Keep this conservative — never prune something
# a legitimate project might live under.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".venv",
        "venv",
        "env",
        "node_modules",
        ".next",
        ".nuxt",
        ".turbo",
        ".cache",
        ".parcel-cache",
        "dist",
        "build",
        "out",
        "target",
        ".gradle",
        ".idea",
        ".vscode",
        ".DS_Store",
    }
)

_INDEXED_EXTENSIONS = frozenset(
    ext for adapter in REGISTRY.supported_adapters() for ext in adapter.file_extensions
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def is_indexable_file(file_path: str) -> bool:
    _, ext = os.path.splitext(file_path)
    return ext.lower() in _INDEXED_EXTENSIONS


def _load_gitignore(root: str):
    """Return a pathspec matcher for the nearest .gitignore, or None."""
    import pathspec

    gitignore = os.path.join(root, ".gitignore")
    if not os.path.exists(gitignore):
        gitignore = os.path.join(ROOT, ".gitignore")
    if not os.path.exists(gitignore):
        return None
    with open(gitignore) as f:
        return pathspec.PathSpec.from_lines("gitwildmatch", f)


def collect_files(project_path: str) -> list[str]:
    """Walk project tree with directory prefilter and gitignore fallback."""
    spec = _load_gitignore(project_path)
    files: list[str] = []

    for root, dirs, filenames in os.walk(project_path):
        # Hard prefilter before gitignore: drop common build/cache dirs by basename.
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]

        # Apply gitignore to surviving dirs so project-specific rules still win.
        if spec:
            rel_root = os.path.relpath(root, ROOT)
            dirs[:] = [d for d in dirs if not spec.match_file(os.path.join(rel_root, d) + "/")]

        for name in filenames:
            if name.startswith(".") or not is_indexable_file(name):
                continue
            full = os.path.join(root, name)
            if spec:
                rel = os.path.relpath(full, ROOT)
                if spec.match_file(rel):
                    continue
            files.append(full)

    return files
