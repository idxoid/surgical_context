"""GraphExpander — Neo4j-only graph traversal and BFS."""

import math
from heapq import heappop, heappush

from sidecar.context.types import Subgraph, SubgraphNode


class GraphExpander:
    """Priority-queue BFS expansion with token budgeting."""

    PREAMBLE_TOKENS = 100
    RELATION_PRIOR = {
        "CALLS_DIRECT_out": 1.0,
        "CALLS_DIRECT_in": 1.2,
        "CALLS_DYNAMIC_out": 0.7,
        "CALLS_DYNAMIC_in": 0.9,
        "CALLS_INFERRED_out": 0.4,
        "CALLS_INFERRED_in": 0.5,
        "IMPLEMENTS": 1.1,
        "OVERRIDES": 1.1,
        "REFERENCES": 0.3,
        "DEPENDS_ON": 0.8,
        "IMPORTS": 0.6,
        "CALLS_out": 1.0,
        "CALLS_in": 1.2,
    }
    WEIGHTS = {
        "rel": 1.0,
        "fan": 0.3,
        "cost": 0.5,
        "dist": 0.4,
    }

    def __init__(self, neo4j_client):
        self.db = neo4j_client

    def expand(self, symbol_name: str, token_budget: int = 4000) -> Subgraph | str:
        """Run token-budget BFS. Returns Subgraph or error string."""
        query = """
        MATCH (s:Symbol {name: $name})
        OPTIONAL MATCH (f:File)-[:CONTAINS]->(s)
        RETURN s, coalesce(f.path, '<unknown>') AS file_path
        LIMIT 1
        """
        with self.db.driver.session() as session:
            result = session.run(query, name=symbol_name).single()

        if not result:
            return f"Error: Symbol '{symbol_name}' not found in graph."

        target = result["s"]
        target_file_path = result["file_path"]
        target_uid = target["uid"]
        target_token_cost = target.get("token_estimate", 0) or self._estimate_tokens(target)

        reserved = self.PREAMBLE_TOKENS + target_token_cost
        if reserved > token_budget:
            return f"Error: Token budget {token_budget} is too small for target symbol (needs {reserved} tokens)."

        primary = SubgraphNode(
            uid=target_uid,
            name=target["name"],
            file_path=target_file_path,
            range=target.get("range", [0, 0]),
            token_estimate=target_token_cost,
            relation="target",
            direction="primary",
            depth=0,
            relevance_score=1.0,
        )

        visited = {target_uid}
        chosen = []
        spent = reserved
        frontier = []
        pruned = 0

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

        while frontier and spent < token_budget:
            neg_score, uid, neighbor, rel_type, outgoing, distance = heappop(frontier)
            score = -neg_score

            if uid in visited:
                continue

            token_cost = neighbor.get("token_estimate", 0) or self._estimate_tokens(neighbor)

            if spent + token_cost > token_budget:
                pruned += 1
                continue

            visited.add(uid)
            direction = self._direction(rel_type, outgoing)
            node = SubgraphNode(
                uid=uid,
                name=neighbor["name"],
                file_path=neighbor["file_path"],
                range=neighbor.get("range", [0, 0]),
                token_estimate=token_cost,
                relation=rel_type,
                direction=direction,
                depth=distance,
                relevance_score=score,
            )
            chosen.append(node)
            spent += token_cost

            next_neighbors = self._get_neighbors(uid, visited, distance=distance + 1)
            for next_n in next_neighbors:
                next_score = self._score(
                    next_n["rel_type"],
                    next_n["outgoing"],
                    next_n["caller_count"],
                    next_n["token_estimate"],
                    distance=distance + 1,
                )
                heappush(
                    frontier,
                    (
                        -next_score,
                        next_n["uid"],
                        next_n,
                        next_n["rel_type"],
                        next_n["outgoing"],
                        distance + 1,
                    ),
                )

        budget_info = {
            "limit": token_budget,
            "spent": spent,
            "reserved": self.PREAMBLE_TOKENS,
            "pruned": pruned,
        }

        return Subgraph(primary=primary, nodes=chosen, budget=budget_info)

    def _score(
        self, rel_type: str, outgoing: bool, caller_count: int, token_estimate: int, distance: int
    ) -> float:
        """Compute relevance score for a candidate symbol."""
        if rel_type in ("CALLS_DIRECT", "CALLS_DYNAMIC", "CALLS_INFERRED", "CALLS"):
            base = rel_type if rel_type != "CALLS" else "CALLS_DIRECT"
            relation = f"{base}_out" if outgoing else f"{base}_in"
        elif rel_type in ("IMPLEMENTS", "OVERRIDES", "REFERENCES"):
            relation = rel_type
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
        if rel_type in ("CALLS", "CALLS_DIRECT", "CALLS_DYNAMIC", "CALLS_INFERRED"):
            return "callee" if outgoing else "caller"
        elif rel_type == "DEPENDS_ON":
            return "type"
        elif rel_type == "IMPORTS":
            return "import"
        elif rel_type == "IMPLEMENTS":
            return "interface"
        elif rel_type == "OVERRIDES":
            return "override"
        elif rel_type == "REFERENCES":
            return "reference"
        return "sibling"

    def _get_neighbors(self, uid: str, visited: set, distance: int) -> list[dict]:
        """Fetch immediate neighbors of a symbol, returning unvisited only."""
        query = """
        MATCH (s:Symbol {uid: $uid})-[r:CALLS|CALLS_DIRECT|CALLS_DYNAMIC|CALLS_INFERRED|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES]-(n:Symbol)
        WHERE NOT n.uid IN $visited
        OPTIONAL MATCH ()-[:CALLS|CALLS_DIRECT|CALLS_DYNAMIC|CALLS_INFERRED]->(n)
        OPTIONAL MATCH (fn:File)-[:CONTAINS]->(n)
        WITH n, fn, r, startNode(r) = s AS outgoing, count(*) AS caller_count
        RETURN n.uid AS uid,
               n.name AS name,
               coalesce(fn.path, '<unknown>') AS file_path,
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
                neighbors.append(
                    {
                        "uid": record["uid"],
                        "name": record["name"],
                        "file_path": record["file_path"],
                        "token_estimate": record["token_estimate"],
                        "range": record["range"],
                        "rel_type": record["rel_type"],
                        "outgoing": record["outgoing"],
                        "caller_count": record["caller_count"],
                    }
                )
        return neighbors

    def _estimate_tokens(self, node: dict) -> int:
        """Cold-path token estimate: ~8 tokens per line."""
        if "range" not in node:
            return 0
        start, end = node["range"]
        return max(1, (end - start + 1) * 8)
