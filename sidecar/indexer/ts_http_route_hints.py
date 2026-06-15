"""Link TypeScript HTTP client calls to Python FastAPI route handlers."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from sidecar.database.neo4j_client import Neo4jClient

_SKIP_DIRS = {
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
    "tests",
    "test",
    "QA",
}


def _should_skip_python_route_file(file_path: str) -> bool:
    normalized = file_path.replace("\\", "/")
    name = os.path.basename(normalized)
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    parts = set(normalized.split("/"))
    return bool(parts & {"tests", "test", "QA"})


def _prefer_route(existing: HttpRoute, candidate: HttpRoute) -> HttpRoute:
    """Prefer app entrypoints over fixtures when the same path appears twice."""
    existing_score = _route_file_score(existing.file_path)
    candidate_score = _route_file_score(candidate.file_path)
    if candidate_score > existing_score:
        return candidate
    return existing


def _route_file_score(file_path: str) -> int:
    normalized = file_path.replace("\\", "/")
    score = 0
    if "/sidecar/" in normalized or normalized.endswith("/sidecar/main.py"):
        score += 10
    if "main.py" in normalized:
        score += 5
    if _should_skip_python_route_file(normalized):
        score -= 20
    return score


_ROUTE_DECORATOR_RE = re.compile(r"""@app\.(?:get|post|put|delete|patch)\(\s*["']([^"']+)["']""")
_PYTHON_HANDLER_RE = re.compile(r"^(?:async\s+)?def\s+(\w+)\s*\(")
_TS_OBJECT_API_RE = re.compile(r"(?m)^export\s+const\s+([A-Za-z_$][\w$]*)\s*=\s*\{")
_TS_HTTP_PATH_RE = re.compile(
    r"""\b(?:post|get|put|delete|patch|fetch)\(\s*(?:`\$\{[^}]+\}([^`'"]+)`|["']([^"']+)["'])"""
)


@dataclass(frozen=True)
class HttpRoute:
    path: str
    handler_name: str
    file_path: str


class TsHttpRouteHintsIndexer:
    """Create SEMANTIC_HINT edges from TS client surfaces to Python route handlers."""

    def __init__(self, db: Neo4jClient, project_path: str):
        self.db = db
        self.project_path = os.path.abspath(project_path)

    def apply(self, diffs: list, workspace_id: str) -> int:
        route_map = self._scan_python_routes()
        if not route_map:
            return 0

        created = 0
        for diff in diffs:
            path = diff.extracted.path
            if not path.endswith((".ts", ".tsx")):
                continue
            source = diff.extracted.source
            object_apis = self._scan_ts_object_apis(source)
            if not object_apis:
                continue
            for route_path, _handler_name, _ in self._scan_ts_http_paths(source):
                route = route_map.get(route_path)
                if route is None:
                    continue
                for object_name in object_apis:
                    if self._link_symbols(
                        workspace_id=workspace_id,
                        source_name=object_name,
                        source_file=path,
                        target_name=route.handler_name,
                        target_file=route.file_path,
                        route_path=route_path,
                    ):
                        created += 1
        return created

    def _scan_python_routes(self) -> dict[str, HttpRoute]:
        routes: dict[str, HttpRoute] = {}
        for root, dirs, filenames in os.walk(self.project_path):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                file_path = os.path.join(root, filename)
                if _should_skip_python_route_file(file_path):
                    continue
                try:
                    lines = open(file_path, encoding="utf-8", errors="ignore").read().splitlines()
                except OSError:
                    continue
                pending_path: str | None = None
                for line in lines:
                    route_match = _ROUTE_DECORATOR_RE.search(line)
                    if route_match:
                        pending_path = route_match.group(1)
                        continue
                    if pending_path:
                        handler_match = _PYTHON_HANDLER_RE.match(line.strip())
                        if handler_match:
                            candidate = HttpRoute(
                                path=pending_path,
                                handler_name=handler_match.group(1),
                                file_path=file_path,
                            )
                            existing = routes.get(pending_path)
                            routes[pending_path] = (
                                _prefer_route(existing, candidate)
                                if existing is not None
                                else candidate
                            )
                            pending_path = None
        return routes

    @staticmethod
    def _scan_ts_object_apis(source: str) -> set[str]:
        return {match.group(1) for match in _TS_OBJECT_API_RE.finditer(source)}

    @staticmethod
    def _scan_ts_http_paths(source: str) -> list[tuple[str, str, int]]:
        found: list[tuple[str, str, int]] = []
        for match in _TS_HTTP_PATH_RE.finditer(source):
            path = match.group(1) or match.group(2) or ""
            if not path.startswith("/"):
                continue
            found.append((path, match.group(0), match.start()))
        return found

    def _link_symbols(
        self,
        *,
        workspace_id: str,
        source_name: str,
        source_file: str,
        target_name: str,
        target_file: str,
        route_path: str,
    ) -> bool:
        query = """
        MATCH (source:Symbol {name: $source_name, workspace_id: $workspace_id})
        MATCH (sf:File {path: $source_file, workspace_id: $workspace_id})-[:CONTAINS]->(source)
        MATCH (target:Symbol {name: $target_name, workspace_id: $workspace_id})
        MATCH (tf:File {path: $target_file, workspace_id: $workspace_id})-[:CONTAINS]->(target)
        MERGE (source)-[r:SEMANTIC_HINT {
            workspace_id: $workspace_id,
            kind: "http_route"
        }]->(target)
        SET r.route_path = $route_path,
            r.derived_at = datetime()
        RETURN count(r) AS linked
        """
        try:
            with self.db.driver.session() as session:
                row = session.run(
                    query,
                    workspace_id=workspace_id,
                    source_name=source_name,
                    source_file=source_file,
                    target_name=target_name,
                    target_file=target_file,
                    route_path=route_path,
                ).single()
        except Exception:
            return False
        return bool(row and row.get("linked"))
