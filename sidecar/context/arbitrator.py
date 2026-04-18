import math
from dataclasses import dataclass, field
from heapq import heappush, heappop


@dataclass
class SymbolContext:
    symbol: str
    file_path: str
    relation: str          # "target" | "CALLS" | "DEPENDS_ON" | "IMPORTS"
    direction: str = "callee"      # "callee" | "caller" | "type" | "import"
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


class ContextArbitrator:
    # Constants from spec_token_budget_bfs.md
    PREAMBLE_TOKENS = 100
    RELATION_PRIOR = {
        "CALLS_out": 1.0,
        "CALLS_in": 1.2,
        "DEPENDS_ON": 0.8,
        "IMPORTS": 0.6,
    }
    WEIGHTS = {
        "rel": 1.0,
        "fan": 0.3,
        "cost": 0.5,
        "dist": 0.4,
    }

    def __init__(self, neo4j_client, overlay=None):
        self.db = neo4j_client
        self.overlay = overlay

    def get_context_for_symbol(
        self,
        symbol_name: str,
        token_budget: int = 4000,
    ) -> PromptContext | str:
        """Returns PromptContext or an error string prefixed with 'Error:'."""
        # Lookup target symbol by name
        query = "MATCH (s:Symbol {name: $name}) RETURN s LIMIT 1"
        with self.db.driver.session() as session:
            result = session.run(query, name=symbol_name).single()

        if not result:
            return f"Error: Symbol '{symbol_name}' not found in graph."

        target = result["s"]
        target_uid = target["uid"]
        target_token_cost = target.get("token_estimate", 0) or self._estimate_tokens(target)

        # Check if target alone exceeds budget
        reserved = self.PREAMBLE_TOKENS + target_token_cost
        if reserved > token_budget:
            return f"Error: Token budget {token_budget} is too small for target symbol (needs {reserved} tokens)."

        primary = self._build_symbol_context(target, "target", depth=0, direction="primary", relevance_score=1.0)

        # Priority queue BFS
        visited = {target_uid}
        chosen = []
        spent = reserved
        frontier = []  # heap of (-score, uid, symbol_node, rel_type, outgoing, distance)
        pruned = 0

        # Push depth-1 neighbors
        neighbors = self._get_neighbors(target_uid, visited, distance=1)
        for n in neighbors:
            score = self._score(
                n["rel_type"],
                n["outgoing"],
                n["caller_count"],
                n["token_estimate"],
                distance=1,
            )
            heappush(frontier, (-score, n["uid"], n, n["rel_type"], n["outgoing"], 1))

        # Expansion loop
        while frontier and spent < token_budget:
            neg_score, uid, neighbor, rel_type, outgoing, distance = heappop(frontier)
            score = -neg_score

            if uid in visited:
                continue

            token_cost = neighbor.get("token_estimate", 0) or self._estimate_tokens(neighbor)

            # "Skip but keep trying" — if this symbol exceeds remaining budget, skip it
            if spent + token_cost > token_budget:
                pruned += 1
                continue

            # Accept this symbol
            visited.add(uid)
            direction = self._direction(rel_type, outgoing)
            ctx = self._build_symbol_context(
                neighbor,
                rel_type,
                depth=distance,
                direction=direction,
                relevance_score=score,
            )
            chosen.append(ctx)
            spent += token_cost

            # Push its neighbors
            next_neighbors = self._get_neighbors(uid, visited, distance=distance + 1)
            for next_n in next_neighbors:
                next_score = self._score(
                    next_n["rel_type"],
                    next_n["outgoing"],
                    next_n["caller_count"],
                    next_n["token_estimate"],
                    distance=distance + 1,
                )
                heappush(frontier, (-next_score, next_n["uid"], next_n, next_n["rel_type"], next_n["outgoing"], distance + 1))

        budget_info = {
            "limit": token_budget,
            "spent": spent,
            "reserved": self.PREAMBLE_TOKENS,
            "pruned": pruned,
        }

        return PromptContext(
            primary_source=primary,
            graph_context=chosen,
            budget=budget_info,
        )

    def _score(self, rel_type: str, outgoing: bool, caller_count: int, token_estimate: int, distance: int) -> float:
        """Compute relevance score for a candidate symbol."""
        # Determine relation type
        if rel_type == "CALLS":
            relation = "CALLS_out" if outgoing else "CALLS_in"
        elif rel_type == "DEPENDS_ON":
            relation = "DEPENDS_ON"
        elif rel_type == "IMPORTS":
            relation = "IMPORTS"
        else:
            relation = "DEPENDS_ON"

        r = self.RELATION_PRIOR.get(relation, 0.5)
        score = (
            self.WEIGHTS["rel"] * r
            + self.WEIGHTS["fan"] * math.log1p(caller_count)
            - self.WEIGHTS["cost"] * token_estimate / 100
            - self.WEIGHTS["dist"] * distance
        )
        return score

    def _direction(self, rel_type: str, outgoing: bool) -> str:
        """Map relation type to direction string for context metadata."""
        if rel_type == "CALLS":
            return "callee" if outgoing else "caller"
        elif rel_type == "DEPENDS_ON":
            return "type"
        elif rel_type == "IMPORTS":
            return "import"
        return "sibling"

    def _get_neighbors(self, uid: str, visited: set, distance: int) -> list[dict]:
        """Fetch immediate neighbors of a symbol, returning unvisited only."""
        query = """
        MATCH (s:Symbol {uid: $uid})-[r:CALLS|DEPENDS_ON]-(n:Symbol)
        WHERE NOT n.uid IN $visited
        OPTIONAL MATCH ()-[:CALLS]->(n)
        WITH n, r, startNode(r) = s AS outgoing, count(*) AS caller_count
        RETURN n.uid AS uid,
               n.name AS name,
               coalesce(n.token_estimate, 0) AS token_estimate,
               n.range AS range,
               type(r) AS rel_type,
               outgoing,
               caller_count
        """
        neighbors = []
        with self.db.driver.session() as session:
            result = session.run(query, uid=uid, visited=list(visited))
            for record in result:
                neighbors.append({
                    "uid": record["uid"],
                    "name": record["name"],
                    "token_estimate": record["token_estimate"],
                    "range": record["range"],
                    "rel_type": record["rel_type"],
                    "outgoing": record["outgoing"],
                    "caller_count": record["caller_count"],
                })
        return neighbors

    def _estimate_tokens(self, node: dict) -> int:
        """Cold-path token estimate: ~8 tokens per line."""
        if "range" not in node:
            return 0
        start, end = node["range"]
        return max(1, (end - start + 1) * 8)

    def _build_symbol_context(
        self,
        symbol_node: dict,
        relation: str,
        depth: int = 0,
        direction: str = "callee",
        relevance_score: float = 0.0,
    ) -> SymbolContext:
        """Build a SymbolContext from a Neo4j symbol node."""
        path_query = "MATCH (f:File)-[:CONTAINS]->(s:Symbol {uid: $uid}) RETURN f.path as path"
        with self.db.driver.session() as session:
            path_res = session.run(path_query, uid=symbol_node["uid"]).single()
            if not path_res:
                # Orphan symbol (no File) — return with empty path
                file_path = "<unknown>"
                code = ""
            else:
                file_path = path_res["path"]

                start, end = symbol_node["range"]
                is_dirty = bool(self.overlay and self.overlay.has(file_path))

                if is_dirty:
                    code = self.overlay.read_lines(file_path, start, end)
                else:
                    try:
                        with open(file_path, encoding="utf-8") as f:
                            lines = f.readlines()
                        code = "".join(lines[start - 1:end])
                    except (FileNotFoundError, IOError):
                        code = ""

        is_dirty = bool(self.overlay and self.overlay.has(file_path))

        return SymbolContext(
            symbol=symbol_node["name"],
            file_path=file_path,
            relation=relation,
            direction=direction,
            depth=depth,
            relevance_score=relevance_score,
            is_dirty=is_dirty,
            code=code,
        )
