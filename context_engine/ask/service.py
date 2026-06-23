"""Ask endpoint orchestration: LLM routing, trace metadata, feedback snapshots."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Generator
from typing import Any, cast

from context_engine.ai.engine import AIEngine
from context_engine.api.errors import (
    LLM_UNREACHABLE_REASON,
    PUBLIC_INTERNAL_ERROR,
    degraded_llm_answer,
)
from context_engine.api.schemas import (
    AskAxisRequest,
    AskAxisResponse,
    AskRequest,
    AxisCandidateResponse,
    AxisContextBundleResponse,
    AxisContextSymbolResponse,
    AxisIntentMatchResponse,
)
from context_engine.api.sse import format_sse
from context_engine.ask.context_builder import AskContextBuilder
from context_engine.auth import AuditLog
from context_engine.cache.layered import LayeredCache
from context_engine.context_types import CONTEXT_PIPELINE_VERSION, PromptContext
from context_engine.feedback import FeedbackStore, RetrievalSnapshot
from context_engine.index_profile import effective_index_workspace_id
from context_engine.observability import (
    MetricsRegistry,
    RequestTrace,
    default_metrics,
    estimate_cost_usd,
    estimate_text_tokens,
    new_trace_id,
)
from context_engine.overlay import InMemoryOverlay

logger = logging.getLogger(__name__)


class AskService:
    """Run /ask, /ask/stream, and /ask/axis with trace and feedback side effects."""

    def __init__(
        self,
        *,
        overlay: InMemoryOverlay,
        ai_engine: AIEngine,
        audit_log: AuditLog,
        feedback_store: FeedbackStore,
        context_builder: AskContextBuilder,
        default_cache: LayeredCache,
        metrics: MetricsRegistry | None = None,
        model_preference: str = "auto",
    ):
        self.overlay = overlay
        self.ai_engine = ai_engine
        self.audit_log = audit_log
        self.feedback_store = feedback_store
        self.context_builder = context_builder
        self.default_cache = default_cache
        self.metrics = metrics if metrics is not None else default_metrics
        self.model_preference = model_preference

    def start_trace(
        self,
        endpoint: str,
        x_trace_id: Any,
        workspace_id: str,
        *,
        header_value: Callable[[Any], str | None],
    ) -> RequestTrace:
        return RequestTrace(
            trace_id=new_trace_id(header_value(x_trace_id)),
            endpoint=endpoint,
            workspace_id=workspace_id,
        )

    def model_route(self, token_count: int, intent: str) -> dict[str, Any]:
        route = getattr(self.ai_engine, "route", None)
        if callable(route):
            return cast(dict[str, Any], route(token_count=token_count, intent=intent))
        return {
            "provider": getattr(self.ai_engine, "model_preference", self.model_preference),
            "model": getattr(self.ai_engine, "ollama_model", "unknown"),
            "preference": getattr(self.ai_engine, "model_preference", self.model_preference),
            "reason": "route_method_unavailable",
        }

    def last_model_route(self, default: dict[str, Any]) -> dict[str, Any]:
        last_route = getattr(self.ai_engine, "last_route", None)
        return cast(dict[str, Any], last_route) if isinstance(last_route, dict) else default

    @staticmethod
    def attach_trace_metadata(ctx: PromptContext, trace: RequestTrace) -> None:
        ctx.trace_id = trace.trace_id
        ctx.workspace_id = trace.workspace_id
        ctx.stage_timings_ms = dict(trace.stage_timings_ms)
        ctx.token_counts = dict(trace.token_counts)
        ctx.model_route = dict(trace.model_route)
        ctx.estimated_cost_usd = trace.estimated_cost_usd
        ctx.cost_basis = trace.cost_basis
        ctx.context_pipeline_version = CONTEXT_PIPELINE_VERSION

    @staticmethod
    def index_manifest_fields(db: Any, workspace_id: str) -> tuple[str | None, int | None]:
        get_m = getattr(db, "get_index_manifest", None)
        if not callable(get_m):
            return None, None
        try:
            raw = get_m(workspace_id=workspace_id)
        except TypeError:
            raw = get_m(workspace_id)
        if not isinstance(raw, dict):
            return None, None
        mid = raw.get("manifest_id")
        manifest_id = str(mid) if mid else None
        schema_v: int | None = None
        sv = raw.get("manifest_schema_version")
        if sv is not None:
            try:
                schema_v = int(sv)
            except (TypeError, ValueError):
                pass
        return manifest_id, schema_v

    def attach_index_manifest(self, ctx: PromptContext, db: Any, workspace_id: str) -> None:
        mid, sv = self.index_manifest_fields(db, workspace_id)
        if mid:
            ctx.index_manifest_id = mid
        if sv is not None:
            ctx.index_manifest_schema_version = sv

    @staticmethod
    def request_metrics(trace: RequestTrace) -> dict[str, Any]:
        return {
            "stage_timings_ms": dict(trace.stage_timings_ms),
            "latency_slo": trace.latency_slo(),
            "token_counts": dict(trace.token_counts),
            "estimated_cost_usd": trace.estimated_cost_usd,
            "cost_basis": trace.cost_basis,
        }

    @staticmethod
    def stream_trace_payload(
        trace: RequestTrace,
        *,
        stage: str | None = None,
        ctx: PromptContext | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "trace",
            "trace_id": trace.trace_id,
        }
        if stage:
            payload["stage"] = stage
            elapsed = trace.stage_timings_ms.get(stage)
            if elapsed is not None:
                payload["elapsed_ms"] = elapsed
        if ctx is not None:
            cache_hits = getattr(ctx, "budget", {}).get("cache_hits")
            if cache_hits:
                payload["cache_hits"] = list(cache_hits)
        if trace.model_route:
            payload["model_route"] = dict(trace.model_route)
        return payload

    @staticmethod
    def degraded_llm_answer(exc: Exception) -> str:
        logger.warning("LLM unreachable, returning degraded context-only response: %s", exc)
        return degraded_llm_answer()

    @staticmethod
    def mark_degraded_route(route: dict[str, Any], exc: Exception) -> dict[str, Any]:
        return {
            **route,
            "degraded": True,
            "reason": LLM_UNREACHABLE_REASON,
        }

    @staticmethod
    def candidate_record(symbol: Any) -> dict[str, Any]:
        return {
            "symbol": getattr(symbol, "symbol", ""),
            "file_path": getattr(symbol, "file_path", ""),
            "relation": getattr(symbol, "relation", ""),
            "direction": getattr(symbol, "direction", ""),
            "depth": getattr(symbol, "depth", 0),
            "relevance_score": getattr(symbol, "relevance_score", 0.0),
            "is_dirty": getattr(symbol, "is_dirty", False),
        }

    @staticmethod
    def doc_record(doc: Any) -> dict[str, Any]:
        return {
            "chunk_id": getattr(doc, "chunk_id", ""),
            "source_file": getattr(doc, "source_file", ""),
            "score": getattr(doc, "score", None),
            "provenance": getattr(doc, "provenance", []),
            "anchor_type": getattr(doc, "anchor_type", ""),
            "anchor_confidence": getattr(doc, "anchor_confidence", 0.0),
            "primary_bias": getattr(doc, "primary_bias", 0.0),
        }

    def record_retrieval_snapshot(
        self,
        *,
        feedback_token: str,
        user_id: str,
        workspace_id: str,
        symbol: str,
        question: str,
        ctx: Any,
        trace: RequestTrace,
    ) -> None:
        selected = [self.candidate_record(ctx.primary_source)]
        selected.extend(
            self.candidate_record(candidate) for candidate in getattr(ctx, "graph_context", [])
        )
        documentation = [self.doc_record(doc) for doc in getattr(ctx, "documentation", [])]
        snapshot = RetrievalSnapshot(
            feedback_token=feedback_token,
            workspace_id=workspace_id,
            user_id=user_id,
            trace_id=trace.trace_id,
            symbol=symbol,
            intent=str(getattr(ctx, "intent", "")),
            mode=str(getattr(ctx, "mode", "")),
            question_hash=hashlib.sha256(question.encode()).hexdigest(),
            question_tokens=trace.token_counts.get("user", estimate_text_tokens(question)),
            context_pipeline_version=getattr(
                ctx, "context_pipeline_version", CONTEXT_PIPELINE_VERSION
            ),
            selected_candidates=selected,
            documentation=documentation,
            context_metadata={
                "budget": getattr(ctx, "budget", {}),
                "tier_tokens": getattr(ctx, "tier_tokens", {}),
                "token_counts": dict(trace.token_counts),
                "model_route": dict(trace.model_route),
                "estimated_cost_usd": trace.estimated_cost_usd,
                "cost_basis": trace.cost_basis,
            },
        )
        self.feedback_store.record_snapshot(snapshot)
        self.metrics.increment(
            "sidecar_feedback_snapshots_total",
            labels={"workspace": workspace_id},
        )

    @staticmethod
    def system_prompt_for_context(ctx: PromptContext) -> str:
        if ctx.mode == "direct":
            return (
                "You are a Surgical Code Assistant. No codebase context was retrieved for this "
                "question. Answer from general engineering knowledge, and clearly state when a "
                "claim would need codebase verification."
            )
        if ctx.mode in {"file", "workspace"}:
            return (
                "You are a Surgical Code Assistant. Use the retrieved context when it is relevant. "
                "If the context is incomplete, keep the answer practical and mark assumptions.\n\n"
                f"{ctx.to_system_prompt()}"
            )
        return (
            "You are a Surgical Code Assistant. Use ONLY the provided context.\n\n"
            f"{ctx.to_system_prompt()}"
        )

    def ask(
        self,
        req: AskRequest,
        *,
        user_id: str,
        workspace_id: str,
        trace: RequestTrace,
        db: Any,
        resolve_ask_context: Callable[..., PromptContext],
    ) -> dict[str, Any]:
        with trace.stage("context"):
            ctx = resolve_ask_context(
                req=req,
                user_id=user_id,
                workspace_id=workspace_id,
                db=db,
            )
            ask_anchor = ctx.primary_source.symbol
            self.metrics.increment(
                "sidecar_ask_context_total",
                labels={"mode": ctx.mode, "workspace": workspace_id},
            )

        with trace.stage("prompt"):
            system_prompt = self.system_prompt_for_context(ctx)
            context_tokens = ctx.token_count()
            trace.token_counts = {
                "context": context_tokens,
                "user": estimate_text_tokens(req.question),
            }
            trace.model_route = self.model_route(context_tokens, ctx.intent)

        with trace.stage("llm"):
            response_cache_hit = False
            degraded_response = False
            prompt_hash = hashlib.sha256(f"{system_prompt}\n{req.question}".encode()).hexdigest()
            cached_response = self.default_cache.get_response(prompt_hash, workspace_id)
            if cached_response:
                response_cache_hit = True
                answer = cached_response.answer
                if hasattr(ctx, "budget"):
                    ctx.budget["cache_hits"] = sorted(
                        {*ctx.budget.get("cache_hits", []), "l3_response"}
                    )
                trace.model_route = {
                    **trace.model_route,
                    "cached": True,
                    "cache_layer": "l3_response",
                }
            else:
                try:
                    answer = self.ai_engine.chat(
                        system_prompt=system_prompt,
                        user_message=req.question,
                        token_count=context_tokens,
                        intent=ctx.intent,
                    )
                except RuntimeError as exc:
                    degraded_response = True
                    answer = self.degraded_llm_answer(exc)
                    trace.model_route = self.mark_degraded_route(trace.model_route, exc)
                    self.metrics.increment(
                        "sidecar_llm_degraded_total",
                        labels={"endpoint": "/ask", "workspace": workspace_id},
                    )
                else:
                    self.default_cache.put_response(
                        prompt_hash,
                        workspace_id,
                        answer,
                        {"intent": ctx.intent, "mode": ctx.mode},
                        file_paths=AskContextBuilder.context_file_paths(ctx),
                    )
        if not response_cache_hit and not degraded_response:
            trace.model_route = self.last_model_route(trace.model_route)

        output_tokens = estimate_text_tokens(answer)
        trace.token_counts["output_estimate"] = output_tokens
        trace.estimated_cost_usd, trace.cost_basis = estimate_cost_usd(
            trace.model_route,
            input_tokens=context_tokens + trace.token_counts["user"],
            output_tokens=output_tokens,
        )

        with trace.stage("audit"):
            self.audit_log.log_query(user_id, ask_anchor, req.question, ctx.intent, ctx.mode)

        self.attach_trace_metadata(ctx, trace)
        self.attach_index_manifest(ctx, db, effective_index_workspace_id(workspace_id))
        feedback_token = self.feedback_store.issue_token()
        ctx.feedback_token = feedback_token
        with trace.stage("feedback_snapshot"):
            self.record_retrieval_snapshot(
                feedback_token=feedback_token,
                user_id=user_id,
                workspace_id=workspace_id,
                symbol=ask_anchor,
                question=req.question,
                ctx=ctx,
                trace=trace,
            )
        logger.info("trace_id=%s endpoint=/ask status=ok", trace.trace_id)
        return {
            "symbol": ask_anchor,
            "answer": answer,
            "context": ctx.to_dict(),
            "user": user_id,
            "cloud": db.is_cloud(),
            "workspace_id": workspace_id,
            "trace_id": trace.trace_id,
            "feedback_token": feedback_token,
            "model_route": trace.model_route,
            "metrics": self.request_metrics(trace),
            "index_manifest_id": ctx.index_manifest_id or None,
            "index_manifest_schema_version": ctx.index_manifest_schema_version,
        }

    def ask_stream(
        self,
        req: AskRequest,
        *,
        user_id: str,
        workspace_id: str,
        trace: RequestTrace,
        db: Any,
        resolve_ask_context: Callable[..., PromptContext],
    ) -> Generator[str, None, None]:
        answer_parts: list[str] = []
        yield format_sse("trace", {"type": "trace", "trace_id": trace.trace_id})
        status = "ok"
        try:
            with trace.stage("context"):
                ctx = resolve_ask_context(
                    req=req,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    db=db,
                )
                ask_anchor = ctx.primary_source.symbol
                self.metrics.increment(
                    "sidecar_ask_context_total",
                    labels={"mode": ctx.mode, "workspace": workspace_id},
                )

            with trace.stage("prompt"):
                system_prompt = self.system_prompt_for_context(ctx)
                context_tokens = ctx.token_count()
                trace.token_counts = {
                    "context": context_tokens,
                    "user": estimate_text_tokens(req.question),
                }
                trace.model_route = self.model_route(context_tokens, ctx.intent)

            with trace.stage("llm"):
                response_cache_hit = False
                degraded_response = False
                prompt_hash = hashlib.sha256(
                    f"{system_prompt}\n{req.question}".encode()
                ).hexdigest()
                cached_response = self.default_cache.get_response(prompt_hash, workspace_id)
                if cached_response:
                    response_cache_hit = True
                    answer_parts.append(cached_response.answer)
                    if hasattr(ctx, "budget"):
                        ctx.budget["cache_hits"] = sorted(
                            {*ctx.budget.get("cache_hits", []), "l3_response"}
                        )
                    trace.model_route = {
                        **trace.model_route,
                        "cached": True,
                        "cache_layer": "l3_response",
                    }
                    yield format_sse(
                        "trace",
                        self.stream_trace_payload(trace, stage="llm", ctx=ctx),
                    )
                    yield format_sse("chunk", {"type": "chunk", "content": cached_response.answer})
                else:
                    try:
                        for chunk in self.ai_engine.stream_chat(
                            system_prompt=system_prompt,
                            user_message=req.question,
                            token_count=context_tokens,
                            intent=ctx.intent,
                        ):
                            answer_parts.append(chunk)
                            yield format_sse("chunk", {"type": "chunk", "content": chunk})
                    except RuntimeError as exc:
                        degraded_response = True
                        degraded_text = self.degraded_llm_answer(exc)
                        answer_parts.append(degraded_text)
                        trace.model_route = self.mark_degraded_route(trace.model_route, exc)
                        self.metrics.increment(
                            "sidecar_llm_degraded_total",
                            labels={
                                "endpoint": "/ask/stream",
                                "workspace": workspace_id,
                            },
                        )
                        yield format_sse("chunk", {"type": "chunk", "content": degraded_text})
                    else:
                        self.default_cache.put_response(
                            prompt_hash,
                            workspace_id,
                            "".join(answer_parts),
                            {"intent": ctx.intent, "mode": ctx.mode},
                            file_paths=AskContextBuilder.context_file_paths(ctx),
                        )
            if not response_cache_hit and not degraded_response:
                trace.model_route = self.last_model_route(trace.model_route)

            output_tokens = estimate_text_tokens("".join(answer_parts))
            trace.token_counts["output_estimate"] = output_tokens
            trace.estimated_cost_usd, trace.cost_basis = estimate_cost_usd(
                trace.model_route,
                input_tokens=context_tokens + trace.token_counts["user"],
                output_tokens=output_tokens,
            )

            with trace.stage("audit"):
                self.audit_log.log_query(user_id, ask_anchor, req.question, ctx.intent, ctx.mode)

            self.attach_trace_metadata(ctx, trace)
            self.attach_index_manifest(ctx, db, workspace_id)
            feedback_token = self.feedback_store.issue_token()
            ctx.feedback_token = feedback_token
            with trace.stage("feedback_snapshot"):
                self.record_retrieval_snapshot(
                    feedback_token=feedback_token,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    symbol=ask_anchor,
                    question=req.question,
                    ctx=ctx,
                    trace=trace,
                )
            yield format_sse(
                "context",
                {
                    "type": "context",
                    "trace_id": trace.trace_id,
                    "feedback_token": feedback_token,
                    "context": ctx.to_dict(),
                    "metrics": self.request_metrics(trace),
                    "index_manifest_id": ctx.index_manifest_id or None,
                    "index_manifest_schema_version": ctx.index_manifest_schema_version,
                },
            )
            yield format_sse("done", {"type": "done", "trace_id": trace.trace_id})
        except Exception:
            status = "error"
            logger.exception("trace_id=%s endpoint=/ask/stream status=error", trace.trace_id)
            yield format_sse(
                "error",
                {
                    "type": "error",
                    "error": PUBLIC_INTERNAL_ERROR,
                    "trace_id": trace.trace_id,
                },
            )
        finally:
            self.metrics.record_trace(trace, status)

    def ask_axis(
        self,
        req: AskAxisRequest,
        *,
        user_id: str,
        base_workspace_id: str,
        trace: RequestTrace,
        db: Any,
    ) -> AskAxisResponse:
        from context_engine.axis.pipeline import run_axis_retrieval

        index_workspace_id = effective_index_workspace_id(base_workspace_id)
        result = run_axis_retrieval(
            req.question,
            workspace_id=index_workspace_id,
            db=db,
            lance=self.context_builder.lance_for_index_workspace(index_workspace_id),
            top_roles=req.top_roles,
            per_role_limit=req.per_role_limit,
            intent_threshold=req.intent_threshold,
            with_context=req.with_context,
            context_per_seed=req.context_per_seed,
            context_seeds_per_role=req.context_seeds_per_role,
            intent_budget=req.intent_budget,
            base_token_budget=req.token_budget,
            trace=trace,
            overlay=self.overlay,
            user_id=user_id,
        )

        intent_payload = [
            AxisIntentMatchResponse(
                role=m.role,
                similarity=m.similarity,
                description=m.description,
            )
            for m in result.intent
        ]

        candidates_by_role: dict[str, list[AxisCandidateResponse]] = {}
        intent_role_order = [m.role for m in result.intent]
        promoted_roles = [r for r in result.raw_by_role if r not in set(intent_role_order)]
        for role in intent_role_order + promoted_roles:
            candidates = result.raw_by_role.get(role) or []
            if not candidates:
                continue
            candidates_by_role[role] = [
                AxisCandidateResponse(
                    uid=c.uid,
                    name=c.name,
                    file_path=c.file_path,
                    role=c.role,
                    satisfying_contracts=list(c.satisfying_contracts),
                    satisfying_kinds=list(c.satisfying_kinds),
                    contract_count=c.contract_count,
                    kind_count=c.kind_count,
                    vector_distance=c.vector_distance,
                    score=c.score,
                )
                for c in candidates
            ]

        bundles_payload: list[AxisContextBundleResponse] = [
            AxisContextBundleResponse(
                role=bundle.role,
                seed=AxisContextSymbolResponse(**bundle.seed.to_dict()),
                related=[AxisContextSymbolResponse(**s.to_dict()) for s in bundle.related],
            )
            for bundle in result.bundles
        ]

        logger.info(
            "trace_id=%s endpoint=/ask/axis status=ok roles=%d candidates=%d bundles=%d",
            trace.trace_id,
            len(intent_payload),
            sum(len(v) for v in candidates_by_role.values()),
            len(bundles_payload),
        )
        return AskAxisResponse(
            question=req.question,
            workspace_id=base_workspace_id,
            user=user_id,
            intent_matches=intent_payload,
            candidates_by_role=candidates_by_role,
            context_bundles=bundles_payload,
        )
