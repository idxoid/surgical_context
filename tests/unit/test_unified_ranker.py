from unittest.mock import MagicMock

from sidecar.context.unified_ranker import UnifiedRanker, VectorSearcher


def _make_db(*, allowed_paths=None, allowed_uids=None):
    session = MagicMock()

    def run(query, **params):
        if "RETURN f.path AS path" in query:
            return [{"path": path} for path in (allowed_paths or [])]
        if "RETURN DISTINCT s.uid AS uid" in query:
            return [{"uid": uid} for uid in (allowed_uids or [])]
        return []

    session.run.side_effect = run
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    driver.session.return_value.__exit__.return_value = None
    db = MagicMock()
    db.driver = driver
    return db


class _FakeVector:
    def __init__(self, docs=None, symbols=None):
        self._docs = docs or []
        self._symbols = symbols or []

    def search(self, query, limit):
        return self._docs[:limit]

    def search_symbols(self, query, limit=30, threshold=1.0):
        return self._symbols[:limit]


def test_doc_candidates_filter_to_workspace_files():
    db = _make_db(allowed_paths=["/repo/docs/allowed.md"])
    vector = VectorSearcher(
        _FakeVector(
            docs=[
                {"id": "a", "file_path": "/repo/docs/allowed.md", "chunk": "allowed", "score": 0.9},
                {"id": "b", "file_path": "/other/docs/nope.md", "chunk": "nope", "score": 0.8},
            ]
        )
    )
    ranker = UnifiedRanker(db, vector, workspace_id="local/repo@main")

    candidates = ranker._doc_candidates("dependency injection", limit=10)

    assert [candidate.file_path for candidate in candidates] == ["/repo/docs/allowed.md"]


def test_symbol_candidates_filter_to_workspace_uids():
    db = _make_db(allowed_uids=["in-workspace"])
    vector = VectorSearcher(
        _FakeVector(
            symbols=[
                {"uid": "in-workspace", "name": "solve_dependencies", "file_path": "/repo/a.py", "score": 0.9},
                {"uid": "other-workspace", "name": "solve_dependencies", "file_path": "/other/a.py", "score": 0.8},
            ]
        )
    )
    ranker = UnifiedRanker(db, vector, workspace_id="local/repo@main")

    candidates = ranker._sym_vec_candidates("dependency injection", limit=10)

    assert [candidate.uid for candidate in candidates] == ["in-workspace"]
