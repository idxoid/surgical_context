"""Python language adapter using tree-sitter."""

import importlib.metadata
import re
import sys
from functools import lru_cache
from pathlib import Path

from tree_sitter import Query

from sidecar.parser.adapters.treesitter_base import TreeSitterAdapter
from sidecar.parser.protocol import ImportEdge, InheritanceEdge
from sidecar.parser.uid import (
    _node_text,
    compute_uid,
    current_project_root,
    module_name_from_path,
    qualified_name_for,
    signature_from_node,
)

# Language/stdlib decorators that are machinery, never a meaningful DECORATED_BY
# target — skip them so we don't attempt no-op edges. Anything else (framework
# or in-repo decorators) is kept and resolved structurally.
_BUILTIN_DECORATORS = frozenset(
    {
        "property",
        "staticmethod",
        "classmethod",
        "abstractmethod",
        "abstractproperty",
        "cached_property",
        "wraps",
        "lru_cache",
        "cache",
        "contextmanager",
        "asynccontextmanager",
        "dataclass",
        "override",
        "final",
        "overload",
        "setter",
        "getter",
        "deleter",
    }
)


class PythonAdapter(TreeSitterAdapter):
    """Python parser adapter."""

    @property
    def language_name(self) -> str:
        return "python"

    @property
    def file_extensions(self) -> set[str]:
        return {".py", ".pyi"}

    @property
    def ts_language_name(self) -> str:
        return "python"

    @property
    def symbol_query(self) -> str:
        return """
            (function_definition name: (identifier) @func.name) @func.def
            (class_definition name: (identifier) @class.name) @class.def
            (module (expression_statement (assignment left: (identifier) @var.name) @var.def))
        """

    @property
    def call_query(self) -> str:
        return "(call) @call"

    @property
    def parent_types(self) -> set[str]:
        return {"function_definition", "class_definition"}

    @property
    def import_query(self) -> str:
        return """
            (import_statement name: (dotted_name) @import.name)
            (import_statement name: (identifier) @import.name)
        """

    def extract_imports(self, source_code: str, file_path: str, *, tree=None) -> list[ImportEdge]:
        """Extract only intra-project import statements (skips stdlib and third-party).

        Imports are line-based regex; ``tree`` is unused but accepted for
        ``extract_all`` parity.
        """
        imports = []
        for line in source_code.split("\n"):
            line = line.strip()
            if line.startswith("import "):
                parts = line[7:].split(",")
                for part in parts:
                    module = part.strip().split(" as ")[0].strip()
                    if module and not self._is_external(module, file_path=file_path):
                        imports.append(ImportEdge(file_path, module, "direct"))
            elif line.startswith("from "):
                match = line.split(" import ")
                if len(match) == 2:
                    module = match[0][5:].strip()
                    if (
                        module
                        and module != "."
                        and not self._is_external(module.lstrip("."), file_path=file_path)
                    ):
                        imports.append(ImportEdge(file_path, module, "from_package"))
        return imports

    def _is_external(self, module: str, *, file_path: str | None = None) -> bool:
        top = module.split(".")[0]
        if not top:
            return False
        if self._is_local_module_root(top, file_path=file_path):
            return False
        if top in sys.stdlib_module_names:
            return True
        return top in self._installed_top_level_packages()

    def _is_local_module_root(self, top: str, *, file_path: str | None = None) -> bool:
        if file_path:
            parent_dirs = {parent.name for parent in Path(file_path).parents if parent.name}
            if top in parent_dirs:
                return True

        roots: list[Path] = []
        project_root = current_project_root()
        if project_root:
            roots.append(Path(project_root))
        roots.append(Path.cwd())

        for root in roots:
            try:
                resolved = root.resolve()
            except OSError:
                continue
            if (resolved / top / "__init__.py").exists() or (resolved / f"{top}.py").exists():
                return True
            src_root = resolved / "src"
            if (src_root / top / "__init__.py").exists() or (src_root / f"{top}.py").exists():
                return True
        return False

    @staticmethod
    @lru_cache(maxsize=1)
    def _installed_top_level_packages() -> frozenset[str]:
        try:
            return frozenset(importlib.metadata.packages_distributions().keys())
        except Exception:
            return frozenset()

    def extract_inheritance(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[InheritanceEdge]:
        """Extract class inheritance from Python source.

        Line-based scan; ``tree`` is accepted for ``extract_all`` parity.
        """
        edges = []
        lines = source_code.split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith("class "):
                match = line[6:].split(":")[0].strip()
                if "(" in match:
                    class_name = match.split("(")[0].strip()
                    bases_str = match.split("(")[1].rstrip(")")
                    for base in bases_str.split(","):
                        base_name = base.strip()
                        if base_name:
                            subclass_uid = self._uid(file_path, class_name)
                            edges.append(InheritanceEdge(subclass_uid, base_name, False))
        return edges

    def extract_proxy_bindings(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[dict]:
        """Module-level lazy-proxy bindings: ``X: TargetType = SomeProxy(...)``.

        Each entry anchors a ProxyBinding node + ``PROXY_OF`` edge in the graph so
        cross-file calls on the proxy (``from .globals import current_app``) can be
        forwarded to the real type. Only the annotated form yields a ``target_type``
        here; the bare form (``x = Proxy(get_current_app)``) needs the callable's
        return type (a separate hop) and is omitted.
        """
        if tree is None:
            tree = self._parse(source_code)
        module = module_name_from_path(file_path)
        import_bindings = self._extract_import_bindings(source_code, file_path)
        table = self._build_proxy_binding_table(tree, import_bindings, module)
        out: list[dict] = []
        for var_name, target_type in table.items():
            out.append(
                {
                    "proxy_uid": self._uid(file_path, var_name),
                    "proxy_name": var_name,
                    "proxy_qualified_name": f"{module}.{var_name}",
                    "target_type": target_type,
                    "file_path": file_path,
                }
            )
        return out

    def extract_decorators(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[dict]:
        """Decoration relations: ``@deco\\ndef f`` → ``f`` is DECORATED_BY ``deco``.

        The ``@decorator`` application is a syntactic fact (the decorator name sits
        directly above the def/class), so the edge is derived, not guessed — unlike a
        closure's runtime call-site. Handles ``@name``, ``@a.b.c``, ``@call(...)``,
        ``@obj.attr(...)``. The decorator name is resolved through the imports table
        to a qualified target where possible; bare same-module names are kept as-is.
        """
        if tree is None:
            tree = self._parse(source_code)
        module = module_name_from_path(file_path)
        import_bindings = self._extract_import_bindings(source_code, file_path)
        out: list[dict] = []
        for node in self._iter_nodes(tree.root_node):
            if node.type != "decorated_definition":
                continue
            defn = node.child_by_field_name("definition")
            if defn is None or defn.type not in ("function_definition", "class_definition"):
                continue
            name_node = defn.child_by_field_name("name")
            if name_node is None:
                continue
            decorated_uid = self._uid_for_node(defn, source_code, file_path)
            decorated_name = _node_text(name_node)
            for deco in node.children:
                if deco.type != "decorator":
                    continue
                base = self._decorator_base_name(deco)
                if not base or base in _BUILTIN_DECORATORS:
                    continue
                resolved = self._resolve_type_name(base, import_bindings, module)
                out.append(
                    {
                        "decorated_uid": decorated_uid,
                        "decorated_name": decorated_name,
                        "decorator_name": base,
                        "decorator_qualified_name": resolved,
                        "file_path": file_path,
                    }
                )
        return out

    @staticmethod
    def _decorator_base_name(decorator_node) -> str:
        """The decorator's callable identifier: ``@route`` → route, ``@app.route(...)`` → route.

        For an attribute chain we take the final attribute (the method being applied);
        for a bare/called name we take the identifier. Returns '' if not extractable.
        """
        # The decorator node wraps an expression after '@': identifier | attribute | call.
        expr = None
        for ch in decorator_node.children:
            if ch.type in ("identifier", "attribute", "call"):
                expr = ch
                break
        if expr is None:
            return ""
        if expr.type == "call":
            expr = expr.child_by_field_name("function")
            if expr is None:
                return ""
        if expr.type == "identifier":
            return _node_text(expr)
        if expr.type == "attribute":
            attr = expr.child_by_field_name("attribute")
            return _node_text(attr) if attr is not None else ""
        return ""

    def _positional_identifier_arguments(
        self, call_node, source_code: str, *, limit: int = 8
    ) -> list[str]:
        """Leading positional arguments that are bare identifiers (for DI-style hints)."""
        arg_list = call_node.child_by_field_name("arguments")
        if arg_list is None:
            return []
        out: list[str] = []
        for child in arg_list.named_children:
            if child.type == "keyword_argument":
                break
            if child.type == "identifier":
                out.append(_node_text(child))
                if len(out) >= limit:
                    break
                continue
            break
        return out

    def extract_calls_from_source(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[dict]:
        """Extract function calls and attach resolver metadata when statically resolvable."""
        if tree is None:
            tree = self._parse(source_code)
        query = Query(self.language, self.call_query)

        # Flatten captures from matches into (node, tag) tuples
        captures = []
        for _match_id, captures_dict in query.matches(tree.root_node):
            for tag, nodes in captures_dict.items():
                for node in nodes:
                    captures.append((node, tag))

        symbols = self.extract_symbols(source_code, file_path, tree=tree)
        by_name: dict[str, list] = {}
        for symbol in symbols:
            by_name.setdefault(symbol.name, []).append(symbol)
        import_bindings = self._extract_import_bindings(source_code, file_path)
        module = module_name_from_path(file_path)
        attr_type_table = self._build_attr_type_table(tree, import_bindings, module)
        method_returns, function_returns = self._build_return_type_table(
            tree, import_bindings, module
        )
        alias_cache: dict[int, dict[str, str]] = {}

        calls = []
        for node, tag in captures:
            if tag != "call":
                continue

            func_node = node.child_by_field_name("function")
            if not func_node:
                continue

            parent = node.parent
            while parent and parent.type not in self.parent_types:
                parent = parent.parent
            if not parent:
                continue

            caller_uid = self._uid_for_node(parent, source_code, file_path)
            call_name = ""
            callee_uid = None
            callee_qualified_name = None
            rel_type = "CALLS_GUESS"
            tier = "guess"
            confidence = 0.4

            if func_node.type == "identifier":
                call_name = _node_text(func_node)
                rel_type = self._classify_direct_call(call_name)
                tier = "direct" if rel_type == "CALLS_DIRECT" else "guess"
                confidence = 1.0 if rel_type == "CALLS_DIRECT" else 0.4

                if call_name in import_bindings:
                    callee_qualified_name = import_bindings[call_name]
                    rel_type = "CALLS_IMPORTED"
                    tier = "imported"
                    confidence = 0.85
                elif len(by_name.get(call_name, [])) == 1:
                    callee_uid = by_name[call_name][0].uid
                    rel_type = "CALLS_SCOPED"
                    tier = "scoped"
                    confidence = 0.9
                elif rel_type != "CALLS_INFERRED":
                    rel_type = "CALLS_GUESS"
                    tier = "guess"
                    confidence = 0.4

            elif func_node.type == "attribute":
                obj_node = func_node.child_by_field_name("object")
                method_node = func_node.child_by_field_name("attribute")
                if obj_node is None or method_node is None or method_node.type != "identifier":
                    continue
                call_name = _node_text(method_node)
                rel_type = "CALLS_DYNAMIC"
                tier = "dynamic"
                confidence = 0.7

                if obj_node.type == "identifier":
                    receiver_text = _node_text(obj_node)
                    if receiver_text == "self":
                        callee_uid = self._resolve_method_uid(parent, call_name, by_name)
                    elif receiver_text in import_bindings:
                        base = import_bindings[receiver_text]
                        callee_qualified_name = f"{base}.{call_name}"
                    else:
                        typed = self._typed_qualified_target(
                            parent, obj_node, call_name, attr_type_table, alias_cache,
                            method_returns, function_returns,
                        )
                        if typed is not None:
                            tier = "typed"
                            confidence = 0.8
                            callee_qualified_name = typed
                elif obj_node.type == "attribute":
                    typed = self._typed_qualified_target(
                        parent, obj_node, call_name, attr_type_table, alias_cache,
                        method_returns, function_returns,
                    )
                    if typed is None:
                        continue
                    tier = "typed"
                    confidence = 0.8
                    callee_qualified_name = typed
                else:
                    continue
            else:
                continue

            if callee_uid == caller_uid:
                continue

            call = {
                "caller_uid": caller_uid,
                "callee_name": call_name,
                "rel_type": rel_type,
                "tier": tier,
                "confidence": confidence,
                "resolver": "py-scope-v1",
                "call_site_line": node.start_point[0] + 1,
            }
            if callee_uid:
                call["callee_uid"] = callee_uid
            if callee_qualified_name:
                call["callee_qualified_name"] = callee_qualified_name
            pos_args = self._positional_identifier_arguments(node, source_code)
            if pos_args:
                call["arguments"] = pos_args
            calls.append(call)

        return calls

    def _classify_direct_call(self, call_name: str) -> str:
        """Classify a direct identifier call as DIRECT or INFERRED based on known patterns."""
        inferred_patterns = {
            "getattr",
            "setattr",
            "hasattr",
            "getattr_static",
            "operator.methodcaller",
            "methodcaller",
            "exec",
            "eval",
            "compile",
            "__import__",
            "importlib.import_module",
        }

        if call_name in inferred_patterns or call_name.startswith("globals()["):
            return "CALLS_INFERRED"

        if call_name in ("__init__", "__call__", "__getattr__", "__setattr__"):
            return "CALLS_DIRECT"

        return "CALLS_DIRECT"

    def _uid(self, file_path: str, name: str) -> str:
        qualified_name = f"{module_name_from_path(file_path)}.{name}"
        return compute_uid(qualified_name, f"{name}()->_", self.language_name)

    def _uid_for_node(self, node, source_code: str, file_path: str) -> str:
        qualified_name = qualified_name_for(node, source_code, file_path)
        raw_signature, _ = signature_from_node(node, source_code, self.language_name)
        return compute_uid(qualified_name, raw_signature, self.language_name)

    @staticmethod
    def _iter_nodes(node):
        stack = [node]
        while stack:
            current = stack.pop()
            yield current
            stack.extend(current.children)

    @staticmethod
    def _string_literal_text(node) -> str:
        raw = _node_text(node).strip()
        idx = 0
        while idx < len(raw) and raw[idx] in "rbfuRBFU":
            idx += 1
        raw = raw[idx:]
        for quote in ("'''", '"""', "'", '"'):
            if raw.startswith(quote) and raw.endswith(quote) and len(raw) >= 2 * len(quote):
                return raw[len(quote) : -len(quote)]
        return raw

    @staticmethod
    def _enclosing_class_name(node) -> str:
        current = node
        while current is not None and current.type != "class_definition":
            current = current.parent
        if current is None:
            return ""
        name_node = current.child_by_field_name("name")
        return _node_text(name_node) if name_node else ""

    def _resolve_type_name(self, raw: str, import_bindings: dict[str, str], module: str) -> str:
        """Map a bare class name to a qualified name via imports, else same-module."""
        if raw in import_bindings:
            return import_bindings[raw]
        return f"{module}.{raw}"

    def _build_attr_type_table(
        self, tree, import_bindings: dict[str, str], module: str
    ) -> dict[str, dict[str, str]]:
        """Infer instance-attribute types per class (structural; no framework literals).

        Sources: ``<base>_cls = 'mod:Class'`` string convention, ``__init__`` direct
        instantiation ``self.x = Class(...)``, and class-level annotation ``x: Class``.
        """
        table: dict[str, dict[str, str]] = {}
        for cls in self._iter_nodes(tree.root_node):
            if cls.type != "class_definition":
                continue
            name_node = cls.child_by_field_name("name")
            body = cls.child_by_field_name("body")
            if name_node is None or body is None:
                continue
            cname = _node_text(name_node)
            attrs: dict[str, str] = {}
            # Class-body assignments: string-cls convention + annotations (direct children only).
            for stmt in body.children:
                if stmt.type != "expression_statement":
                    continue
                for assign in stmt.children:
                    if assign.type != "assignment":
                        continue
                    left = assign.child_by_field_name("left")
                    right = assign.child_by_field_name("right")
                    typ = assign.child_by_field_name("type")
                    if left is None or left.type != "identifier":
                        continue
                    lname = _node_text(left)
                    if lname.endswith("_cls") and right is not None and right.type == "string":
                        literal = self._string_literal_text(right)
                        if ":" in literal:
                            attrs.setdefault(lname[:-4], literal.replace(":", "."))
                    elif typ is not None:
                        type_ident = self._type_identifier(typ)
                        if type_ident:
                            attrs.setdefault(
                                lname, self._resolve_type_name(type_ident, import_bindings, module)
                            )
            # __init__ direct instantiation: self.x = ClassName(...)
            for fn in body.children:
                if fn.type != "function_definition":
                    continue
                fn_name = fn.child_by_field_name("name")
                if fn_name is None or _node_text(fn_name) != "__init__":
                    continue
                for assign in self._iter_nodes(fn):
                    if assign.type != "assignment":
                        continue
                    left = assign.child_by_field_name("left")
                    right = assign.child_by_field_name("right")
                    if left is None or left.type != "attribute" or right is None:
                        continue
                    obj = left.child_by_field_name("object")
                    attr = left.child_by_field_name("attribute")
                    if obj is None or _node_text(obj) != "self" or attr is None:
                        continue
                    if right.type != "call":
                        continue
                    callee = right.child_by_field_name("function")
                    if callee is not None and callee.type == "identifier":
                        attrs.setdefault(
                            _node_text(attr),
                            self._resolve_type_name(_node_text(callee), import_bindings, module),
                        )
            if attrs:
                table.setdefault(cname, {}).update(attrs)
        return table

    def _build_return_type_table(
        self, tree, import_bindings: dict[str, str], module: str
    ) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
        """Infer function/method return types, structurally and conservatively.

        Two sources, both unambiguous: an explicit ``-> Type`` annotation, or a body
        whose return yields a direct constructor ``return SomeClass(...)``. Returns
        ``return some_global`` / ``return self.x`` / bare names are NOT inferred (their
        type is not statically present) — precision over recall.

        Returns ``(method_returns, function_returns)``:
        - ``method_returns[(ClassName, method)] = qualified_type``
        - ``function_returns[func] = qualified_type`` (module-level / nested funcs)
        """
        method_returns: dict[tuple[str, str], str] = {}
        function_returns: dict[str, str] = {}

        def _return_type_of(fn_node) -> str:
            ret = fn_node.child_by_field_name("return_type")
            if ret is not None:
                ident = self._type_identifier(ret)
                if ident:
                    return self._resolve_type_name(ident, import_bindings, module)
            body = fn_node.child_by_field_name("body")
            if body is None:
                return ""
            for node in self._iter_nodes(body):
                if node.type != "return_statement":
                    continue
                expr = node.named_children[0] if node.named_children else None
                # Don't descend into nested functions' returns.
                if expr is not None and expr.type == "call":
                    callee = expr.child_by_field_name("function")
                    if callee is not None and callee.type == "identifier":
                        name = _node_text(callee)
                        if name[:1].isupper():
                            return self._resolve_type_name(name, import_bindings, module)
            return ""

        for cls in self._iter_nodes(tree.root_node):
            if cls.type != "class_definition":
                continue
            cname_node = cls.child_by_field_name("name")
            body = cls.child_by_field_name("body")
            if cname_node is None or body is None:
                continue
            cname = _node_text(cname_node)
            for fn in body.children:
                if fn.type != "function_definition":
                    continue
                fname_node = fn.child_by_field_name("name")
                if fname_node is None:
                    continue
                rtype = _return_type_of(fn)
                if rtype:
                    method_returns.setdefault((cname, _node_text(fname_node)), rtype)

        for fn in self._iter_nodes(tree.root_node):
            if fn.type != "function_definition":
                continue
            fname_node = fn.child_by_field_name("name")
            if fname_node is None:
                continue
            rtype = _return_type_of(fn)
            if rtype:
                function_returns.setdefault(_node_text(fname_node), rtype)

        return method_returns, function_returns

    def _build_proxy_binding_table(
        self, tree, import_bindings: dict[str, str], module: str
    ) -> dict[str, str]:
        """Resolve module-level lazy-proxy variables to the type they forward to.

        ``X = SomeProxy(callable)`` is a generic Python idiom (werkzeug ``LocalProxy``,
        celery ``Proxy`` — "stolen from werkzeug"); attribute access forwards to the
        wrapped object. Detection is by class-name convention (ends with ``Proxy``),
        mirroring the ``_cls = 'mod:Class'`` convention, not a receiver name-match.

        Only the ANNOTATED form (``current_app: FlaskProxy = LocalProxy(...)``) yields a
        type here: the annotation names the forwarded type directly. The bare form
        (``current_app = Proxy(get_current_app)``, no annotation) is intentionally left
        unresolved — recovering it needs the callable's return type (a separate hop).
        """
        table: dict[str, str] = {}
        for stmt in self._iter_nodes(tree.root_node):
            if stmt.type != "assignment":
                continue
            left = stmt.child_by_field_name("left")
            right = stmt.child_by_field_name("right")
            typ = stmt.child_by_field_name("type")
            if left is None or left.type != "identifier" or right is None or typ is None:
                continue
            if right.type != "call":
                continue
            callee = right.child_by_field_name("function")
            if callee is None or callee.type != "identifier":
                continue
            if not _node_text(callee).endswith("Proxy"):
                continue
            type_ident = self._type_identifier(typ)
            if not type_ident:
                continue
            table[_node_text(left)] = self._resolve_type_name(type_ident, import_bindings, module)
        return table

    @staticmethod
    def _type_identifier(type_node) -> str:
        """Extract a bare class name from an annotation node, ignoring generics/unions."""
        if type_node.type == "identifier":
            return _node_text(type_node)
        for child in PythonAdapter._iter_nodes(type_node):
            if child.type == "identifier":
                return _node_text(child)
        return ""

    def _local_alias_types(
        self,
        func_node,
        cls_table: dict[str, str],
        *,
        enclosing_class: str = "",
        method_returns: dict[tuple[str, str], str] | None = None,
        function_returns: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Map locals to types within a function.

        - ``v = self.attr`` → the attribute's type (attr_type_table).
        - ``v = self.method()`` → the method's inferred return type.
        - ``v = func()`` → a module/nested function's inferred return type.
        """
        method_returns = method_returns or {}
        function_returns = function_returns or {}
        aliases: dict[str, str] = {}
        for assign in self._iter_nodes(func_node):
            if assign.type != "assignment":
                continue
            left = assign.child_by_field_name("left")
            right = assign.child_by_field_name("right")
            if left is None or left.type != "identifier" or right is None:
                continue
            local_name = _node_text(left)
            if right.type == "attribute":
                obj = right.child_by_field_name("object")
                attr = right.child_by_field_name("attribute")
                if obj is not None and _node_text(obj) == "self" and attr is not None:
                    inferred = cls_table.get(_node_text(attr))
                    if inferred:
                        aliases[local_name] = inferred
            elif right.type == "call":
                callee = right.child_by_field_name("function")
                if callee is None:
                    continue
                if callee.type == "attribute":
                    inner = callee.child_by_field_name("object")
                    meth = callee.child_by_field_name("attribute")
                    if (
                        inner is not None
                        and _node_text(inner) == "self"
                        and meth is not None
                        and enclosing_class
                    ):
                        inferred = method_returns.get((enclosing_class, _node_text(meth)))
                        if inferred:
                            aliases[local_name] = inferred
                elif callee.type == "identifier":
                    inferred = function_returns.get(_node_text(callee))
                    if inferred:
                        aliases[local_name] = inferred
        return aliases

    def _typed_qualified_target(
        self,
        parent,
        receiver_node,
        call_name: str,
        attr_type_table: dict[str, dict[str, str]],
        alias_cache: dict[int, dict[str, str]],
        method_returns: dict[tuple[str, str], str] | None = None,
        function_returns: dict[str, str] | None = None,
    ) -> str | None:
        """Tier 4.5 CALLS_TYPED: resolve ``self.attr.m()`` / ``local.m()`` to ``Type.m``."""
        enclosing = self._enclosing_class_name(parent)
        cls_table = attr_type_table.get(enclosing, {})
        target_type: str | None = None
        if receiver_node.type == "attribute":
            inner_obj = receiver_node.child_by_field_name("object")
            inner_attr = receiver_node.child_by_field_name("attribute")
            if (
                inner_obj is not None
                and inner_obj.type == "identifier"
                and _node_text(inner_obj) == "self"
                and inner_attr is not None
            ):
                target_type = cls_table.get(_node_text(inner_attr))
        elif receiver_node.type == "identifier":
            if parent.id not in alias_cache:
                alias_cache[parent.id] = self._local_alias_types(
                    parent,
                    cls_table,
                    enclosing_class=enclosing,
                    method_returns=method_returns,
                    function_returns=function_returns,
                )
            target_type = alias_cache[parent.id].get(_node_text(receiver_node))
        if target_type:
            return f"{target_type}.{call_name}"
        return None

    def _resolve_method_uid(
        self, caller_node, method_name: str, by_name: dict[str, list]
    ) -> str | None:
        candidates = by_name.get(method_name, [])
        if not candidates:
            return None

        class_node = caller_node
        while class_node and class_node.type != "class_definition":
            class_node = class_node.parent
        if not class_node:
            return candidates[0].uid if len(candidates) == 1 else None

        class_name_node = class_node.child_by_field_name("name")
        if not class_name_node:
            return None
        class_name = class_name_node.text.decode("utf-8")
        for candidate in candidates:
            if f".{class_name}.{method_name}" in candidate.qualified_name:
                return str(candidate.uid)
        return str(candidates[0].uid) if len(candidates) == 1 else None

    def _extract_import_bindings(self, source_code: str, file_path: str) -> dict[str, str]:
        """Return local import alias -> best-effort target qualified name."""
        module = module_name_from_path(file_path)
        package = module.rsplit(".", 1)[0] if "." in module else ""
        bindings: dict[str, str] = {}
        for line in source_code.splitlines():
            stripped = line.strip()
            from_match = re.match(r"from\s+([.\w]+)\s+import\s+(.+)$", stripped)
            if from_match:
                import_module, names = from_match.groups()
                target_module = self._resolve_import_module(import_module, package)
                for item in names.split(","):
                    item = item.strip()
                    if not item or item == "*":
                        continue
                    original, _, alias = item.partition(" as ")
                    local_name = alias.strip() or original.strip()
                    bindings[local_name] = f"{target_module}.{original.strip()}"
                continue

            import_match = re.match(r"import\s+(.+)$", stripped)
            if import_match:
                for item in import_match.group(1).split(","):
                    item = item.strip()
                    original, _, alias = item.partition(" as ")
                    target_module = original.strip()
                    if not target_module:
                        continue
                    local_name = alias.strip() or target_module.split(".")[0]
                    bindings[local_name] = target_module
        return bindings

    def _resolve_import_module(self, import_module: str, package: str) -> str:
        if not import_module.startswith("."):
            return import_module
        dots = len(import_module) - len(import_module.lstrip("."))
        remainder = import_module.lstrip(".")
        parts = package.split(".") if package else []
        prefix = parts[: max(0, len(parts) - dots + 1)]
        if remainder:
            prefix.append(remainder)
        return ".".join(p for p in prefix if p)


def make_adapter() -> PythonAdapter:
    """Factory function for adapter discovery."""
    return PythonAdapter()
