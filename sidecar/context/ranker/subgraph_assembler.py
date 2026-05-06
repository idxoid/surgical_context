from __future__ import annotations


class SubgraphAssembler:
    """Materialize selected candidates into subgraph payload."""

    def __init__(self, host):
        self.host = host

    def candidates_to_subgraph(self, payload):
        return self.host._candidates_to_subgraph_impl(payload)
