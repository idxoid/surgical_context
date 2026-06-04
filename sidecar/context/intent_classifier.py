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


# ----------------------------------------------------------------------------
# Intent profile dictionary. Single source of truth for every per-intent
# fact a context consumer needs — the legacy `IntentConfig.PRIORITY`,
# `IntentClassifier._SECONDARY_INTENT_ROLES`, `_DOC_FIRST_INTENTS`, and
# `unified_ranker._CHAIN_PURSUIT_INTENTS` are now derived from these
# constants. The four dimensions an intent shapes are:
#   - role profile     — which roles may participate
#   - edge priority    — which graph relations carry the most evidence
#   - traversal shape  — direction / depth / breadth / chain-pursuit /
#                        doc-first / tier priority
#   - pack mapping     — benchmark vocab → engine vocab
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class TraversalShape:
    """How far / wide / which way to walk for an intent."""

    direction: tuple[str, ...]   # ("forward",) | ("backward",) | both
    max_depth: int               # 1-2 shallow / 3-5 medium / 6+ transitive
    chase_chains: bool           # follow registration/marker chains beyond max_depth
    breadth: str                 # "focused" | "medium" | "wide"
    # ``doc_first`` is intentionally a separate signal from ``breadth``: a
    # REFACTORING is wide on code touchpoints but code-first in rendering;
    # NEW_FEATURE is medium on candidates but doc-first because reading the
    # existing patterns precedes writing the new one. Encoding it on the
    # shape lets the doc-first set derive from a single source while
    # preserving the semantics the prompt compiler already relies on.
    doc_first: bool
    # Per-intent content tier priority (highest → lowest). Drives both the
    # prompt compiler's tier-fill order and the policy's tier_scores. Was a
    # separate `IntentConfig.PRIORITY` mapping until Phase 3b consolidated
    # it onto the traversal shape so the dictionary owns every per-intent
    # fact a consumer might need.
    tier_priority: tuple[str, ...]


# Roles that *may* participate in answering this intent. Not all of them
# will exist in every repository — engine should treat the profile as a
# soft preference (boost candidates that fulfil it), never as a hard
# requirement. Current _SECONDARY_INTENT_ROLES is a sparse subset of
# these; expansion lands in a later phase.
INTENT_ROLE_PROFILE: dict[Intent, tuple[str, ...]] = {
    Intent.NAVIGATION: (
        "api_surface",
        "representation_surface",
        "registration_step",
        "core_runtime",
        "factory_surface",
    ),
    Intent.DEBUGGING: (
        "error_surface",
        "executor",
        "runtime_surface",
        "interceptor",
        "orchestrator",
        "core_runtime",
    ),
    Intent.REFACTORING: (
        "impact_runtime",
        "impact_public_api",
        "impact_test_surface",
        "integration_surface",
        "representation_surface",
        "api_surface",
    ),
    Intent.EXPLORATION: (
        "core_runtime",
        "api_surface",
        "docs_or_concept",
        "composition_surface",
        "runtime_surface",
        "abstract_contract",
        "orchestrator",
        "registration_step",
    ),
    Intent.NEW_FEATURE: (
        "docs_or_concept",
        "api_surface",
        "composition_surface",
        "factory_surface",
        "registration_step",
    ),
    Intent.DESIGN_QUESTION: (
        "docs_or_concept",
        "abstract_contract",
        "composition_surface",
    ),
    Intent.IMPACT_ANALYSIS: (
        "impact_runtime",
        "impact_public_api",
        "impact_test_surface",
        "integration_surface",
    ),
}


