"""Semantic hint indexer — applies YAML rule types to create specialized edges.

Rules should describe reusable graph patterns (for example
``call_argument_link`` for dependency-like APIs), not framework-specific
fixtures. Exact trigger names and qualified-prefix gates are still supported
for external/custom packs, but bundled rules should prefer shared subtypes and
token predicates over repo- or framework-named literals.
"""

import os
import re
from typing import Any

import yaml

from context_engine.database.neo4j_client import Neo4jClient


def _matches_callee_qualified_gate(call: dict, rule: dict) -> bool:
    """If ``require_callee_qualified_prefix`` is set, require resolved callee name."""
    prefix = rule.get("require_callee_qualified_prefix")
    if not prefix:
        return True
    trig = rule.get("trigger_call") or ""
    q = call.get("callee_qualified_name")
    if not q or not trig:
        return False
    return bool(q.startswith(f"{prefix}.") and q.endswith(f".{trig}"))


def _identifier_terms(*parts: str) -> list[str]:
    terms: list[str] = []
    for part in parts:
        spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(part or ""))
        terms.extend(t.lower() for t in re.split(r"[^A-Za-z0-9]+", spaced) if t)
    return terms


def _matches_trigger(call: dict, rule: dict) -> bool:
    trigger = rule.get("trigger_call")
    if trigger and call.get("callee_name") == trigger:
        return _matches_callee_qualified_gate(call, rule)

    tokens = [str(t).lower() for t in rule.get("trigger_call_tokens") or [] if str(t).strip()]
    if not tokens:
        return False

    terms = _identifier_terms(
        call.get("callee_name") or "",
        call.get("callee_qualified_name") or "",
    )
    return any(term == token or term.startswith(token) for term in terms for token in tokens)


def _matches_call_argument_rule(call: dict, rule: dict) -> bool:
    if rule.get("type") != "call_argument_link":
        return False
    return _matches_trigger(call, rule)


class FrameworkHintsIndexer:
    """Indexer that applies semantic hint rules to the graph."""

    def __init__(self, db: Neo4jClient):
        self.db = db
        self.rules = self._load_rules()

    def _load_rules(self) -> list[dict[str, Any]]:
        # TODO: merge rules from workspace/profile keyed by shared types/subtypes.
        rules_dir = os.path.dirname(__file__)
        all_rules = []
        if not os.path.exists(rules_dir):
            return []
        for filename in os.listdir(rules_dir):
            if filename.endswith(".yaml") or filename.endswith(".yml"):
                try:
                    with open(os.path.join(rules_dir, filename)) as f:
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
                for rule in self.rules:
                    if _matches_call_argument_rule(call, rule):
                        self._apply_call_arg_link(call, rule, workspace_id)

    def _apply_call_arg_link(self, call: dict, rule: dict, workspace_id: str):
        """
        Rule type: call_argument_link.
        Links the caller symbol to the symbol named in a specific call argument.
        Example: dependency_marker(get_db) -> links caller to get_db.
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
