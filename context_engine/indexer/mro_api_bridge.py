"""MRO API bridge: expose public method surfaces on class nodes.

For each indexed class, link public methods defined directly on that class:

- ``(Class)-[:HAS_API]->(method)``

For inherited methods, walk the already-materialized ``DEPENDS_ON`` inheritance
graph and link visible ancestor methods:

- ``(Class)-[:INHERITED_API]->(ancestor_method)``
"""

from __future__ import annotations

from dataclasses import dataclass

from context_engine.database.neo4j_client import Neo4jClient
from context_engine.parser.protocol import ClassApiEdge

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
    owner_class_uid: str = ""
    owner_class_name: str = ""


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
    if not name:
        return False
    if name == "__init__":
        return True
    return not name.startswith(_SKIP_METHOD_PREFIXES)


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
        owner = (
            method.owner_class_uid
            or method.owner_class_name
            or _owner_class_from_qualified_name(method.qualified_name)
        )
        if not owner:
            continue
        grouped.setdefault(owner, []).append(method)
    return grouped


def _methods_for_class(
    cls: ClassRecord,
    methods_by_owner_name: dict[str, list[MethodRecord]],
) -> list[MethodRecord]:
    return list(methods_by_owner_name.get(cls.uid, []))


def _originating_class(method: MethodRecord, class_by_uid: dict[str, ClassRecord]) -> str:
    if method.owner_class_uid:
        owner = class_by_uid.get(method.owner_class_uid)
        if owner:
            return owner.qualified_name or owner.name
    return method.owner_class_name or _owner_class_from_qualified_name(method.qualified_name) or ""


def build_mro_api_edges(
    classes: list[ClassRecord],
    inheritance: dict[str, list[str]],
    methods_by_owner_name: dict[str, list[MethodRecord]],
    *,
    class_by_uid: dict[str, ClassRecord],
) -> list[ClassApiEdge]:
    """Build direct ``HAS_API`` and inherited ``INHERITED_API`` edges."""
    edges: list[ClassApiEdge] = []
    seen: set[tuple[str, str, str]] = set()
    direct_methods_by_class = {
        cls.uid: _methods_for_class(cls, methods_by_owner_name) for cls in classes
    }
    api_surface_cache: dict[str, dict[str, MethodRecord]] = {}

    def api_surface(class_uid: str, visiting: set[str] | None = None) -> dict[str, MethodRecord]:
        cached = api_surface_cache.get(class_uid)
        if cached is not None:
            return cached
        if visiting is None:
            visiting = set()
        if class_uid in visiting:
            return {}
        visiting.add(class_uid)

        surface: dict[str, MethodRecord] = {}
        for method in direct_methods_by_class.get(class_uid, []):
            surface.setdefault(method.name, method)
        for superclass_uid in inheritance.get(class_uid, []):
            if superclass_uid not in class_by_uid:
                continue
            for name, method in api_surface(superclass_uid, visiting).items():
                surface.setdefault(name, method)

        visiting.remove(class_uid)
        api_surface_cache[class_uid] = surface
        return surface

    for cls in classes:
        direct_methods = direct_methods_by_class.get(cls.uid, [])
        direct_names = {method.name for method in direct_methods}

        for method in direct_methods:
            key = ("HAS_API", cls.uid, method.uid)
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                ClassApiEdge(
                    class_uid=cls.uid,
                    method_uid=method.uid,
                    edge_type="HAS_API",
                )
            )

        inherited_names: set[str] = set()
        for superclass_uid in inheritance.get(cls.uid, []):
            if superclass_uid not in class_by_uid:
                continue
            for name, method in api_surface(superclass_uid).items():
                if name in direct_names or name in inherited_names:
                    continue
                if method.owner_class_uid == cls.uid:
                    continue
                key = ("INHERITED_API", cls.uid, method.uid)
                if key in seen:
                    continue
                seen.add(key)
                inherited_names.add(name)
                edges.append(
                    ClassApiEdge(
                        class_uid=cls.uid,
                        method_uid=method.uid,
                        edge_type="INHERITED_API",
                        originating_class=_originating_class(method, class_by_uid),
                    )
                )
    return edges


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
        methods = self._load_methods(workspace_id, classes)
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

    def _load_methods(self, workspace_id: str, classes: list[ClassRecord]) -> list[MethodRecord]:
        classes_by_file = _classes_by_file(classes)
        query = """
        MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(m:Symbol)
        WHERE coalesce(m.kind, '') IN ['function', 'method']
        RETURN m.uid AS uid,
               m.name AS name,
               coalesce(m.qualified_name, m.name) AS qualified_name,
               f.path AS file_path
        """
        with self.db.driver.session() as session:
            rows = list(session.run(query, workspace_id=workspace_id))
        methods: list[MethodRecord] = []
        for row in rows:
            qn = str(row.get("qualified_name") or row.get("name") or "")
            owner = _method_owner_class(qn, str(row.get("file_path") or ""), classes_by_file)
            methods.append(
                MethodRecord(
                    uid=str(row["uid"]),
                    name=str(row["name"]),
                    qualified_name=qn,
                    owner_class_uid=owner.uid if owner else "",
                    owner_class_name=owner.name if owner else "",
                )
            )
        return methods


def _classes_by_file(classes: list[ClassRecord]) -> dict[str, list[ClassRecord]]:
    grouped: dict[str, list[ClassRecord]] = {}
    for cls in classes:
        grouped.setdefault(cls.file_path, []).append(cls)
    for file_classes in grouped.values():
        file_classes.sort(key=lambda cls: len(cls.qualified_name or cls.name), reverse=True)
    return grouped


def _method_owner_class(
    method_qualified_name: str,
    file_path: str,
    classes_by_file: dict[str, list[ClassRecord]],
) -> ClassRecord | None:
    if not method_qualified_name or not file_path:
        return None
    for cls in classes_by_file.get(file_path, []):
        class_qualified_name = cls.qualified_name or cls.name
        if method_qualified_name.startswith(f"{class_qualified_name}."):
            return cls
    return None