# Edge types in priority order for graph expansion. The same edge can carry
# different evidence weight for different intents — DEBUGGING needs HANDLES
# (handler→error path) ahead of HAS_API (which carries surface, not flow),
# while NAVIGATION wants HAS_API early (the surface IS the answer).
INTENT_EDGE_PRIORITY: dict[Intent, tuple[str, ...]] = {
    Intent.NAVIGATION: (
        "HAS_API",
        "INHERITED_API",
        "CALLS_DIRECT",
        "CALLS_SCOPED",
        "CALLS_IMPORTED",
        "USES_TYPE",
        "IMPORTS",
    ),
    Intent.DEBUGGING: (
        "HANDLES",
        "DECORATED_BY",
        "CALLS_DIRECT",
        "CALLS_SCOPED",
        "USES_TYPE",
    ),
    Intent.REFACTORING: (
        "CALLS_DIRECT",
        "CALLS_SCOPED",
        "CALLS_IMPORTED",
        "USES_TYPE",
        "INHERITED_API",
        "DECORATED_BY",
        "IMPORTS",
    ),
    Intent.EXPLORATION: (
        "CALLS_DIRECT",
        "CALLS_SCOPED",
        "HAS_API",
        "USES_TYPE",
        "DECORATED_BY",
        "COMPOSES",
    ),
    Intent.NEW_FEATURE: (
        "HAS_API",
        "DECORATED_BY",
        "COMPOSES",
        "USES_TYPE",
    ),
    Intent.DESIGN_QUESTION: (
        "DEPENDS_ON",
        "INHERITED_API",
        "COMPOSES",
    ),
    Intent.IMPACT_ANALYSIS: (
        "AFFECTS",
        "CALLS_DIRECT",
        "CALLS_SCOPED",
        "CALLS_IMPORTED",
        "USES_TYPE",
        "INHERITED_API",
        "DEPENDS_ON",
    ),
}


# Direction / depth / breadth per intent. ``max_depth`` >= 6 means
# "transitive — chase as far as budget allows"; the actual depth in the
# graph expander still has a hard ceiling on tokens to keep the LLM
# context from exploding.
INTENT_TRAVERSAL: dict[Intent, TraversalShape] = {
    Intent.NAVIGATION: TraversalShape(
        direction=("backward",),
        max_depth=2,
        chase_chains=False,
        breadth="focused",
        doc_first=False,
        tier_priority=("code", "cross_refs", "architecture", "specs", "concept", "idea"),
    ),
    Intent.DEBUGGING: TraversalShape(
        direction=("backward", "forward"),
        max_depth=3,
        chase_chains=False,  # stay on the failure path, don't survey
        breadth="medium",
        doc_first=False,
        tier_priority=("code", "cross_refs", "specs", "architecture", "concept", "idea"),
    ),
    Intent.REFACTORING: TraversalShape(
        direction=("backward",),
        max_depth=10,
        chase_chains=False,
        breadth="wide",
        doc_first=False,  # wide on code touchpoints, not docs
        tier_priority=("cross_refs", "code", "architecture", "specs", "concept", "idea"),
    ),
    Intent.EXPLORATION: TraversalShape(
        direction=("forward", "backward"),
        max_depth=4,
        chase_chains=True,  # follow registration / marker chains
        breadth="medium",
        doc_first=False,
        tier_priority=("code", "concept", "architecture", "cross_refs", "specs", "idea"),
    ),
    Intent.NEW_FEATURE: TraversalShape(
        direction=("forward",),
        max_depth=2,
        chase_chains=False,
        breadth="medium",
        doc_first=True,  # read existing patterns before writing the new one
        tier_priority=("idea", "concept", "architecture", "specs", "cross_refs", "code"),
    ),
    Intent.DESIGN_QUESTION: TraversalShape(
        direction=("forward",),
        max_depth=2,
        chase_chains=False,
        breadth="wide",
        doc_first=True,
        tier_priority=("concept", "idea", "architecture", "specs", "code", "cross_refs"),
    ),
    Intent.IMPACT_ANALYSIS: TraversalShape(
        direction=("backward",),
        max_depth=10,
        chase_chains=False,
        breadth="wide",
        doc_first=True,
        tier_priority=("cross_refs", "code", "specs", "architecture", "concept", "idea"),
    ),
}


# Pack-intent → engine-intent canonical mapping. Benchmark packs use a
# three-value vocabulary; engine has seven. The mapping is conceptual,
# not lexical (`trace_dependency` is shaped like EXPLORATION with the
# chain-chasing dimension turned on, not a different intent kind):
PACK_INTENT_TO_ENGINE: dict[str, Intent] = {
    "explain_behavior": Intent.EXPLORATION,
    "trace_dependency": Intent.EXPLORATION,  # + chase_chains=True (already default)
    "impact_analysis": Intent.IMPACT_ANALYSIS,
}


