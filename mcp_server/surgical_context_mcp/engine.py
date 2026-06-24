"""In-process bridge to the axis retrieval pipeline.

This runs the same read path as the ``/ask/axis`` HTTP route and the
``QA/axis_benchmark`` harness: ``run_axis_retrieval`` over a single, long-lived
Neo4j + LanceDB handle. The retrieval is LLM-free — it returns ranked,
graph-expanded code bundles (already budget-trimmed by the Token Credit path
when ``intent_budget`` is on); the host chat model reasons over them.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

# Load the repo .env (NEO4J_*, model creds) before any heavy import — exactly
# what context_engine/main.py does for the server.
from context_engine.env_loader import load_repo_dotenv

load_repo_dotenv()


@dataclass
class AskResult:
    question: str
    workspace_id: str
    intent: list[tuple[str, float]] = field(default_factory=list)
    candidate_count: int = 0
    files: list[str] = field(default_factory=list)
    text: str = ""


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


@dataclass
class WorkspaceInfo:
    base: str  # client-facing id (profile suffix stripped) — pass to `workspace=`
    indexed: str  # physical index namespace (with profile suffix)
    files: int


def _render_bundles(result) -> tuple[list[str], str]:
    """Flatten ``result.bundles`` into a deduped, prompt-ready markdown block.

    Dedupes by uid (highest-rank / shallowest occurrence wins), preserving the
    candidate-rank, seed-before-related order — the same content the benchmark
    counts as the prompt the LLM actually receives.
    """
    if not result.bundles:
        return [], ""

    seen_uid: set[str] = set()
    seen_file: set[str] = set()
    files: list[str] = []
    parts: list[str] = []

    for bundle in result.bundles:
        for sym in bundle.all_symbols():
            if sym.uid in seen_uid:
                continue
            seen_uid.add(sym.uid)

            fp = sym.file_path or ""
            if fp and fp not in seen_file:
                seen_file.add(fp)
                files.append(fp)

            step = sym.expansion_step or "seed"
            parts.append(
                f"### {fp} :: {sym.name}  "
                f"({sym.role}, depth={sym.distance_from_seed}, {step})"
            )
            if sym.code:
                parts.append("```python")
                parts.append(sym.code.rstrip())
                parts.append("```")
            parts.append("")

    return files, "\n".join(parts).rstrip() + "\n"


class AxisEngine:
    """Long-lived holder for the Neo4j + LanceDB handles and the read path.

    Handles are opened lazily on the first query so the MCP server starts (and
    Claude Code connects) fast; the SentenceTransformer cold-start is paid once,
    on the first ``ask``. A lock serialises concurrent tool calls since the DB
    handles are shared and not assumed thread-safe.
    """

    def __init__(self) -> None:
        self._db = None
        self._lance = None
        self._lock = threading.Lock()

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

    def _ensure(self) -> None:
        """Open Neo4j + LanceDB. The Lance handle pulls SentenceTransformer
        (one-time cold-start), needed only for the embedding-driven ask path —
        impact / list_workspaces stay on the cheap ``_ensure_db`` path.

        Verified clean on stdout — the model load logs to stderr only, so the
        MCP stdio JSON-RPC channel stays uncorrupted without redirection.
        """
        self._ensure_db()
        if self._lance is not None:
            return
        from context_engine.database.lancedb_client import LanceDBClient
        from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE

        self._lance = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)

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
    ) -> AskResult:
        from context_engine.axis.pipeline import run_axis_retrieval

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
                top_roles=top_roles,
                per_role_limit=per_role_limit,
                with_context=with_context,
                intent_budget=True,
                base_token_budget=token_budget,
                hook_transparency=True,
                intent_override=intent_override,
            )

        intent = [(m.role, m.similarity) for m in result.intent]
        files, text = _render_bundles(result)
        return AskResult(
            question=question,
            workspace_id=workspace_id,
            intent=intent,
            candidate_count=len(result.candidates_for_context),
            files=files,
            text=text,
        )

    def impact(
        self,
        symbol: str,
        workspace_id: str,
        *,
        file_path: str | None = None,
        max_depth: int = 3,
    ) -> ImpactResult:
        """Downstream dependents of ``symbol`` — the committed surface only.

        Same in-process path as the ``/impact`` route (``resolve_impact_symbol_uid``
        → ``build_impact_surface``), minus the live-overlay augmentation (no
        editor buffer in an MCP session). Pure Neo4j graph walk — no embeddings.
        """
        from context_engine.axis.impact_surface import build_impact_surface

        requested_path = file_path.strip() if isinstance(file_path, str) and file_path.strip() else None
        with self._lock:
            self._ensure_db()
            uid = self._db.resolve_impact_symbol_uid(
                symbol, workspace_id, file_path=requested_path
            )
            if not uid:
                return ImpactResult(symbol=symbol, workspace_id=workspace_id, found=False)
            symbol_file = self._db.get_file_path_for_symbol(uid, workspace_id=workspace_id)
            surface = build_impact_surface(
                db=self._db,
                symbol_uid=uid,
                symbol_name=symbol,
                file_path=symbol_file,
                workspace_id=workspace_id,
                max_depth=max_depth,
            )

        return ImpactResult(
            symbol=symbol,
            workspace_id=workspace_id,
            found=True,
            symbol_uid=uid,
            file_path=symbol_file,
            affected_symbols=surface["affected_symbols"],
            affected_files=surface["affected_files"],
            max_depth=surface["max_depth"],
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

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None
        self._lance = None
