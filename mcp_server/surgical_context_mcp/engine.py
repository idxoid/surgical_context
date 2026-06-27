"""In-process bridge to the axis retrieval pipeline.

This runs the same read path as the ``/ask/axis`` HTTP route and the
``QA/axis_benchmark`` harness: ``run_axis_retrieval`` over a single, long-lived
Neo4j + LanceDB handle. The retrieval is LLM-free — it returns ranked,
graph-expanded code bundles (already budget-trimmed by the Token Credit path
when ``intent_budget`` is on); the host chat model reasons over them.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

# Load the repo .env (NEO4J_*, model creds) before any heavy import — exactly
# what context_engine/main.py does for the server.
from context_engine.env_loader import load_repo_dotenv

load_repo_dotenv()

# Single-user MCP session: every overlay buffer is stored and read under one
# stable user id, so ``set_overlay`` writes and ``ask``/``impact`` reads hit the
# same key. The overlay collapses the workspace id to its base internally, so
# passing the profile-suffixed index id everywhere is consistent.
OVERLAY_USER_ID = "mcp"


@dataclass
class AskResult:
    question: str
    workspace_id: str
    intent: list[tuple[str, float]] = field(default_factory=list)
    candidate_count: int = 0
    files: list[str] = field(default_factory=list)
    text: str = ""
    symbols: list[dict] = field(default_factory=list)


@dataclass
class ImpactResult:
    symbol: str
    workspace_id: str
    found: bool = False
    symbol_uid: str = ""
    file_path: str = ""
    affected_symbols: list[dict] = field(default_factory=list)
    affected_files: list[str] = field(default_factory=list)
    max_depth: int = 0
    degraded: bool = False
    overlay_count: int = 0


@dataclass
class WorkspaceInfo:
    base: str  # client-facing id (profile suffix stripped) — pass to `workspace=`
    indexed: str  # physical index namespace (with profile suffix)
    files: int


@dataclass
class FileEntry:
    path: str
    symbols: int = 0


@dataclass
class InvestigateResult:
    question: str
    workspace_id: str
    depth: str
    intent: list[tuple[str, float]] = field(default_factory=list)
    candidate_count: int = 0
    files: list[str] = field(default_factory=list)
    context_text: str = ""
    blast: list[dict] = field(default_factory=list)
    symbols: list[dict] = field(default_factory=list)


@dataclass
class SymbolSource:
    """Exact on-disk source of one symbol — the deterministic read ``ask_code``
    can't guarantee (it budget-trims). ``found=False`` when the name/path miss."""

    name: str
    workspace_id: str
    found: bool = False
    uid: str = ""
    file_path: str = ""
    start_line: int = 0
    end_line: int = 0
    code: str = ""


@dataclass
class NeighbourRow:
    uid: str
    name: str
    file_path: str
    depth: int


@dataclass
class NeighbourResult:
    """Result of a one-direction call walk (callers / callees)."""

    symbol: str
    workspace_id: str
    found: bool = False
    symbol_uid: str = ""
    direction: str = ""
    rows: list[NeighbourRow] = field(default_factory=list)


@dataclass
class DefinitionHit:
    uid: str
    name: str
    file_path: str
    kind: str
    start_line: int


@dataclass
class OutlineRow:
    name: str
    kind: str
    start_line: int


@dataclass
class FileOutline:
    requested_path: str
    workspace_id: str
    found: bool = False
    file_path: str = ""
    rows: list[OutlineRow] = field(default_factory=list)


@dataclass
class PathResult:
    symbol_a: str
    symbol_b: str
    workspace_id: str
    found: bool = False
    reason: str = ""
    node_names: list[str] = field(default_factory=list)
    rel_types: list[str] = field(default_factory=list)


@dataclass
class DocCoverRow:
    chunk_id: str
    anchor_type: str
    confidence: float
    files: list[str]


@dataclass
class DocsForResult:
    symbol: str
    workspace_id: str
    found: bool = False
    symbol_uid: str = ""
    rows: list[DocCoverRow] = field(default_factory=list)


@dataclass
class ExplainConnection:
    name: str
    file_path: str


@dataclass
class ExplainGroup:
    label: str
    rows: list[ExplainConnection] = field(default_factory=list)


@dataclass
class ExplainResult:
    concept: str
    workspace_id: str
    found: bool = False
    resolved_via: str = ""  # "exact" | "vector"
    seed_name: str = ""
    seed_uid: str = ""
    seed_file: str = ""
    signature: str = ""
    groups: list[ExplainGroup] = field(default_factory=list)
    docs: list[DocCoverRow] = field(default_factory=list)


# Edge type -> (outgoing label, incoming label) for explain's connection map.
# AFFECTS / CONTAINS are deliberately omitted: too broad (impact covers AFFECTS).
_LABEL_CALLS = "calls"
_LABEL_CALLED_BY = "called by"
_CALLS_EDGE_LABELS = (_LABEL_CALLS, _LABEL_CALLED_BY)

