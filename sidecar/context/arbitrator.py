"""ContextArbitrator — thin orchestrator facade composing pure components."""

from sidecar.context.code_resolver import CodeResolver
from sidecar.context.deduplicator import ContextDeduplicator
from sidecar.context.graph_expander import GraphExpander
from sidecar.context.prompt_compiler import PromptCompiler
from sidecar.context.types import PromptContext


class ContextArbitrator:
    """Orchestrator: composes GraphExpander, ContextDeduplicator, CodeResolver, DocResolver, PromptCompiler."""

    def __init__(self, neo4j_client, overlay=None):
        self.db = neo4j_client
        self.overlay = overlay

    def get_context_for_symbol(
        self,
        symbol_name: str,
        token_budget: int = 4000,
    ) -> PromptContext | str:
        """Orchestrate the pipeline: expand → deduplicate → resolve → compile."""
        # 1. Expand graph
        subgraph = GraphExpander(self.db).expand(symbol_name, token_budget=token_budget)
        if isinstance(subgraph, str):
            return subgraph

        # 2. Deduplicate (remove redundant symbols and docs)
        subgraph = ContextDeduplicator().deduplicate(subgraph)

        # 3. Resolve code
        resolver = CodeResolver(self.overlay)
        code_map = {
            n.uid: resolver.resolve(n.file_path, n.range[0], n.range[1])
            for n in [subgraph.primary] + subgraph.nodes
        }

        # 4. Resolve docs (empty in this method; caller optionally populates)
        docs = []

        # 5. Compile prompt
        return PromptCompiler().compile(subgraph, code_map, docs)
