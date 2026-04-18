"""PromptCompiler — deterministic PromptContext assembly."""

from sidecar.context.types import Subgraph, SymbolContext, PromptContext, DocChunk


class PromptCompiler:
    """Stateless compiler: maps expanded graph + resolved code + docs → PromptContext."""

    def compile(
        self,
        subgraph: Subgraph,
        code_map: dict[str, tuple[str, bool]],
        docs: list[DocChunk],
    ) -> PromptContext:
        """Assemble PromptContext from resolved parts. No I/O."""

        def to_symbol_context(node) -> SymbolContext:
            code, is_dirty = code_map.get(node.uid, ("", False))
            return SymbolContext(
                symbol=node.name,
                file_path=node.file_path,
                relation=node.relation,
                direction=node.direction,
                depth=node.depth,
                relevance_score=node.relevance_score,
                is_dirty=is_dirty,
                code=code,
            )

        primary = to_symbol_context(subgraph.primary)
        graph = [to_symbol_context(n) for n in subgraph.nodes]

        return PromptContext(
            primary_source=primary,
            graph_context=graph,
            documentation=docs,
            budget=subgraph.budget,
        )
