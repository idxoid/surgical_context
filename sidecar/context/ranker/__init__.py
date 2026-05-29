"""UnifiedRanker decomposition components.

Heavier slices (scoring, pruning, role_fulfilment) live in sibling
modules; import them explicitly, e.g. ``from sidecar.context.ranker.scoring``.
"""

from .budget_selector import BudgetSelector
from .graph_candidate_source import GraphCandidateSource
from .role_backfill import RoleBackfill
from .subgraph_assembler import SubgraphAssembler
from .target_selector import TargetSelector
from .vector_candidate_source import VectorCandidateSource

__all__ = [
    "BudgetSelector",
    "GraphCandidateSource",
    "RoleBackfill",
    "SubgraphAssembler",
    "TargetSelector",
    "VectorCandidateSource",
]
