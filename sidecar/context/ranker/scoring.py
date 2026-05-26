"""Blended scoring and graph neighbor scoring helpers."""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

from sidecar.context.intent_classifier import Intent
from sidecar.context.role_taxonomy import normalize_roles

from .candidate_pool import Candidate
from .signal_constants import FOCUS_QUERY_STOPWORDS

if TYPE_CHECKING:
    from sidecar.context.types import SubgraphNode


class RankerScoring:
    """Graph/normalization/blended score helpers."""

    def __init__(self, host):
        self.host = host

    def blended(self, c: Candidate) -> float:
        w = self.host.weights
        overlap_bonus = w.delta if c.overlap else 0.0
        positive = (
            w.alpha * c.graph_score
            + w.beta * c.semantic_score
            + w.gamma * c.intent_weight
            + overlap_bonus
        )
        return float(positive * c.noise_factor - w.epsilon * c.token_cost / 100)

    def normalize(self, pool: list[Candidate]) -> None:
        g_vals = sorted(c.graph_score for c in pool if c.graph_score > 0)
        s_vals = sorted(c.semantic_score for c in pool if c.semantic_score > 0)

        def _bounds(vals: list[float]) -> tuple[float, float]:
            if not vals:
                return 0.0, 1.0
            if len(vals) < 10:
                # Small pool: clip to [p10, p90] to prevent score collapse when
                # all candidates cluster at the same raw value.
                lo = vals[max(0, len(vals) // 10)]
                hi = vals[min(len(vals) - 1, (9 * len(vals)) // 10)]
                if hi <= lo:
                    hi = vals[-1]
                    lo = vals[0]
            else:
                p10 = vals[len(vals) // 10]
                p90 = vals[(9 * len(vals)) // 10]
                lo, hi = p10, p90
            return lo, hi

        g_lo, g_hi = _bounds(g_vals)
        s_lo, s_hi = _bounds(s_vals)
        g_range = (g_hi - g_lo) or 1.0
        s_range = (s_hi - s_lo) or 1.0

        for c in pool:
            if c.graph_score > 0:
                c.graph_score = max(0.0, min(1.0, (c.graph_score - g_lo) / g_range))
            if c.semantic_score > 0:
                c.semantic_score = max(0.0, min(1.0, (c.semantic_score - s_lo) / s_range))

    def intent_priors(self, intent: Intent) -> dict[str, float]:
        if intent in (Intent.DEBUGGING, Intent.NAVIGATION):
            return {"symbol": 0.6, "doc": 0.2}
        if intent in (Intent.NEW_FEATURE, Intent.DESIGN_QUESTION):
            return {"symbol": 0.2, "doc": 0.6}
        if intent == Intent.IMPACT_ANALYSIS:
            return {"symbol": 0.3, "doc": 0.5}
        return {"symbol": 0.4, "doc": 0.4}

    def topic_focus_factor(
        self,
        candidate: Candidate,
        target: SubgraphNode,
        *,
        query: str,
        mechanism: str,
        intent: Intent,
        required_roles: list[str],
    ) -> float:
        if intent == Intent.IMPACT_ANALYSIS or mechanism == "workspace_structure":
            return 1.0

        path = (candidate.file_path or "").lower()
        target_path = (target.file_path or "").lower()
        query_terms = set(self.focus_query_terms(query))
        has_explicit_role_backfill = self.host._has_role_backfill(candidate)

        path_terms = set(self.focus_identifier_terms(path))
        target_path_terms = set(self.focus_identifier_terms(target_path))
        # Subsystem isolation: if the candidate lives in a directory subtree that
        # shares no path-identifier overlap with the target's subtree, and the query
        # doesn't mention any of the candidate's path terms, it's likely off-topic.
        # Use at least 2-term overlap as the "same subsystem" threshold so generic
        # parent dirs (src/, lib/) don't count.
        candidate_unique_terms = path_terms - target_path_terms
        if (
            len(candidate_unique_terms) >= 2
            and not (candidate_unique_terms & query_terms)
            and not has_explicit_role_backfill
        ):
            return 0.15 if candidate.kind != "doc" else 0.45

        required = set(normalize_roles(required_roles)) - {"docs_or_concept"}
        primary_role = self.host.role_fulfilment.role_of(candidate)
        if candidate.kind != "doc" and (primary_role in required or has_explicit_role_backfill):
            return 1.0

        if self.candidate_matches_query_topic(candidate, target, query=query):
            return 1.0

        if candidate.kind != "doc" and candidate.depth >= 5:
            return 0.25 if candidate.depth >= 7 else 0.45

        if candidate.kind == "doc":
            low_anchor = candidate.anchor_type in ("", "reference") and (
                not candidate.anchor_confidence or candidate.anchor_confidence < 0.4
            )
            if low_anchor:
                return 0.65

        return 1.0

    def candidate_matches_query_topic(
        self,
        candidate: Candidate | SubgraphNode,
        target: SubgraphNode,
        *,
        query: str,
    ) -> bool:
        terms = set(self.focus_query_terms(query))
        terms.update(self.focus_query_terms(target.name or ""))
        if not terms:
            return False
        haystack = " ".join(
            part.lower()
            for part in (
                getattr(candidate, "name", "") or "",
                getattr(candidate, "file_path", "") or "",
                getattr(candidate, "qualified_name", "") or "",
            )
            if part
        )
        return any(term in haystack for term in terms)

    @staticmethod
    def focus_query_terms(text: str) -> list[str]:
        return [
            term
            for term in re.findall(r"[a-z][a-z0-9_]{3,}", (text or "").lower())
            if term not in FOCUS_QUERY_STOPWORDS
        ]

    @staticmethod
    def focus_identifier_terms(text: str) -> list[str]:
        spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
        return [
            term
            for term in re.findall(r"[a-z][a-z0-9]{2,}", spaced.lower())
            if term not in FOCUS_QUERY_STOPWORDS
        ]

    def raw_graph_score(
        self,
        neighbor: dict,
        distance: int,
        *,
        chain_pursuit: bool = False,
    ) -> float:
        rel_type = neighbor["rel_type"]
        outgoing = neighbor["outgoing"]
        caller_count = neighbor["caller_count"]
        token_estimate = neighbor.get("token_estimate", 0)

        if rel_type in (
            "CALLS_DIRECT",
            "CALLS_SCOPED",
            "CALLS_IMPORTED",
            "CALLS_DYNAMIC",
            "CALLS_INFERRED",
            "CALLS_GUESS",
            "CALLS",
        ):
            base = rel_type if rel_type != "CALLS" else "CALLS_DIRECT"
            relation = f"{base}_out" if outgoing else f"{base}_in"
        elif rel_type in ("IMPLEMENTS", "OVERRIDES", "REFERENCES"):
            relation = rel_type
        elif rel_type == "DEPENDS_ON":
            relation = "DEPENDS_ON"
        elif rel_type == "IMPORTS":
            relation = "IMPORTS"
        elif rel_type == "SEMANTIC_HINT":
            relation = "SEMANTIC_HINT_out" if outgoing else "SEMANTIC_HINT_in"
        else:
            relation = "DEPENDS_ON"

        r = self.host._RELATION_PRIOR.get(relation, 0.5)

        if (
            chain_pursuit and self.host._is_outgoing_call(rel_type, outgoing)
        ) or rel_type == "SEMANTIC_HINT":
            distance_penalty = 0.15 * distance
        else:
            distance_penalty = 0.4 * distance

        return float(
            r + 0.3 * math.log1p(caller_count) - 0.1 * token_estimate / 100 - distance_penalty
        )

    def direction(self, rel_type: str, outgoing: bool) -> str:
        if rel_type in (
            "CALLS",
            "CALLS_DIRECT",
            "CALLS_SCOPED",
            "CALLS_IMPORTED",
            "CALLS_DYNAMIC",
            "CALLS_INFERRED",
            "CALLS_GUESS",
        ):
            return "callee" if outgoing else "caller"
        if rel_type == "DEPENDS_ON":
            return "type"
        if rel_type == "IMPORTS":
            return "import"
        if rel_type in ("IMPLEMENTS", "OVERRIDES", "REFERENCES"):
            return rel_type.lower()
        return "sibling"

    @staticmethod
    def trace_dependency_gain_mode(mechanism: str, query: str) -> bool:
        m = (mechanism or "").lower()
        q = (query or "").lower()
        if "depend" in m or "trace_dependency" in m:
            return True
        if "hook" in m or "lifecycle" in m:
            return True
        if "dependency injection" in q:
            return True
        if "inject" in q and "depend" in q:
            return True
        if "before_request" in q or "after_request" in q:
            return True
        if "relationship" in q and any(term in q for term in ("foreign", "lazy", "collection")):
            return True
        if (
            "query" in q
            and ("lazy" in q or "lazily" in q)
            and any(term in q for term in ("execute", "sql"))
        ):
            return True
        if any(term in q for term in ("actor", "identity", "principal", "same_actor")) and any(
            term in q for term in ("resolve", "refer", "same", "decide", "mapping")
        ):
            return True
        if any(term in q for term in ("clock", "window", "event_time", "ingested_time")) and any(
            term in q for term in ("chain", "correlation", "match", "time")
        ):
            return True
        if any(term in q for term in ("compile", "compiler", "template")) and any(
            term in q for term in ("render", "runtime", "update")
        ):
            return True
        return False
