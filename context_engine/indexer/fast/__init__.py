"""Alternative (fast) indexer — parallel track to context_engine.indexer.code.

Design goals over the baseline indexer:
- File is read from disk exactly once per index pass.
- Tree-sitter parsing and call/import/inheritance extraction run under a
  thread pool with per-thread adapter instances (tree-sitter Parser is not
  thread-safe; instances are not shared across workers).
- Embedding generation is deferred until all changed symbols are known,
  then encoded in one global batch (saturates SentenceTransformer).
- AFFECTS reverse index is rebuilt once at the end over the union of
  changed UIDs, not per-file.
- Directory prefilter skips build/cache dirs before gitignore evaluation.

This module is additive. The canonical indexer in ``context_engine.indexer.code``
remains untouched, so existing tests and importers keep working.
"""

from context_engine.indexer.fast.pipeline import run_fast_indexing

__all__ = ["run_fast_indexing"]