class IntentConfig:
    """Maps intent to content tier priority (highest → lowest).

    Tier priority is owned by `INTENT_TRAVERSAL[i].tier_priority`; the
    `PRIORITY` dict here is a derived view kept on the same name so the
    existing call sites in prompt_compiler / policy helpers don't need to
    move yet. The TIERS constant is the canonical tier list.
    """

    TIERS = ["code", "cross_refs", "specs", "architecture", "concept", "idea"]

    PRIORITY: dict[Intent, list[str]] = {
        intent: list(shape.tier_priority) for intent, shape in INTENT_TRAVERSAL.items()
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
class IntentPolicy:
    """Retrieval policy derived from a weighted intent distribution."""

    primary: Intent
    distribution: dict[str, float]
    active_intents: tuple[Intent, ...]
    secondary_intents: tuple[Intent, ...]
    tier_scores: dict[str, float]
    tier_order: tuple[str, ...]
    budget_share: dict[str, float]
    supplemental_roles: tuple[str, ...] = ()
    doc_first: bool = False

    def weight(self, intent: Intent) -> float:
        return float(self.distribution.get(intent.value, 0.0))


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

    # Multi-word phrases that act as strong overrides when present.
    # Each entry is (phrase, intent, weight). Weight is additive to the raw score.
    # Match order does not matter — all matches contribute.
    _PHRASE_OVERRIDES: list[tuple[str, "Intent", float]] = [
        # Debugging phrases that contain action verbs (add/remove) as instruments, not goals
        ("to understand why", Intent.DEBUGGING, 2.0),
        ("to figure out why", Intent.DEBUGGING, 2.0),
        ("to see why", Intent.DEBUGGING, 2.0),
        ("understand why", Intent.DEBUGGING, 1.8),
        ("figure out why", Intent.DEBUGGING, 1.8),
        ("to debug", Intent.DEBUGGING, 1.5),
        ("to fix", Intent.DEBUGGING, 1.5),
        ("why it fails", Intent.DEBUGGING, 2.0),
        ("why it's failing", Intent.DEBUGGING, 2.0),
        ("why the function", Intent.DEBUGGING, 1.5),
        ("why this", Intent.DEBUGGING, 1.2),
        # Exploration phrases that contain action verbs
        ("how does this", Intent.EXPLORATION, 1.5),
        ("how does the", Intent.EXPLORATION, 1.5),
        ("what does this", Intent.EXPLORATION, 1.5),
        ("explain how", Intent.EXPLORATION, 1.5),
        ("explain why", Intent.EXPLORATION, 1.5),
    ]

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
            "affected",
            "would be affected",
            "tests affected",
            "what tests",
            "test suites",
            "most likely to break",
            "most likely to be affected",
            "likely to break",
            "would break",
            # "which parts" / "what parts" alone are NOT impact — they equally fit an
            # explanatory "which parts ARE X" (e.g. "which parts rely on pydantic-core").
            # Genuine impact questions still match via the breakage/change phrases
            # above ("would break", "likely to break", "if i change"). Keep only the
            # breakage-bearing variants.
            "what would break",
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

    # Phase 1 wire-up: the policy now derives supplemental roles from
    # ``INTENT_ROLE_PROFILE`` — the single source of truth defined at module
    # scope alongside the rest of the intent dictionary. Two of the seven
    # intents (NAVIGATION, EXPLORATION) previously had no entry here at all
    # and so the engine treated their role plans as a blank slate; the
    # expanded profile fills those in. Semantics of the field are unchanged
    # in this commit (still added to required_roles downstream) — the soft
    # preference vs hard requirement split lands in a later phase.
    _SECONDARY_INTENT_ROLES: dict[Intent, tuple[str, ...]] = INTENT_ROLE_PROFILE
    # Derived from `INTENT_TRAVERSAL[i].doc_first` so the dictionary is the
    # single source of truth; flipping `doc_first` for an intent there
    # propagates here automatically.
    _DOC_FIRST_INTENTS = frozenset(
        intent for intent, shape in INTENT_TRAVERSAL.items() if shape.doc_first
    )
    _SECONDARY_WEIGHT_THRESHOLD = 0.25

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

        # Apply phrase overrides: multi-word patterns boost specific intents
        # independently of individual token scores.
        for phrase, intent, weight in cls._PHRASE_OVERRIDES:
            if phrase in query_lower:
                raw_scores[intent] = raw_scores.get(intent, 0.0) + weight
                phrase_list = matched_keywords.setdefault(intent.value, [])
                if phrase not in phrase_list:
                    phrase_list.append(phrase)

        max_score = max(raw_scores.values(), default=0.0)
        if max_score <= 0:
            return IntentSignal(
                primary=Intent.EXPLORATION,
                distribution={Intent.EXPLORATION.value: 1.0},
                confidence=0.0,
                ambiguous=False,
                matched_keywords=matched_keywords,
            )

        primary = next(intent for intent in cls.ORDER if raw_scores.get(intent, 0.0) == max_score)
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
    def policy_from_signal(
        cls,
        signal: IntentSignal,
        *,
        secondary_threshold: float = _SECONDARY_WEIGHT_THRESHOLD,
    ) -> IntentPolicy:
        """Turn intent metadata into a budget/tier policy for retrieval.

        The primary intent remains the anchor, but strong secondary intents can
        add role coverage and alter tier ordering without forcing a second full
        retrieval pass.
        """
        distribution = cls._normalized_distribution(signal.primary, signal.distribution)
        ranked = sorted(
            ((intent, distribution.get(intent.value, 0.0)) for intent in Intent),
            key=lambda item: item[1],
            reverse=True,
        )
        active: list[Intent] = [signal.primary]
        for intent, weight in ranked:
            if intent == signal.primary or weight <= 0:
                continue
            if weight >= secondary_threshold or (signal.ambiguous and len(active) < 2):
                active.append(intent)

        active = list(dict.fromkeys(active))
        budget_total = sum(distribution.get(intent.value, 0.0) for intent in active) or 1.0
        budget_share = {
            intent.value: distribution.get(intent.value, 0.0) / budget_total for intent in active
        }
        tier_scores = cls._blended_tier_scores(distribution)
        tier_order = tuple(
            sorted(
                IntentConfig.TIERS,
                key=lambda tier: (tier_scores.get(tier, 0.0), -IntentConfig.TIERS.index(tier)),
                reverse=True,
            )
        )

        supplemental_roles: list[str] = []
        for intent in active:
            if intent == signal.primary:
                continue
            supplemental_roles.extend(cls._SECONDARY_INTENT_ROLES.get(intent, ()))

        doc_first = any(
            intent in cls._DOC_FIRST_INTENTS and budget_share.get(intent.value, 0.0) >= 0.2
            for intent in active
        )

        return IntentPolicy(
            primary=signal.primary,
            distribution={k: round(v, 6) for k, v in distribution.items() if v > 0},
            active_intents=tuple(active),
            secondary_intents=tuple(intent for intent in active if intent != signal.primary),
            tier_scores={tier: round(score, 6) for tier, score in tier_scores.items()},
            tier_order=tier_order,
            budget_share={k: round(v, 6) for k, v in budget_share.items()},
            supplemental_roles=tuple(dict.fromkeys(supplemental_roles)),
            doc_first=doc_first,
        )

    @classmethod
    def policy_for_primary(cls, intent: Intent) -> IntentPolicy:
        """Back-compat policy for callers that only know a single intent."""
        return cls.policy_from_signal(
            IntentSignal(
                primary=intent,
                distribution={intent.value: 1.0},
                confidence=1.0,
                ambiguous=False,
            )
        )

    @classmethod
    def _normalized_distribution(
        cls, primary: Intent, distribution: dict[str, float] | None
    ) -> dict[str, float]:
        raw: dict[str, float] = {}
        for name, weight in (distribution or {}).items():
            try:
                intent = Intent(name)
            except ValueError:
                continue
            try:
                numeric_weight = float(weight)
            except (TypeError, ValueError):
                continue
            if numeric_weight > 0:
                raw[intent.value] = numeric_weight
        if not raw:
            raw = {primary.value: 1.0}
        elif primary.value not in raw:
            raw[primary.value] = max(raw.values())

        total = sum(raw.values()) or 1.0
        return {name: weight / total for name, weight in raw.items()}

    @staticmethod
    def _blended_tier_scores(distribution: dict[str, float]) -> dict[str, float]:
        scores = {tier: 0.0 for tier in IntentConfig.TIERS}
        max_rank = len(IntentConfig.TIERS)
        for name, weight in distribution.items():
            try:
                intent = Intent(name)
            except ValueError:
                continue
            priority = IntentConfig.PRIORITY[intent]
            for idx, tier in enumerate(priority):
                scores[tier] += weight * (max_rank - idx)
        total = sum(scores.values()) or 1.0
        return {tier: score / total for tier, score in scores.items()}

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