_EXPLAIN_EDGE_LABELS: dict[str, tuple[str, str]] = {
    "CALLS": _CALLS_EDGE_LABELS,
    "CALLS_DIRECT": _CALLS_EDGE_LABELS,
    "CALLS_SCOPED": _CALLS_EDGE_LABELS,
    "CALLS_IMPORTED": _CALLS_EDGE_LABELS,
    "CALLS_DYNAMIC": _CALLS_EDGE_LABELS,
    "CALLS_INFERRED": _CALLS_EDGE_LABELS,
    "CALLS_GUESS": _CALLS_EDGE_LABELS,
    "USES_TYPE": ("uses type", "used as type by"),
    "INSTANTIATES": ("instantiates", "instantiated by"),
    "DECORATED_BY": ("decorated by", "decorates"),
    "INJECTS": ("injects", "injected by"),
    "HANDLES": ("handles", "handled by"),
    "HAS_API": ("exposes API", "API exposed by"),
    "INHERITED_API": ("inherited API", "inherited API by"),
    "DEPENDS_ON": ("inherits from / depends on", "subclassed by / depended on by"),
    "REFERENCES": ("references", "referenced by"),
    "READS_ATTR": ("reads attr of", "attr read by"),
    "WRITES_ATTR": ("writes attr of", "attr written by"),
    "RESOLVES_ATTR": ("resolves attr of", "attr resolved by"),
    "EVENT_PUB": ("publishes event to", "event published by"),
    "EVENT_SUB": ("subscribes to event from", "event subscribed by"),
    "METADATA_BRIDGE": ("metadata bridge to", "metadata bridge from"),
    "HOOK_CONFIG": ("hook config for", "hook configured by"),
    "HOOK_EXEC": ("hook exec for", "hook executed by"),
    "CALLS_ENDPOINT": ("calls endpoint", "endpoint called by"),
    "IMPLEMENTS_ENDPOINT": ("implements endpoint", "endpoint implemented by"),
}


def _read_seed_signature(resolved: str | Path, start_line: int, end_line: int) -> str:
    from context_engine.axis.context_builder import _code_signature  # noqa: PLC0415

    if start_line < 1 or end_line < start_line:
        return ""
    try:
        lines = Path(resolved).read_text(encoding="utf-8").splitlines()
        return _code_signature("\n".join(lines[start_line - 1 : end_line]))
    except OSError:
        return ""


def _group_explain_connections(
    edge_rows: list[dict],
    *,
    max_per_group: int,
) -> list[ExplainGroup]:
    buckets: dict[str, list[ExplainConnection]] = {}
    seen: dict[str, set[tuple[str, str]]] = {}
    for row in edge_rows:
        rel = str(row.get("rel") or "")
        labels = _EXPLAIN_EDGE_LABELS.get(rel)
        if not labels:
            continue
        label = labels[0] if row.get("outgoing") else labels[1]
        name = str(row.get("name") or "")
        fp = str(row.get("file_path") or "")
        if not name:
            continue
        key = (name, fp)
        seen.setdefault(label, set())
        if key in seen[label]:
            continue
        seen[label].add(key)
        buckets.setdefault(label, []).append(ExplainConnection(name=name, file_path=fp))
    return [
        ExplainGroup(label=label, rows=rows[:max_per_group])
        for label, rows in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]


def _common_dir_prefix(paths: list[str]) -> str:
    """Longest shared directory prefix (with trailing /) across ``paths``.

    Stripping it from every header drops the repeated absolute-root noise (one
    workspace = one repo per call) — printed once instead of per symbol.
    """
    real = [p for p in paths if p]
    if len(real) < 2:
        return ""
    try:
        pref = os.path.commonpath(real)
    except ValueError:
        return ""
    if not pref or pref == "/":
        return ""
    return pref.rstrip("/") + "/"


def _collect_deduped_bundle_symbols(result) -> tuple[list, list[str]]:
    seen_uid: set[str] = set()
    seen_file: set[str] = set()
    files: list[str] = []
    syms = []
    for bundle in result.bundles:
        for sym in bundle.all_symbols():
            if sym.uid in seen_uid:
                continue
            seen_uid.add(sym.uid)
            syms.append(sym)
            fp = sym.file_path or ""
            if fp and fp not in seen_file:
                seen_file.add(fp)
                files.append(fp)
    return syms, files


def _bundle_symbol_meta(sym) -> str:
    meta = f"{sym.role} · d{sym.distance_from_seed}"
    step = sym.expansion_step or ""
    if step and step not in (sym.role, "seed"):
        meta += f" · {step}"
    return meta


def _append_bundle_symbol_lines(
    parts: list[str],
    sym,
    *,
    prefix: str,
    names_only: bool,
) -> None:
    fp = sym.file_path or ""
    rel = fp[len(prefix) :] if prefix and fp.startswith(prefix) else fp
    meta = _bundle_symbol_meta(sym)
    if names_only:
        parts.append(f"- {rel} :: {sym.name} ({meta})")
        return
    parts.append(f"### {rel} :: {sym.name} · {meta}")
    if sym.code:
        parts.append("```python")
        parts.append(sym.code.rstrip())
        parts.append("```")
    parts.append("")


def _context_symbol_row(sym) -> dict:
    """Lean, machine-readable row for one context symbol — stable ``uid`` ID,
    role/kind, depth + ``expansion_step`` provenance, and relevance/utility
    scores. Drops the code body (it already rides in the markdown render) in
    favour of a ``has_code`` flag so ``structuredContent`` stays compact."""
    return {
        "uid": sym.uid,
        "name": sym.name,
        "file_path": sym.file_path or "",
        "role": sym.role or "",
        "kind": sym.kind or "",
        "depth": sym.distance_from_seed,
        "expansion_step": sym.expansion_step,
        "relevance_score": round(float(sym.relevance_score), 4),
        "utility_score": round(float(sym.utility_score), 4),
        "has_code": bool(sym.code),
        "start_line": sym.start_line or None,
        "end_line": sym.end_line or None,
    }


