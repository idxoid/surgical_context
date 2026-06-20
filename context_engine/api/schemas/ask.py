"""Ask, axis, impact, and feedback request/response models."""

from typing import Any

from pydantic import BaseModel, Field

from context_engine.api.schemas.common import TOKEN_BUDGET_MAX, TOKEN_BUDGET_MIN


class AskRequest(BaseModel):
    symbol: str | None = None
    question: str = "What does this code do?"
    token_budget: int = Field(default=6000, ge=TOKEN_BUDGET_MIN, le=TOKEN_BUDGET_MAX)
    file_path: str | None = None


class AskResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    symbol: str
    answer: str
    context: dict[str, Any]
    user: str
    cloud: bool
    workspace_id: str
    trace_id: str
    feedback_token: str
    model_route: dict[str, Any]
    metrics: dict[str, Any]
    index_manifest_id: str | None = None
    index_manifest_schema_version: int | None = None


class AskAxisRequest(BaseModel):
    """``/ask/axis`` payload — axis-pipeline-only retrieval shape.

    No symbol anchor: the axis pipeline picks candidates by role intent.
    ``with_context`` toggles whether expanded code bundles come back; without
    it the response only carries intent matches + ranked candidates (cheap).
    ``intent_budget`` is on by default so context rendering uses the same Token
    Credit path as production ``/ask``.
    """

    question: str
    top_roles: int = Field(default=3, ge=1, le=10)
    intent_threshold: float = Field(default=0.20, ge=0.0, le=1.0)
    per_role_limit: int = Field(default=7, ge=1, le=50)
    with_context: bool = True
    context_seeds_per_role: int | None = Field(default=None, ge=1, le=10)
    context_per_seed: int = Field(default=4, ge=1, le=20)
    intent_budget: bool = True
    token_budget: int = Field(default=6000, ge=TOKEN_BUDGET_MIN, le=TOKEN_BUDGET_MAX)


class AxisIntentMatchResponse(BaseModel):
    role: str
    similarity: float
    description: str


class AxisCandidateResponse(BaseModel):
    uid: str
    name: str
    file_path: str
    role: str
    satisfying_contracts: list[str]
    satisfying_kinds: list[str] = Field(default_factory=list)
    contract_count: int
    kind_count: int = 0
    vector_distance: float | None
    score: float


class AxisContextSymbolResponse(BaseModel):
    uid: str
    name: str
    file_path: str
    role: str
    distance_from_seed: int
    expansion_step: str | None
    code: str | None


class AxisContextBundleResponse(BaseModel):
    role: str
    seed: AxisContextSymbolResponse
    related: list[AxisContextSymbolResponse]


class AskAxisResponse(BaseModel):
    """Axis-pipeline response. The endpoint does not call an LLM —
    callers can plug ``context_bundles`` into their own prompt.
    """

    question: str
    workspace_id: str
    user: str
    intent_matches: list[AxisIntentMatchResponse]
    candidates_by_role: dict[str, list[AxisCandidateResponse]]
    context_bundles: list[AxisContextBundleResponse]


class FeedbackRequest(BaseModel):
    feedback_token: str
    kind: str
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = ""


class FeedbackResponse(BaseModel):
    status: str
    feedback_token: str
    kind: str
    outcome: str
    workspace_id: str
    trace_id: str


class ImpactResponse(BaseModel):
    symbol: str
    symbol_uid: str
    file_path: str
    affected_symbols: list[dict[str, Any]]
    affected_files: list[str]
    affected_count: int
    affected_file_count: int
    max_depth: int
