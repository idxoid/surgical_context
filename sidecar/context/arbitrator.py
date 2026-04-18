from dataclasses import dataclass, field


@dataclass
class SymbolContext:
    symbol: str
    file_path: str
    relation: str          # "target" | "CALLS"
    is_dirty: bool
    code: str


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
                "is_dirty": self.primary_source.is_dirty,
                "code": self.primary_source.code,
            },
            "graph_context": [
                {
                    "symbol": dep.symbol,
                    "file_path": dep.file_path,
                    "relation": dep.relation,
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


class ContextArbitrator:
    def __init__(self, neo4j_client, overlay=None):
        self.db = neo4j_client
        self.overlay = overlay

    def get_context_for_symbol(self, symbol_name: str) -> PromptContext | str:
        """Returns PromptContext or an error string prefixed with 'Error:'."""
        query = """
        MATCH (s:Symbol {name: $name})
        OPTIONAL MATCH (s)-[:CALLS]->(call_dep:Symbol)
        OPTIONAL MATCH (s)-[:DEPENDS_ON]->(type_dep:Symbol)
        OPTIONAL MATCH (f:File)-[:CONTAINS]->(s), (f)-[:IMPORTS]->(import_file:File), (import_file)-[:CONTAINS]->(import_dep:Symbol)
        WITH s, collect(DISTINCT call_dep) as calls, collect(DISTINCT type_dep) as depends_on, collect(DISTINCT import_dep) as imports
        RETURN s as target, calls, depends_on, imports
        """
        with self.db.driver.session() as session:
            result = session.run(query, name=symbol_name).single()

        if not result or not result['target']:
            return f"Error: Symbol '{symbol_name}' not found in graph."

        target = result['target']
        call_deps = [dep for dep in result['calls'] if dep]
        type_deps = [dep for dep in result['depends_on'] if dep]
        import_deps = [dep for dep in result['imports'] if dep]

        primary = self._build_symbol_context(target, "target")
        graph_context = (
            [self._build_symbol_context(dep, "CALLS") for dep in call_deps] +
            [self._build_symbol_context(dep, "DEPENDS_ON") for dep in type_deps] +
            [self._build_symbol_context(dep, "IMPORTS") for dep in import_deps]
        )

        return PromptContext(primary_source=primary, graph_context=graph_context)

    def _build_symbol_context(self, symbol_node, relation: str) -> SymbolContext:
        path_query = "MATCH (f:File)-[:CONTAINS]->(s:Symbol {uid: $uid}) RETURN f.path as path"
        with self.db.driver.session() as session:
            path_res = session.run(path_query, uid=symbol_node['uid']).single()
            file_path = path_res['path']

        start, end = symbol_node['range']
        is_dirty = bool(self.overlay and self.overlay.has(file_path))

        if is_dirty:
            code = self.overlay.read_lines(file_path, start, end)
        else:
            with open(file_path, encoding='utf-8') as f:
                lines = f.readlines()
            code = "".join(lines[start - 1:end])

        return SymbolContext(
            symbol=symbol_node['name'],
            file_path=file_path,
            relation=relation,
            is_dirty=is_dirty,
            code=code,
        )
