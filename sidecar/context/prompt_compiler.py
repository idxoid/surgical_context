"""PromptCompiler — deterministic PromptContext assembly."""

from collections import defaultdict

from sidecar.context.intent_classifier import Intent
from sidecar.context.types import DocChunk, PromptContext, Subgraph, SymbolContext


class PromptCompiler:
    """Stateless compiler: maps expanded graph + resolved code + docs → PromptContext."""

    MAX_DOCS_PER_SOURCE = 2

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
                uid=node.uid,
                range=node.range,
                kind=getattr(node, "kind", ""),
                direction=node.direction,
                depth=node.depth,
                relevance_score=node.relevance_score,
                graph_score=getattr(node, "graph_score", 0.0),
                semantic_score=getattr(node, "semantic_score", 0.0),
                blended_score=getattr(node, "blended_score", node.relevance_score),
                intent_weight=getattr(node, "intent_weight", 0.0),
                render_mode=getattr(node, "render_mode", "full"),
                is_dirty=is_dirty,
                code=code,
                provenance=getattr(node, "provenance", None) or ["graph", "code_resolver"],
                chain_kind=getattr(node, "chain_kind", ""),
            )

        primary = to_symbol_context(subgraph.primary)
        graph = self._dedupe_graph_context([to_symbol_context(n) for n in subgraph.nodes])
        docs = self._dedupe_docs(docs)

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
        *,
        tier_priority: list[str] | tuple[str, ...] | None = None,
    ) -> PromptContext:
        """Assemble PromptContext with tier-based budget filling per intent."""

        def to_symbol_context(node) -> SymbolContext:
            code, is_dirty = code_map.get(node.uid, ("", False))
            return SymbolContext(
                symbol=node.name,
                file_path=node.file_path,
                relation=node.relation,
                uid=node.uid,
                range=node.range,
                kind=getattr(node, "kind", ""),
                direction=node.direction,
                depth=node.depth,
                relevance_score=node.relevance_score,
                graph_score=getattr(node, "graph_score", 0.0),
                semantic_score=getattr(node, "semantic_score", 0.0),
                blended_score=getattr(node, "blended_score", node.relevance_score),
                intent_weight=getattr(node, "intent_weight", 0.0),
                render_mode=getattr(node, "render_mode", "full"),
                is_dirty=is_dirty,
                code=code,
                provenance=getattr(node, "provenance", None) or ["graph", "code_resolver"],
                chain_kind=getattr(node, "chain_kind", ""),
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
        graph = self._dedupe_graph_context(
            [to_symbol_context(n) for n in subgraph.nodes],
            intent=intent,
        )

        # Calculate tokens per tier (for observability)
        tier_tokens = {
            "code": estimate_tokens(primary.code),
            "cross_refs": sum(estimate_tokens(sym.code) for sym in graph),
        }

        # Organize docs by tier
        docs_by_tier: dict[str, list[DocChunk]] = {
            "specs": [],
            "architecture": [],
            "concept": [],
            "idea": [],
        }
        for doc in self._dedupe_docs(docs, intent=intent):
            tier = infer_doc_type(doc.source_file)
            if tier in docs_by_tier:
                docs_by_tier[tier].append(doc)

        # Calculate doc tier tokens
        for tier_name, doc_list in docs_by_tier.items():
            tier_tokens[tier_name] = sum(estimate_tokens(doc.content) for doc in doc_list)

        # Get tier priority for this intent
        from sidecar.context.intent_classifier import IntentConfig

        tier_priority = list(tier_priority or IntentConfig.PRIORITY[intent])

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

    def _dedupe_graph_context(
        self,
        graph: list[SymbolContext],
        *,
        intent: Intent | None = None,
    ) -> list[SymbolContext]:
        """Collapse exact duplicate code snippets that add no new evidence.

        This is intentionally post-resolution: some duplicate noise only becomes
        obvious after multiple symbols resolve to the same implementation text.
        We keep impact-analysis contexts untouched because parallel call sites in
        different files can matter there.
        """
        if intent == Intent.IMPACT_ANALYSIS:
            return graph

        deduped: list[SymbolContext] = []
        seen_exact_code: dict[tuple[str, str], int] = {}
        for symbol in graph:
            code_key = self._normalized_code_key(symbol.code)
            dedupe_key = (symbol.symbol, code_key) if code_key else None
            if dedupe_key and dedupe_key in seen_exact_code:
                existing_index = seen_exact_code[dedupe_key]
                existing = deduped[existing_index]
                preferred = self._preferred_symbol_context(existing, symbol)
                preferred.provenance = sorted(
                    set(existing.provenance or []).union(symbol.provenance or [])
                )
                deduped[existing_index] = preferred
                continue
            if dedupe_key:
                seen_exact_code[dedupe_key] = len(deduped)
            deduped.append(symbol)
        return deduped

    def _dedupe_docs(
        self,
        docs: list[DocChunk],
        *,
        intent: Intent | None = None,
    ) -> list[DocChunk]:
        """Remove repeated doc chunks and cap same-file repetition."""
        max_per_source = None if intent == Intent.IMPACT_ANALYSIS else self.MAX_DOCS_PER_SOURCE
        per_source_counts: dict[str, int] = defaultdict(int)
        seen_chunk_ids: set[str] = set()
        seen_exact_content: set[tuple[str, str]] = set()
        deduped: list[DocChunk] = []
        for doc in docs:
            if doc.chunk_id in seen_chunk_ids:
                continue
            content_key = self._normalized_code_key(doc.content)
            exact_key = (doc.source_file, content_key)
            if content_key and exact_key in seen_exact_content:
                continue
            if max_per_source is not None and per_source_counts[doc.source_file] >= max_per_source:
                continue
            deduped.append(doc)
            seen_chunk_ids.add(doc.chunk_id)
            if content_key:
                seen_exact_content.add(exact_key)
            per_source_counts[doc.source_file] += 1
        return deduped

    @staticmethod
    def _normalized_code_key(text: str) -> str:
        if not text:
            return ""
        return " ".join(text.split())

    @staticmethod
    def _preferred_symbol_context(a: SymbolContext, b: SymbolContext) -> SymbolContext:
        a_key = (a.blended_score or a.relevance_score, a.relevance_score, -a.depth)
        b_key = (b.blended_score or b.relevance_score, b.relevance_score, -b.depth)
        return a if a_key >= b_key else b
