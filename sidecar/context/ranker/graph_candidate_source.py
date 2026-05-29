from __future__ import annotations


class GraphCandidateSource:
    """Graph neighbor candidate loader."""

    def __init__(self, host):
        self.host = host

    def graph_candidates(self, target_uid: str, hop_limit: int, *, intent=None):
        return self.host._graph_candidates_impl(target_uid, hop_limit, intent=intent)
