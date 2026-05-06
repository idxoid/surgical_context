"""Intent classification — detect user query type to guide context assembly."""

import re
from dataclasses import dataclass, field
from enum import Enum


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


@dataclass(frozen=True)
class IntentResolution:
    """Desired user intent resolved against repository capabilities."""

    desired_intent: str
    effective_mode: str
    confidence: float
    ambiguous: bool
    degraded: bool
    required_capabilities: list[str]
    available_capabilities: dict[str, str]
    repository_readiness: str = ""
    repository_indexability: str = ""
    allowed_reasoning: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "desired_intent": self.desired_intent,
            "effective_mode": self.effective_mode,
            "confidence": self.confidence,
            "ambiguous": self.ambiguous,
            "degraded": self.degraded,
            "required_capabilities": self.required_capabilities,
            "available_capabilities": self.available_capabilities,
            "repository_readiness": self.repository_readiness,
            "repository_indexability": self.repository_indexability,
            "allowed_reasoning": self.allowed_reasoning,
            "risks": self.risks,
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

        primary = next(intent for intent in cls.ORDER if raw_scores.get(intent, 0.0) > 0)
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

    @classmethod
    def resolve_with_profile(
        cls,
        query: str,
        repository_profile: dict | None = None,
    ) -> IntentResolution:
        """Resolve desired query intent against index-time repository capabilities."""
        signal = cls.classify_with_metadata(query)
        return cls.resolve_signal_with_profile(signal, repository_profile)

    @classmethod
    def resolve_signal_with_profile(
        cls,
        signal: IntentSignal,
        repository_profile: dict | None = None,
    ) -> IntentResolution:
        profile = repository_profile if isinstance(repository_profile, dict) else {}
        capabilities = profile.get("capabilities") or {}
        contract = profile.get("reasoning_contract") or {}
        required = cls._required_capabilities(signal.primary)
        available = {
            capability: cls._capability_status(capability, capabilities) for capability in required
        }
        effective_mode, degraded, risks = cls._effective_mode(
            signal.primary,
            available,
            capabilities,
            profile,
        )
        profile_risks = list(contract.get("risky") or [])
        if profile_risks:
            risks.extend(profile_risks)

        return IntentResolution(
            desired_intent=signal.primary.value,
            effective_mode=effective_mode,
            confidence=signal.confidence,
            ambiguous=signal.ambiguous,
            degraded=degraded,
            required_capabilities=required,
            available_capabilities=available,
            repository_readiness=str(profile.get("retrieval_readiness") or ""),
            repository_indexability=str(profile.get("indexability") or ""),
            allowed_reasoning=list(contract.get("allowed") or []),
            risks=_dedupe_preserve_order(risks),
        )

    @staticmethod
    def _required_capabilities(intent: Intent) -> list[str]:
        return {
            Intent.NAVIGATION: ["code_navigation"],
            Intent.DEBUGGING: ["code_navigation", "static_call_reasoning"],
            Intent.REFACTORING: ["code_navigation", "static_call_reasoning", "impact_analysis"],
            Intent.EXPLORATION: ["code_navigation", "static_call_reasoning", "doc_code_bridge"],
            Intent.NEW_FEATURE: ["doc_code_bridge", "code_navigation"],
            Intent.DESIGN_QUESTION: ["doc_code_bridge"],
            Intent.IMPACT_ANALYSIS: [
                "impact_analysis",
                "static_call_reasoning",
                "runtime_registry_semantics",
            ],
        }[intent]

    @staticmethod
    def _capability_status(capability: str, capabilities: dict) -> str:
        return str(capabilities.get(capability) or "unknown")

    @classmethod
    def _effective_mode(
        cls,
        intent: Intent,
        available: dict[str, str],
        capabilities: dict,
        profile: dict,
    ) -> tuple[str, bool, list[str]]:
        if not profile:
            return (
                "unprofiled_intent_routing",
                True,
                ["repository profile is unavailable; intent is only a text routing hint"],
            )

        risks: list[str] = []
        readiness = str(profile.get("retrieval_readiness") or "")
        if readiness in {"unsupported_symbol_surface", "limited"}:
            risks.append("repository readiness is low; retrieval may need fallback context")

        if intent == Intent.IMPACT_ANALYSIS:
            impact = available.get("impact_analysis", "unknown")
            if impact == "shallow":
                return (
                    "reachability_impact_candidates",
                    True,
                    ["impact is reachability-based, not causal breakage proof"],
                )
            if impact == "shallow_partial":
                return (
                    "shallow_reachability_impact",
                    True,
                    ["impact may miss dynamic/framework/test-surface edges"],
                )
            return (
                "unsupported_impact_request",
                True,
                ["impact analysis is not supported by the current index profile"],
            )

        if intent == Intent.NAVIGATION:
            mode = (
                "exact_symbol_navigation"
                if cls._is_usable(available["code_navigation"])
                else "low_confidence_navigation"
            )
            return mode, mode != "exact_symbol_navigation", risks

        if intent == Intent.DEBUGGING:
            usable = cls._is_usable(available["code_navigation"]) and cls._is_usable(
                available["static_call_reasoning"]
            )
            return (
                "code_grounded_debugging" if usable else "limited_debugging_context",
                not usable,
                risks,
            )

        if intent == Intent.REFACTORING:
            impact = available.get("impact_analysis", "unknown")
            usable = impact in {"shallow", "shallow_partial"}
            return (
                "reverse_dependency_refactor_candidates" if usable else "limited_refactor_search",
                True,
                risks + ["refactor blast radius is candidate-based until validated"],
            )

        if intent == Intent.EXPLORATION:
            dynamic = (
                capabilities.get("decorator_semantics"),
                capabilities.get("runtime_registry_semantics"),
            )
            if any(value == "medium" for value in dynamic):
                return (
                    "mechanism_explanation_with_caveats",
                    True,
                    risks + ["framework/runtime semantics need mechanism validation"],
                )
            return (
                "code_grounded_explanation"
                if cls._is_usable(available["code_navigation"])
                else "docs_grounded_explanation",
                not cls._is_usable(available["code_navigation"]),
                risks,
            )

        if intent == Intent.NEW_FEATURE:
            return "design_context_planning", False, risks

        if intent == Intent.DESIGN_QUESTION:
            return "design_reasoning", False, risks

        return "unprofiled_intent_routing", True, risks

    @staticmethod
    def _is_usable(status: str) -> bool:
        return status in {"high", "medium", "shallow", "shallow_partial"}


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
