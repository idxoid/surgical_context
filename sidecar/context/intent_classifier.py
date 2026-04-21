"""Intent classification — detect user query type to guide context assembly."""

from enum import Enum


class Intent(Enum):
    """Query intent types that determine content tier priority."""

    NAVIGATION = "navigation"  # "Where is X? What calls X?"
    DEBUGGING = "debugging"  # "Why does X fail? Why is this broken?"
    REFACTORING = "refactor"  # "Change X to use Y. Rename everywhere."
    EXPLORATION = "exploration"  # "What does this do? How does it work?"
    NEW_FEATURE = "new_feature"  # "Add X that does Y. Implement Z."
    DESIGN_QUESTION = "design_question"  # "How should we do this? What pattern?"


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
    }


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

    @staticmethod
    def classify_intent(query: str) -> Intent:
        """
        Detect query intent using keyword matching.

        Strategy: case-insensitive greedy match. If no match, default to EXPLORATION.
        """
        if not query:
            return Intent.EXPLORATION

        query_lower = query.lower()

        # Greedy matching: debug, refactor, new feature, design, navigation, exploration
        # Order matters: debug and refactor are more specific than exploration
        order = [
            Intent.DEBUGGING,
            Intent.REFACTORING,
            Intent.NEW_FEATURE,
            Intent.DESIGN_QUESTION,
            Intent.NAVIGATION,
            Intent.EXPLORATION,
        ]

        for intent in order:
            keywords = IntentClassifier.KEYWORDS[intent]
            if any(kw in query_lower for kw in keywords):
                return intent

        # Default to exploration if no keywords match
        return Intent.EXPLORATION

    @staticmethod
    def get_tier_priority(intent: Intent) -> list[str]:
        """Return tier priority ordering for a given intent."""
        return IntentConfig.PRIORITY[intent]
