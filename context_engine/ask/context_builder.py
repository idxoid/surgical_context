"""Resolve PromptContext for /ask via axis and file/workspace/direct fallbacks."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from context_engine.api.schemas import AskRequest
from context_engine.context_types import DocChunk, PromptContext, SymbolContext
from context_engine.database.lancedb_client import LanceDBClient
from context_engine.doc_resolver import DocResolver
from context_engine.index_profile import (
    AXIS_PYTHON_V1_PROFILE,
    effective_index_workspace_id,
    resolve_index_profile,
)
from context_engine.observability import estimate_text_tokens
from context_engine.overlay import InMemoryOverlay

logger = logging.getLogger(__name__)

FALLBACK_INTENT = "exploration"


class AskContextBuilder:
    """Build PromptContext from axis, file, workspace, or direct providers."""

    def __init__(self, *, overlay: InMemoryOverlay, vector_db: LanceDBClient):
        self.overlay = overlay
        self.vector_db = vector_db
        self._axis_vector_db: LanceDBClient | None = None

    def lance_for_index_workspace(self, index_workspace_id: str) -> LanceDBClient:
        """Return a Lance client whose physical tables match ``index_workspace_id``.

        Axis retrieval requires ``symbols_axis_python_v1`` (etc.). When the
        process-default ``vector_db`` uses the legacy profile, reuse a lazily
        opened axis-profile client instead of scanning the wrong table.
        """
        if self.vector_db.index_profile_name == AXIS_PYTHON_V1_PROFILE:
            return self.vector_db
        axis_suffix = resolve_index_profile(AXIS_PYTHON_V1_PROFILE).workspace_suffix
        if axis_suffix and index_workspace_id.endswith(axis_suffix):
            if self._axis_vector_db is None:
                self._axis_vector_db = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)
            return self._axis_vector_db
        return self.vector_db

    def find_symbol_line(self, file_path: str, symbol: str | None) -> int | None:
        """Return the 1-based line number of the first definition matching ``symbol``."""
        if not symbol or not file_path:
            return None
        try:
            with open(file_path, encoding="utf-8") as file:
                for lineno, line in enumerate(file, 1):
                    stripped = line.lstrip()
                    if stripped.startswith(
                        ("def ", "class ", "async def ", "function ", "const ", "let ", "var ")
                    ) and symbol in line:
                        return lineno
        except (OSError, FileNotFoundError):
            pass
        return None

    def trim_text_to_budget(
        self,
        text: str,
        token_budget: int,
        anchor_line: int | None = None,
    ) -> str:
        if not text:
            return ""
        max_tokens = max(400, int(token_budget * 0.75))
        if estimate_text_tokens(text) <= max_tokens:
            return text

        lines = text.splitlines()
        total = len(lines)
        max_lines = max(50, max_tokens // 4)

        if anchor_line is not None:
            center = max(0, min(anchor_line - 1, total - 1))
            half = max_lines // 2
            start = max(0, center - half // 2)
            end = min(total, start + max_lines)
            start = max(0, end - max_lines)
            return "\n".join(lines[start:end])

        kept: list[str] = []
        for line in lines:
            candidate = "\n".join([*kept, line])
            if estimate_text_tokens(candidate) > max_tokens:
                break
            kept.append(line)
        return "\n".join(kept)

    def read_file_context(
        self,
        file_path: str,
        *,
        workspace_id: str,
        user_id: str,
        token_budget: int,
        anchor_line: int | None = None,
    ) -> tuple[str, bool]:
        if self.overlay.has(file_path, workspace_id=workspace_id, user_id=user_id):
            symbols = self.overlay.get_symbols(
                file_path, workspace_id=workspace_id, user_id=user_id
            )
            if symbols:
                start = min(line_range[0] for line_range in symbols.values())
                end = max(line_range[1] for line_range in symbols.values())
                code = self.overlay.read_lines(
                    file_path,
                    start,
                    end,
                    workspace_id=workspace_id,
                    user_id=user_id,
                )
            else:
                code = self.overlay.read_lines(
                    file_path,
                    1,
                    500,
                    workspace_id=workspace_id,
                    user_id=user_id,
                )
            return self.trim_text_to_budget(code, token_budget, anchor_line), True

        try:
            with open(file_path, encoding="utf-8") as file:
                code = file.read()
        except (OSError, FileNotFoundError):
            return "", False
        return self.trim_text_to_budget(code, token_budget, anchor_line), False

    def search_docs(self, query: str, limit: int, *, workspace_id: str) -> list[DocChunk]:
        try:
            return DocResolver(self.vector_db).search(query, limit=limit, workspace_id=workspace_id)
        except Exception:
            return []

    def search_symbols(self, query: str, limit: int, *, workspace_id: str) -> list[SymbolContext]:
        search_symbols = getattr(self.vector_db, "search_symbols", None)
        if not callable(search_symbols):
            return []
        try:
            raw_symbols = search_symbols(
                query, limit=limit, threshold=1.0, workspace_id=workspace_id
            )
        except Exception:
            return []
        return [
            SymbolContext(
                symbol=str(symbol.get("name", "")),
                file_path=str(symbol.get("file_path", "")),
                relation="workspace_match",
                relevance_score=float(symbol.get("score") or 0.0),
                provenance=["vector:symbols"],
            )
            for symbol in raw_symbols
        ]

    def doc_tier_tokens(self, docs: list[DocChunk]) -> dict[str, int]:
        if not docs:
            return {}
        return {"docs": sum(estimate_text_tokens(doc.content) for doc in docs)}

    def context_from_file(
        self,
        *,
        file_path: str,
        question: str,
        token_budget: int,
        base_workspace_id: str,
        index_workspace_id: str,
        user_id: str,
        symbol: str | None = None,
    ) -> PromptContext | None:
        anchor_line = self.find_symbol_line(file_path, symbol)
        code, is_dirty = self.read_file_context(
            file_path,
            workspace_id=base_workspace_id,
            user_id=user_id,
            token_budget=token_budget,
            anchor_line=anchor_line,
        )
        if not code:
            return None

        ctx = PromptContext(
            primary_source=SymbolContext(
                symbol=os.path.basename(file_path) or file_path,
                file_path=file_path,
                relation="file",
                relevance_score=1.0,
                is_dirty=is_dirty,
                code=code,
                provenance=["file"],
            ),
            graph_context=[],
            documentation=self.search_docs(
                f"{file_path} {question}",
                limit=3,
                workspace_id=index_workspace_id,
            ),
            mode="file",
            intent=FALLBACK_INTENT,
            tier_tokens={"code": estimate_text_tokens(code)},
        )
        ctx.tier_tokens.update(self.doc_tier_tokens(ctx.documentation))
        return ctx

    def context_from_workspace(
        self,
        question: str,
        token_budget: int,
        *,
        index_workspace_id: str,
    ) -> PromptContext | None:
        docs = self.search_docs(question, limit=5, workspace_id=index_workspace_id)
        symbols = self.search_symbols(question, limit=5, workspace_id=index_workspace_id)
        if not docs and not symbols:
            return None

        ctx = PromptContext(
            primary_source=SymbolContext(
                symbol="workspace",
                file_path="<workspace>",
                relation="workspace",
                relevance_score=1.0,
                provenance=["workspace_search"],
            ),
            graph_context=symbols,
            documentation=docs,
            mode="workspace",
            intent=FALLBACK_INTENT,
            tier_tokens={"cross_refs": sum(estimate_text_tokens(sym.symbol) for sym in symbols)},
        )
        ctx.tier_tokens.update(self.doc_tier_tokens(docs))
        ctx.budget["token_budget"] = token_budget
        return ctx

    def context_from_direct(self, question: str, token_budget: int) -> PromptContext:
        return PromptContext(
            primary_source=SymbolContext(
                symbol="direct",
                file_path="<none>",
                relation="direct",
                relevance_score=0.0,
                provenance=["direct_llm"],
            ),
            graph_context=[],
            documentation=[],
            mode="direct",
            intent=FALLBACK_INTENT,
            tier_tokens={},
            budget={"token_budget": token_budget},
        )

    def context_from_axis(
        self,
        question: str,
        *,
        base_workspace_id: str,
        index_workspace_id: str,
        db: Any,
        token_budget: int = 6000,
        anchor_path: str | None = None,
        anchor_symbol: str | None = None,
        trace_id: str = "",
        user_id: str = "anonymous",
    ) -> PromptContext | None:
        """Axis-pipeline provider: canonical retrieval -> renderable PromptContext."""
        from context_engine.axis.pipeline import run_axis_retrieval
        from context_engine.axis.prompt_provider import axis_bundles_to_prompt_context
        from context_engine.axis.retrieval_budget import budget_for_intent

        result = run_axis_retrieval(
            question,
            workspace_id=index_workspace_id,
            db=db,
            lance=self.lance_for_index_workspace(index_workspace_id),
            intent_budget=True,
            base_token_budget=token_budget,
            anchor_path=anchor_path,
            anchor_symbol=anchor_symbol,
            hook_transparency=True,
            overlay=self.overlay,
            user_id=user_id,
        )
        intent = result.intent[0].role if result.intent else ""
        ctx = axis_bundles_to_prompt_context(
            result.bundles,
            question=question,
            workspace_id=base_workspace_id,
            intent=intent,
            trace_id=trace_id,
            render_mode=result.render_mode,
        )
        if ctx is None:
            return None

        matches = list(result.intent)
        ctx.intent_distribution = {match.role: match.similarity for match in matches}
        ctx.intent_confidence = matches[0].similarity if matches else 0.0
        ctx.intent_ambiguous = len(matches) > 1
        profile = budget_for_intent(matches)
        ctx.intent_effective_mode = profile.name
        ctx.intent_resolution = {
            "source": "axis_classifier",
            "matches": [match.to_dict() for match in matches],
        }
        ctx.tier_tokens = {
            "code": estimate_text_tokens(ctx.primary_source.code),
            "cross_refs": sum(
                estimate_text_tokens(symbol.code)
                for symbol in ctx.graph_context
                if symbol.code and symbol.code.strip()
            ),
        }
        ctx.budget["intent_profile"] = profile.name
        return ctx

    @staticmethod
    def ask_axis_first_enabled() -> bool:
        return os.environ.get("ASK_AXIS_FIRST", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def try_axis_context(
        self,
        *,
        req: AskRequest,
        base_workspace_id: str,
        index_workspace_id: str,
        db: Any,
        anchor_path: str | None = None,
        user_id: str,
        context_from_axis: Callable[..., PromptContext | None] | None = None,
    ) -> PromptContext | None:
        provider = context_from_axis or self.context_from_axis
        try:
            return provider(
                req.question,
                base_workspace_id=base_workspace_id,
                index_workspace_id=index_workspace_id,
                db=db,
                token_budget=req.token_budget,
                anchor_path=anchor_path,
                anchor_symbol=req.symbol,
                user_id=user_id,
            )
        except Exception:
            logger.exception("ask_axis_first provider failed; falling through")
            return None

    @staticmethod
    def context_budget(ctx: Any) -> dict[str, Any]:
        budget = getattr(ctx, "budget", None)
        if not isinstance(budget, dict):
            budget = {}
            ctx.budget = budget
        return budget

    @staticmethod
    def fallback_reason(symbol_error: str) -> str:
        if "not found" in symbol_error.lower():
            return "symbol_not_found"
        if symbol_error:
            return "symbol_context_unavailable"
        return "symbol_not_provided"

    @staticmethod
    def append_context_warning(current: Any, warning: dict[str, str]) -> list[dict[str, str]]:
        warnings = (
            [item for item in current if isinstance(item, dict)]
            if isinstance(current, list)
            else []
        )
        if not any(item.get("code") == warning["code"] for item in warnings):
            warnings.append(warning)
        return warnings

    def mark_ask_fallback(
        self,
        ctx: PromptContext,
        req: AskRequest,
        ask_level: str,
        symbol_error: str = "",
    ) -> None:
        budget = self.context_budget(ctx)
        budget["ask_level"] = ask_level
        budget["fallback_ladder"] = ["symbol", "file", "workspace", "direct_llm"]
        if req.symbol:
            display_level = "direct LLM" if ask_level == "direct_llm" else ask_level
            budget["missing_symbol"] = req.symbol
            budget["fallback_from"] = "symbol"
            budget["fallback_reason"] = self.fallback_reason(symbol_error)
            budget["warnings"] = self.append_context_warning(
                budget.get("warnings"),
                {
                    "code": budget["fallback_reason"],
                    "severity": "warning",
                    "message": (
                        f"Symbol '{req.symbol}' was not found; using {display_level} context."
                    ),
                },
            )

    def resolve_ask_context(
        self,
        *,
        req: AskRequest,
        user_id: str,
        workspace_id: str,
        db: Any,
        sandbox_path: Callable[..., str],
        context_from_axis: Callable[..., PromptContext | None] | None = None,
        context_from_file: Callable[..., PromptContext | None] | None = None,
        context_from_workspace: Callable[..., PromptContext | None] | None = None,
        context_from_direct: Callable[..., PromptContext | None] | None = None,
    ) -> PromptContext:
        base_workspace_id = workspace_id
        index_workspace_id = effective_index_workspace_id(base_workspace_id)
        safe_file_path = ""
        if req.file_path:
            safe_file_path = sandbox_path(req.file_path, workspace_id=base_workspace_id, db=db)

        axis_provider = context_from_axis or self.context_from_axis
        file_provider = context_from_file or self.context_from_file
        workspace_provider = context_from_workspace or self.context_from_workspace
        direct_provider = context_from_direct or self.context_from_direct

        if self.ask_axis_first_enabled():
            axis_ctx = self.try_axis_context(
                req=req,
                base_workspace_id=base_workspace_id,
                index_workspace_id=index_workspace_id,
                db=db,
                anchor_path=safe_file_path or None,
                user_id=user_id,
                context_from_axis=axis_provider,
            )
            if axis_ctx is not None:
                self.context_budget(axis_ctx)["ask_level"] = "axis"
                return axis_ctx

        symbol_error = f"Error: Symbol '{req.symbol}' not found in graph." if req.symbol else ""
        if req.file_path:
            file_ctx = file_provider(
                file_path=safe_file_path,
                question=req.question,
                token_budget=req.token_budget,
                base_workspace_id=base_workspace_id,
                index_workspace_id=index_workspace_id,
                user_id=user_id,
                symbol=req.symbol,
            )
            if file_ctx:
                self.mark_ask_fallback(file_ctx, req, "file", symbol_error)
                return file_ctx

        workspace_ctx = workspace_provider(
            req.question,
            req.token_budget,
            index_workspace_id=index_workspace_id,
        )
        if workspace_ctx:
            self.mark_ask_fallback(workspace_ctx, req, "workspace", symbol_error)
            return workspace_ctx

        direct_ctx = direct_provider(req.question, req.token_budget)
        if direct_ctx is None:
            raise RuntimeError("ask context ladder exhausted without a prompt context")
        self.mark_ask_fallback(direct_ctx, req, "direct_llm", symbol_error)
        return direct_ctx

    @staticmethod
    def context_file_paths(ctx: PromptContext) -> list[str]:
        paths: list[str] = []
        seen: set[str] = set()
        for sym in [ctx.primary_source, *ctx.graph_context]:
            fp = getattr(sym, "file_path", "") or ""
            if fp and fp not in ("<none>", "<unknown>", "<workspace>") and fp not in seen:
                seen.add(fp)
                paths.append(fp)
        return paths
