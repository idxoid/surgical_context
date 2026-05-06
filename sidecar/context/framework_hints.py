"""FrameworkHintsIndexer — applies YAML-based semantic rules to create specialized edges.

**Current state:** only ``fastapi.yaml`` lives under ``sidecar/context/``; there are
no ``pydantic.yaml`` / ``redux*.yaml`` (or similar) here yet. Pydantic and Redux Toolkit
biases that exist today are elsewhere (e.g. ranker topic / path heuristics, question
packs) — not this indexer-time YAML hook. If we add Pydantic or RTK rules later, they
should not copy the per-framework file pattern long-term.

REFACTOR (planned): bundled per-framework YAML in this package (e.g. ``fastapi.yaml``)
should give way to hints generated from **shared rule types / subtypes** (a small
taxonomy of semantic patterns: call-argument links, decorator bridges, etc.) so new
stacks add subtype instances instead of new framework-named trigger literals scattered
in repo YAML. Until then, ``*.yaml`` here remains an indexer-time escape hatch.
"""

import os
import yaml
from typing import Any
from sidecar.database.neo4j_client import Neo4jClient


def _matches_callee_qualified_gate(call: dict, rule: dict) -> bool:
    """If ``require_callee_qualified_prefix`` is set, require resolved callee name.

    Example: prefix ``fastapi`` + trigger ``Depends`` accepts ``fastapi.Depends`` and
    ``fastapi.dependencies.Depends``; rejects bare ``Depends`` with no import binding.
    """
    prefix = rule.get("require_callee_qualified_prefix")
    if not prefix:
        return True
    trig = rule.get("trigger_call") or ""
    q = call.get("callee_qualified_name")
    if not q or not trig:
        return False
    return q.startswith(f"{prefix}.") and q.endswith(f".{trig}")


class FrameworkHintsIndexer:
    """Indexer that applies framework-specific rules to the graph."""

    def __init__(self, db: Neo4jClient):
        self.db = db
        self.rules = self._load_rules()

    def _load_rules(self) -> list[dict[str, Any]]:
        # TODO: merge rules from workspace/profile keyed by shared types/subtypes instead
        # of scanning framework-named files only (see module docstring).
        rules_dir = os.path.dirname(__file__)
        all_rules = []
        if not os.path.exists(rules_dir):
            return []
        for filename in os.listdir(rules_dir):
            if filename.endswith(".yaml") or filename.endswith(".yml"):
                try:
                    with open(os.path.join(rules_dir, filename), "r") as f:
                        data = yaml.safe_load(f)
                        if data and "rules" in data:
                            all_rules.extend(data["rules"])
                except Exception:
                    continue
        return all_rules

    def apply_rules(self, diffs: list, workspace_id: str):
        """Scan extracted diffs for patterns defined in rules and create SEMANTIC_HINT edges."""
        for diff in diffs:
            extracted = diff.extracted
            for call in extracted.calls:
                # Check each rule against this call
                for rule in self.rules:
                    if (
                        rule["type"] == "call_argument_link"
                        and call.get("callee_name") == rule["trigger_call"]
                    ):
                        if not _matches_callee_qualified_gate(call, rule):
                            continue
                        self._apply_call_arg_link(call, rule, workspace_id)

    def _apply_call_arg_link(self, call: dict, rule: dict, workspace_id: str):
        """
        Rule type: call_argument_link.
        Links the caller symbol to the symbol named in a specific call argument.
        Example: Depends(get_db) -> links caller to get_db.
        """
        args = call.get("arguments", [])
        idx = rule.get("argument_index", 0)
        if idx >= len(args):
            return

        target_name = args[idx]
        caller_uid = call.get("caller_uid")
        if not caller_uid or not target_name:
            return

        # Create the edge in Neo4j with "edge class" metadata
        query = """
        MATCH (caller:Symbol {uid: $caller_uid})
        MATCH (target:Symbol {name: $target_name})
        WHERE coalesce(target.workspace_id, $workspace_id) = $workspace_id
        MERGE (caller)-[r:SEMANTIC_HINT {
            workspace_id: $workspace_id,
            rule_id: $rule_id,
            kind: $kind
        }]->(target)
        SET r.derived_at = datetime()
        """
        try:
            with self.db.driver.session() as session:
                session.run(
                    query,
                    caller_uid=caller_uid,
                    target_name=target_name,
                    workspace_id=workspace_id,
                    rule_id=rule["id"],
                    kind=rule.get("metadata", {}).get("kind", "generic"),
                )
        except Exception:
            pass

    def clear_hints_for_uids(self, uids: list[str], workspace_id: str):
        """Remove outgoing hints for symbols being re-indexed."""
        query = "MATCH (s:Symbol)-[r:SEMANTIC_HINT]->() WHERE s.uid IN $uids AND r.workspace_id = $workspace_id DELETE r"
        with self.db.driver.session() as session:
            session.run(query, uids=uids, workspace_id=workspace_id)