def _render_bundles(result, *, names_only: bool = False) -> tuple[list[dict], list[str], str]:
    """Flatten ``result.bundles`` into a deduped, prompt-ready markdown block
    plus the matching machine-readable symbol rows.

    Dedupes by uid (highest-rank / shallowest occurrence wins), preserving the
    candidate-rank, seed-before-related order — the same content the benchmark
    counts as the prompt the LLM actually receives. Headers are compact: the
    shared path prefix is factored out once, ``depth`` → ``d``, and the
    expansion ``step`` is dropped when it just echoes the role or the seed.

    ``names_only`` emits one line per symbol (file :: name + role/depth, NO
    code) — a census view: many coupling symbols fit per token. Pair it with
    ``intent_budget=False`` upstream so the token-credit packer doesn't evict
    expanded neighbours before they reach here.

    Returns ``(rows, files, markdown)`` — ``rows`` is the structured payload
    (see ``_context_symbol_row``), in the same deduped order as the markdown.
    """
    if not result.bundles:
        return [], [], ""

    syms, files = _collect_deduped_bundle_symbols(result)
    rows = [_context_symbol_row(s) for s in syms]
    prefix = _common_dir_prefix(files)
    parts: list[str] = []
    if prefix:
        parts.append(f"_paths relative to {prefix}_")
        parts.append("")

    for sym in syms:
        _append_bundle_symbol_lines(parts, sym, prefix=prefix, names_only=names_only)

    return rows, files, "\n".join(parts).rstrip() + "\n"


