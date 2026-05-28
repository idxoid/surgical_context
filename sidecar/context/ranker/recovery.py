"""Structural recovery: trace anchors, import bridging, role-surface heuristics."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from pathlib import Path

from sidecar.context.role_taxonomy import infer_identity_trace_roles, normalize_roles
from sidecar.context.types import SubgraphNode

from .candidate_pool import Candidate
from .scoring import RankerScoring
from .signal_constants import (
    API_SIGNAL_TOKENS,
    FACTORY_SIGNAL_PATH_TOKENS,
    FACTORY_SIGNAL_PREFIXES,
    FACTORY_SIGNAL_TOKENS,
    HOOK_FLOW_PATH_TOKENS,
    HOOK_FLOW_TARGET_TOKENS,
    HOOK_RUNTIME_TOKENS,
    IDENTITY_ENGINE_PATH_MARKERS,
    IDENTITY_TRACE_EXECUTOR_NAMES,
    IDENTITY_TRACE_ORCHESTRATOR_NAMES,
    NOISE_PATH_PATTERNS,
    REGISTRATION_FACTORY_TOKENS,
    REGISTRATION_FLOW_PATH_TOKENS,
    REGISTRATION_FLOW_TARGET_TOKENS,
    REGISTRATION_REPRESENTATION_TOKENS,
    REGISTRATION_RUNTIME_TOKENS,
    REPRESENTATION_SIGNAL_PATH_TOKENS,
    REPRESENTATION_SIGNAL_TOKENS,
    ROUTING_COMPOSITION_SYMBOL_NAMES,
    ROUTING_FLOW_PATH_TOKENS,
    ROUTING_FLOW_TARGET_TOKENS,
    RUNTIME_SIGNAL_TOKENS,
    TRACE_CONSUME_PATH_PENALTIES,
    TRACE_CONSUME_RUNTIME_NAMES,
    TRACE_CONSUME_SCOPE_SEGMENT,
    TRACE_CONSUME_TARGET_NAMES,
    TRACE_DEPENDENCY_RUNTIME_NAME_TOKENS,
    TRACE_DEPENDENCY_TARGET_TOKENS,
    TRACE_EXECUTION_SIBLING_FILE_MARKERS,
    TRACE_HOOK_RUNTIME_NAMES,
    TRACE_HOOK_RUNTIME_TRIGGER_NAMES,
    MANDATORY_CALLEE_RELATION,
    THIN_DISPATCH_MAX_CHAIN_CALLEES,
    THIN_DISPATCH_MAX_MANDATORY_CALLEES,
    THIN_DISPATCH_MAX_TOKEN_ESTIMATE,
    TRACE_PUBLISH_APP_METHOD_NAMES,
    TRACE_PUBLISH_RUNTIME_NAMES,
    TRACE_PUBLISH_SCOPE_SEGMENT,
    TRACE_TASK_REGISTRATION_TARGET_NAMES,
)


class StructuralRecovery:
    def __init__(self, host):
        self.host = host
        self.db = host.db
        self.workspace_id = host.workspace_id

    def generic_role_recovery_candidates(
        self,
        target: SubgraphNode,
        roles: list[str],
        *,
        excluded_uids: set[str],
    ) -> list[Candidate]:
        scoped_roles = set(normalize_roles(roles))
        if not scoped_roles:
            return []

        rows: list[tuple[str, dict]] = []
        rows.extend(
            ("same_file", row)
            for row in self.host._same_file_symbol_rows(
                target.file_path, excluded_uids=excluded_uids
            )
        )
        rows.extend(
            ("imported_file", row)
            for row in self.host._imported_symbol_rows(
                target.file_path, excluded_uids=excluded_uids
            )
        )
        if not rows:
            return []

        candidates: list[Candidate] = []
        for origin, row in rows:
            candidate = self.host._recovery_candidate_from_row(
                row,
                origin=origin,
                scoped_roles=scoped_roles,
                target=target,
            )
            if candidate is not None:
                candidates.append(candidate)

        deduped: dict[str, Candidate] = {}
        for candidate in candidates:
            existing = deduped.get(candidate.uid)
            if existing is None or existing.graph_score < candidate.graph_score:
                deduped[candidate.uid] = candidate
        return list(deduped.values())

    def first_reasoning_role(self, required_roles: list[str]) -> str:
        for role in normalize_roles(required_roles):
            if role != "docs_or_concept":
                return role
        return "supporting_surface"

    def rank_rows_for_trace_import_anchors(
        self,
        rows: list[dict],
        *,
        max_per_file: int = 4,
        max_total: int = 36,
    ) -> list[dict]:
        """Prefer structural hubs per imported file, cap total work."""
        by_file: dict[str, list[dict]] = {}
        for row in rows:
            fp = row.get("file_path") or ""
            by_file.setdefault(fp, []).append(row)
        picked: list[dict] = []

        def _file_rank(item: tuple[str, list[dict]]) -> tuple[float, float, str]:
            fp, rows_for_file = item
            anchor = max(float(row.get("trace_anchor_score", 0) or 0) for row in rows_for_file)
            anchor += max(self._trace_execution_sibling_file_bonus(row) for row in rows_for_file)
            edges = max(
                float(row.get("inbound_edges", 0) or 0) + float(row.get("outbound_edges", 0) or 0)
                for row in rows_for_file
            )
            return (anchor, edges, fp)

        for _fp, rows_for_file_all in sorted(by_file.items(), key=_file_rank, reverse=True):
            rows_for_fp = sorted(
                rows_for_file_all,
                key=lambda r: (
                    float(r.get("trace_anchor_score", 0) or 0) + self._trace_execution_sibling_file_bonus(r),
                    float(r.get("inbound_edges", 0) or 0) + float(r.get("outbound_edges", 0) or 0),
                    str(r.get("name") or ""),
                ),
                reverse=True,
            )[:max_per_file]
            picked.extend(rows_for_fp)
        return picked[:max_total]

    def minimal_trace_import_anchor_candidate(
        self,
        row: dict,
        *,
        required_roles: list[str],
    ) -> Candidate:
        """Seat imported-module symbols when catalog roles miss strict recovery overlap."""
        raw_tc = int(row.get("token_estimate") or 0) or self.host._estimate_tokens_range(
            row.get("range") or [0, 0]
        )
        token_cost = min(raw_tc, 140)
        edge_bonus = 0.08 * math.log1p(float(row.get("inbound_edges", 0) or 0)) + 0.10 * math.log1p(
            float(row.get("outbound_edges", 0) or 0)
        )
        trace_anchor_score = float(row.get("trace_anchor_score", 0) or 0)
        identity_roles = infer_identity_trace_roles(
            file_path=row.get("file_path") or "",
            name=row.get("name") or "",
        )
        scoped = set(normalize_roles(required_roles))
        if identity_roles and trace_anchor_score > 0:
            matched = [role for role in identity_roles if role in scoped]
            role = matched[0] if matched else identity_roles[0]
            supporting = [r for r in identity_roles if r != role and r in scoped]
        elif trace_anchor_score > 0:
            role = self.first_reasoning_role(required_roles)
            supporting = []
        else:
            role = "supporting_surface"
            supporting = []
        candidate = Candidate(
            kind="symbol",
            uid=str(row["uid"]),
            token_cost=token_cost,
            graph_score=1.18 + edge_bonus,
            semantic_score=0.52,
            name=row.get("name") or "",
            file_path=row.get("file_path") or "",
            range=row.get("range") or [0, 0],
            render_mode="signature_only",
            relation="ROLE_BACKFILL",
            direction="backfill",
            depth=2,
            file_hash=row.get("file_hash") or "",
            evidence_role=role,
            supporting_roles=normalize_roles(supporting),
            provenance=["recovery:import-module-trace"],
        )
        candidate.symbol_kind = row.get("symbol_kind", "")
        candidate.qualified_name = row.get("qualified_name", "")
        return candidate

    def package_root_prefix(self, file_path: str) -> str | None:
        """Directory name that owns the module file → ``…/fastapi/x.py`` → ``fastapi/``.

        Using only the first path segment would turn ``/repo/fastapi/x.py`` into
        ``repo/`` and miss ``fastapi/dependencies/*.py``.
        """
        norm = (file_path or "").replace("\\", "/").strip("/")
        if "/" not in norm:
            return None
        parent = norm.rsplit("/", 1)[0]
        pkg = parent.split("/")[-1].strip()
        if not pkg or pkg.startswith("."):
            return None
        return f"{pkg}/"

    def source_scope_prefixes(self, file_path: str) -> list[str]:
        """Source-root prefixes for trace recovery.

        Absolute benchmark paths make substring package checks too broad:
        ``.../fastapi/tests`` and ``.../fastapi/fastapi`` both contain
        ``/fastapi/``. Prefer the nearest production root instead.
        """
        norm = (file_path or "").replace("\\", "/").strip()
        if "/" not in norm:
            return []

        parts = norm.split("/")
        prefixes: list[str] = []

        for marker in ("src", "lib"):
            if marker not in parts:
                continue
            idx = parts.index(marker)
            if idx + 1 >= len(parts):
                continue
            next_part = parts[idx + 1]
            # ``lib/express.js`` means the root is ``lib/``; ``lib/sqlalchemy/``
            # means package-scoped source.
            end_idx = idx + 1 if "." in next_part else idx + 2
            prefixes.append("/".join(parts[:end_idx]) + "/")

        parent = norm.rsplit("/", 1)[0]
        file_name = norm.rsplit("/", 1)[-1]
        file_stem = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
        if "/" in parent:
            grandparent = parent.rsplit("/", 1)[0]
            parent_dir_name = parent.rsplit("/", 1)[-1]
            if parent_dir_name == grandparent.rsplit("/", 1)[-1]:
                # foo/foo/ pattern — scope to the repeated dir
                prefixes.append(parent + "/")
            elif file_stem == parent_dir_name:
                # Subpackage entry point: consumer/consumer.py, worker/worker.py, etc.
                # Siblings live in the grandparent dir, so widen scope there.
                prefixes.append(grandparent + "/")

        if not prefixes:
            prefixes.append(parent + "/")

        out: list[str] = []
        seen: set[str] = set()
        for prefix in prefixes:
            if prefix and prefix not in seen:
                seen.add(prefix)
                out.append(prefix)
        return out

    @staticmethod
    def trace_query_terms(query: str, target: SubgraphNode) -> list[str]:
        path = Path((target.file_path or "").replace("\\", "/"))
        path_terms: list[str] = []
        file_stem = path.stem.lower()
        generic_file_stems = {"__init__", "index", "main", "utils", "types", "models"}
        if file_stem and file_stem not in generic_file_stems:
            path_terms.append(file_stem)
        parts = [part.lower() for part in path.parts if part not in {"/", ""}]
        if len(parts) >= 2:
            parent = parts[-2]
            grandparent = parts[-3] if len(parts) >= 3 else ""
            generic_dirs = {"src", "lib", "app", "apps", "packages", "pkg"}
            if parent and parent != grandparent and parent not in generic_dirs:
                path_terms.append(parent)

        text = f"{target.name or ''} {' '.join(path_terms)} {query or ''}".lower()
        stop = {
            "before",
            "called",
            "does",
            "during",
            "from",
            "function",
            "gets",
            "handle",
            "with",
        }
        terms = {term for term in re.findall(r"[a-z_][a-z0-9_]{3,}", text) if term not in stop}
        if any(term in text for term in ("depend", "inject")):
            terms.update(
                {
                    "container",
                    "depend",
                    "dependant",
                    "dependency",
                    "inject",
                    "injector",
                    "instance",
                    "module",
                    "provider",
                    "resolve",
                    "resolver",
                    "solve",
                    "wrapper",
                }
            )
        if any(term in text for term in ("route", "routing", "dispatch", "middleware", "handler")):
            terms.update({"route", "router", "dispatch", "middleware", "handle"})
        if "delegate" in text:
            terms.update({"router", "dispatch", "handle", "middleware"})
        if any(term in text for term in HOOK_FLOW_TARGET_TOKENS):
            terms.update(HOOK_FLOW_TARGET_TOKENS)
            terms.update(HOOK_RUNTIME_TOKENS)
        if any(
            term in text for term in ("relationship", "foreign", "lazy", "loading", "collection")
        ):
            terms.update(
                {
                    "relationship",
                    "relationships",
                    "foreign",
                    "collection",
                    "lazy",
                    "loader",
                    "strategy",
                    "strategies",
                }
            )
        if "render" in text and any(term in text for term in ("compile", "template", "dom")):
            terms.update({"compile", "patch", "renderer"})
        if any(term in text for term in ("webview", "extension", "vscode", "provider")):
            terms.update({"webview", "provider", "activate", "register", "handler"})
        if "sql" in text and any(term in text for term in ("query", "statement", "execute")):
            terms.update({"select", "clause", "compile", "compiler", "statement", "sql"})
        if any(term in text for term in ("actor", "identity", "principal", "same_actor")):
            terms.update(
                {
                    "actor",
                    "identity",
                    "principal",
                    "same_actor",
                    "ingest",
                    "index",
                }
            )
        if any(term in text for term in ("gate", "lifecycle", "ingested", "event_time", "clock")):
            terms.update({"gate", "ingest", "engine", "chain", "window", "time"})
        if any(term in text for term in ("broker", "publish", "enqueue")) and any(
            term in text for term in ("message", "task", "send", "sent")
        ):
            terms.update({"producer", "publish", "channel", "connection", "backend", "send"})
        if any(term in text for term in ("execute", "receive", "process")) and any(
            term in text for term in ("worker", "task", "message", "job")
        ):
            terms.update({"request", "strategy", "handler", "pool", "invoke"})
        return sorted(term for term in terms if len(term) >= 4)

    @staticmethod
    def _normalized_path(file_path: str) -> str:
        return (file_path or "").replace("\\", "/")

    @staticmethod
    def _path_scope_through_segment(file_path: str, segment: str) -> str | None:
        """Return path prefix through ``.../segment/`` when present as a directory."""
        norm = StructuralRecovery._normalized_path(file_path)
        marker = f"/{segment.strip('/')}/"
        idx = norm.find(marker)
        if idx < 0:
            return None
        return norm[: idx + len(marker)]

    @staticmethod
    def _trace_execution_sibling_file_bonus(row: dict) -> float:
        fp = (row.get("file_path") or "").replace("\\", "/").lower()
        return (
            3.0
            if any(marker in fp for marker in TRACE_EXECUTION_SIBLING_FILE_MARKERS)
            else 0.0
        )

    def is_message_publish_trace_target(
        self,
        target: SubgraphNode,
        mechanism: str = "",
    ) -> bool:
        """App-layer dispatch entrypoints that delegate to publisher/broker runtime."""
        _ = mechanism  # reserved for future mechanism-specific scopes
        name = (target.name or "").lower()
        path = self._normalized_path(target.file_path).lower()
        if name not in TRACE_PUBLISH_APP_METHOD_NAMES:
            return False
        return TRACE_PUBLISH_SCOPE_SEGMENT in path or path.endswith("/app.py")

    def is_worker_consume_trace_target(
        self,
        target: SubgraphNode,
        mechanism: str = "",
        query: str = "",
    ) -> bool:
        """Worker-side consumer targets (not result-backend consumers)."""
        m = (mechanism or "").lower()
        q = (query or "").lower()
        name = (target.name or "").strip()
        if name.lower() not in {n.lower() for n in TRACE_CONSUME_TARGET_NAMES}:
            return False
        if m.endswith("_consume") or "consume" in m:
            return True
        if not any(term in q for term in ("worker", "execute", "receive", "broker", "message")):
            return False
        path = self._normalized_path(target.file_path).lower()
        if TRACE_CONSUME_SCOPE_SEGMENT in path:
            return True
        if any(penalty in path for penalty in TRACE_CONSUME_PATH_PENALTIES):
            return bool(
                any(term in q for term in ("worker", "execute", "receive"))
                and "broker" in q
            )
        return "execute" in q and "receive" in q

    def is_task_registration_target(
        self,
        target: SubgraphNode,
        mechanism: str = "",
        query: str = "",
    ) -> bool:
        """Celery decorator registration entrypoint, even when lookup lands on a homonym."""
        name = (target.name or "").strip()
        if name not in TRACE_TASK_REGISTRATION_TARGET_NAMES:
            return False
        m = (mechanism or "").lower()
        q = (query or "").lower()
        if "task_registration" in m:
            return True
        return bool(
            "@app.task" in q
            or (
                "decorator" in q
                and "register" in q
                and any(term in q for term in ("registry", "worker", "task"))
            )
        )

    def trace_runtime_scope_prefixes(
        self,
        target: SubgraphNode,
        *,
        mechanism: str = "",
        query: str = "",
    ) -> list[str]:
        """Scope prefixes for runtime-row lookup (may widen beyond target file)."""
        prefixes = list(self.source_scope_prefixes(target.file_path or ""))
        path = self._normalized_path(target.file_path)
        seen: set[str] = set(prefixes)
        if self.is_message_publish_trace_target(target, mechanism):
            app_scope = self._path_scope_through_segment(path, "app")
            if app_scope and app_scope not in seen:
                seen.add(app_scope)
                prefixes.append(app_scope)
        if self.is_worker_consume_trace_target(target, mechanism, query):
            worker_scope = self._path_scope_through_segment(path, "worker")
            if not worker_scope:
                worker_scope = self._infer_worker_scope_from_path(path)
            if worker_scope and worker_scope not in seen:
                seen.add(worker_scope)
                prefixes.append(worker_scope)
        if self.is_task_registration_target(target, mechanism, query):
            app_scope = self._path_scope_through_segment(path, "app")
            if app_scope and app_scope not in seen:
                seen.add(app_scope)
                prefixes.append(app_scope)
        return prefixes

    @staticmethod
    def _infer_worker_scope_from_path(path: str) -> str | None:
        """When the target sits under backends/helpers, infer sibling ``worker/`` scope."""
        norm = StructuralRecovery._normalized_path(path)
        for segment in ("backends", "backend", "helpers", "utils"):
            marker = f"/{segment}/"
            if marker in norm:
                return norm.split(marker, 1)[0] + TRACE_CONSUME_SCOPE_SEGMENT
        pkg = norm.split("/")[0] if "/" in norm else ""
        if pkg:
            return f"{pkg}{TRACE_CONSUME_SCOPE_SEGMENT}"
        return None

    def trace_runtime_path_penalties(
        self,
        target: SubgraphNode,
        *,
        mechanism: str = "",
        query: str = "",
    ) -> list[str]:
        if self.is_worker_consume_trace_target(target, mechanism, query):
            return list(TRACE_CONSUME_PATH_PENALTIES)
        return []

    def is_subpackage_entry_target(self, target: SubgraphNode) -> bool:
        """``pkg/pkg.py`` entry modules whose siblings live in the parent directory."""
        path = self._normalized_path(target.file_path)
        if "/" not in path:
            return False
        parent, file_name = path.rsplit("/", 1)
        file_stem = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
        parent_dir = parent.rsplit("/", 1)[-1] if "/" in parent else parent
        return bool(file_stem and parent_dir and file_stem == parent_dir)

    def trace_dependency_runtime_symbol_rows(
        self,
        target: SubgraphNode,
        *,
        excluded_uids: set[str],
        mechanism: str = "",
        query: str = "",
    ) -> list[dict]:
        """Workspace symbols for trace/runtime resolution when import topology is sparse."""
        target_name = target.name or ""
        name_terms: list[str] = []
        names: list[str] = []
        if self.is_dependency_marker_target(target):
            name_terms = list(TRACE_DEPENDENCY_RUNTIME_NAME_TOKENS)
        elif target_name in TRACE_HOOK_RUNTIME_TRIGGER_NAMES:
            names = list(TRACE_HOOK_RUNTIME_NAMES)
        elif self.is_message_publish_trace_target(target, mechanism):
            names = sorted(TRACE_PUBLISH_RUNTIME_NAMES)
        elif self.is_worker_consume_trace_target(target, mechanism, query):
            names = sorted(TRACE_CONSUME_RUNTIME_NAMES | TRACE_CONSUME_TARGET_NAMES)
        else:
            return []
        excluded = set(excluded_uids)
        if target.uid:
            excluded.add(target.uid)
        scope_prefixes = self.trace_runtime_scope_prefixes(
            target, mechanism=mechanism, query=query
        )
        path_penalties = self.trace_runtime_path_penalties(
            target, mechanism=mechanism, query=query
        )
        query_cypher = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
        WHERE (
            (size($names) > 0 AND s.name IN $names)
            OR any(term IN $name_terms
                WHERE toLower(s.name) CONTAINS term
                   OR toLower(coalesce(s.qualified_name, '')) CONTAINS term)
          )
          AND NOT s.uid IN $excluded_uids
          AND (size($scope_prefixes) = 0 OR any(prefix IN $scope_prefixes WHERE f.path STARTS WITH prefix))
          AND NOT any(noise IN $noise_patterns WHERE f.path CONTAINS noise)
          AND NOT any(penalty IN $path_penalties WHERE f.path CONTAINS penalty)
        WITH s, f, c, coalesce(s.in_degree, 0) AS inbound_edges
        WITH s, f, c, inbound_edges, coalesce(s.out_degree, 0) AS outbound_edges,
             CASE
               WHEN any(term IN $name_terms WHERE toLower(f.path) CONTAINS term) THEN 2
               ELSE 0
             END
             + CASE
               WHEN any(term IN $name_terms WHERE toLower(s.name) CONTAINS term) THEN 2
               ELSE 0
             END AS anchor_score
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               outbound_edges,
               anchor_score AS trace_anchor_score,
               false AS trace_topic_anchor
        ORDER BY anchor_score DESC,
          inbound_edges + outbound_edges DESC,
          size(coalesce(f.path, '<unknown>')) ASC
        LIMIT 64
        """
        try:
            with self.db.driver.session() as session:
                return list(
                    session.run(
                        query_cypher,
                        workspace_id=self.workspace_id,
                        names=names,
                        name_terms=name_terms,
                        excluded_uids=list(excluded),
                        scope_prefixes=scope_prefixes,
                        noise_patterns=list(NOISE_PATH_PATTERNS),
                        path_penalties=path_penalties,
                    )
                )
        except Exception:
            return []

    def trace_dependency_topic_symbol_rows(
        self,
        target: SubgraphNode,
        *,
        query: str,
        excluded_uids: set[str],
        mechanism: str = "",
        max_rows: int = 64,
    ) -> list[dict]:
        """Source-local symbols whose names/paths match the trace question recipe."""
        terms = self.trace_query_terms(query, target)
        scope_prefixes = self.trace_topic_scope_prefixes(
            target.file_path or "",
            terms,
            target=target,
            mechanism=mechanism,
            query=query,
        )
        if not terms or not scope_prefixes:
            return []

        query_cypher = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
        WHERE NOT s.uid IN $excluded_uids
          AND any(prefix IN $scope_prefixes WHERE f.path STARTS WITH prefix)
          AND NOT any(noise IN $noise_patterns WHERE f.path CONTAINS noise)
          AND any(term IN $terms
            WHERE toLower(f.path) CONTAINS term
               OR toLower(s.name) CONTAINS term
               OR toLower(coalesce(s.qualified_name, '')) CONTAINS term)
        WITH s, f, c, coalesce(s.in_degree, 0) AS inbound_edges
        WITH s, f, c, inbound_edges, coalesce(s.out_degree, 0) AS outbound_edges,
             size([term IN $terms WHERE toLower(s.name) = term]) AS exact_name_hits,
             size([term IN $terms WHERE toLower(s.name) CONTAINS term]) AS name_hits,
             size([term IN $terms WHERE toLower(coalesce(s.qualified_name, '')) CONTAINS term]) AS qname_hits,
             size([term IN $terms WHERE toLower(f.path) CONTAINS term]) AS path_hits
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               outbound_edges,
               (exact_name_hits * 5 + name_hits * 2 + qname_hits + path_hits) AS trace_anchor_score,
               true AS trace_topic_anchor
        ORDER BY (exact_name_hits * 5 + name_hits * 2 + qname_hits + path_hits) DESC,
                 inbound_edges + outbound_edges DESC,
                 size(file_path) ASC
        LIMIT $limit
        """
        try:
            with self.db.driver.session() as session:
                return list(
                    session.run(
                        query_cypher,
                        workspace_id=self.workspace_id,
                        terms=terms,
                        scope_prefixes=scope_prefixes,
                        excluded_uids=list(excluded_uids),
                        noise_patterns=list(NOISE_PATH_PATTERNS),
                        limit=max_rows,
                    )
                )
        except Exception:
            return []

    def impact_query_terms(self, query: str) -> list[str]:
        terms = set(self.host.scoring.focus_query_terms(query or ""))
        anchors: set[str] = set()
        if terms.intersection({"route", "routes", "routing", "dispatch", "endpoint", "url"}):
            anchors.update(
                {"route", "routes", "router", "routing", "url", "rule", "map", "dispatch"}
            )
        return sorted(term for term in anchors if len(term) >= 3)

    def impact_reference_terms(self, target: SubgraphNode, query: str) -> list[str]:
        focus_terms = {
            term.lower()
            for term in self.host.scoring.focus_query_terms(query or "")
            if len(term) >= 4
        }
        focus_terms -= {
            "affected",
            "affect",
            "change",
            "changes",
            "code",
            "docs",
            "documentation",
            "example",
            "examples",
            "handling",
            "likely",
            "mechanism",
            "module",
            "modules",
            "most",
            "suite",
            "suites",
            "test",
            "tests",
            "what",
            "would",
        }
        if not focus_terms:
            return []

        path = Path(target.file_path or "")
        if not path.is_file():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []

        start, end = (target.range or [0, 0])[:2]
        target_span = max(0, end - start + 1) if start and end and end >= start else 0
        if (target.kind or "").lower() == "variable" or target_span <= 2:
            header = "\n".join(lines[:160])
            local = (
                "\n".join(lines[max(0, start - 40) : min(len(lines), end + 40)]) if start else ""
            )
            source = "\n".join(part for part in (header, local) if part)
        elif start and end and end >= start:
            # Keep this cheap: the target body/signature is enough to catch
            # imported public APIs without turning impact mode into a file scan.
            source = "\n".join(lines[max(0, start - 1) : min(len(lines), end, start + 260)])
        else:
            source = "\n".join(lines[:180])

        skip = {
            "Any",
            "Callable",
            "ClassVar",
            "False",
            "Literal",
            "None",
            "Self",
            "True",
            "TypedDict",
            "TypeAlias",
            "TypeVar",
            "Unpack",
            "annotations",
            "args",
            "bool",
            "class",
            "def",
            "dict",
            "float",
            "for",
            "if",
            "import",
            "int",
            "list",
            "none",
            "return",
            "self",
            "str",
            "tuple",
        }
        target_name = (target.name or "").lower()
        names: list[str] = []
        seen: set[str] = set()
        for identifier in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", source):
            ident_lc = identifier.lower()
            if (
                identifier in skip
                or ident_lc in skip
                or ident_lc == target_name
                or identifier.startswith("__")
                or len(identifier) < 3
            ):
                continue
            if not any(term in ident_lc or ident_lc in term for term in focus_terms):
                continue
            if identifier not in seen:
                seen.add(identifier)
                names.append(identifier)
        return names[:32]

    def impact_reference_anchor_candidates(
        self,
        target: SubgraphNode,
        *,
        query: str,
        excluded_uids: set[str],
        pool: list[Candidate],
        limit: int = 16,
    ) -> list[Candidate]:
        names = self.impact_reference_terms(target, query)
        if not names:
            return []
        rows = self.impact_reference_symbol_rows(
            target,
            names=names,
            excluded_uids={*excluded_uids, *(c.uid for c in pool if c.uid)},
            limit=limit,
        )
        name_order = {name: idx for idx, name in enumerate(names)}
        out: list[Candidate] = []
        for row in rows:
            raw_token_cost = int(
                row.get("token_estimate") or 0
            ) or self.host._estimate_tokens_range(row.get("range") or [0, 0])
            name = row.get("name") or ""
            edge_bonus = 0.06 * math.log1p(
                float(row.get("inbound_edges", 0) or 0)
            ) + 0.08 * math.log1p(float(row.get("outbound_edges", 0) or 0))
            order_bonus = max(0.0, 0.16 - 0.01 * name_order.get(name, len(name_order)))
            candidate = Candidate(
                kind="symbol",
                uid=str(row["uid"]),
                token_cost=min(raw_token_cost, 160),
                graph_score=1.35 + order_bonus + edge_bonus,
                semantic_score=0.45,
                name=name,
                file_path=row.get("file_path") or "",
                range=row.get("range") or [0, 0],
                render_mode="signature_only",
                relation="ROLE_BACKFILL",
                direction="impact",
                depth=1,
                file_hash=row.get("file_hash") or "",
                evidence_role="impact_public_api",
                supporting_roles=["impact_runtime"],
                provenance=["impact-reference-anchor"],
            )
            candidate.symbol_kind = row.get("symbol_kind", "")
            candidate.qualified_name = row.get("qualified_name", "")
            out.append(candidate)
        return out

    def impact_reference_symbol_rows(
        self,
        target: SubgraphNode,
        *,
        names: list[str],
        excluded_uids: set[str],
        limit: int,
    ) -> list[dict]:
        scope_prefixes = self.source_scope_prefixes(target.file_path or "")
        if not names or not scope_prefixes:
            return []
        query = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
        WHERE NOT s.uid IN $excluded_uids
          AND any(prefix IN $scope_prefixes WHERE f.path STARTS WITH prefix)
          AND NOT any(noise IN $noise_patterns WHERE f.path CONTAINS noise)
          AND s.name IN $names
        WITH s, f, c, coalesce(s.in_degree, 0) AS inbound_edges
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               coalesce(s.out_degree, 0) AS outbound_edges
        ORDER BY inbound_edges + outbound_edges DESC,
                 size(file_path) ASC
        LIMIT $limit
        """
        try:
            with self.db.driver.session() as session:
                return list(
                    session.run(
                        query,
                        workspace_id=self.workspace_id,
                        names=names,
                        scope_prefixes=scope_prefixes,
                        excluded_uids=list(excluded_uids),
                        noise_patterns=list(NOISE_PATH_PATTERNS),
                        limit=limit,
                    )
                )
        except Exception:
            return []

    def impact_topic_anchor_candidates(
        self,
        target: SubgraphNode,
        *,
        query: str,
        excluded_uids: set[str],
        pool: list[Candidate],
        limit: int = 24,
    ) -> list[Candidate]:
        terms = self.impact_query_terms(query)
        if not terms:
            return []
        rows = self.impact_topic_symbol_rows(
            target,
            terms=terms,
            excluded_uids={*excluded_uids, *(c.uid for c in pool if c.uid)},
            limit=limit,
        )
        out: list[Candidate] = []
        for row in rows:
            raw_token_cost = int(
                row.get("token_estimate") or 0
            ) or self.host._estimate_tokens_range(row.get("range") or [0, 0])
            anchor_score = float(row.get("impact_anchor_score", 0) or 0)
            edge_bonus = 0.06 * math.log1p(
                float(row.get("inbound_edges", 0) or 0)
            ) + 0.08 * math.log1p(float(row.get("outbound_edges", 0) or 0))
            candidate = Candidate(
                kind="symbol",
                uid=str(row["uid"]),
                token_cost=min(raw_token_cost, 160),
                graph_score=1.05 + min(0.8, anchor_score * 0.08) + edge_bonus,
                semantic_score=0.35,
                name=row.get("name") or "",
                file_path=row.get("file_path") or "",
                range=row.get("range") or [0, 0],
                render_mode="signature_only",
                relation="ROLE_BACKFILL",
                direction="impact",
                depth=2,
                file_hash=row.get("file_hash") or "",
                evidence_role="impact_public_api",
                supporting_roles=["impact_runtime"],
                provenance=["impact-topic-anchor"],
            )
            candidate.symbol_kind = row.get("symbol_kind", "")
            candidate.qualified_name = row.get("qualified_name", "")
            out.append(candidate)
        return out

    def query_topic_anchor_candidates(
        self,
        target: SubgraphNode,
        *,
        query: str,
        excluded_uids: set[str],
        limit: int = 16,
    ) -> list[Candidate]:
        terms = self.query_topic_terms(query, target)
        if not terms:
            return []
        # This recovery is for explicit pipeline-stage questions ("ranking",
        # "PromptContext", "compiler"). Generic request/handler terms are better
        # handled by trace-specific recovery, otherwise they can crowd out flow
        # files in route/webview dependency questions.
        if not ({"ranker", "ranking", "prompt", "compile", "compiler"} & set(terms)):
            return []
        rows = self.query_topic_symbol_rows(
            target,
            terms=terms,
            excluded_uids=excluded_uids,
            limit=limit,
        )
        out: list[Candidate] = []
        for row in rows:
            raw_token_cost = int(
                row.get("token_estimate") or 0
            ) or self.host._estimate_tokens_range(row.get("range") or [0, 0])
            anchor_score = float(row.get("query_anchor_score", 0) or 0)
            edge_bonus = 0.05 * math.log1p(
                float(row.get("inbound_edges", 0) or 0)
            ) + 0.07 * math.log1p(float(row.get("outbound_edges", 0) or 0))
            candidate = Candidate(
                kind="symbol",
                uid=str(row["uid"]),
                token_cost=min(raw_token_cost, 160),
                graph_score=1.1 + min(0.9, anchor_score * 0.09) + edge_bonus,
                semantic_score=0.30,
                name=row.get("name") or "",
                file_path=row.get("file_path") or "",
                range=row.get("range") or [0, 0],
                render_mode="signature_only",
                relation="ROLE_BACKFILL",
                direction="topic",
                depth=2,
                file_hash=row.get("file_hash") or "",
                evidence_role=self.first_reasoning_role(["supporting_surface"]),
                supporting_roles=["supporting_surface"],
                provenance=["query-topic-anchor"],
            )
            candidate.symbol_kind = row.get("symbol_kind", "")
            candidate.qualified_name = row.get("qualified_name", "")
            out.append(candidate)
        return out

    @staticmethod
    def query_topic_terms(query: str, target: SubgraphNode) -> list[str]:
        text = f"{target.name or ''} {query or ''}"
        raw_terms = set(RankerScoring.focus_query_terms(text))
        stop = {
            "assemble",
            "assembled",
            "assembly",
            "context",
            "symbol",
            "symbols",
            "through",
        }
        terms = {term for term in raw_terms if term not in stop}
        lowered = text.lower()
        if any(term in lowered for term in ("rank", "ranking", "ranker")):
            terms.update({"rank", "ranker", "ranking"})
        if any(term in lowered for term in ("prompt", "promptcontext", "compile", "compiler")):
            terms.update({"prompt", "compile", "compiler"})
        return sorted(term for term in terms if len(term) >= 4)

    def query_topic_symbol_rows(
        self,
        target: SubgraphNode,
        *,
        terms: list[str],
        excluded_uids: set[str],
        limit: int,
    ) -> list[dict]:
        scope_prefixes = self.source_scope_prefixes(target.file_path or "")
        if not terms or not scope_prefixes:
            return []
        query = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
        WHERE NOT s.uid IN $excluded_uids
          AND any(prefix IN $scope_prefixes WHERE f.path STARTS WITH prefix)
          AND NOT any(noise IN $noise_patterns WHERE f.path CONTAINS noise)
          AND any(term IN $terms
            WHERE toLower(s.name) CONTAINS term
               OR toLower(coalesce(s.qualified_name, '')) CONTAINS term
               OR toLower(f.path) CONTAINS term)
        WITH s, f, c, coalesce(s.in_degree, 0) AS inbound_edges
        WITH s, f, c, inbound_edges, coalesce(s.out_degree, 0) AS outbound_edges,
             size([term IN $terms WHERE toLower(s.name) CONTAINS term]) AS name_hits,
             size([term IN $terms WHERE toLower(coalesce(s.qualified_name, '')) CONTAINS term]) AS qname_hits,
             size([term IN $terms WHERE toLower(f.path) CONTAINS term]) AS path_hits
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               outbound_edges,
               (name_hits * 3 + qname_hits * 2 + path_hits) AS query_anchor_score
        ORDER BY (name_hits * 3 + qname_hits * 2 + path_hits) DESC,
                 inbound_edges + outbound_edges DESC,
                 size(file_path) ASC
        LIMIT $limit
        """
        try:
            with self.db.driver.session() as session:
                return list(
                    session.run(
                        query,
                        workspace_id=self.workspace_id,
                        terms=terms,
                        scope_prefixes=scope_prefixes,
                        excluded_uids=list(excluded_uids),
                        noise_patterns=list(NOISE_PATH_PATTERNS),
                        limit=limit,
                    )
                )
        except Exception:
            return []

    def impact_topic_symbol_rows(
        self,
        target: SubgraphNode,
        *,
        terms: list[str],
        excluded_uids: set[str],
        limit: int,
    ) -> list[dict]:
        scope_prefixes = self.source_scope_prefixes(target.file_path or "")
        if not terms or not scope_prefixes:
            return []
        query = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
        WHERE NOT s.uid IN $excluded_uids
          AND any(prefix IN $scope_prefixes WHERE f.path STARTS WITH prefix)
          AND NOT any(noise IN $noise_patterns WHERE f.path CONTAINS noise)
          AND any(term IN $terms
            WHERE toLower(s.name) CONTAINS term
               OR toLower(coalesce(s.qualified_name, '')) CONTAINS term
               OR toLower(f.path) CONTAINS term)
        WITH s, f, c, coalesce(s.in_degree, 0) AS inbound_edges
        WITH s, f, c, inbound_edges, coalesce(s.out_degree, 0) AS outbound_edges,
             size([term IN $terms WHERE toLower(s.name) CONTAINS term]) AS name_hits,
             size([term IN $terms WHERE toLower(coalesce(s.qualified_name, '')) CONTAINS term]) AS qname_hits,
             size([term IN $terms WHERE toLower(f.path) CONTAINS term]) AS path_hits
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               outbound_edges,
               (name_hits * 2 + qname_hits + path_hits) AS impact_anchor_score
        ORDER BY (name_hits * 2 + qname_hits + path_hits) DESC,
                 inbound_edges + outbound_edges DESC,
                 size(file_path) ASC
        LIMIT $limit
        """
        try:
            with self.db.driver.session() as session:
                return list(
                    session.run(
                        query,
                        workspace_id=self.workspace_id,
                        terms=terms,
                        scope_prefixes=scope_prefixes,
                        excluded_uids=list(excluded_uids),
                        noise_patterns=list(NOISE_PATH_PATTERNS),
                        limit=limit,
                    )
                )
        except Exception:
            return []

    def trace_topic_scope_prefixes(
        self,
        file_path: str,
        terms: list[str],
        *,
        target: SubgraphNode | None = None,
        mechanism: str = "",
        query: str = "",
    ) -> list[str]:
        """Scopes for topic search, widened for monorepo cross-package traces."""
        prefixes = self.source_scope_prefixes(file_path)
        lowered_terms = {term.lower() for term in terms}
        if target is not None:
            prefixes = list(
                dict.fromkeys(
                    prefixes
                    + self.trace_runtime_scope_prefixes(
                        target, mechanism=mechanism, query=query
                    )
                )
            )
        if lowered_terms.intersection({"request", "strategy", "pool", "handler", "invoke"}):
            worker_scope = self._path_scope_through_segment(file_path, "worker")
            if not worker_scope:
                worker_scope = self._infer_worker_scope_from_path(file_path)
            if worker_scope:
                prefixes.append(worker_scope)
        if lowered_terms.intersection(
            {
                "build",
                "compile",
                "compiler",
                "container",
                "depend",
                "dependency",
                "hydrate",
                "inject",
                "injector",
                "instance",
                "module",
                "provider",
                "render",
                "resolve",
                "resolver",
                "template",
                "transform",
                "wrapper",
            }
        ):
            norm = (file_path or "").replace("\\", "/")
            marker = "/packages/"
            if marker in norm:
                packages_root = norm.split(marker, 1)[0] + marker
                prefixes.append(packages_root)

        out: list[str] = []
        seen: set[str] = set()
        for prefix in prefixes:
            if prefix and prefix not in seen:
                seen.add(prefix)
                out.append(prefix)
        return out

    def row_in_source_scope(self, row: dict, scope_prefixes: list[str]) -> bool:
        file_path = (row.get("file_path") or "").replace("\\", "/")
        if not file_path:
            return False
        return any(file_path.startswith(prefix) for prefix in scope_prefixes)

    @staticmethod
    def row_is_trace_noise(row: dict) -> bool:
        file_path = (row.get("file_path") or "").replace("\\", "/")
        if any(pattern in file_path for pattern in NOISE_PATH_PATTERNS):
            return True
        return "/docs/" in file_path or file_path.endswith("/docs/conf.py")

    def is_dependency_marker_target(self, target: SubgraphNode) -> bool:
        haystack = " ".join(
            part.lower()
            for part in (
                target.name or "",
                target.file_path or "",
                getattr(target, "qualified_name", "") or "",
            )
            if part
        )
        return any(token in haystack for token in TRACE_DEPENDENCY_TARGET_TOKENS)

    def trace_dependency_sibling_dir_symbol_rows(
        self,
        seed_rows: list[dict],
        *,
        excluded_uids: set[str],
        max_rows: int = 48,
    ) -> list[dict]:
        """Expand trace seeds to sibling modules in the same directory/directories.

        Universal fallback: when a DI marker resolves to one runtime function in
        ``x/dependencies/utils.py``, useful intermediate models often live in
        neighboring files under ``x/dependencies/*.py``.
        """
        dir_prefixes: set[str] = set()
        for row in seed_rows:
            fp = (row.get("file_path") or "").replace("\\", "/")
            if "/" not in fp:
                continue
            parent = fp.rsplit("/", 1)[0].rstrip("/")
            if not parent:
                continue
            dir_prefixes.add(f"{parent}/")
        if not dir_prefixes:
            return []

        query = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
        WHERE NOT s.uid IN $excluded_uids
          AND any(prefix IN $dir_prefixes WHERE f.path STARTS WITH prefix)
        WITH s, f, c, coalesce(s.in_degree, 0) AS inbound_edges
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               coalesce(s.out_degree, 0) AS outbound_edges
        LIMIT $limit
        """
        try:
            with self.db.driver.session() as session:
                return list(
                    session.run(
                        query,
                        workspace_id=self.workspace_id,
                        excluded_uids=list(excluded_uids),
                        dir_prefixes=sorted(dir_prefixes),
                        limit=max_rows,
                    )
                )
        except Exception:
            return []

    def trace_dependency_dir_symbol_rows(
        self,
        dir_prefixes: list[str],
        *,
        excluded_uids: set[str],
        max_rows: int = 48,
    ) -> list[dict]:
        """Load symbols under explicit directory prefixes (worker/ sibling expansion)."""
        prefixes = [p for p in dir_prefixes if p]
        if not prefixes:
            return []
        query_cypher = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
        WHERE NOT s.uid IN $excluded_uids
          AND any(prefix IN $dir_prefixes WHERE f.path STARTS WITH prefix)
          AND NOT any(noise IN $noise_patterns WHERE f.path CONTAINS noise)
        WITH s, f, c, coalesce(s.in_degree, 0) AS inbound_edges
        WITH s, f, c, inbound_edges, coalesce(s.out_degree, 0) AS outbound_edges,
             CASE
               WHEN any(marker IN $sibling_markers WHERE f.path CONTAINS marker) THEN 4
               ELSE 0
             END AS trace_anchor_score
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               outbound_edges,
               trace_anchor_score,
               false AS trace_topic_anchor
        ORDER BY trace_anchor_score DESC,
                 inbound_edges + outbound_edges DESC,
                 size(f.path) ASC
        LIMIT $limit
        """
        try:
            with self.db.driver.session() as session:
                return list(
                    session.run(
                        query_cypher,
                        workspace_id=self.workspace_id,
                        excluded_uids=list(excluded_uids),
                        dir_prefixes=sorted(set(prefixes)),
                        noise_patterns=list(NOISE_PATH_PATTERNS),
                        sibling_markers=list(TRACE_EXECUTION_SIBLING_FILE_MARKERS),
                        limit=max_rows,
                    )
                )
        except Exception:
            return []

    def trace_dependency_parent_dir_symbol_rows(
        self,
        seed_rows: list[dict],
        *,
        excluded_uids: set[str],
        max_rows: int = 36,
    ) -> list[dict]:
        """Expand trace seeds one directory upward for wrapper->runtime bridges.

        Helps when lifecycle APIs live in nested modules (for example ``x/sansio/*``)
        while request orchestration occurs in the parent package module.
        """
        parent_prefixes: set[str] = set()
        for row in seed_rows:
            fp = (row.get("file_path") or "").replace("\\", "/")
            if "/" not in fp:
                continue
            parent = fp.rsplit("/", 1)[0].rstrip("/")
            if "/" not in parent:
                continue
            grandparent = parent.rsplit("/", 1)[0].rstrip("/")
            if not grandparent:
                continue
            parent_prefixes.add(f"{grandparent}/")
        if not parent_prefixes:
            return []

        query = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
        WHERE NOT s.uid IN $excluded_uids
          AND any(prefix IN $parent_prefixes WHERE f.path STARTS WITH prefix)
        WITH s, f, c, coalesce(s.in_degree, 0) AS inbound_edges
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               coalesce(s.out_degree, 0) AS outbound_edges
        LIMIT $limit
        """
        try:
            with self.db.driver.session() as session:
                return list(
                    session.run(
                        query,
                        workspace_id=self.workspace_id,
                        excluded_uids=list(excluded_uids),
                        parent_prefixes=sorted(parent_prefixes),
                        limit=max_rows,
                    )
                )
        except Exception:
            return []

    def trace_dependency_import_anchor_candidates(
        self,
        target: SubgraphNode,
        *,
        query: str,
        mechanism: str,
        required_roles: list[str],
        excluded_uids: set[str],
        pool: list[Candidate],
    ) -> list[Candidate]:
        """Boost symbols from modules the target file imports (graph + FS), for DI/trace queries.

        Uses the same discovery path as generic recovery (``_imported_symbol_rows``) so
        no framework names are hard-coded; adds minimal-role anchors when strict Pass-1
        role overlap would otherwise drop high-value imported symbols.
        """
        if not RankerScoring.trace_dependency_gain_mode(mechanism, query):
            return []

        scoped = set(normalize_roles(required_roles))
        existing_uids = {c.uid for c in pool if getattr(c, "uid", "")}
        hook_flow_context = self.is_hook_flow_context(
            target=target, mechanism=mechanism, query=query
        )
        runtime_rows = self.host._trace_dependency_runtime_symbol_rows(
            target,
            excluded_uids=excluded_uids,
            mechanism=mechanism,
            query=query,
        )
        topic_rows = self.trace_dependency_topic_symbol_rows(
            target,
            query=query,
            excluded_uids=excluded_uids,
            mechanism=mechanism,
        )
        sibling_rows = self.trace_dependency_sibling_dir_symbol_rows(
            runtime_rows,
            excluded_uids=excluded_uids,
        )
        if self.is_worker_consume_trace_target(
            target, mechanism, query
        ) or self.is_subpackage_entry_target(target):
            worker_scope = self.trace_runtime_scope_prefixes(
                target, mechanism=mechanism, query=query
            )
            merged_siblings: dict[str, dict] = {}
            for row in (
                *sibling_rows,
                *self.trace_dependency_dir_symbol_rows(
                    worker_scope,
                    excluded_uids=excluded_uids,
                ),
            ):
                uid = str(row.get("uid") or "")
                if uid:
                    merged_siblings[uid] = row
            sibling_rows = list(merged_siblings.values())
        parent_rows = (
            self.trace_dependency_parent_dir_symbol_rows(
                runtime_rows,
                excluded_uids=excluded_uids,
            )
            if hook_flow_context
            else []
        )
        imported_rows = self.host._imported_symbol_rows(
            target.file_path, excluded_uids=excluded_uids
        )
        source_scope = self.source_scope_prefixes(target.file_path or "")
        topic_scope = self.trace_topic_scope_prefixes(
            target.file_path or "",
            self.trace_query_terms(query, target),
        )
        merged: list[dict] = []
        seen_merge: set[str] = set()
        for row in (*runtime_rows, *sibling_rows, *parent_rows, *imported_rows):
            if self.row_is_trace_noise(row) or not self.row_in_source_scope(row, source_scope):
                continue
            uid = str(row.get("uid") or "")
            if uid and uid not in seen_merge:
                seen_merge.add(uid)
                merged.append(row)
        for row in topic_rows:
            if self.row_is_trace_noise(row) or not self.row_in_source_scope(row, topic_scope):
                continue
            uid = str(row.get("uid") or "")
            if uid and uid not in seen_merge:
                seen_merge.add(uid)
                merged.append(row)
        rows = self.rank_rows_for_trace_import_anchors(merged)

        out: list[Candidate] = []
        seen = set(existing_uids)
        for row in rows:
            uid = str(row.get("uid") or "")
            if not uid or uid in seen:
                continue
            candidate = self.host._recovery_candidate_from_row(
                row,
                origin="import_module_trace",
                scoped_roles=scoped,
                target=target,
            )
            if candidate is None:
                candidate = self.minimal_trace_import_anchor_candidate(
                    row,
                    required_roles=required_roles,
                )
            trace_anchor_score = float(row.get("trace_anchor_score", 0) or 0)
            if row.get("trace_topic_anchor") and trace_anchor_score > 0:
                candidate.graph_score += min(1.4, trace_anchor_score * 0.08)
                candidate.provenance.append("trace-topic-anchor")
            seen.add(uid)
            out.append(candidate)
        return out

    def is_routing_flow_context(
        self,
        *,
        target: SubgraphNode,
        mechanism: str,
        query: str,
        required_roles: list[str],
    ) -> bool:
        scoped = set(normalize_roles(required_roles))
        _routing_eligible = {"composition_surface", "factory_surface", "route_builder", "decorator_processor"}
        if not scoped.intersection(_routing_eligible):
            return False
        m = (mechanism or "").lower()
        q = (query or "").lower()
        if "routing" in m or "dispatch" in m:
            return True
        target_ctx = f"{(target.name or '').lower()} {(target.file_path or '').lower()}"
        if any(token in target_ctx for token in ROUTING_FLOW_TARGET_TOKENS):
            return True
        return any(term in q for term in ("router", "routing", "middleware")) and any(
            term in q
            for term in ("delegate", "handling", "handler", "request", "dispatch", "create")
        )

    def routing_flow_recovery_hint(self, row: dict, *, target: SubgraphNode) -> bool:
        target_ctx = f"{(target.name or '').lower()} {(target.file_path or '').lower()}"
        row_ctx = " ".join(
            [
                str(row.get("name") or "").lower(),
                str(row.get("qualified_name") or "").lower(),
                str(row.get("file_path") or "").lower(),
            ]
        )
        target_hit = any(token in target_ctx for token in ROUTING_FLOW_TARGET_TOKENS) or any(
            token in target_ctx for token in ROUTING_FLOW_PATH_TOKENS
        )
        row_hit = any(token in row_ctx for token in ROUTING_FLOW_TARGET_TOKENS)
        path_hit = any(token in row_ctx for token in ROUTING_FLOW_PATH_TOKENS)
        name_hit = (row.get("name") or "").lower() in ROUTING_COMPOSITION_SYMBOL_NAMES
        return (target_hit and row_hit) or path_hit or name_hit

    def routing_flow_symbol_rows(
        self,
        target: SubgraphNode,
        *,
        excluded_uids: set[str],
        limit: int = 16,
    ) -> list[dict]:
        """Symbols in ``lib/`` (and scoped source roots) that evidence router/middleware flow."""
        scope_prefixes = self.source_scope_prefixes(target.file_path or "")
        if not scope_prefixes:
            return []
        # Monorepo widening: packages/<pkg>/... → packages/ so that sibling
        # packages (e.g. packages/core/router vs packages/common/decorators)
        # are reachable for routing resolution questions.
        norm = (target.file_path or "").replace("\\", "/")
        marker = "/packages/"
        if marker in norm:
            packages_root = norm.split(marker, 1)[0] + marker
            if packages_root not in scope_prefixes:
                scope_prefixes = [packages_root, *scope_prefixes]
        terms = sorted(
            {
                "router",
                "routing",
                "middleware",
                "dispatch",
                "handle",
                "layer",
                "use",
                "init",
            }
        )
        query_cypher = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
        WHERE NOT s.uid IN $excluded_uids
          AND any(prefix IN $scope_prefixes WHERE f.path STARTS WITH prefix)
          AND NOT any(noise IN $noise_patterns WHERE f.path CONTAINS noise)
          AND (
            f.path CONTAINS '/lib/'
            OR any(term IN $terms
              WHERE toLower(s.name) CONTAINS term
                 OR toLower(f.path) CONTAINS term)
          )
        WITH s, f, c, coalesce(s.in_degree, 0) AS inbound_edges
        WITH s, f, c, inbound_edges, coalesce(s.out_degree, 0) AS outbound_edges,
             CASE WHEN f.path CONTAINS '/lib/' THEN 3.0 ELSE 0.0 END
             + CASE WHEN toLower(s.name) IN $composition_names THEN 2.5 ELSE 0.0 END
             + size([term IN $terms WHERE toLower(s.name) = term]) * 2.0
             + size([term IN $terms WHERE toLower(s.name) CONTAINS term]) AS routing_anchor_score
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               outbound_edges,
               routing_anchor_score
        ORDER BY routing_anchor_score DESC,
                 inbound_edges + outbound_edges DESC,
                 size(file_path) ASC
        LIMIT $limit
        """
        try:
            with self.db.driver.session() as session:
                return list(
                    session.run(
                        query_cypher,
                        workspace_id=self.workspace_id,
                        scope_prefixes=scope_prefixes,
                        excluded_uids=list(excluded_uids),
                        noise_patterns=list(NOISE_PATH_PATTERNS),
                        terms=terms,
                        composition_names=sorted(ROUTING_COMPOSITION_SYMBOL_NAMES),
                        limit=limit,
                    )
                )
        except Exception:
            return []

    def trace_routing_composition_anchor_candidates(
        self,
        target: SubgraphNode,
        *,
        query: str,
        mechanism: str,
        required_roles: list[str],
        excluded_uids: set[str],
        pool: list[Candidate],
        limit: int = 8,
    ) -> list[Candidate]:
        """Explicit ``composition_surface`` anchors for router/middleware trace questions."""
        m = (mechanism or "").lower()
        if not RankerScoring.trace_dependency_gain_mode(mechanism, query) and "decorator_routing" not in m:
            return []
        if not self.is_routing_flow_context(
            target=target,
            mechanism=mechanism,
            query=query,
            required_roles=required_roles,
        ):
            return []

        existing_uids = {c.uid for c in pool if getattr(c, "uid", "")}
        excluded = {*excluded_uids, *existing_uids}
        if target.uid:
            excluded.add(target.uid)

        rows = self.routing_flow_symbol_rows(target, excluded_uids=excluded, limit=limit * 3)
        out: list[Candidate] = []
        seen: set[str] = set()
        for row in rows:
            if not self.routing_flow_recovery_hint(row, target=target):
                continue
            uid = str(row.get("uid") or "")
            if not uid or uid in seen:
                continue
            raw_token_cost = int(
                row.get("token_estimate") or 0
            ) or self.host._estimate_tokens_range(row.get("range") or [0, 0])
            anchor_score = float(row.get("routing_anchor_score", 0) or 0)
            edge_bonus = 0.08 * math.log1p(
                float(row.get("inbound_edges", 0) or 0)
            ) + 0.10 * math.log1p(float(row.get("outbound_edges", 0) or 0))
            name = row.get("name") or ""
            supporting: list[str] = []
            if name.lower() == "router":
                supporting.append("factory_surface")
            candidate = Candidate(
                kind="symbol",
                uid=uid,
                token_cost=min(raw_token_cost, 140),
                graph_score=1.35 + min(1.2, anchor_score * 0.15) + edge_bonus,
                semantic_score=0.42,
                name=name,
                file_path=row.get("file_path") or "",
                range=row.get("range") or [0, 0],
                render_mode="signature_only",
                relation="ROLE_BACKFILL",
                direction="routing",
                depth=2,
                file_hash=row.get("file_hash") or "",
                evidence_role="composition_surface",
                supporting_roles=normalize_roles(supporting),
                provenance=["trace-routing-composition-anchor"],
            )
            candidate.symbol_kind = row.get("symbol_kind", "")
            candidate.qualified_name = row.get("qualified_name", "")
            seen.add(uid)
            out.append(candidate)
            if len(out) >= limit:
                break
        return out

    def same_file_symbol_rows(
        self,
        file_path: str,
        *,
        excluded_uids: set[str],
    ) -> list[dict]:
        query = """
        MATCH (f:File {workspace_id: $workspace_id, path: $file_path})-[c:CONTAINS]->(s:Symbol)
        WHERE NOT s.uid IN $excluded_uids
        WITH s, f, c, coalesce(s.in_degree, 0) AS inbound_edges
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               coalesce(s.out_degree, 0) AS outbound_edges
        """
        try:
            with self.db.driver.session() as session:
                return list(
                    session.run(
                        query,
                        workspace_id=self.workspace_id,
                        file_path=file_path,
                        excluded_uids=list(excluded_uids),
                    )
                )
        except Exception:
            return []

    def imported_symbol_rows(
        self,
        file_path: str,
        *,
        excluded_uids: set[str],
    ) -> list[dict]:
        query = """
        MATCH (f:File {workspace_id: $workspace_id, path: $file_path})-[:IMPORTS]->(direct_dep:File {workspace_id: $workspace_id})
        WITH collect(DISTINCT direct_dep) AS direct_deps
        UNWIND direct_deps AS direct_dep
        OPTIONAL MATCH (direct_dep)-[:IMPORTS]->(barrel_dep:File {workspace_id: $workspace_id})
        WHERE direct_dep.path =~ '.*/index\\.(js|jsx|ts|tsx)$'
        WITH direct_deps, collect(DISTINCT barrel_dep) AS barrel_deps
        WITH direct_deps + barrel_deps AS deps
        UNWIND deps AS dep
        WITH DISTINCT dep
        WHERE dep IS NOT NULL
        MATCH (dep)-[c:CONTAINS]->(s:Symbol)
        WHERE NOT s.uid IN $excluded_uids
        WITH s, dep, c, coalesce(s.in_degree, 0) AS inbound_edges
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(dep.path, '<unknown>') AS file_path,
               coalesce(dep.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               coalesce(s.out_degree, 0) AS outbound_edges
        """
        try:
            with self.db.driver.session() as session:
                rows = list(
                    session.run(
                        query,
                        workspace_id=self.workspace_id,
                        file_path=file_path,
                        excluded_uids=list(excluded_uids),
                    )
                )
        except Exception:
            rows = []

        seen_paths = {row.get("file_path") for row in rows}
        fallback_paths: list[str] = []
        for path in self.resolve_filesystem_import_paths(file_path):
            if path and path not in seen_paths:
                fallback_paths.append(path)
                seen_paths.add(path)
        for path in self.resolve_intra_repo_package_import_paths(file_path):
            if path and path not in seen_paths:
                fallback_paths.append(path)
                seen_paths.add(path)
        if fallback_paths:
            rows.extend(
                self.symbol_rows_for_file_paths(
                    fallback_paths,
                    excluded_uids=excluded_uids,
                )
            )
        return rows

    def symbol_rows_for_file_paths(
        self,
        file_paths: list[str],
        *,
        excluded_uids: set[str],
    ) -> list[dict]:
        if not file_paths:
            return []

        query = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
        WHERE f.path IN $file_paths
          AND NOT s.uid IN $excluded_uids
        WITH s, f, c, coalesce(s.in_degree, 0) AS inbound_edges
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               coalesce(s.out_degree, 0) AS outbound_edges
        """
        try:
            with self.db.driver.session() as session:
                return list(
                    session.run(
                        query,
                        workspace_id=self.workspace_id,
                        file_paths=file_paths,
                        excluded_uids=list(excluded_uids),
                    )
                )
        except Exception:
            return []

    def resolve_filesystem_import_paths(self, file_path: str) -> list[str]:
        path = Path(file_path)
        if not path.exists():
            return []

        adapter = self.adapter_for_path(path)
        if adapter is None:
            return []

        try:
            source = path.read_text(encoding="utf-8")
        except Exception:
            return []

        resolved: set[str] = set()
        for edge in adapter.extract_imports(source, str(path)):
            if edge.import_type != "relative":
                continue
            resolved.update(self.resolve_relative_import_targets(path, edge.target_module_name))
        return sorted(resolved)

    def resolve_intra_repo_package_import_paths(self, file_path: str) -> list[str]:
        """Resolve dotted imports (``from x.y import z``) to files inside the checkout.

        Relative imports are handled by :meth:`_resolve_filesystem_import_paths`.
        Absolute package imports (parser marks them ``from_package`` / ``direct``) are
        skipped there but are often the only link from a thin wrapper (e.g. DI markers)
        to sibling packages such as ``.../pkg/dependencies/*.py`` when Neo4j has no
        ``IMPORTS`` edge yet.
        """
        path = Path(file_path)
        if not path.exists():
            return []
        adapter = self.adapter_for_path(path)
        if adapter is None:
            return []

        try:
            source = path.read_text(encoding="utf-8")
        except Exception:
            return []

        resolved: set[str] = set()
        for edge in adapter.extract_imports(source, str(path)):
            if edge.import_type == "relative":
                continue
            mod = (edge.target_module_name or "").strip()
            if not mod or mod.startswith("."):
                continue
            parts = [p for p in mod.split(".") if p]
            if len(parts) < 2:
                continue
            resolved.update(StructuralRecovery.resolve_dotted_module_under_ancestors(path, parts))
        return sorted(resolved)

    @staticmethod
    def resolve_dotted_module_under_ancestors(source_file: Path, parts: list[str]) -> list[str]:
        """Try ``ancestor / part0 / part1 / ...``.py walking upward from ``source_file``."""
        out: list[str] = []
        cur = source_file.resolve().parent
        for _ in range(48):
            candidate = (cur / Path(*parts)).with_suffix(".py")
            if candidate.is_file():
                out.append(str(candidate.resolve()))
            pkg_init = (cur / Path(*parts)) / "__init__.py"
            if pkg_init.is_file():
                out.append(str(pkg_init.resolve()))
            parent = cur.parent
            if parent == cur:
                break
            cur = parent
        return out

    def adapter_for_path(self, path: Path):
        suffix = path.suffix.lower()
        if suffix in {".ts", ".tsx"}:
            from sidecar.parser.adapters.typescript_adapter import TypeScriptAdapter

            return TypeScriptAdapter()
        if suffix in {".py", ".pyi"}:
            from sidecar.parser.adapters.python_adapter import PythonAdapter

            return PythonAdapter()
        return None

    def resolve_relative_import_targets(
        self,
        source_path: Path,
        import_source: str,
    ) -> list[str]:
        source = (import_source or "").strip()
        if not source.startswith("."):
            return []

        candidates: list[Path] = []
        if "/" in source or source.startswith("./") or source.startswith("../"):
            base = (source_path.parent / source).resolve()
            candidates.extend(StructuralRecovery.path_resolution_candidates(base))
        else:
            leading = len(source) - len(source.lstrip("."))
            remainder = source.lstrip(".").replace(".", "/")
            base_dir = source_path.parent
            for _ in range(max(leading - 1, 0)):
                base_dir = base_dir.parent
            base = (base_dir / remainder).resolve() if remainder else base_dir.resolve()
            candidates.extend(StructuralRecovery.path_resolution_candidates(base))

        return [str(candidate) for candidate in candidates if candidate.exists()]

    @staticmethod
    def path_resolution_candidates(base: Path) -> list[Path]:
        if base.suffix:
            return [base]
        return [
            base.with_suffix(".ts"),
            base.with_suffix(".tsx"),
            base / "index.ts",
            base / "index.tsx",
            base.with_suffix(".py"),
            base.with_suffix(".pyi"),
            base / "__init__.py",
        ]

    def recovery_candidate_from_row(
        self,
        row: dict,
        *,
        origin: str,
        scoped_roles: set[str],
        target: SubgraphNode,
    ) -> Candidate | None:
        raw_token_cost = int(row["token_estimate"]) or self.host._estimate_tokens_range(
            row.get("range") or [0, 0]
        )
        name_lower = (row["name"] or "").lower()
        file_stem = Path(row["file_path"]).stem.lower()
        is_stem_match = file_stem == name_lower
        is_builder_surface = name_lower.startswith(
            ("build", "create", "configure", "combine", "compose")
        )
        token_cost = min(
            raw_token_cost,
            80 if is_stem_match else 120 if is_builder_surface else 180,
        )
        probe = Candidate(
            kind="symbol",
            uid=row["uid"],
            token_cost=token_cost,
            name=row["name"],
            file_path=row["file_path"],
            range=row.get("range") or [0, 0],
            file_hash=row.get("file_hash") or "",
        )
        symbol_kind = row.get("symbol_kind", "") or row.get("kind", "")
        probe.symbol_kind = symbol_kind
        probe.qualified_name = row.get("qualified_name", "")

        primary_role = self.host.role_fulfilment.role_of(probe)
        supporting_roles = self.host.role_fulfilment.supporting_roles_of(probe)
        candidate_roles = normalize_roles([primary_role, *supporting_roles])
        registration_flow_context = self.is_registration_flow_context(
            target=target,
            scoped_roles=scoped_roles,
        )
        factory_signal = self.factory_surface_recovery_signal(
            row,
            target=target,
            registration_flow_context=registration_flow_context,
        )
        api_signal = self.api_surface_recovery_signal(
            row,
            target=target,
            registration_flow_context=registration_flow_context,
        )
        representation_signal = self.representation_surface_recovery_signal(
            row,
            target=target,
            registration_flow_context=registration_flow_context,
        )
        runtime_signal = self.runtime_surface_recovery_signal(
            row,
            target=target,
            registration_flow_context=registration_flow_context,
        )
        config_signal = self.config_surface_recovery_signal(row, target=target)
        orchestrator_signal = self.orchestrator_recovery_signal(row, target=target)
        identity_orchestrator_signal = self.identity_orchestrator_recovery_signal(
            row, target=target
        )
        executor_signal = self.executor_recovery_signal(row, target=target)
        if "api_surface" in scoped_roles and api_signal:
            candidate_roles = normalize_roles([*candidate_roles, "api_surface"])
        if "factory_surface" in scoped_roles and factory_signal:
            candidate_roles = normalize_roles([*candidate_roles, "factory_surface"])
        identity_config_signal = self.identity_config_recovery_signal(row, target=target)
        if "config_surface" in scoped_roles and (config_signal or identity_config_signal):
            candidate_roles = normalize_roles([*candidate_roles, "config_surface"])
        if "orchestrator" in scoped_roles and (orchestrator_signal or identity_orchestrator_signal):
            candidate_roles = normalize_roles([*candidate_roles, "orchestrator"])
        if "executor" in scoped_roles and executor_signal:
            candidate_roles = normalize_roles([*candidate_roles, "executor"])
        if "representation_surface" in scoped_roles and representation_signal:
            candidate_roles = normalize_roles([*candidate_roles, "representation_surface"])
        if "runtime_surface" in scoped_roles and runtime_signal:
            candidate_roles = normalize_roles([*candidate_roles, "runtime_surface"])
        matched_roles = [role for role in candidate_roles if role in scoped_roles]
        if not matched_roles:
            return None
        matched_roles.sort(key=lambda role: (role == primary_role, role == "docs_or_concept"))

        origin_bonus = 0.45 if origin == "same_file" else 0.35
        stem_bonus = 0.35 if is_stem_match else 0.12 if is_builder_surface else 0.0
        api_bonus = 0.18 if ("api_surface" in matched_roles and api_signal) else 0.0
        factory_bonus = 0.22 if ("factory_surface" in matched_roles and factory_signal) else 0.0
        representation_bonus = (
            0.18 if ("representation_surface" in matched_roles and representation_signal) else 0.0
        )
        runtime_bonus = 0.20 if ("runtime_surface" in matched_roles and runtime_signal) else 0.0
        config_bonus = 0.18 if ("config_surface" in matched_roles and config_signal) else 0.0
        orchestrator_bonus = (
            0.22
            if (
                "orchestrator" in matched_roles
                and (orchestrator_signal or identity_orchestrator_signal)
            )
            else 0.0
        )
        executor_bonus = 0.22 if ("executor" in matched_roles and executor_signal) else 0.0
        if registration_flow_context and "runtime_surface" in matched_roles and runtime_signal:
            runtime_bonus += 0.06
        role_bonus = 0.18 * len(matched_roles)
        edge_bonus = 0.08 * math.log1p(float(row.get("inbound_edges", 0) or 0)) + 0.10 * math.log1p(
            float(row.get("outbound_edges", 0) or 0)
        )
        candidate = Candidate(
            kind="symbol",
            uid=row["uid"],
            token_cost=token_cost,
            graph_score=(
                1.0
                + origin_bonus
                + stem_bonus
                + api_bonus
                + factory_bonus
                + representation_bonus
                + runtime_bonus
                + config_bonus
                + orchestrator_bonus
                + executor_bonus
                + role_bonus
                + edge_bonus
            ),
            name=row["name"],
            file_path=row["file_path"],
            range=row.get("range") or [0, 0],
            render_mode="signature_only",
            relation="ROLE_BACKFILL",
            direction="backfill",
            depth=1 if origin == "same_file" else 2,
            file_hash=row.get("file_hash") or "",
            evidence_role=matched_roles[0],
            supporting_roles=[role for role in candidate_roles if role != matched_roles[0]],
            provenance=[f"{origin}-backfill:{matched_roles[0]}"],
        )
        candidate.symbol_kind = symbol_kind
        candidate.qualified_name = row.get("qualified_name", "")
        return candidate

    def dependency_flow_recovery_hint(self, row: dict, *, target: SubgraphNode) -> bool:
        target_ctx = f"{(target.name or '').lower()} {(target.file_path or '').lower()}"
        row_ctx = " ".join(
            [
                str(row.get("name") or "").lower(),
                str(row.get("qualified_name") or "").lower(),
                str(row.get("file_path") or "").lower(),
            ]
        )
        dependency_terms = (
            "depend",
            "dependent",
            "dependant",
            "dependency",
            "dependencies",
            "inject",
            "provider",
            "container",
        )
        target_hit = any(token in target_ctx for token in dependency_terms)
        row_hit = any(token in row_ctx for token in dependency_terms)
        path_hit = "/dependencies/" in row_ctx
        return row_hit and (target_hit or path_hit)

    def identity_flow_recovery_hint(
        self,
        row: dict,
        *,
        target: SubgraphNode | None = None,
    ) -> bool:
        path = (row.get("file_path") or "").lower()
        if not any(marker in path for marker in IDENTITY_ENGINE_PATH_MARKERS):
            return False
        name = (row.get("name") or "").lower()
        if name in IDENTITY_TRACE_EXECUTOR_NAMES or name in IDENTITY_TRACE_ORCHESTRATOR_NAMES:
            return True
        if name.endswith("_gate") or "_gate" in name:
            return True
        if target is not None and "actor" in (target.name or "").lower():
            return name in {"same_actor", "ingest", "ingested"} or "gate" in name
        return False

    def identity_orchestrator_recovery_signal(self, row: dict, *, target: SubgraphNode) -> bool:
        if not self.identity_flow_recovery_hint(row, target=target):
            return False
        name = (row.get("name") or "").lower()
        return name in IDENTITY_TRACE_ORCHESTRATOR_NAMES

    def executor_recovery_signal(self, row: dict, *, target: SubgraphNode) -> bool:
        if not self.identity_flow_recovery_hint(row, target=target):
            return False
        name = (row.get("name") or "").lower()
        return name in IDENTITY_TRACE_EXECUTOR_NAMES or name.endswith("_gate") or "_gate" in name

    def identity_config_recovery_signal(self, row: dict, *, target: SubgraphNode) -> bool:
        if not self.identity_flow_recovery_hint(row, target=target):
            return False
        name = (row.get("name") or "").lower()
        return name.endswith("_gate") or "_gate" in name

    def config_surface_recovery_signal(self, row: dict, *, target: SubgraphNode) -> bool:
        kind = (row.get("symbol_kind") or "").lower()
        if kind and kind not in {"function", "method", "class"}:
            return False
        if not self.dependency_flow_recovery_hint(row, target=target):
            return False
        haystack = " ".join(
            [
                str(row.get("name") or "").lower(),
                str(row.get("qualified_name") or "").lower(),
                str(row.get("file_path") or "").lower(),
            ]
        )
        return any(
            token in haystack
            for token in (
                "config",
                "param",
                "annotation",
                "field",
                "dependent",
                "dependant",
                "dependency",
            )
        )

    def orchestrator_recovery_signal(self, row: dict, *, target: SubgraphNode) -> bool:
        kind = (row.get("symbol_kind") or "").lower()
        if kind and kind not in {"function", "method", "class"}:
            return False
        name = str(row.get("name") or "").lower()
        qualified = str(row.get("qualified_name") or "").lower()
        haystack = f"{name} {qualified} {str(row.get('file_path') or '').lower()}"
        if self.hook_flow_recovery_hint(row, target=target):
            return any(token in haystack for token in HOOK_RUNTIME_TOKENS)
        if not self.dependency_flow_recovery_hint(row, target=target):
            return False
        action_hit = any(
            token in haystack
            for token in ("solve", "resolve", "get", "build", "create", "call", "execute")
        )
        dependency_hit = any(
            token in haystack
            for token in ("depend", "dependent", "dependant", "dependency", "inject")
        )
        edge_hint = float(row.get("outbound_edges", 0) or 0) >= 1.0
        return dependency_hit and (action_hit or edge_hint)

    def factory_surface_recovery_signal(
        self,
        row: dict,
        *,
        target: SubgraphNode,
        registration_flow_context: bool = False,
    ) -> bool:
        """Heuristic factory-surface signal from target-local recovery rows."""
        kind = (row.get("symbol_kind") or "").lower()
        if kind and kind not in {"function", "method", "class"}:
            return False

        name = (row.get("name") or "").lower()
        qualified = (row.get("qualified_name") or "").lower()
        file_path = (row.get("file_path") or "").lower()
        target_name = (target.name or "").lower()
        target_path = (target.file_path or "").lower()
        haystack = " ".join([name, qualified, file_path])

        prefix_hit = name.startswith(FACTORY_SIGNAL_PREFIXES)
        token_hit = any(token in haystack for token in FACTORY_SIGNAL_TOKENS)
        path_hit = any(token in file_path for token in FACTORY_SIGNAL_PATH_TOKENS)
        target_hit = any(
            token in f"{target_name} {target_path}"
            for token in ("api", "route", "router", "openapi")
        )
        edge_hint = (
            float(row.get("outbound_edges", 0) or 0) + float(row.get("inbound_edges", 0) or 0)
        ) >= 1.0
        reg_hint = (
            registration_flow_context
            and self.registration_flow_recovery_hint(row, target=target)
            and any(token in haystack for token in REGISTRATION_FACTORY_TOKENS)
        )

        score = (
            int(prefix_hit)
            + int(token_hit)
            + int(path_hit)
            + int(target_hit)
            + int(edge_hint)
            + int(reg_hint)
        )
        return score >= 2

    def api_surface_recovery_signal(
        self,
        row: dict,
        *,
        target: SubgraphNode,
        registration_flow_context: bool = False,
    ) -> bool:
        kind = (row.get("symbol_kind") or "").lower()
        if kind and kind not in {"function", "method", "class", "object_api"}:
            return False
        name = (row.get("name") or "").lower()
        qualified = (row.get("qualified_name") or "").lower()
        file_path = (row.get("file_path") or "").lower()
        haystack = " ".join([name, qualified, file_path])
        token_hit = any(token in haystack for token in API_SIGNAL_TOKENS if token != "api") or bool(
            re.search(r"(^|[^a-z])api([^a-z]|$)", haystack)
        )
        if kind == "object_api":
            token_hit = token_hit or any(
                token in haystack for token in ("client", "handler", "endpoint", "route")
            )
        edge_hint = float(row.get("outbound_edges", 0) or 0) >= 1.0
        reg_hint = (
            registration_flow_context
            and self.registration_flow_recovery_hint(row, target=target)
            and any(token in haystack for token in ("request", "app", "blueprint", "handler"))
        )
        score = int(token_hit) + int(edge_hint) + int(reg_hint)
        return score >= 2

    def representation_surface_recovery_signal(
        self,
        row: dict,
        *,
        target: SubgraphNode,
        registration_flow_context: bool = False,
    ) -> bool:
        kind = (row.get("symbol_kind") or "").lower()
        if kind and kind not in {"function", "method", "class"}:
            return False
        name = (row.get("name") or "").lower()
        qualified = (row.get("qualified_name") or "").lower()
        file_path = (row.get("file_path") or "").lower()
        target_ctx = f"{(target.name or '').lower()} {(target.file_path or '').lower()}"
        haystack = " ".join([name, qualified, file_path])
        token_hit = any(token in haystack for token in REPRESENTATION_SIGNAL_TOKENS)
        path_hit = any(token in file_path for token in REPRESENTATION_SIGNAL_PATH_TOKENS)
        target_dep_like = any(token in target_ctx for token in ("depend", "dependency", "param"))
        class_bonus = kind == "class" and any(
            t in haystack for t in ("schema", "model", "field", "response")
        )
        edge_hint = float(row.get("inbound_edges", 0) or 0) >= 1.0
        reg_hint = (
            registration_flow_context
            and self.registration_flow_recovery_hint(row, target=target)
            and any(token in haystack for token in REGISTRATION_REPRESENTATION_TOKENS)
        )
        score = (
            int(token_hit)
            + int(path_hit)
            + int(target_dep_like and (token_hit or path_hit))
            + int(class_bonus)
            + int(edge_hint)
            + int(reg_hint)
        )
        return score >= 2

    def runtime_surface_recovery_signal(
        self,
        row: dict,
        *,
        target: SubgraphNode,
        registration_flow_context: bool = False,
    ) -> bool:
        kind = (row.get("symbol_kind") or "").lower()
        if kind and kind not in {"function", "method", "class"}:
            return False
        name = (row.get("name") or "").lower()
        qualified = (row.get("qualified_name") or "").lower()
        file_path = (row.get("file_path") or "").lower()
        haystack = " ".join([name, qualified, file_path])
        token_hit = any(token in haystack for token in RUNTIME_SIGNAL_TOKENS)
        edge_hint = (
            float(row.get("outbound_edges", 0) or 0) >= 1.0
            or float(row.get("inbound_edges", 0) or 0) >= 2.0
        )
        reg_hint = (
            registration_flow_context
            and self.registration_flow_recovery_hint(row, target=target)
            and any(token in haystack for token in REGISTRATION_RUNTIME_TOKENS)
        )
        hook_hint = self.hook_flow_recovery_hint(row, target=target) and any(
            token in haystack for token in HOOK_RUNTIME_TOKENS
        )
        score = int(token_hit) + int(edge_hint) + int(reg_hint) + int(hook_hint)
        return score >= 2

    def is_registration_flow_context(
        self,
        *,
        target: SubgraphNode,
        scoped_roles: set[str],
    ) -> bool:
        if "deferred_registration" in scoped_roles:
            return True
        path = (target.file_path or "").lower()
        name = (target.name or "").lower()
        kind = (getattr(target, "kind", "") or "").lower()
        role_hint = {"api_surface", "factory_surface", "runtime_surface"}.intersection(scoped_roles)
        if not role_hint:
            return False
        haystack = f"{name} {path}"
        if kind == "object_api" or "client" in name:
            return True
        return any(token in haystack for token in REGISTRATION_FLOW_TARGET_TOKENS)

    def registration_flow_recovery_hint(self, row: dict, *, target: SubgraphNode) -> bool:
        """Shared cue pack for framework registration/request handler flows.

        Keeps Flask/Django-like registration traces from looking framework-specific
        while still requiring structural/name/path evidence.
        """
        target_ctx = f"{(target.name or '').lower()} {(target.file_path or '').lower()}"
        row_ctx = " ".join(
            [
                str(row.get("name") or "").lower(),
                str(row.get("qualified_name") or "").lower(),
                str(row.get("file_path") or "").lower(),
            ]
        )
        target_hit = any(token in target_ctx for token in REGISTRATION_FLOW_TARGET_TOKENS)
        row_hit = any(token in row_ctx for token in REGISTRATION_FLOW_TARGET_TOKENS)
        path_hit = any(token in row_ctx for token in REGISTRATION_FLOW_PATH_TOKENS)
        return (target_hit and row_hit) or path_hit

    def hook_flow_recovery_hint(self, row: dict, *, target: SubgraphNode) -> bool:
        target_ctx = f"{(target.name or '').lower()} {(target.file_path or '').lower()}"
        row_ctx = " ".join(
            [
                str(row.get("name") or "").lower(),
                str(row.get("qualified_name") or "").lower(),
                str(row.get("file_path") or "").lower(),
            ]
        )
        target_hit = any(token in target_ctx for token in HOOK_FLOW_TARGET_TOKENS)
        row_hit = any(token in row_ctx for token in HOOK_FLOW_TARGET_TOKENS)
        path_hit = any(token in row_ctx for token in HOOK_FLOW_PATH_TOKENS)
        return (target_hit and row_hit) or (target_hit and path_hit)

    def is_hook_flow_context(self, *, target: SubgraphNode, mechanism: str, query: str) -> bool:
        m = (mechanism or "").lower()
        q = (query or "").lower()
        if "hook" in m or "lifecycle" in m:
            return True
        target_ctx = f"{(target.name or '').lower()} {(target.file_path or '').lower()}"
        if any(token in target_ctx for token in HOOK_FLOW_TARGET_TOKENS):
            return True
        return any(token in q for token in HOOK_FLOW_TARGET_TOKENS)

    def needs_structural_recovery(self, target: SubgraphNode) -> bool:
        """Identify thin wrapper targets that benefit from file/import recovery.

        These symbols often act as public API facades over heavier builder
        functions. Static call edges can be sparse or parser-recovery can miss
        the inner implementation entirely, so we proactively widen the pool to
        nearby same-file/imported helpers.
        """
        if (target.kind or "") == "variable":
            return True
        if target.token_estimate and target.token_estimate <= 40:
            return True
        start, end = (target.range or [0, 0])[:2]
        return bool(start and end and start == end)

    def is_thin_dispatch_wrapper_target(
        self,
        target: SubgraphNode,
        *,
        mechanism: str = "",
        query: str = "",
    ) -> bool:
        """Public API facades that delegate in one hop (delay → apply_async, etc.)."""
        if not RankerScoring.trace_dependency_gain_mode(mechanism, query):
            return False
        if self.is_message_publish_trace_target(target, mechanism):
            return True
        token_est = int(target.token_estimate or 0)
        if 0 < token_est <= THIN_DISPATCH_MAX_TOKEN_ESTIMATE:
            start, end = (target.range or [0, 0])[:2]
            if start and end and end > start:
                return True
        return False

    @staticmethod
    def _mandatory_callee_priority(target: SubgraphNode, neighbor: dict) -> float:
        score = 1.0
        target_path = (target.file_path or "").replace("\\", "/")
        neighbor_path = (neighbor.get("file_path") or "").replace("\\", "/")
        if target_path and neighbor_path == target_path:
            score += 2.5
        name = str(neighbor.get("name") or "")
        if name in TRACE_PUBLISH_RUNTIME_NAMES:
            score += 1.5
        if name in TRACE_CONSUME_RUNTIME_NAMES:
            score += 1.5
        rel = str(neighbor.get("rel_type") or "")
        if rel in ("CALLS_DIRECT", "HAS_API", "INHERITED_API"):
            score += 0.75
        return score

    def _mandatory_callee_from_row(
        self,
        target: SubgraphNode,
        row: dict,
        *,
        depth: int,
        provenance: list[str],
    ) -> Candidate | None:
        uid = str(row.get("uid") or "")
        if not uid:
            return None
        token_cost = int(row.get("token_estimate") or 0) or self.host._estimate_tokens_range(
            row.get("range") or [0, 0]
        )
        priority = self._mandatory_callee_priority(target, row)
        candidate = Candidate(
            kind="symbol",
            uid=uid,
            token_cost=token_cost,
            graph_score=3.5 + priority,
            name=row.get("name") or "",
            file_path=row.get("file_path") or "",
            range=row.get("range") or [0, 0],
            render_mode="full",
            relation=MANDATORY_CALLEE_RELATION,
            direction="callee",
            depth=depth,
            file_hash=row.get("file_hash") or "",
            provenance=provenance,
        )
        candidate.symbol_kind = row.get("symbol_kind", "")
        candidate.qualified_name = row.get("qualified_name", "")
        return candidate


    def direct_callee_anchor_candidates(
        self,
        target: SubgraphNode,
        *,
        mechanism: str = "",
        query: str = "",
        excluded_uids: set[str],
    ) -> list[Candidate]:
        """Force depth-1 outgoing callees into the pool so BFS cannot skip them."""
        if not self.is_thin_dispatch_wrapper_target(
            target, mechanism=mechanism, query=query
        ):
            return []

        visited = set(excluded_uids)
        visited.add(target.uid)
        neighbors = self.host._get_neighbors(target.uid, visited, distance=1)
        outgoing = [
            row
            for row in neighbors
            if row.get("outgoing")
            and self.host._is_outgoing_call(str(row.get("rel_type") or ""), True)
        ]
        if not outgoing:
            return []

        outgoing.sort(
            key=lambda row: self._mandatory_callee_priority(target, row),
            reverse=True,
        )
        target_path = (target.file_path or "").replace("\\", "/")
        selected: list[dict] = []
        seen_uids: set[str] = set()
        for row in outgoing:
            uid = str(row.get("uid") or "")
            if not uid or uid in seen_uids:
                continue
            seen_uids.add(uid)
            selected.append(row)
            if len(selected) >= THIN_DISPATCH_MAX_MANDATORY_CALLEES:
                break
        if target_path:
            for row in outgoing:
                uid = str(row.get("uid") or "")
                if not uid or uid in seen_uids:
                    continue
                if (row.get("file_path") or "").replace("\\", "/") != target_path:
                    continue
                selected.insert(0, row)
                seen_uids.add(uid)
                if len(selected) > THIN_DISPATCH_MAX_MANDATORY_CALLEES:
                    selected.pop()
                break

        candidates: list[Candidate] = []
        for row in selected:
            candidate = self._mandatory_callee_from_row(
                target,
                row,
                depth=1,
                provenance=[
                    f"mandatory-callee:{row.get('rel_type', 'CALLS')}",
                    f"callee:{row.get('name', '')}",
                ],
            )
            if candidate is not None:
                candidates.append(candidate)

        if self.is_message_publish_trace_target(target, mechanism) and candidates:
            chain_seen = set(excluded_uids) | {target.uid} | {c.uid for c in candidates}
            chain_added = 0
            for hop1 in candidates[:THIN_DISPATCH_MAX_MANDATORY_CALLEES]:
                hop_neighbors = self.host._get_neighbors(hop1.uid, set(chain_seen), distance=1)
                outgoing_chain = [
                    row
                    for row in hop_neighbors
                    if row.get("outgoing")
                    and self.host._is_outgoing_call(str(row.get("rel_type") or ""), True)
                ]
                outgoing_chain.sort(
                    key=lambda row: self._mandatory_callee_priority(target, row),
                    reverse=True,
                )
                for row in outgoing_chain:
                    uid = str(row.get("uid") or "")
                    if not uid or uid in chain_seen:
                        continue
                    chain_seen.add(uid)
                    chain_candidate = self._mandatory_callee_from_row(
                        target,
                        row,
                        depth=2,
                        provenance=[
                            f"mandatory-chain:{hop1.name}",
                            f"callee:{row.get('name', '')}",
                        ],
                    )
                    if chain_candidate is None:
                        continue
                    candidates.append(chain_candidate)
                    chain_added += 1
                    if chain_added >= THIN_DISPATCH_MAX_CHAIN_CALLEES:
                        break
                if chain_added >= THIN_DISPATCH_MAX_CHAIN_CALLEES:
                    break
        return candidates

    @staticmethod
    def merge_mandatory_callee_pool(
        pool: list[Candidate], mandatory: list[Candidate]
    ) -> list[Candidate]:
        merged: dict[str, Candidate] = {candidate.uid: candidate for candidate in pool}
        for candidate in mandatory:
            existing = merged.get(candidate.uid)
            if existing is None:
                merged[candidate.uid] = candidate
                continue
            existing.graph_score = max(existing.graph_score, candidate.graph_score)
            existing.relation = MANDATORY_CALLEE_RELATION
            existing.render_mode = "full"
            existing.depth = min(existing.depth, candidate.depth)
            is_contract = any(
                str(step).startswith("mandatory-") and str(step).endswith("-contract")
                for step in candidate.provenance
            )
            candidate_range_contains_existing = (
                isinstance(existing.range, list)
                and isinstance(candidate.range, list)
                and len(existing.range) >= 2
                and len(candidate.range) >= 2
                and int(candidate.range[0]) <= int(existing.range[0])
                and int(candidate.range[1]) >= int(existing.range[1])
            )
            if not existing.range or existing.range == [0, 0]:
                existing.range = candidate.range
            elif is_contract and candidate_range_contains_existing:
                existing.range = candidate.range
            if existing.token_cost <= 1 or existing.range == candidate.range:
                existing.token_cost = max(existing.token_cost, candidate.token_cost)
            if not existing.file_hash:
                existing.file_hash = candidate.file_hash
            if not getattr(existing, "qualified_name", ""):
                existing.qualified_name = getattr(candidate, "qualified_name", "")
            existing.provenance = list(existing.provenance) + list(candidate.provenance)
        return list(merged.values())

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------
