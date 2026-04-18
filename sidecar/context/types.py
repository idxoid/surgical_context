"""Shared dataclasses for context assembly pipeline."""

from dataclasses import dataclass, field


@dataclass
class SymbolContext:
    symbol: str
    file_path: str
    relation: str
    direction: str = "callee"
    depth: int = 0
    relevance_score: float = 0.0
    is_dirty: bool = False
    code: str = ""


@dataclass
class DocChunk:
    source_file: str
    chunk_id: str
    content: str


@dataclass
class PromptContext:
    primary_source: SymbolContext
    graph_context: list[SymbolContext] = field(default_factory=list)
    documentation: list[DocChunk] = field(default_factory=list)
    budget: dict = field(default_factory=dict)

    def to_system_prompt(self) -> str:
        """Render to the flat text format the LLM receives."""
        blocks = [
            f"--- TARGET SYMBOL: {self.primary_source.symbol} ---",
            self.primary_source.code,
        ]
        if self.graph_context:
            blocks.append("\n--- DEPENDENCIES ---")
            for dep in self.graph_context:
                blocks.append(f"\n# From {dep.symbol} [{dep.relation}]:")
                blocks.append(dep.code)
        if self.documentation:
            blocks.append("\n--- DOCUMENTATION ---")
            for doc in self.documentation:
                blocks.append(f"[{doc.source_file}]\n{doc.content}")
        return "\n".join(blocks)

    def to_dict(self) -> dict:
        """Serialize to the JSON Prompt Contract shape."""
        return {
            "primary_source": {
                "symbol": self.primary_source.symbol,
                "file_path": self.primary_source.file_path,
                "depth": self.primary_source.depth,
                "direction": self.primary_source.direction,
                "relevance_score": self.primary_source.relevance_score,
                "is_dirty": self.primary_source.is_dirty,
                "code": self.primary_source.code,
            },
            "graph_context": [
                {
                    "symbol": dep.symbol,
                    "file_path": dep.file_path,
                    "relation": dep.relation,
                    "direction": dep.direction,
                    "depth": dep.depth,
                    "relevance_score": dep.relevance_score,
                    "is_dirty": dep.is_dirty,
                    "code": dep.code,
                }
                for dep in self.graph_context
            ],
            "documentation": [
                {
                    "chunk_id": doc.chunk_id,
                    "source_file": doc.source_file,
                    "content": doc.content,
                }
                for doc in self.documentation
            ],
            "budget": self.budget,
        }

    def token_count(self) -> int:
        """Count tokens in the assembled prompt using cl100k_base encoding (GPT-3.5/4)."""
        try:
            import tiktoken
        except ImportError:
            raise ImportError("tiktoken is required for token counting")

        enc = tiktoken.get_encoding("cl100k_base")
        prompt_text = self.to_system_prompt()
        return len(enc.encode(prompt_text))


class BudgetTooSmall(ValueError):
    """Raised when target symbol alone exceeds token_budget."""
    pass


@dataclass
class SubgraphNode:
    """Internal: graph node with metadata from expansion."""
    uid: str
    name: str
    file_path: str
    range: list[int]
    token_estimate: int
    relation: str
    direction: str
    depth: int
    relevance_score: float


@dataclass
class Subgraph:
    """Internal: result of graph expansion (before code resolution)."""
    primary: SubgraphNode
    nodes: list[SubgraphNode]
    budget: dict
