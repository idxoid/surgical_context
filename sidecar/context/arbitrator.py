"""ContextArbitrator — thin orchestrator facade composing pure components."""

import hashlib

from sidecar.cache.layered import CachedBody, LayeredCache, default_cache
from sidecar.context.code_resolver import CodeResolver
from sidecar.context.deduplicator import ContextDeduplicator
from sidecar.context.doc_resolver import DocResolver
from sidecar.context.graph_expander import GraphExpander
from sidecar.context.intent_classifier import IntentClassifier
from sidecar.context.prompt_compiler import PromptCompiler
from sidecar.context.types import PromptContext
from sidecar.workspace import DEFAULT_WORKSPACE_ID


class ContextArbitrator:
    """Orchestrator: composes GraphExpander, ContextDeduplicator, CodeResolver, DocResolver, PromptCompiler."""

    def __init__(
        self,
        neo4j_client,
        overlay=None,
        vector_db=None,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        cache: LayeredCache | None = None,
    ):
        self.db = neo4j_client
        self.overlay = overlay
        self.vector_db = vector_db
        self.workspace_id = workspace_id
        self.cache = cache or default_cache

    def get_context_for_symbol(
        self,
        symbol_name: str,
        question: str = "",
        token_budget: int = 4000,
    ) -> PromptContext | str:
        """Orchestrate the pipeline: expand → deduplicate → resolve → compile (with intent-aware tier selection)."""
        # 0. Classify intent from question (determines tier priority in compilation)
        intent = IntentClassifier.classify_intent(question)
        intent_hash = hashlib.sha256(intent.value.encode("utf-8")).hexdigest()
        graph_version = self._graph_version()

        # 1. Expand graph
        subgraph = None
        cache_hits = []
        primary_uid = self._primary_uid(symbol_name)
        if primary_uid:
            subgraph = self.cache.get_subgraph(
                primary_uid, intent_hash, token_budget, self.workspace_id, graph_version
            )
            if subgraph is not None:
                cache_hits.append("l2_subgraph")

        if subgraph is None:
            subgraph = GraphExpander(self.db, workspace_id=self.workspace_id).expand(
                symbol_name, token_budget=token_budget
            )
            if not isinstance(subgraph, str):
                primary_uid = subgraph.primary.uid
                self.cache.put_subgraph(
                    primary_uid,
                    intent_hash,
                    token_budget,
                    self.workspace_id,
                    graph_version,
                    subgraph,
                )
        if isinstance(subgraph, str):
            return subgraph

        # 2. Deduplicate (remove redundant symbols and docs)
        subgraph = ContextDeduplicator().deduplicate(subgraph)

        # 3. Resolve code
        resolver = CodeResolver(self.overlay, workspace_id=self.workspace_id)
        code_map = {}
        for node in [subgraph.primary] + subgraph.nodes:
            line_range = (node.range[0], node.range[1])
            cached = None
            overlay_dirty = bool(
                self.overlay and self.overlay.has(node.file_path, workspace_id=self.workspace_id)
            )
            if node.file_hash and not overlay_dirty:
                cached = self.cache.get_body(node.file_path, line_range, node.file_hash)
            if cached is not None:
                cache_hits.append("l1_body")
                code_map[node.uid] = (cached.code, cached.is_dirty)
                continue
            code, is_dirty = resolver.resolve(node.file_path, *line_range)
            code_map[node.uid] = (code, is_dirty)
            if node.file_hash and not is_dirty:
                self.cache.put_body(
                    node.file_path,
                    line_range,
                    node.file_hash,
                    CachedBody(code=code, token_count=node.token_estimate, is_dirty=False),
                )

        # 4. Resolve docs before compilation so intent-aware tier selection can include them.
        docs = []
        if self.vector_db:
            docs = DocResolver(self.vector_db).search(f"{symbol_name} {question}", limit=3)

        # 5. Compile prompt with intent-aware tier selection
        ctx = PromptCompiler().compile_with_intent(subgraph, code_map, docs, intent)
        ctx.budget["cache_hits"] = sorted(set(cache_hits))
        return ctx

    def _primary_uid(self, symbol_name: str) -> str | None:
        query = """
        MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol {name: $name})
        RETURN s.uid AS uid
        LIMIT 1
        """
        try:
            with self.db.driver.session() as session:
                result = session.run(
                    query, name=symbol_name, workspace_id=self.workspace_id
                ).single()
        except Exception:
            return None
        if not result:
            return None
        try:
            return result["uid"]
        except (KeyError, TypeError):
            return None

    def _graph_version(self) -> int:
        query = """
        MATCH (w:Workspace {id: $workspace_id})
        RETURN coalesce(w.graph_version, 0) AS graph_version
        """
        try:
            with self.db.driver.session() as session:
                result = session.run(query, workspace_id=self.workspace_id).single()
        except Exception:
            return 0
        if not result:
            return 0
        try:
            return int(result["graph_version"])
        except (KeyError, TypeError, ValueError):
            return 0
