"""MRO API bridge: expose inherited public methods on class nodes without graph explosion.

For each indexed Python class, walk the ``DEPENDS_ON`` inheritance chain and link
public methods as a dynamic API surface:

- ``(Class)-[:HAS_API]->(method)`` for methods defined on the class
- ``(Class)-[:INHERITED_API {originating_class}]->(method)`` for inherited methods

This lets ``get_target("Task.apply_async")`` resolve via API edges instead of
requiring every inherited method to be re-indexed under the subclass name.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from sidecar.database.neo4j_client import Neo4jClient
from sidecar.parser.protocol import ClassApiEdge

_MAX_MRO_DEPTH = 24
_SKIP_METHOD_PREFIXES = ("_",)


@dataclass(frozen=True)
class ClassRecord:
    uid: str
    name: str
    qualified_name: str
    file_path: str


@dataclass(frozen=True)
class MethodRecord:
    uid: str
    name: str
    qualified_name: str
    owner_class_name: str


def parse_class_method_symbol(symbol_name: str) -> tuple[str, str] | None:
    """Split ``Class.method`` benchmark notation; reject path-like tokens."""
    raw = (symbol_name or "").strip()
    if not raw or raw.count(".") != 1:
        return None
    class_name, method_name = raw.split(".", 1)
    if not class_name or not method_name:
        return None
    if "/" in class_name or "/" in method_name:
        return None
    if class_name.startswith(".") or method_name.startswith("."):
        return None
    return class_name, method_name


def _is_public_api_method(name: str) -> bool:
    return bool(name) and not name.startswith(_SKIP_METHOD_PREFIXES)


def _owner_class_from_qualified_name(qualified_name: str) -> str | None:
    parts = [part for part in (qualified_name or "").split(".") if part]
    if len(parts) < 2:
        return None
    return parts[-2]


def index_methods_by_owner(methods: list[MethodRecord]) -> dict[str, list[MethodRecord]]:
    grouped: dict[str, list[MethodRecord]] = {}
    for method in methods:
        if not _is_public_api_method(method.name):
            continue
        owner = method.owner_class_name or _owner_class_from_qualified_name(method.qualified_name)
        if not owner:
            continue
        grouped.setdefault(owner, []).append(method)
    return grouped


def build_mro_api_edges(
    classes: list[ClassRecord],
    inheritance: dict[str, list[str]],
    methods_by_owner_name: dict[str, list[MethodRecord]],
    *,
    class_by_uid: dict[str, ClassRecord],
) -> list[ClassApiEdge]:
    """Build HAS_API / INHERITED_API edges from inheritance + owner-indexed methods."""
    edges: list[ClassApiEdge] = []
    seen: set[tuple[str, str, str]] = set()

    for cls in classes:
        chain = _iter_mro(cls.uid, inheritance)
        for depth, ancestor_uid in enumerate(chain):
            ancestor = class_by_uid.get(ancestor_uid)
            if ancestor is None:
                continue
            for method in methods_by_owner_name.get(ancestor.name, []):
                key = (cls.uid, method.uid, "HAS_API" if depth == 0 else "INHERITED_API")
                if key in seen:
                    continue
                seen.add(key)
                if depth == 0:
                    edges.append(
                        ClassApiEdge(
                            class_uid=cls.uid,
                            method_uid=method.uid,
                            edge_type="HAS_API",
                        )
                    )
                else:
                    edges.append(
                        ClassApiEdge(
                            class_uid=cls.uid,
                            method_uid=method.uid,
                            edge_type="INHERITED_API",
                            originating_class=ancestor.qualified_name or ancestor.name,
                        )
                    )
    return edges


def _iter_mro(class_uid: str, inheritance: dict[str, list[str]]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(class_uid, 0)])
    while queue:
        uid, depth = queue.popleft()
        if uid in seen or depth > _MAX_MRO_DEPTH:
            continue
        seen.add(uid)
        ordered.append(uid)
        for parent_uid in inheritance.get(uid, []):
            queue.append((parent_uid, depth + 1))
    return ordered


class MroApiBridgeIndexer:
    """Workspace pass that materializes class API surfaces after inheritance linking."""

    def __init__(self, db: Neo4jClient):
        self.db = db

    def apply(self, workspace_id: str) -> int:
        classes = self._load_classes(workspace_id)
        if not classes:
            self.db.clear_class_api_edges(workspace_id=workspace_id)
            return 0

        inheritance = self._load_inheritance(workspace_id)
        methods = self._load_methods(workspace_id)
        methods_by_owner = index_methods_by_owner(methods)
        class_by_uid = {cls.uid: cls for cls in classes}
        edges = build_mro_api_edges(
            classes,
            inheritance,
            methods_by_owner,
            class_by_uid=class_by_uid,
        )
        self.db.clear_class_api_edges(workspace_id=workspace_id)
        self.db.link_class_api(edges, workspace_id=workspace_id)
        return len(edges)

    def _load_classes(self, workspace_id: str) -> list[ClassRecord]:
        query = """
        MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(c:Symbol {kind: 'class'})
        RETURN c.uid AS uid,
               c.name AS name,
               coalesce(c.qualified_name, c.name) AS qualified_name,
               f.path AS file_path
        """
        with self.db.driver.session() as session:
            rows = list(session.run(query, workspace_id=workspace_id))
        return [
            ClassRecord(
                uid=str(row["uid"]),
                name=str(row["name"]),
                qualified_name=str(row["qualified_name"] or row["name"]),
                file_path=str(row["file_path"]),
            )
            for row in rows
            if row.get("uid") and row.get("name")
        ]

    def _load_inheritance(self, workspace_id: str) -> dict[str, list[str]]:
        query = """
        MATCH (sub:Symbol)-[:DEPENDS_ON {workspace_id: $workspace_id}]->(sup:Symbol)
        RETURN sub.uid AS subclass_uid, sup.uid AS superclass_uid
        """
        with self.db.driver.session() as session:
            rows = list(session.run(query, workspace_id=workspace_id))
        graph: dict[str, list[str]] = {}
        for row in rows:
            sub_uid = str(row.get("subclass_uid") or "")
            sup_uid = str(row.get("superclass_uid") or "")
            if sub_uid and sup_uid:
                graph.setdefault(sub_uid, []).append(sup_uid)
        return graph

    def _load_methods(self, workspace_id: str) -> list[MethodRecord]:
        query = """
        MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(m:Symbol)
        WHERE coalesce(m.kind, '') IN ['function', 'method']
        RETURN m.uid AS uid,
               m.name AS name,
               coalesce(m.qualified_name, m.name) AS qualified_name
        """
        with self.db.driver.session() as session:
            rows = list(session.run(query, workspace_id=workspace_id))
        methods: list[MethodRecord] = []
        for row in rows:
            qn = str(row.get("qualified_name") or row.get("name") or "")
            owner = _owner_class_from_qualified_name(qn) or ""
            methods.append(
                MethodRecord(
                    uid=str(row["uid"]),
                    name=str(row["name"]),
                    qualified_name=qn,
                    owner_class_name=owner,
                )
            )
        return methods
