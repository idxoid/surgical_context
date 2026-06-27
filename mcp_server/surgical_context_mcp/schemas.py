"""Structured output schemas for the surgical_context MCP tools.

Every tool returns one of these Pydantic models as its ``structuredContent``
(FastMCP derives the tool's ``outputSchema`` from the annotation) while the
human-facing markdown render rides in the result's text content. The contract,
held by every model here:

  * ``tool`` / ``ok`` / ``workspace`` — a stable envelope for programmatic
    dispatch and error handling without parsing prose.
  * ``markdown`` — the optional human/LLM render (the same block the tools used
    to return as a bare string). Consumers may ignore it and read the fields.
  * **Stable IDs** — every symbol-shaped row carries the index ``uid`` (or a
    deterministic ``overlay::…`` id for uncommitted-only symbols), so results
    can be joined across calls.
  * **Machine-readable scores / provenance** — ranking scores are numbers, not
    embedded in text, and each row records where it came from (``expansion_step``
    for context, ``provenance`` for impact, ``resolved_via`` for explain).

Keep these models in sync with ``engine.py``'s dataclasses; the server maps
engine results onto them in one place per tool.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    """Shared envelope for every tool result."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    ok: bool = True
    markdown: str = ""
    workspace: str | None = None


# --------------------------------------------------------------------------
# Shared item models
# --------------------------------------------------------------------------


class IntentRole(BaseModel):
    """One role the embedding intent-classifier mapped the question to."""

    model_config = ConfigDict(extra="forbid")

    role: str
    score: float
    description: str | None = None


class ContextItem(BaseModel):
    """One symbol in a retrieval bundle (ask_code / investigate). ``depth`` is
    the distance from its seed; ``expansion_step`` records HOW it entered the
    context (the provenance — seed, fold, a graph-walk step, …)."""

    model_config = ConfigDict(extra="forbid")

    uid: str
    name: str
    file_path: str
    role: str | None = None
    kind: str | None = None
    depth: int = 0
    expansion_step: str | None = None
    relevance_score: float = 0.0
    utility_score: float = 0.0
    has_code: bool = False
    start_line: int | None = None
    end_line: int | None = None


class BlastItem(BaseModel):
    """One downstream dependent surfaced by investigate's blast pass."""

    model_config = ConfigDict(extra="forbid")

    seed: str
    name: str
    file_path: str | None = None
    depth: int | None = None
    kind: str | None = None


class ImpactItem(BaseModel):
    """One downstream-affected symbol. ``provenance`` is ``"committed"`` for
    rows from the index or ``"overlay"`` for degraded rows recovered from an
    uncommitted ``set_overlay`` buffer."""

    model_config = ConfigDict(extra="forbid")

    uid: str | None = None
    name: str
    file_path: str | None = None
    depth: int | None = None
    kind: str | None = None
    severity: str | None = None
    provenance: str = "committed"


class NeighbourItem(BaseModel):
    """One caller/callee on a CALLS walk."""

    model_config = ConfigDict(extra="forbid")

    uid: str
    name: str
    file_path: str
    depth: int


class SearchItem(BaseModel):
    """One vector-search hit (symbol or doc)."""

    model_config = ConfigDict(extra="forbid")

    uid: str | None = None
    name: str | None = None
    file_path: str | None = None
    score: float | None = None
    distance: float | None = None
    snippet: str | None = None


class DefinitionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uid: str
    name: str
    file_path: str
    kind: str | None = None
    start_line: int


class OutlineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str | None = None
    start_line: int


class DocAnchorItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str | None = None
    anchor_type: str | None = None
    confidence: float = 0.0
    files: list[str] = Field(default_factory=list)


class ConnectionGroup(BaseModel):
    """One relationship bucket in an explain card (e.g. "calls", "uses type")."""

    model_config = ConfigDict(extra="forbid")

    label: str
    names: list[str] = Field(default_factory=list)


class WorkspaceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base: str
    indexed: str
    files: int


class FileItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    symbols: int | None = None


class RoleItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    description: str


# --------------------------------------------------------------------------
# Per-tool output models
# --------------------------------------------------------------------------


class AskCodeOutput(_Base):
    question: str
    render: str = "full"
    token_budget: int = 0
    intent: list[IntentRole] = Field(default_factory=list)
    candidate_count: int = 0
    files: list[str] = Field(default_factory=list)
    symbols: list[ContextItem] = Field(default_factory=list)


class InvestigateOutput(_Base):
    question: str
    depth: str = "full"
    intent: list[IntentRole] = Field(default_factory=list)
    candidate_count: int = 0
    files: list[str] = Field(default_factory=list)
    symbols: list[ContextItem] = Field(default_factory=list)
    blast: list[BlastItem] = Field(default_factory=list)


class ImpactOutput(_Base):
    symbol: str
    found: bool = False
    symbol_uid: str | None = None
    file_path: str | None = None
    max_depth: int = 0
    degraded: bool = False
    overlay_count: int = 0
    affected_files: list[str] = Field(default_factory=list)
    affected_symbols: list[ImpactItem] = Field(default_factory=list)


class NeighboursOutput(_Base):
    symbol: str
    relation: str  # "callers" | "callees"
    found: bool = False
    symbol_uid: str | None = None
    max_hops: int = 1
    neighbours: list[NeighbourItem] = Field(default_factory=list)


class SearchCodeOutput(_Base):
    query: str
    kind: str = "symbol"
    hits: list[SearchItem] = Field(default_factory=list)


class FindDefinitionOutput(_Base):
    name: str
    definitions: list[DefinitionItem] = Field(default_factory=list)


class FileOutlineOutput(_Base):
    requested_path: str
    found: bool = False
    file_path: str | None = None
    symbols: list[OutlineItem] = Field(default_factory=list)


class ReadSymbolOutput(_Base):
    name: str
    found: bool = False
    uid: str | None = None
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    language: str | None = None
    code: str | None = None


class PathOutput(_Base):
    symbol_a: str
    symbol_b: str
    found: bool = False
    reason: str | None = None
    hops: int = 0
    node_names: list[str] = Field(default_factory=list)
    rel_types: list[str] = Field(default_factory=list)


class DocsForOutput(_Base):
    symbol: str
    found: bool = False
    symbol_uid: str | None = None
    anchors: list[DocAnchorItem] = Field(default_factory=list)


class ExplainOutput(_Base):
    concept: str
    found: bool = False
    resolved_via: str | None = None  # "exact" | "vector"
    seed_name: str | None = None
    seed_uid: str | None = None
    seed_file: str | None = None
    signature: str | None = None
    groups: list[ConnectionGroup] = Field(default_factory=list)
    docs: list[DocAnchorItem] = Field(default_factory=list)


class ListWorkspacesOutput(_Base):
    workspaces: list[WorkspaceItem] = Field(default_factory=list)


class ListFilesOutput(_Base):
    path_prefix: str | None = None
    files: list[FileItem] = Field(default_factory=list)


class ClassifyIntentOutput(_Base):
    question: str
    intent: list[IntentRole] = Field(default_factory=list)


class ListRolesOutput(_Base):
    roles: list[RoleItem] = Field(default_factory=list)


class SetOverlayOutput(_Base):
    file_path: str
    symbols: list[str] = Field(default_factory=list)


class ClearOverlayOutput(_Base):
    file_path: str
    cleared: bool = False


class BatchOpResult(BaseModel):
    """One sub-op's outcome inside a batch — its structured payload plus status."""

    model_config = ConfigDict(extra="forbid")

    index: int
    tool: str
    ok: bool = True
    error: str | None = None
    result: dict | None = None  # the sub-tool's structuredContent


class BatchOutput(_Base):
    op_count: int = 0
    collapsed_code_blocks: int = 0
    results: list[BatchOpResult] = Field(default_factory=list)
