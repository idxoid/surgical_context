"""Structural recovery: trace anchors, import bridging, role-surface heuristics."""

from __future__ import annotations

import math
from pathlib import Path

from sidecar.context.role_taxonomy import normalize_roles
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
    REGISTRATION_FACTORY_TOKENS,
    REGISTRATION_FLOW_PATH_TOKENS,
    REGISTRATION_FLOW_TARGET_TOKENS,
    REGISTRATION_REPRESENTATION_TOKENS,
    REGISTRATION_RUNTIME_TOKENS,
    REPRESENTATION_SIGNAL_PATH_TOKENS,
    REPRESENTATION_SIGNAL_TOKENS,
    RUNTIME_SIGNAL_TOKENS,
    TRACE_DEPENDENCY_RUNTIME_NAME_TOKENS,
    TRACE_DEPENDENCY_TARGET_TOKENS,
    TRACE_HOOK_RUNTIME_NAMES,
    TRACE_HOOK_RUNTIME_TRIGGER_NAMES,
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
        for fp in sorted(by_file.keys()):
            rows_for_fp = sorted(
                by_file[fp],
                key=lambda r: (
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
        role = self.first_reasoning_role(required_roles)
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
            supporting_roles=[],
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

    def trace_dependency_runtime_symbol_rows(
        self,
        target: SubgraphNode,
        *,
        excluded_uids: set[str],
    ) -> list[dict]:
        """Workspace symbols for trace/runtime resolution when import topology is sparse."""
        target_name = target.name or ""
        name_terms: list[str] = []
        names: list[str] = []
        if self.is_dependency_marker_target(target):
            name_terms = list(TRACE_DEPENDENCY_RUNTIME_NAME_TOKENS)
        elif target_name in TRACE_HOOK_RUNTIME_TRIGGER_NAMES:
            names = list(TRACE_HOOK_RUNTIME_NAMES)
        else:
            return []
        excluded = set(excluded_uids)
        if target.uid:
            excluded.add(target.uid)
        pkg_prefix = self.package_root_prefix(target.file_path or "") or ""
        query = """
        MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
        WHERE (
            (size($names) > 0 AND s.name IN $names)
            OR any(term IN $name_terms
                WHERE toLower(s.name) CONTAINS term
                   OR toLower(coalesce(s.qualified_name, '')) CONTAINS term)
          )
          AND NOT s.uid IN $excluded_uids
          AND (
            $pkg_prefix = ''
            OR f.path STARTS WITH $pkg_prefix
            OR f.path CONTAINS '/' + $pkg_prefix
          )
        OPTIONAL MATCH ()-[cr:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->(s)
        WHERE coalesce(cr.workspace_id, $workspace_id) = $workspace_id
        WITH s, f, c, count(DISTINCT cr) AS inbound_edges
        OPTIONAL MATCH (s)-[or:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->()
        WHERE coalesce(or.workspace_id, $workspace_id) = $workspace_id
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               count(DISTINCT or) AS outbound_edges
        LIMIT 32
        """
        try:
            with self.db.driver.session() as session:
                return list(
                    session.run(
                        query,
                        workspace_id=self.workspace_id,
                        names=names,
                        name_terms=name_terms,
                        excluded_uids=list(excluded),
                        pkg_prefix=pkg_prefix,
                    )
                )
        except Exception:
            return []

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
        OPTIONAL MATCH ()-[cr:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->(s)
        WHERE coalesce(cr.workspace_id, $workspace_id) = $workspace_id
        WITH s, f, c, count(DISTINCT cr) AS inbound_edges
        OPTIONAL MATCH (s)-[or:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->()
        WHERE coalesce(or.workspace_id, $workspace_id) = $workspace_id
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               count(DISTINCT or) AS outbound_edges
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
        OPTIONAL MATCH ()-[cr:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->(s)
        WHERE coalesce(cr.workspace_id, $workspace_id) = $workspace_id
        WITH s, f, c, count(DISTINCT cr) AS inbound_edges
        OPTIONAL MATCH (s)-[or:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->()
        WHERE coalesce(or.workspace_id, $workspace_id) = $workspace_id
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               count(DISTINCT or) AS outbound_edges
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
        )
        sibling_rows = self.trace_dependency_sibling_dir_symbol_rows(
            runtime_rows,
            excluded_uids=excluded_uids,
        )
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
        merged: list[dict] = []
        seen_merge: set[str] = set()
        for row in (*runtime_rows, *sibling_rows, *parent_rows, *imported_rows):
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
            seen.add(uid)
            out.append(candidate)
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
        OPTIONAL MATCH ()-[cr:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->(s)
        WHERE coalesce(cr.workspace_id, $workspace_id) = $workspace_id
        WITH s, f, c, count(DISTINCT cr) AS inbound_edges
        OPTIONAL MATCH (s)-[or:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->()
        WHERE coalesce(or.workspace_id, $workspace_id) = $workspace_id
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               count(DISTINCT or) AS outbound_edges
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
        MATCH (f:File {workspace_id: $workspace_id, path: $file_path})-[:IMPORTS]->(dep:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol)
        WHERE NOT s.uid IN $excluded_uids
        OPTIONAL MATCH ()-[cr:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->(s)
        WHERE coalesce(cr.workspace_id, $workspace_id) = $workspace_id
        WITH s, dep, c, count(DISTINCT cr) AS inbound_edges
        OPTIONAL MATCH (s)-[or:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->()
        WHERE coalesce(or.workspace_id, $workspace_id) = $workspace_id
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(dep.path, '<unknown>') AS file_path,
               coalesce(dep.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               count(DISTINCT or) AS outbound_edges
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
        OPTIONAL MATCH ()-[cr:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->(s)
        WHERE coalesce(cr.workspace_id, $workspace_id) = $workspace_id
        WITH s, f, c, count(DISTINCT cr) AS inbound_edges
        OPTIONAL MATCH (s)-[or:CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|SEMANTIC_HINT]->()
        WHERE coalesce(or.workspace_id, $workspace_id) = $workspace_id
        RETURN s.uid AS uid,
               s.name AS name,
               coalesce(s.kind, '') AS symbol_kind,
               coalesce(s.token_estimate, 0) AS token_estimate,
               coalesce(s.qualified_name, '') AS qualified_name,
               coalesce(f.path, '<unknown>') AS file_path,
               coalesce(f.hash, '') AS file_hash,
               coalesce(c.range, s.range, [0, 0]) AS range,
               inbound_edges,
               count(DISTINCT or) AS outbound_edges
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
        probe.symbol_kind = row.get("symbol_kind", "")
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
        if "api_surface" in scoped_roles and api_signal:
            candidate_roles = normalize_roles([*candidate_roles, "api_surface"])
        if "factory_surface" in scoped_roles and factory_signal:
            candidate_roles = normalize_roles([*candidate_roles, "factory_surface"])
        if "config_surface" in scoped_roles and config_signal:
            candidate_roles = normalize_roles([*candidate_roles, "config_surface"])
        if "orchestrator" in scoped_roles and orchestrator_signal:
            candidate_roles = normalize_roles([*candidate_roles, "orchestrator"])
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
            0.22 if ("orchestrator" in matched_roles and orchestrator_signal) else 0.0
        )
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
        candidate.symbol_kind = row.get("symbol_kind", "")
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
        if not self.dependency_flow_recovery_hint(row, target=target):
            return False
        name = str(row.get("name") or "").lower()
        qualified = str(row.get("qualified_name") or "").lower()
        haystack = f"{name} {qualified} {str(row.get('file_path') or '').lower()}"
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
        if kind and kind not in {"function", "method", "class"}:
            return False
        name = (row.get("name") or "").lower()
        qualified = (row.get("qualified_name") or "").lower()
        file_path = (row.get("file_path") or "").lower()
        target_ctx = f"{(target.name or '').lower()} {(target.file_path or '').lower()}"
        haystack = " ".join([name, qualified, file_path, target_ctx])
        token_hit = any(token in haystack for token in API_SIGNAL_TOKENS)
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
        haystack = " ".join([name, qualified, file_path, target_ctx])
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
        target_ctx = f"{(target.name or '').lower()} {(target.file_path or '').lower()}"
        haystack = " ".join([name, qualified, file_path, target_ctx])
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
        role_hint = {"api_surface", "factory_surface", "runtime_surface"}.intersection(scoped_roles)
        if not role_hint:
            return False
        haystack = f"{name} {path}"
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
        return "before_request" in q or "after_request" in q

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

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------