class AxisEngine:
    """Long-lived holder for the Neo4j + LanceDB handles and the read path.

    Handles are opened lazily on the first query so the MCP server starts (and
    Claude Code connects) fast; the SentenceTransformer cold-start is paid once,
    on the first ``ask``. A lock serialises concurrent tool calls since the DB
    handles are shared and not assumed thread-safe.
    """

    def __init__(self) -> None:
        # Typed Any: opened lazily by _ensure_db/_ensure; mypy can't narrow the
        # None→client transition across the helper call, and the read path is
        # duck-typed over the Neo4j/Lance handles anyway.
        self._db: Any = None
        self._lance: Any = None
        self._overlay = None
        self._lock = threading.Lock()

    def _ensure_overlay(self):
        """Lazily create the in-memory editor overlay (cheap — no model, no DB).
        Holds the host LLM's uncommitted edits so ``ask``/``impact`` can reflect
        them, mirroring the HTTP server's live-buffer augmentation."""
        if self._overlay is None:
            from context_engine.overlay import InMemoryOverlay

            self._overlay = InMemoryOverlay()
        return self._overlay

    def _ensure_db(self) -> None:
        """Open just the Neo4j handle — cheap (no embedding model)."""
        if self._db is not None:
            return
        from context_engine.database.neo4j_client import Neo4jClient
        from context_engine.indexer.fast.pipeline import (
            NEO4J_PASSWORD,
            NEO4J_URI,
            NEO4J_USER,
        )

        self._db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)

    def _ensure_lance(self) -> None:
        """Open just the LanceDB handle (pulls SentenceTransformer; one-time
        cold-start). Needed for any embedding — intent classification and the
        vector seeds. No Neo4j, so intent preview stays off the graph path."""
        if self._lance is not None:
            return
        from context_engine.database.lancedb_client import LanceDBClient
        from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE

        self._lance = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)

    def _ensure(self) -> None:
        """Open Neo4j + LanceDB. The Lance handle pulls SentenceTransformer
        (one-time cold-start), needed only for the embedding-driven ask path —
        impact / list_workspaces stay on the cheap ``_ensure_db`` path.

        Verified clean on stdout — the model load logs to stderr only, so the
        MCP stdio JSON-RPC channel stays uncorrupted without redirection.
        """
        self._ensure_db()
        self._ensure_lance()

    def classify_intent(
        self, question: str, *, top_roles: int = 5, threshold: float = 0.20
    ) -> list[tuple[str, float, str]]:
        """Preview the embedding intent-classifier: roles whose description
        embeds closest to ``question`` (cosine ≥ threshold). Embedding only —
        no Neo4j, no retrieval. Returns (role, similarity, description)."""
        from context_engine.axis.intent_classifier import classify_intent

        with self._lock:
            self._ensure_lance()

            def embed(text: str):
                return self._lance._embed([text])[0]  # noqa: SLF001

            matches = classify_intent(question, embed, top_k=top_roles, threshold=threshold)
        return [(m.role, m.similarity, m.description) for m in matches]

    def available_roles(self) -> dict[str, str]:
        """Canonical role → description map (the intent vocabulary). No DB —
        used for ``list_roles`` and to validate caller-supplied ``roles``."""
        from context_engine.axis.intent_classifier import ROLE_INTENT_DESCRIPTIONS

        return dict(ROLE_INTENT_DESCRIPTIONS)

    def ask(
        self,
        question: str,
        workspace_id: str,
        *,
        token_budget: int = 6000,
        top_roles: int = 3,
        per_role_limit: int = 7,
        with_context: bool = True,
        roles: list[str] | None = None,
        render: str = "full",
    ) -> AskResult:
        from context_engine.axis.pipeline import AxisRetrievalConfig, run_axis_retrieval

        # render="names": census view — strip bodies AND disable the token-credit
        # eviction so the full graph expansion survives (signature_only can't do
        # this: it only acts inside intent_budget, where it ≈ full and still evicts).
        names_only = render == "names"

        # Variant A: when the caller supplies roles, skip the embedding
        # intent-classifier and drive retrieval with those roles directly. The
        # vector seeds still rerank by embedding (only role selection changes).
        intent_override = None
        if roles:
            from context_engine.axis.intent_classifier import (
                ROLE_INTENT_DESCRIPTIONS,
                IntentMatch,
            )

            intent_override = [
                IntentMatch(
                    role=r,
                    similarity=1.0,
                    description=ROLE_INTENT_DESCRIPTIONS.get(r, ""),
                )
                for r in roles
            ]

        with self._lock:
            self._ensure()
            result = run_axis_retrieval(
                question,
                workspace_id=workspace_id,
                db=self._db,
                lance=self._lance,
                config=AxisRetrievalConfig(
                    top_roles=top_roles,
                    per_role_limit=per_role_limit,
                    with_context=with_context,
                    intent_budget=not names_only,
                    base_token_budget=token_budget,
                    hook_transparency=True,
                    intent_override=intent_override,
                    # Uncommitted edits pushed via ``set_overlay`` win over indexed
                    # code (dirty buffers patched in, brand-new symbols anchored).
                    overlay=self._overlay,
                    user_id=OVERLAY_USER_ID,
                ),
            )

        intent = [(m.role, m.similarity) for m in result.intent]
        rows, files, text = _render_bundles(result, names_only=names_only)
        return AskResult(
            question=question,
            workspace_id=workspace_id,
            intent=intent,
            candidate_count=len(result.candidates_for_context),
            files=files,
            text=text,
            symbols=rows,
        )

    def investigate(
        self,
        question: str,
        workspace_id: str,
        *,
        depth: str = "full",
        token_budget: int = 4000,
        max_blast: int = 30,
    ) -> InvestigateResult:
        """One-call planned pipeline run server-side (one host round-trip): intent
        → ranked code context (``run_axis_retrieval``) → downstream blast surface
        of the top seeds (``build_impact_surface``), de-duplicated against the
        context. ``depth="full"`` = code bundles + impact on top 5 seeds (self-
        contained first shot); ``depth="lean"`` = names-only context + impact on
        top 3 (cheaper). Replaces dripping many granular calls — the internal
        steps are free of host-context replay."""
        from context_engine.axis.impact_surface import build_impact_surface
        from context_engine.axis.pipeline import AxisRetrievalConfig, run_axis_retrieval

        lean = depth == "lean"
        k = 3 if lean else 5
        with self._lock:
            self._ensure()
            result = run_axis_retrieval(
                question,
                workspace_id=workspace_id,
                db=self._db,
                lance=self._lance,
                config=AxisRetrievalConfig(
                    with_context=True,
                    intent_budget=not lean,
                    base_token_budget=token_budget,
                    hook_transparency=True,
                ),
            )
            rows, files, context_text = _render_bundles(result, names_only=lean)
            ctx_uids = {s.uid for b in result.bundles for s in b.all_symbols()}
            blast: list[dict] = []
            seen: set[str] = set()
            for c in result.candidates_for_context[:k]:
                try:
                    surface = build_impact_surface(
                        db=self._db,
                        symbol_uid=c.uid,
                        symbol_name=c.name,
                        file_path=c.file_path or "",
                        workspace_id=workspace_id,
                        max_depth=2,
                    )
                except Exception:
                    continue
                for row in surface.get("affected_symbols", []):
                    uid = row.get("uid")
                    if not uid or uid in ctx_uids or uid in seen:
                        continue
                    seen.add(uid)
                    blast.append(
                        {
                            "seed": c.name,
                            "name": row.get("name"),
                            "file_path": row.get("file_path"),
                            "depth": row.get("depth"),
                            "kind": row.get("kind"),
                        }
                    )
                    if len(blast) >= max_blast:
                        break
                if len(blast) >= max_blast:
                    break

        return InvestigateResult(
            question=question,
            workspace_id=workspace_id,
            depth=depth,
            intent=[(m.role, m.similarity) for m in result.intent],
            candidate_count=len(result.candidates_for_context),
            files=files,
            context_text=context_text,
            blast=blast,
            symbols=rows,
        )

    def impact(
        self,
        symbol: str,
        workspace_id: str,
        *,
        file_path: str | None = None,
        max_depth: int = 3,
    ) -> ImpactResult:
        """Downstream dependents of ``symbol``: the committed Neo4j surface,
        augmented with degraded ``overlay_caller`` rows parsed from any
        uncommitted buffers pushed via ``set_overlay``.

        Mirrors the ``/impact`` route (``resolve_impact_symbol_uid`` →
        ``build_impact_surface`` + ``build_overlay_impact_callers``): callers a
        host LLM just typed but hasn't committed surface as ``degraded=True``
        rows, and a brand-new symbol that exists only in the overlay still
        resolves. Pure Neo4j + in-process parse — no embeddings.
        """
        from context_engine.axis.impact_surface import build_impact_surface
        from context_engine.axis.overlay_impact import build_overlay_impact_callers

        requested_path = (
            file_path.strip() if isinstance(file_path, str) and file_path.strip() else None
        )
        with self._lock:
            self._ensure_db()
            uid = self._db.resolve_impact_symbol_uid(symbol, workspace_id, file_path=requested_path)

            committed_rows: list[dict] = []
            committed_files: list[str] = []
            symbol_file = ""
            walk_depth = max_depth
            if uid:
                symbol_file = self._db.get_file_path_for_symbol(uid, workspace_id=workspace_id)
                surface = build_impact_surface(
                    db=self._db,
                    symbol_uid=uid,
                    symbol_name=symbol,
                    file_path=symbol_file,
                    workspace_id=workspace_id,
                    max_depth=max_depth,
                )
                committed_rows = surface["affected_symbols"]
                committed_files = surface["affected_files"]
                walk_depth = surface["max_depth"]

            overlay_rows = build_overlay_impact_callers(
                self._overlay,
                symbol_name=symbol,
                workspace_id=workspace_id,
                user_id=OVERLAY_USER_ID,
            )

        # A symbol unknown to the index but typed into a dirty buffer is still a
        # legitimate target — the overlay callers (or its own buffer definition)
        # anchor it. Only a true miss (no uid, no overlay evidence) is not-found.
        overlay_defines = False
        if not uid and self._overlay is not None and requested_path:
            try:
                overlay_defines = symbol in self._overlay.get_symbols(
                    requested_path, workspace_id=workspace_id, user_id=OVERLAY_USER_ID
                )
            except Exception:
                overlay_defines = False

        if not uid and not overlay_rows and not overlay_defines:
            return ImpactResult(symbol=symbol, workspace_id=workspace_id, found=False)

        committed_keys = {(r.get("file_path"), r.get("name")) for r in committed_rows}
        extra_rows = [
            r for r in overlay_rows if (r.get("file_path"), r.get("name")) not in committed_keys
        ]
        affected_symbols = committed_rows + extra_rows
        affected_files = sorted(
            set(committed_files) | {r["file_path"] for r in extra_rows if r.get("file_path")}
        )
        if not symbol_file and requested_path:
            symbol_file = requested_path

        return ImpactResult(
            symbol=symbol,
            workspace_id=workspace_id,
            found=True,
            symbol_uid=uid or (f"overlay::{workspace_id}::{symbol_file}::{symbol}"),
            file_path=symbol_file,
            affected_symbols=affected_symbols,
            affected_files=affected_files,
            max_depth=walk_depth,
            degraded=not uid or bool(extra_rows),
            overlay_count=len(extra_rows),
        )

    def list_workspaces(self) -> list[WorkspaceInfo]:
        """Indexed workspaces the tools can serve, so the caller can discover
        what is queryable and pass a ``base`` to ``workspace=``.

        Filtered to the axis_python_v1 index namespace (the profile ask/impact
        force), so legacy-only indexes don't show as falsely queryable.
        """
        from context_engine.index_profile import (
            AXIS_PYTHON_V1_PROFILE,
            base_workspace_id,
            resolve_index_profile,
        )

        suffix = resolve_index_profile(AXIS_PYTHON_V1_PROFILE).workspace_suffix
        with self._lock:
            self._ensure_db()
            with self._db.driver.session() as session:
                rows = session.run(
                    "MATCH (f:File) WHERE f.workspace_id IS NOT NULL "
                    "RETURN f.workspace_id AS ws, count(*) AS n ORDER BY n DESC"
                ).data()

        out: list[WorkspaceInfo] = []
        for r in rows:
            ws = str(r.get("ws") or "")
            if not ws or (suffix and not ws.endswith(suffix)):
                continue
            out.append(
                WorkspaceInfo(base=base_workspace_id(ws), indexed=ws, files=int(r["n"] or 0))
            )
        return out

    def list_files(
        self,
        workspace_id: str,
        *,
        path_prefix: str | None = None,
        with_counts: bool = False,
        limit: int = 400,
    ) -> list[FileEntry]:
        """Indexed files of one workspace — the top rung of navigation
        (list_workspaces → list_files → file_outline → read_symbol). Pure Neo4j;
        the only way to enumerate a non-local workspace the host can't glob.
        ``path_prefix`` substring-filters; ``with_counts`` adds per-file symbol
        counts (one extra OPTIONAL MATCH)."""
        prefix = (path_prefix or "").strip()
        count_clause = (
            "OPTIONAL MATCH (f)-[:CONTAINS]->(s:Symbol) RETURN f.path AS path, count(s) AS n"
            if with_counts
            else "RETURN f.path AS path, 0 AS n"
        )
        query = (
            "MATCH (f:File {workspace_id: $ws}) "
            "WHERE $prefix = '' OR f.path CONTAINS $prefix "
            f"{count_clause} ORDER BY path LIMIT $limit"
        )
        with self._lock:
            self._ensure_db()
            with self._db.driver.session() as session:
                rows = session.run(query, ws=workspace_id, prefix=prefix, limit=int(limit)).data()
        return [
            FileEntry(path=str(r["path"]), symbols=int(r.get("n") or 0))
            for r in rows
            if r.get("path")
        ]

    # ------------------------------------------------------------------
    # P0/P1 navigation primitives — thin wrappers over the engine read path.
    # All graph-only tools stay on the cheap ``_ensure_db`` (no embedding
    # cold-start); only ``search_code`` pulls the SentenceTransformer.
    # ------------------------------------------------------------------

    def _resolve_uid(
        self, name: str, workspace_id: str, file_path: str | None = None
    ) -> str | None:
        """Resolve ``name`` to a workspace uid. Caller MUST hold ``self._lock``
        and have opened the Neo4j handle (the lock is non-reentrant)."""
        path = file_path.strip() if isinstance(file_path, str) and file_path.strip() else None
        if path:
            return cast(
                "str | None", self._db.get_symbol_uid_by_name_in_file(name, path, workspace_id)
            )
        return cast("str | None", self._db.get_symbol_uid_by_name(name, workspace_id))

    def set_overlay(
        self, file_path: str, content: str, workspace_id: str, *, dirty: bool = True
    ) -> list[str]:
        """Stash an uncommitted edit so ``ask``/``impact`` reflect it. Returns
        the symbol names parsed from the buffer. No DB, no embedding."""
        with self._lock:
            overlay = self._ensure_overlay()
            overlay.update(
                file_path, content, workspace_id=workspace_id, user_id=OVERLAY_USER_ID, dirty=dirty
            )
            try:
                symbols = overlay.get_symbols(
                    file_path, workspace_id=workspace_id, user_id=OVERLAY_USER_ID
                )
            except Exception:
                symbols = {}
        return list(symbols.keys())

    def clear_overlay(self, file_path: str, workspace_id: str) -> bool:
        """Drop a single overlay buffer. True if one was present."""
        if self._overlay is None:
            return False
        with self._lock:
            present = self._overlay.has(
                file_path, workspace_id=workspace_id, user_id=OVERLAY_USER_ID
            )
            self._overlay.clear(file_path, workspace_id=workspace_id, user_id=OVERLAY_USER_ID)
        return present

    def read_symbol(
        self, name: str, workspace_id: str, *, file_path: str | None = None
    ) -> SymbolSource:
        """Exact source of one symbol: resolve uid → Neo4j line span → read the
        slice off disk (sandboxed to the registered workspace root). No
        embedding — pure Neo4j + filesystem."""
        from context_engine.workspace_paths import (
            registered_workspace_root,
            resolve_graph_file_path,
        )

        with self._lock:
            self._ensure_db()
            uid = self._resolve_uid(name, workspace_id, file_path)
            if not uid:
                return SymbolSource(name=name, workspace_id=workspace_id, found=False)
            spans = self._db.get_symbol_spans_by_uids([uid], workspace_id=workspace_id)
            span = spans.get(uid)
            if not span:
                return SymbolSource(name=name, workspace_id=workspace_id, found=False)
            fp = str(span.get("file_path") or "")
            start = int(span.get("start_line") or 0)
            end = int(span.get("end_line") or 0)
            root = registered_workspace_root(self._db, workspace_id)
            code = ""
            resolved = resolve_graph_file_path(fp, workspace_root=root)
            if resolved and start >= 1 and end >= start:
                try:
                    lines = Path(resolved).read_text(encoding="utf-8").splitlines()
                    code = "\n".join(lines[start - 1 : end])
                except OSError:
                    code = ""

        return SymbolSource(
            name=str(span.get("name") or name),
            workspace_id=workspace_id,
            found=True,
            uid=uid,
            file_path=fp,
            start_line=start,
            end_line=end,
            code=code,
        )

    def search_code(
        self,
        query: str,
        workspace_id: str,
        *,
        limit: int = 10,
        kind: str = "symbol",
    ) -> list[dict]:
        """Cheap vector search — symbol or doc hits, no graph expansion. Pulls
        the embedding model (one-time cold-start) but skips Neo4j entirely.

        Symbol search reuses the axis pipeline's role-agnostic seed recall
        (``find_seeds_by_vector`` — a numpy scan over the workspace vector
        matrix), which is the schema-correct path for the multi-vector axis
        symbols table (a plain ``table.search`` can't pick the vector column)."""
        with self._lock:
            self._ensure_lance()
            if kind == "doc":
                return cast(
                    "list[dict]", self._lance.search(query, limit, workspace_id=workspace_id)
                )

            from context_engine.axis.role_retrieval import find_seeds_by_vector
            from context_engine.database.lancedb_client import DB_PATH

            def embed(text: str):
                return self._lance._embed([text])[0]  # noqa: SLF001

            candidates = find_seeds_by_vector(
                workspace_id,
                query,
                embed_fn=embed,
                limit=limit,
                lance_db_path=DB_PATH,
            )
        return [
            {
                "uid": c.uid,
                "name": c.name,
                "file_path": c.file_path,
                "distance": c.vector_distance,
                "score": c.score,
            }
            for c in candidates
        ]

    def call_neighbours(
        self,
        symbol: str,
        workspace_id: str,
        *,
        direction: str,
        file_path: str | None = None,
        max_hops: int = 1,
        limit: int = 50,
    ) -> NeighbourResult:
        """Directional CALLS walk: ``reverse`` = callers (who calls symbol),
        ``forward`` = callees (what symbol calls). Pure Neo4j graph walk."""
        from context_engine.axis.graph_walk import EdgeProfile, walk_neighbours

        with self._lock:
            self._ensure_db()
            uid = self._resolve_uid(symbol, workspace_id, file_path)
            if not uid:
                return NeighbourResult(
                    symbol=symbol, workspace_id=workspace_id, found=False, direction=direction
                )
            neighbours = walk_neighbours(
                self._db,
                workspace_id,
                [uid],
                edges=EdgeProfile.CALLS,
                direction=cast(Any, direction),
                max_hops=max_hops,
                limit=limit,
            )

        return NeighbourResult(
            symbol=symbol,
            workspace_id=workspace_id,
            found=True,
            symbol_uid=uid,
            direction=direction,
            rows=[
                NeighbourRow(uid=n.uid, name=n.name, file_path=n.file_path, depth=n.depth)
                for n in neighbours
            ],
        )

    def find_definition(
        self, name: str, workspace_id: str, *, limit: int = 20
    ) -> list[DefinitionHit]:
        """Every symbol defined under ``name`` in the workspace (go-to-definition;
        returns all overloads/collisions). Pure Neo4j."""
        with self._lock:
            self._ensure_db()
            with self._db.driver.session() as session:
                rows = session.run(
                    """
                    MATCH (f:File {workspace_id: $ws})-[c:CONTAINS]->(s:Symbol {name: $name})
                    RETURN s.uid AS uid, s.name AS name, f.path AS file_path,
                           coalesce(s.kind, '') AS kind,
                           coalesce(c.start_line, s.range[0], 0) AS start_line
                    ORDER BY file_path, start_line
                    LIMIT $limit
                    """,
                    ws=workspace_id,
                    name=name,
                    limit=int(limit),
                ).data()
        return [
            DefinitionHit(
                uid=str(r.get("uid") or ""),
                name=str(r.get("name") or name),
                file_path=str(r.get("file_path") or ""),
                kind=str(r.get("kind") or ""),
                start_line=int(r.get("start_line") or 0),
            )
            for r in rows
        ]

    def file_outline(self, file_path: str, workspace_id: str, *, limit: int = 400) -> FileOutline:
        """Symbol map of one file (name, kind, start line), ordered top-to-bottom.
        Pure Neo4j — no code bodies."""
        path = file_path.strip()
        if not path:
            return FileOutline(requested_path=file_path, workspace_id=workspace_id, found=False)
        suffix = f"/{path.rsplit('/', 1)[-1]}"
        with self._lock:
            self._ensure_db()
            with self._db.driver.session() as session:
                rows = session.run(
                    """
                    MATCH (f:File {workspace_id: $ws})-[c:CONTAINS]->(s:Symbol)
                    WHERE f.path = $path OR f.path ENDS WITH $path OR f.path ENDS WITH $suffix
                    RETURN f.path AS file_path, s.name AS name,
                           coalesce(s.kind, '') AS kind,
                           coalesce(c.start_line, s.range[0], 0) AS start_line
                    ORDER BY start_line
                    LIMIT $limit
                    """,
                    ws=workspace_id,
                    path=path,
                    suffix=suffix,
                    limit=int(limit),
                ).data()
        if not rows:
            return FileOutline(requested_path=file_path, workspace_id=workspace_id, found=False)
        return FileOutline(
            requested_path=file_path,
            workspace_id=workspace_id,
            found=True,
            file_path=str(rows[0].get("file_path") or path),
            rows=[
                OutlineRow(
                    name=str(r.get("name") or ""),
                    kind=str(r.get("kind") or ""),
                    start_line=int(r.get("start_line") or 0),
                )
                for r in rows
            ],
        )

    def path(
        self,
        symbol_a: str,
        symbol_b: str,
        workspace_id: str,
        *,
        file_a: str | None = None,
        file_b: str | None = None,
        max_hops: int = 6,
    ) -> PathResult:
        """Shortest undirected path between two symbols across all edge types —
        the "how does A relate to B" navigator. Pure Neo4j."""
        hops = max_hops if isinstance(max_hops, int) and 1 <= max_hops <= 10 else 6
        with self._lock:
            self._ensure_db()
            uid_a = self._resolve_uid(symbol_a, workspace_id, file_a)
            uid_b = self._resolve_uid(symbol_b, workspace_id, file_b)
            if not uid_a or not uid_b:
                missing = symbol_a if not uid_a else symbol_b
                return PathResult(
                    symbol_a=symbol_a,
                    symbol_b=symbol_b,
                    workspace_id=workspace_id,
                    found=False,
                    reason=f"symbol not found: {missing}",
                )
            cypher = (
                "MATCH (a:Symbol {uid: $ua}), (b:Symbol {uid: $ub}) "
                f"MATCH p = shortestPath((a)-[*..{hops}]-(b)) "
                "RETURN [n IN nodes(p) | coalesce(n.name, n.uid)] AS names, "
                "[r IN relationships(p) | type(r)] AS rels"
            )
            with self._db.driver.session() as session:
                rec = session.run(cypher, ua=uid_a, ub=uid_b).single()
        if not rec:
            return PathResult(
                symbol_a=symbol_a,
                symbol_b=symbol_b,
                workspace_id=workspace_id,
                found=False,
                reason=f"no path within {hops} hops",
            )
        return PathResult(
            symbol_a=symbol_a,
            symbol_b=symbol_b,
            workspace_id=workspace_id,
            found=True,
            node_names=[str(n) for n in (rec.get("names") or [])],
            rel_types=[str(t) for t in (rec.get("rels") or [])],
        )

    def docs_for(
        self, symbol: str, workspace_id: str, *, file_path: str | None = None, limit: int = 20
    ) -> DocsForResult:
        """Documentation anchored to ``symbol`` via DocAnchor COVERS edges
        (anchor type + confidence + source doc files). Pure Neo4j."""
        with self._lock:
            self._ensure_db()
            uid = self._resolve_uid(symbol, workspace_id, file_path)
            if not uid:
                return DocsForResult(symbol=symbol, workspace_id=workspace_id, found=False)
            with self._db.driver.session() as session:
                rows = session.run(
                    """
                    MATCH (a:DocAnchor)-[r:COVERS]->(s:Symbol {uid: $uid})
                    WHERE coalesce(r.workspace_id, $ws) = $ws
                    OPTIONAL MATCH (a)-[:FROM]->(f:File {workspace_id: $ws})
                    WITH a, r, collect(DISTINCT f.path) AS files
                    RETURN coalesce(a.chunk_id, '') AS chunk_id,
                           coalesce(r.anchor_type, '') AS anchor_type,
                           coalesce(r.confidence, 0.0) AS confidence,
                           files AS files
                    ORDER BY confidence DESC
                    LIMIT $limit
                    """,
                    ws=workspace_id,
                    uid=uid,
                    limit=int(limit),
                ).data()
        return DocsForResult(
            symbol=symbol,
            workspace_id=workspace_id,
            found=True,
            symbol_uid=uid,
            rows=[
                DocCoverRow(
                    chunk_id=str(r.get("chunk_id") or ""),
                    anchor_type=str(r.get("anchor_type") or ""),
                    confidence=float(r.get("confidence") or 0.0),
                    files=[str(f) for f in (r.get("files") or []) if f],
                )
                for r in rows
            ],
        )

    def _resolve_explain_uid(
        self,
        concept: str,
        workspace_id: str,
        file_path: str | None,
    ) -> tuple[str, str]:
        resolved_via = "exact"
        with self._lock:
            self._ensure_db()
            uid = self._resolve_uid(concept, workspace_id, file_path)
        if uid:
            return uid, resolved_via
        hits = self.search_code(concept, workspace_id, limit=1, kind="symbol")
        if hits:
            return str(hits[0].get("uid") or ""), "vector"
        return "", resolved_via

    def _fetch_explain_seed_data(
        self,
        uid: str,
        workspace_id: str,
        concept: str,
        allowed: list[str],
    ) -> tuple[str, str, str, list[dict]]:
        from context_engine.workspace_paths import (
            registered_workspace_root,
            resolve_graph_file_path,
        )

        with self._lock:
            self._ensure_db()
            spans = self._db.get_symbol_spans_by_uids([uid], workspace_id=workspace_id)
            span = spans.get(uid) or {}
            seed_name = str(span.get("name") or concept)
            seed_file = str(span.get("file_path") or "")
            start = int(span.get("start_line") or 0)
            end = int(span.get("end_line") or 0)

            signature = ""
            root = registered_workspace_root(self._db, workspace_id)
            resolved = resolve_graph_file_path(seed_file, workspace_root=root)
            if resolved and start >= 1 and end >= start:
                signature = _read_seed_signature(resolved, start, end)

            with self._db.driver.session() as session:
                edge_rows = session.run(
                    """
                    MATCH (a:Symbol {uid: $uid})-[r]-(n:Symbol)
                    MATCH (fn:File {workspace_id: $ws})-[:CONTAINS]->(n)
                    WHERE type(r) IN $allowed
                      AND coalesce(r.workspace_id, $ws) = $ws
                    RETURN type(r) AS rel, (startNode(r) = a) AS outgoing,
                           coalesce(n.name, '') AS name, fn.path AS file_path
                    """,
                    uid=uid,
                    ws=workspace_id,
                    allowed=allowed,
                ).data()
        return seed_name, seed_file, signature, edge_rows

    def explain(
        self,
        concept: str,
        workspace_id: str,
        *,
        file_path: str | None = None,
        max_per_group: int = 8,
    ) -> ExplainResult:
        """Concept card for ``concept``: resolve it to a seed symbol, then map
        its one-hop connections grouped by relationship type (calls, uses type,
        decorated by, inherits, …) plus its documentation. The graphify
        ``explain`` analog — structured connections + source locations for the
        host LLM to narrate. AFFECTS is excluded (too broad; use ``impact``).

        Resolution: exact symbol name first (no embedding); free-text falls back
        to the top vector hit (pulls the model)."""
        from context_engine.axis.graph_walk import EdgeProfile

        allowed = sorted(set(EdgeProfile.PROXIMITY) & set(_EXPLAIN_EDGE_LABELS))
        uid, resolved_via = self._resolve_explain_uid(concept, workspace_id, file_path)
        if not uid:
            return ExplainResult(concept=concept, workspace_id=workspace_id, found=False)

        seed_name, seed_file, signature, edge_rows = self._fetch_explain_seed_data(
            uid,
            workspace_id,
            concept,
            allowed,
        )
        docs = self.docs_for(seed_name, workspace_id, file_path=seed_file, limit=10)
        groups = _group_explain_connections(edge_rows, max_per_group=max_per_group)

        return ExplainResult(
            concept=concept,
            workspace_id=workspace_id,
            found=True,
            resolved_via=resolved_via,
            seed_name=seed_name,
            seed_uid=uid,
            seed_file=seed_file,
            signature=signature,
            groups=groups,
            docs=docs.rows if docs.found else [],
        )

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None
        self._lance = None
        self._overlay = None
