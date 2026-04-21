"""PromptCompiler — deterministic PromptContext assembly."""

from sidecar.context.types import DocChunk, PromptContext, Subgraph, SymbolContext
from sidecar.context.intent_classifier import Intent


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

    def compile_with_intent(
        self,
        subgraph: Subgraph,
        code_map: dict[str, tuple[str, bool]],
        docs: list[DocChunk],
        intent: Intent,
    ) -> PromptContext:
        """Assemble PromptContext with tier-based budget filling per intent."""

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

        def estimate_tokens(text: str) -> int:
            """Rough token estimate: ~4 chars per token."""
            return max(1, len(text) // 4)

        def infer_doc_type(source_file: str) -> str:
            """Infer doc tier from source file path."""
            lower = source_file.lower()
            if "spec_" in lower:
                return "specs"
            elif "idea_" in lower:
                return "idea"
            elif "concept" in lower:
                return "concept"
            elif "architecture" in lower or "architectura" in lower:
                return "architecture"
            return "idea"  # default to idea tier

        primary = to_symbol_context(subgraph.primary)
        graph = [to_symbol_context(n) for n in subgraph.nodes]

        # Calculate tokens per tier (for observability)
        tier_tokens = {
            "code": estimate_tokens(primary.code),
            "cross_refs": sum(estimate_tokens(sym.code) for sym in graph),
        }

        # Organize docs by tier
        docs_by_tier = {
            "specs": [],
            "architecture": [],
            "concept": [],
            "idea": [],
        }
        for doc in docs:
            tier = infer_doc_type(doc.source_file)
            if tier in docs_by_tier:
                docs_by_tier[tier].append(doc)

        # Calculate doc tier tokens
        for tier_name, doc_list in docs_by_tier.items():
            tier_tokens[tier_name] = sum(estimate_tokens(doc.content) for doc in doc_list)

        # Get tier priority for this intent
        from sidecar.context.intent_classifier import IntentConfig

        tier_priority = IntentConfig.PRIORITY[intent]

        # Determine mode and fill docs tier-aware
        selected_docs = []
        has_code = bool(primary.code)
        has_graph = bool(graph)
        has_docs = False

        # Tier-based filling: iterate through priority order, add docs until tiers exhausted
        for tier in tier_priority:
            if tier == "code":
                # Code tier is already included if primary.code is not empty
                continue
            elif tier == "cross_refs":
                # Cross-refs are already in graph if has_graph is True
                continue
            elif tier in docs_by_tier and docs_by_tier[tier]:
                selected_docs.extend(docs_by_tier[tier])
                has_docs = True

        # Determine mode based on what's populated
        if has_code and has_graph:
            mode = "surgical_full"
        elif has_docs or (has_code or has_graph):
            mode = "surgical_doc_only" if has_docs else "surgical_full"
        else:
            mode = "standard"

        return PromptContext(
            primary_source=primary,
            graph_context=graph,
            documentation=selected_docs,
            budget=subgraph.budget,
            mode=mode,
            intent=intent.value,
            tier_tokens=tier_tokens,
        )
