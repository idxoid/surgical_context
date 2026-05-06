from __future__ import annotations


class VectorCandidateSource:
    """Doc/symbol vector retrieval with workspace-aware filtering."""

    def __init__(self, host):
        self.host = host

    def doc_candidates(self, query: str, limit: int):
        return self.host._doc_candidates_impl(query, limit)

    def sym_vec_candidates(self, query: str, limit: int):
        return self.host._sym_vec_candidates_impl(query, limit)
