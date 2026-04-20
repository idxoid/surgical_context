"""ContextDeduplicator — removes redundant symbols and doc chunks from expanded subgraphs."""

from sidecar.context.types import Subgraph, SubgraphNode, DocChunk


class ContextDeduplicator:
    """Stateless transform: deduplicates nodes and docs from an expanded Subgraph."""

    def deduplicate(self, subgraph: Subgraph) -> Subgraph:
        """Remove redundant symbols and doc chunks. Returns new Subgraph; never mutates input."""
        if not subgraph or not subgraph.nodes:
            return subgraph

        nodes = self._deduplicate_nodes(subgraph.nodes, primary_uid=subgraph.primary.uid)
        nodes = self._collapse_line_ranges(nodes)

        original_tokens = sum(n.token_estimate for n in subgraph.nodes)
        new_tokens = sum(n.token_estimate for n in nodes)
        saved_tokens = original_tokens - new_tokens

        updated_budget = dict(subgraph.budget)
        # Include primary in spent calculation
        updated_budget["spent"] = subgraph.primary.token_estimate + new_tokens
        updated_budget["dedup_saved"] = saved_tokens

        return Subgraph(primary=subgraph.primary, nodes=nodes, budget=updated_budget)

    def _deduplicate_nodes(self, nodes: list[SubgraphNode], primary_uid: str = None) -> list[SubgraphNode]:
        """Keep only one copy of each UID; prefer lowest depth, then highest relevance_score.
        Never include primary_uid in result (primary is stored separately)."""
        seen: dict[str, SubgraphNode] = {}
        for node in nodes:
            # Skip if this node has the same UID as the primary
            if primary_uid and node.uid == primary_uid:
                continue
            existing = seen.get(node.uid)
            if existing is None:
                seen[node.uid] = node
            elif node.depth < existing.depth:
                seen[node.uid] = node
            elif node.depth == existing.depth and node.relevance_score > existing.relevance_score:
                seen[node.uid] = node
        return list(seen.values())

    def _collapse_line_ranges(self, nodes: list[SubgraphNode]) -> list[SubgraphNode]:
        """Merge nodes with overlapping line ranges in the same file."""
        from collections import defaultdict

        by_file: dict[str, list[SubgraphNode]] = defaultdict(list)
        for node in nodes:
            if node.file_path != "<unknown>":
                by_file[node.file_path].append(node)
            else:
                by_file[None].append(node)  # Keep unknowns separate

        result = []
        for file_path, file_nodes in by_file.items():
            if file_path is None:
                result.extend(file_nodes)
            else:
                result.extend(self._merge_overlapping_ranges(file_nodes))
        return result

    def _merge_overlapping_ranges(self, nodes: list[SubgraphNode]) -> list[SubgraphNode]:
        """Merge nodes with overlapping/adjacent line ranges within the same file.
        Return nodes deduplicated by identity (if merged, skip both originals)."""
        if not nodes:
            return []

        # Fast path: if all UIDs are unique, no merging needed
        uids = {n.uid for n in nodes}
        if len(uids) == len(nodes):
            return nodes

        # Slow path: check for actual line overlaps (rare)
        sorted_nodes = sorted(nodes, key=lambda n: (n.range[0], n.range[1]))
        merged = []
        current = sorted_nodes[0]

        for next_node in sorted_nodes[1:]:
            # Check for overlap or adjacency: current.end >= next.start - 1
            if current.range[1] >= next_node.range[0] - 1:
                # Merge: extend range, keep lower depth, higher score
                merged_range = [current.range[0], max(current.range[1], next_node.range[1])]
                merged_depth = min(current.depth, next_node.depth)
                merged_score = max(current.relevance_score, next_node.relevance_score)

                current = SubgraphNode(
                    uid=current.uid,  # Keep first uid
                    name=current.name,
                    file_path=current.file_path,
                    range=merged_range,
                    token_estimate=current.token_estimate,  # Keep first estimate (safe)
                    relation=current.relation,
                    direction=current.direction,
                    depth=merged_depth,
                    relevance_score=merged_score,
                )
            else:
                # No overlap, emit current and move to next
                merged.append(current)
                current = next_node

        merged.append(current)
        return merged

    def _deduplicate_docs(self, docs: list) -> list:
        """Remove doc chunks with >85% content overlap. Heuristic, not exact."""
        if not docs:
            return docs

        kept = []
        for doc in docs:
            is_duplicate = False
            for existing in kept:
                if self._overlap_ratio(str(existing.get("content", "")), str(doc.get("content", ""))) > 0.85:
                    is_duplicate = True
                    break
            if not is_duplicate:
                kept.append(doc)
        return kept

    def _overlap_ratio(self, a: str, b: str) -> float:
        """Heuristic: check for content overlap via substring search."""
        if not a or not b:
            return 0.0
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        chunk_size = max(40, len(shorter) // 10)
        if chunk_size == 0:
            return 0.0
        matches = sum(1 for i in range(len(shorter) - chunk_size + 1) if shorter[i : i + chunk_size] in longer)
        return matches / max(1, len(shorter) // chunk_size)
