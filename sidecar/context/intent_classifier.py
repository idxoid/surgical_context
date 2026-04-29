"""Intent classification — detect user query type to guide context assembly."""

from dataclasses import dataclass, field
from enum import Enum
import re


class Intent(Enum):
    """Query intent types that determine content tier priority."""

    NAVIGATION = "navigation"  # "Where is X? What calls X?"
    DEBUGGING = "debugging"  # "Why does X fail? Why is this broken?"
    REFACTORING = "refactor"  # "Change X to use Y. Rename everywhere."
    EXPLORATION = "exploration"  # "What does this do? How does it work?"
    NEW_FEATURE = "new_feature"  # "Add X that does Y. Implement Z."
    DESIGN_QUESTION = "design_question"  # "How should we do this? What pattern?"
    IMPACT_ANALYSIS = "impact_analysis"  # "If I change X, what breaks? What's affected?"


class IntentConfig:
    """Maps intent to content tier priority (highest → lowest)."""

    # Tier definitions
    TIERS = ["code", "cross_refs", "specs", "architecture", "concept", "idea"]

    # Intent → tier priority ordering
    PRIORITY = {
        Intent.NAVIGATION: ["code", "cross_refs", "architecture", "specs", "concept", "idea"],
        Intent.DEBUGGING: ["code", "cross_refs", "specs", "architecture", "concept", "idea"],
        Intent.REFACTORING: ["cross_refs", "code", "architecture", "specs", "concept", "idea"],
        Intent.EXPLORATION: ["code", "concept", "architecture", "cross_refs", "specs", "idea"],
        Intent.NEW_FEATURE: ["idea", "concept", "architecture", "specs", "cross_refs", "code"],
        Intent.DESIGN_QUESTION: ["concept", "idea", "architecture", "specs", "code", "cross_refs"],
        Intent.IMPACT_ANALYSIS: ["cross_refs", "code", "specs", "architecture", "concept", "idea"],
    }


@dataclass(frozen=True)
class IntentSignal:
    """Intent classification result with lightweight observability metadata."""

    primary: Intent
    distribution: dict[str, float]
    confidence: float
    ambiguous: bool
    matched_keywords: dict[str, list[str]] = field(default_factory=dict)


class IntentClassifier:
    """Classify query intent using keyword heuristics."""

    # Keyword sets for intent detection (greedy match: first matching intent wins)
    KEYWORDS = {
        Intent.DEBUGGING: {
            "why",
            "fail",
            "failing",
            "failure",
            "break",
            "broken",
            "debug",
            "debugg",
            "error",
            "bug",
            "wrong",
            "issue",
            "problem",
            "crash",
            "exception",
            "not working",
            "doesn't work",
        },
        Intent.REFACTORING: {
            "change",
            "rename",
            "move",
            "refactor",
            "replace",
            "refactoring",
            "remove",
            "delete",
            "update",
            "modify",
            "rewrit",
        },
        Intent.NEW_FEATURE: {
            "add",
            "implement",
            "build",
            "create",
            "new",
            "make",
            "how do i",
            "how do we",
            "how to",
            "support",
        },
        Intent.DESIGN_QUESTION: {
            "should",
            "best way",
            "best approach",
            "pattern",
            "design",
            "architect",
            "approach",
            "strategy",
            "recommend",
            "suggestion",
        },
        Intent.IMPACT_ANALYSIS: {
            "most likely to break",
            "most likely to be affected",
            "likely to break",
            "what would break",
            "what parts",
            "what breaks",
            "are most likely",
        },
        Intent.NAVIGATION: {
            "where",
            "locate",
            "find",
            "defined",
            "calls",
            "called by",
            "uses",
            "import",
            "reference",
            "location",
        },
        Intent.EXPLORATION: {
            "what does",
            "how does",
            "explain",
            "understand",
            "works",
            "purpose",
            "mean",
            "does this",
            "this function",
        },
    }

    ORDER = [
        Intent.DEBUGGING,
        Intent.IMPACT_ANALYSIS,
        Intent.REFACTORING,
        Intent.NEW_FEATURE,
        Intent.DESIGN_QUESTION,
        Intent.NAVIGATION,
        Intent.EXPLORATION,
    ]

    @staticmethod
    def _keyword_weight(keyword: str) -> float:
        """Prefer more specific phrase matches over short generic tokens."""
        words = keyword.split()
        if len(words) > 1:
            return 1.5 + 0.2 * min(3, len(words) - 1)
        if len(keyword) >= 8:
            return 1.2
        return 1.0

    @staticmethod
    def _keyword_in_query(keyword: str, query_lower: str) -> bool:
        """Match standalone keywords precisely while keeping phrase checks simple."""
        if " " in keyword:
            return keyword in query_lower
        return re.search(rf"\b{re.escape(keyword)}\b", query_lower) is not None

    @classmethod
    def classify_with_metadata(cls, query: str) -> IntentSignal:
        """Classify intent and expose a lightweight keyword-based distribution."""
        if not query:
            return IntentSignal(
                primary=Intent.EXPLORATION,
                distribution={Intent.EXPLORATION.value: 1.0},
                confidence=0.0,
                ambiguous=False,
            )

        query_lower = query.lower()
        raw_scores: dict[Intent, float] = {}
        matched_keywords: dict[str, list[str]] = {}

        for intent in cls.ORDER:
            matches = [
                keyword
                for keyword in cls.KEYWORDS[intent]
                if cls._keyword_in_query(keyword, query_lower)
            ]
            if matches:
                matched_keywords[intent.value] = sorted(matches)
            raw_scores[intent] = sum(cls._keyword_weight(keyword) for keyword in matches)

        max_score = max(raw_scores.values(), default=0.0)
        if max_score <= 0:
            return IntentSignal(
                primary=Intent.EXPLORATION,
                distribution={Intent.EXPLORATION.value: 1.0},
                confidence=0.0,
                ambiguous=False,
                matched_keywords=matched_keywords,
            )

        primary = next(
            intent for intent in cls.ORDER if raw_scores.get(intent, 0.0) > 0
        )
        total_score = sum(score for score in raw_scores.values() if score > 0)
        distribution = {
            intent.value: raw_scores[intent] / total_score
            for intent in cls.ORDER
            if raw_scores[intent] > 0
        }
        sorted_scores = sorted(
            (score for score in raw_scores.values() if score > 0),
            reverse=True,
        )
        second_score = sorted_scores[1] if len(sorted_scores) > 1 else 0.0
        confidence = max_score / total_score if total_score > 0 else 0.0
        ambiguous = second_score > 0 and (max_score - second_score) <= 0.35 * max_score

        return IntentSignal(
            primary=primary,
            distribution=distribution,
            confidence=confidence,
            ambiguous=ambiguous,
            matched_keywords=matched_keywords,
        )

    @staticmethod
    def classify_intent(query: str) -> Intent:
        """
        Detect query intent using keyword matching.

        Strategy: case-insensitive greedy match. If no match, default to EXPLORATION.
        """
        return IntentClassifier.classify_with_metadata(query).primary

    @staticmethod
    def get_tier_priority(intent: Intent) -> list[str]:
        """Return tier priority ordering for a given intent."""
        return IntentConfig.PRIORITY[intent]
