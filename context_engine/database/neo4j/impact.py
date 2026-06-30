"""Symbol resolution and structural impact-surface lookups."""

from typing import Any

from context_engine.workspace import DEFAULT_WORKSPACE_ID


class ImpactMixin:
    driver: Any

    def get_symbol_uid_by_name(
        self, name: str, workspace_id: str = DEFAULT_WORKSPACE_ID
    ) -> str | None:
        """Return the UID of the first symbol matching `name` in this workspace, or None."""
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol {name: $name})
                RETURN s.uid AS uid LIMIT 1
                """,
                name=name,
                workspace_id=workspace_id,
            ).single()
        return result["uid"] if result else None

    def get_symbol_uid_by_name_in_file(
        self,
        name: str,
        file_path: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> str | None:
        """Return symbol UID when `name` is defined in `file_path` for this workspace."""
        path = file_path.strip()
        if not path:
            return None
        suffix = f"/{path.rsplit('/', 1)[-1]}"
        with self.driver.session() as session:
            rows = session.run(
                """
                MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol {name: $name})
                WHERE f.path = $path
                   OR f.path ENDS WITH $path
                   OR f.path ENDS WITH $suffix
                RETURN s.uid AS uid, f.path AS path
                """,
                name=name,
                workspace_id=workspace_id,
                path=path,
                suffix=suffix,
            )
            candidates = [
                {"uid": str(row["uid"]), "path": str(row["path"] or "")}
                for row in rows
                if row.get("uid")
            ]
        if not candidates:
            return None
        best = min(candidates, key=lambda row: self._impact_path_rank(str(row["path"])))
        return str(best["uid"])

    def list_symbol_impact_candidates(
        self,
        name: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> list[dict[str, str | int]]:
        """Return symbol candidates with call fan-in and endpoint evidence."""
        with self.driver.session() as session:
            rows = session.run(
                """
                MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol {name: $name})
                OPTIONAL MATCH (caller:Symbol)-[:CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED]->(s)
                OPTIONAL MATCH (s)-[endpoint_rel:CALLS_ENDPOINT|IMPLEMENTS_ENDPOINT]->(:ApiEndpoint)
                RETURN s.uid AS uid, f.path AS path, count(DISTINCT caller) AS incoming,
                       count(DISTINCT endpoint_rel) AS endpoint_edges
                ORDER BY endpoint_edges DESC, incoming DESC, path ASC
                """,
                name=name,
                workspace_id=workspace_id,
            )
            return [
                {
                    "uid": str(row["uid"]),
                    "path": str(row["path"]),
                    "incoming": int(row["incoming"] or 0),
                    "endpoint_edges": int(row["endpoint_edges"] or 0),
                }
                for row in rows
            ]

    @staticmethod
    def _path_matches_file(stored_path: str, file_path: str) -> bool:
        stored = stored_path.strip().replace("\\", "/").rstrip("/")
        requested = file_path.strip().replace("\\", "/").rstrip("/")
        if not stored or not requested:
            return False
        if (
            stored == requested
            or stored.endswith(f"/{requested.lstrip('/')}")
            or requested.endswith(f"/{stored.lstrip('/')}")
        ):
            return True
        return "/" not in requested and stored.rsplit("/", 1)[-1] == requested

    @staticmethod
    def _impact_path_rank(path: str) -> tuple[int, int]:
        """Prefer canonical source trees over legacy mirror paths."""
        lowered = path.lower()
        penalty = 0
        if "/context_engine/" in lowered or lowered.endswith("/context_engine"):
            penalty += 10
        if "/qa/" in lowered:
            penalty += 5
        return (penalty, len(path))

    def resolve_impact_symbol_uid(
        self,
        name: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        *,
        file_path: str | None = None,
    ) -> str | None:
        """Resolve an impact target, preserving explicit file identity when possible."""
        candidates = self.list_symbol_impact_candidates(name, workspace_id=workspace_id)
        if not candidates:
            return None

        requested = file_path.strip() if file_path else ""
        if requested:
            matched = [
                candidate
                for candidate in candidates
                if self._path_matches_file(str(candidate["path"]), requested)
            ]
            if matched:
                best_matched = max(
                    matched,
                    key=lambda candidate: (
                        int(candidate.get("endpoint_edges", 0)),
                        int(candidate["incoming"]),
                        -self._impact_path_rank(str(candidate["path"]))[0],
                    ),
                )
                return str(best_matched["uid"])
            # An explicit editor path is an identity constraint, not a ranking
            # hint. Falling back to another file here makes common names such
            # as ``ask`` or ``run`` silently jump across languages/modules.
            return None

        best = max(
            candidates,
            key=lambda candidate: (
                int(candidate.get("endpoint_edges", 0)),
                int(candidate["incoming"]),
                -self._impact_path_rank(str(candidate["path"]))[0],
            ),
        )
        return str(best["uid"])

    def get_file_path_for_symbol(
        self, symbol_uid: str, workspace_id: str = DEFAULT_WORKSPACE_ID
    ) -> str:
        """Return the file path containing `symbol_uid`, or '<unknown>'."""
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (s:Symbol {uid: $uid})
                OPTIONAL MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(s)
                RETURN coalesce(f.path, '<unknown>') AS file_path
                """,
                uid=symbol_uid,
                workspace_id=workspace_id,
            ).single()
        return result["file_path"] if result else "<unknown>"

    def get_symbol_spans_by_uids(
        self,
        uids: list[str],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> dict[str, dict[str, int | str]]:
        """Return name, file path, and line span for each uid in the workspace."""
        if not uids:
            return {}
        with self.driver.session() as session:
            rows = list(
                session.run(
                    """
                UNWIND $uids AS uid
                MATCH (f:File {workspace_id: $workspace_id})-[c:CONTAINS]->(s:Symbol {uid: uid})
                RETURN s.uid AS uid,
                       s.name AS name,
                       f.path AS file_path,
                       coalesce(c.start_line, s.range[0], 0) AS start_line,
                       coalesce(c.end_line, s.range[1], 0) AS end_line
                """,
                    uids=list(dict.fromkeys(uids)),
                    workspace_id=workspace_id,
                )
            )
        out: dict[str, dict[str, int | str]] = {}
        for row in rows:
            uid = str(row.get("uid") or "")
            if not uid:
                continue
            out[uid] = {
                "name": str(row.get("name") or ""),
                "file_path": str(row.get("file_path") or ""),
                "start_line": int(row.get("start_line") or 0),
                "end_line": int(row.get("end_line") or 0),
            }
        return out
