"""File collection with directory prefilter.

The baseline collector in context_engine.indexer.code walks the entire project tree
and then asks .gitignore whether each file should be dropped. On repos with
node_modules / .venv / target / dist, that descent costs seconds per pass.
The prefilter below prunes obvious build and cache dirs before gitignore is
ever consulted. gitignore matching still runs on survivors so project-specific
exclusions are honored.
"""

import os

from context_engine.parser.registry import REGISTRY

# Repo root when invoked from ``python -m context_engine.indexer.fast``.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

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


def is_indexable_file(file_path: str) -> bool:
    _, ext = os.path.splitext(file_path)
    return ext.lower() in _INDEXED_EXTENSIONS


def _load_gitignore(root: str):
    """Return a pathspec matcher for the nearest .gitignore, or None."""
    import pathspec

    gitignore = os.path.join(root, ".gitignore")
    if not os.path.exists(gitignore):
        return None
    with open(gitignore) as f:
        return pathspec.PathSpec.from_lines("gitwildmatch", f)


def _filter_walk_dirs(
    dirs: list[str],
    spec,
    root: str,
    project_root: str,
) -> None:
    dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
    if not spec:
        return
    rel_root = os.path.relpath(root, project_root)
    rel_prefix = "" if rel_root in (".", "") else rel_root.replace(os.sep, "/")
    kept_dirs: list[str] = []
    for directory in dirs:
        rel_dir = f"{rel_prefix}/{directory}/" if rel_prefix else f"{directory}/"
        if not spec.match_file(rel_dir):
            kept_dirs.append(directory)
    dirs[:] = kept_dirs


def _maybe_collect_file(
    root: str,
    name: str,
    *,
    project_root: str,
    spec,
    files: list[str],
) -> None:
    if name.startswith(".") or not is_indexable_file(name):
        return
    full = os.path.join(root, name)
    if spec:
        rel = os.path.relpath(full, project_root)
        if spec.match_file(rel):
            return
    files.append(full)


def collect_files(project_path: str) -> list[str]:
    """Walk project tree with directory prefilter and gitignore fallback."""
    project_root = os.path.abspath(project_path)
    spec = _load_gitignore(project_root)
    files: list[str] = []

    for root, dirs, filenames in os.walk(project_root):
        _filter_walk_dirs(dirs, spec, root, project_root)
        for name in filenames:
            _maybe_collect_file(root, name, project_root=project_root, spec=spec, files=files)

    from context_engine.indexer.git_committed import filter_indexable_paths

    return filter_indexable_paths(files, project_root)
