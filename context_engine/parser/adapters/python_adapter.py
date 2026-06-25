"""Python language adapter using tree-sitter."""

import ast
import importlib.metadata
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from context_engine.parser.adapters.treesitter_base import TreeSitterAdapter, iter_ts_query_matches
from context_engine.parser.import_scan import split_python_from_import, split_python_import_clause
from context_engine.parser.protocol import ImportEdge, InheritanceEdge, SymbolMetadata
from context_engine.parser.uid import (
    UNRESOLVED_SIGNATURE,
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

    def extract_axis_facts(
        self,
        source_code: str,
        file_path: str,
        *,
        tree=None,
        symbols: list[SymbolMetadata] | None = None,
        project_root: str | None = None,
    ):
        """Return common symbol facts plus Python AST-physical axis facts."""
        from context_engine.parser.adapters.python_axis_extractor import PythonAxisExtractor

        facts = super().extract_axis_facts(
            source_code,
            file_path,
            tree=tree,
            symbols=symbols,
            project_root=project_root,
        )
        try:
            py_facts = PythonAxisExtractor().extract_facts(
                source_code,
                file_path,
                project_root=project_root,
            )
        except SyntaxError:
            py_facts = []
        return [*facts, *py_facts]

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

    @staticmethod
    def _module_symbol_identity(file_path: str) -> tuple[str, str, str]:
        """Return ``(module_name, qualified_name, uid)`` for the file's module Symbol.

        Anchors module-level execution (imports, top-level assignments / calls,
        decorator applications outside any function/class) to a single
        Symbol of kind ``"module"``. Without this anchor, AST facts whose
        ``_enclosing_def_node`` is ``None`` had no caller to attach to and
        were silently dropped — most notably ``app = FastAPI()`` and the like.

        The uid formula matches the axis extractor's module-scope uid so that
        downstream consumers see the same identity from both paths.
        """
        module_name = module_name_from_path(file_path)
        uid = compute_uid(module_name, UNRESOLVED_SIGNATURE, "python")
        return module_name, module_name, uid

    # Python built-in callables whose construction we *can* recognise but do
    # not need to model as graph anchors. ``T = type(...)`` / ``c = dict(...)``
    # is captured as a parsed fact but does not produce a variable Symbol;
    # the catalogue is for application-level objects.
    _BUILTIN_CALLABLE_NAMES = frozenset(
        {
            "bool",
            "bytearray",
            "bytes",
            "complex",
            "dict",
            "enumerate",
            "filter",
            "float",
            "frozenset",
            "int",
            "list",
            "map",
            "object",
            "range",
            "reversed",
            "set",
            "slice",
            "str",
            "tuple",
            "type",
            "zip",
            "Exception",
            "ValueError",
            "TypeError",
            "KeyError",
            "IndexError",
            "RuntimeError",
            "OSError",
            "IOError",
            "AttributeError",
            "NotImplementedError",
            "StopIteration",
            "FileNotFoundError",
        }
    )

    @staticmethod
    def _module_level_classes(tree) -> set[str]:
        """Bare names of class definitions sitting directly in the module body."""
        classes: set[str] = set()
        for child in tree.root_node.named_children:
            if child.type != "class_definition":
                continue
            name = child.child_by_field_name("name")
            if name is not None:
                classes.add(_node_text(name))
        return classes

    @classmethod
    def _resolve_construction_callee(
        cls,
        fn_node,
        *,
        import_bindings: dict[str, str],
        local_classes: set[str],
        module: str,
    ) -> tuple[str, str, bool] | None:
        """Classify a Call's callee as a constructor or drop it.

        Returns ``(type_name, type_qualified_name, is_external)`` when the
        callee is a class the parser can name. ``None`` means "drop": the
        name is not in this file's imports, not a local class definition,
        and not a recognised Python built-in callable — almost certainly a
        typo, unimported dependency, or a non-constructor call.

        Externality is decided here, with the file's imports table as the
        only proof — the linker does not get to guess.
        """
        if fn_node is None:
            return None

        if fn_node.type == "identifier":
            name = _node_text(fn_node)
            if not name:
                return None
            if name in local_classes:
                return (name, f"{module}.{name}", False)
            if name in import_bindings:
                return (name, import_bindings[name], True)
            if name in cls._BUILTIN_CALLABLE_NAMES:
                return None  # built-in: don't model as a graph anchor
            return None  # unresolved → drop

        if fn_node.type == "attribute":
            head = fn_node.child_by_field_name("object")
            attr = fn_node.child_by_field_name("attribute")
            if head is None or attr is None or head.type != "identifier":
                return None
            head_name = _node_text(head)
            attr_name = _node_text(attr)
            if not head_name or not attr_name:
                return None
            if head_name in import_bindings:
                qn = f"{import_bindings[head_name]}.{attr_name}"
                return (attr_name, qn, True)
            return None
        return None

    def _module_constructor_variables(
        self,
        source_code: str,
        file_path: str,
        tree,
        *,
        base_symbols: list[SymbolMetadata],
    ) -> list[SymbolMetadata]:
        """Variable Symbols for module-level ``name = SomeClass(...)`` lines.

        Triggered only by class-construction assignments at the module body;
        function-scope locals stay invisible (per design — modelling every
        local would blow up the graph for no analytic gain). The variable
        Symbol becomes the DFG anchor decorators / cross-file lookups need
        to talk about the constructed object.
        """
        module_name = module_name_from_path(file_path)
        import_bindings = self._extract_import_bindings(source_code, file_path)
        local_classes = self._module_level_classes(tree)
        existing_names = {s.name for s in base_symbols if s.kind in {"function", "class"}}
        existing_var_names = {s.name for s in base_symbols if s.kind == "variable"}
        out: list[SymbolMetadata] = []
        for stmt in tree.root_node.named_children:
            if stmt.type != "expression_statement":
                continue
            assignment = next(
                (c for c in stmt.named_children if c.type == "assignment"),
                None,
            )
            if assignment is None:
                continue
            lhs = assignment.child_by_field_name("left")
            rhs = assignment.child_by_field_name("right")
            if lhs is None or rhs is None or lhs.type != "identifier":
                continue
            var_name = _node_text(lhs)
            if not var_name or var_name in existing_names or var_name in existing_var_names:
                continue
            if rhs.type != "call":
                continue
            fn_node = rhs.child_by_field_name("function")
            resolved = self._resolve_construction_callee(
                fn_node,
                import_bindings=import_bindings,
                local_classes=local_classes,
                module=module_name,
            )
            if resolved is None:
                continue
            qualified_name = f"{module_name}.{var_name}"
            signature = f"{var_name}()->_"
            uid = compute_uid(qualified_name, signature, self.language_name)
            out.append(
                SymbolMetadata(
                    uid=uid,
                    name=var_name,
                    kind="variable",
                    start_line=assignment.start_point[0] + 1,
                    end_line=assignment.end_point[0] + 1,
                    content_hash="",
                    file_path=file_path,
                    qualified_name=qualified_name,
                    signature=signature,
                    signature_hash="",
                    signature_status="resolved",
                    language=self.language_name,
                )
            )
        return out

    @classmethod
    def _module_symbol(cls, source_code: str, file_path: str) -> SymbolMetadata:
        module_name, qualified_name, uid = cls._module_symbol_identity(file_path)
        line_count = source_code.count("\n") + 1
        return SymbolMetadata(
            uid=uid,
            name=module_name,
            kind="module",
            start_line=1,
            end_line=line_count,
            content_hash="",  # the module symbol is structural, not content-keyed
            file_path=file_path,
            qualified_name=qualified_name,
            signature=UNRESOLVED_SIGNATURE,
            signature_hash="",
            signature_status="resolved",
            language="python",
        )

    def extract_symbols(self, source_code: str, file_path: str, *, tree=None):
        """Patch SymbolMetadata with return-shape AST markers after the base
        extraction. Each function symbol gains booleans describing the shape
        of values its top-level ``return`` statements yield — a mapping
        (``{...}`` / ``dict(...)`` / dict-comp), a sequence (``[...]`` /
        ``list(...)`` / list-comp / tuple), or a constructed type (``return
        SomeClass(...)``). These are the foundation for the binding_surface
        and dependency_solver composite predicates documented in
        spec_intent_classifier.md.

        Also synthesizes one module-scope Symbol per file so AST facts whose
        nearest enclosing definition is the module itself (top-level calls,
        decorator applications, module-execution-time assignments) have a
        coherent caller to attach to.

        Module-level constructor assignments — ``app = FastAPI()`` and the like
        — become their own ``kind="variable"`` Symbols. These are DFG anchors:
        without them no decorator application ``@app.get(...)`` has a graph
        node to attach to as receiver. We only materialize variables when the
        right-hand side resolves to a class (local definition or imported
        external symbol); unresolvable names are dropped at this layer (the
        parser is the only place that has both the imports table and the
        local class set, so spurious references die here instead of polluting
        the graph).
        """
        symbols = super().extract_symbols(source_code, file_path, tree=tree)
        symbols.insert(0, self._module_symbol(source_code, file_path))
        if tree is None:
            tree = self._parse(source_code)
        module_var_symbols = self._module_constructor_variables(
            source_code, file_path, tree, base_symbols=symbols
        )
        symbols.extend(module_var_symbols)
        shapes = self._function_return_shapes(tree)
        iteration = self._function_iteration_shapes(tree)
        if shapes or iteration:
            for symbol in symbols:
                shape = shapes.get(symbol.name) if shapes else None
                if shape is not None:
                    if shape.get("mapping"):
                        symbol.returns_mapping = True
                    if shape.get("sequence"):
                        symbol.returns_sequence = True
                    if shape.get("constructed"):
                        symbol.returns_constructed_type = True
                it = iteration.get(symbol.name) if iteration else None
                if it is not None:
                    if it.get("iterates_attr_call"):
                        symbol.iterates_attr_call = True
                    if it.get("assembles_mapping_in_loop"):
                        symbol.assembles_mapping_in_loop = True
        from context_engine.parser.docstring_extract import attach_docstrings

        attach_docstrings(
            symbols,
            source_code,
            file_path,
            tree=tree,
            language="python",
        )
        return symbols

    def _function_iteration_shapes(self, tree) -> dict[str, dict[str, bool]]:
        """Collect per-function iteration-shape booleans.

        A ``for X in obj.attr: …`` loop carries two structural sub-signals:
          * ``iterates_attr_call``: a method is called on ``X`` (the
            iteration variable) inside the body, e.g. ``X.method()``.
          * ``assembles_mapping_in_loop``: a subscript-assignment writes
            into a local or ``self.attr`` inside the body, e.g. ``result[k]
            = X.foo()`` — the binding-surface assemble-from-collection
            shape that the return-shape scan alone cannot see.
        Nested function / lambda boundaries are honoured: a closure's loop
        inside a helper credits the helper, not the outer scope.
        """
        out: dict[str, dict[str, bool]] = {}
        for fn in self._iter_nodes(tree.root_node):
            if fn.type != "function_definition":
                continue
            name_node = fn.child_by_field_name("name")
            body = fn.child_by_field_name("body")
            if name_node is None or body is None:
                continue
            flags = self._collect_iteration_shape(body)
            if not any(flags.values()):
                continue
            out[_node_text(name_node)] = flags
        return out

    @classmethod
    def _collect_iteration_shape(cls, body) -> dict[str, bool]:
        """Per-function iteration-shape booleans.

        ``iterates_attr_call`` is *strict* — only fires when the iteration
        source is an attribute access (``for x in obj.attr``) AND the body
        calls a method on the loop variable. This is the precision signal
        for "iterate over a collection of objects, call something on each".

        ``assembles_mapping_in_loop`` is *permissive* — any ``for`` loop
        whose body subscript-writes into a local or ``self.attr`` counts.
        Real-world binders iterate over typed parameters
        (``for field in body_fields``) or function calls
        (``for f in sorted(chain(opts.fields, …))``) — both shapes carry
        the same binder semantics as a bare ``obj.attr`` iteration. The
        composite predicate (``assembles_mapping_in_loop`` together with
        ``returns_mapping`` / ``write_subscript`` write fan) keeps the
        false-positive rate down.
        """
        flags = {"iterates_attr_call": False, "assembles_mapping_in_loop": False}
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type in ("function_definition", "lambda"):
                continue  # don't descend into a nested callable
            if n.type == "for_statement":
                left = n.child_by_field_name("left")
                right = n.child_by_field_name("right")
                fbody = n.child_by_field_name("body")
                if fbody is None:
                    for child in n.children:
                        stack.append(child)
                    continue
                # Strict attribute-iteration + method-call-on-loop-var.
                if right is not None and right.type == "attribute":
                    loop_var = (
                        _node_text(left) if (left is not None and left.type == "identifier") else ""
                    )
                    if cls._for_body_calls_on(fbody, loop_var):
                        flags["iterates_attr_call"] = True
                # Permissive: any for-loop body that writes a subscript.
                if cls._for_body_writes_subscript(fbody):
                    flags["assembles_mapping_in_loop"] = True
            for child in n.children:
                stack.append(child)
        return flags

    @classmethod
    def _for_body_calls_on(cls, body, loop_var: str) -> bool:
        """``loop_var.method(...)`` anywhere inside ``body`` (not nested fn)."""
        if not loop_var:
            return False
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type in ("function_definition", "lambda"):
                continue
            if n.type == "call":
                fn = n.child_by_field_name("function")
                if fn is not None and fn.type == "attribute":
                    obj = fn.child_by_field_name("object")
                    if obj is not None and obj.type == "identifier" and _node_text(obj) == loop_var:
                        return True
            for child in n.children:
                stack.append(child)
        return False

    @classmethod
    def _for_body_writes_subscript(cls, body) -> bool:
        """``result[k] = …`` (subscript assignment on local or self.attr)."""
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type in ("function_definition", "lambda"):
                continue
            if n.type == "assignment":
                left = n.child_by_field_name("left")
                if left is not None and left.type == "subscript":
                    base = left.child_by_field_name("value")
                    if base is not None and base.type in ("identifier", "attribute"):
                        return True
            for child in n.children:
                stack.append(child)
        return False

    _MAPPING_CTOR_NAMES = frozenset({"dict", "OrderedDict", "defaultdict", "Counter", "ChainMap"})
    _SEQUENCE_CTOR_NAMES = frozenset({"list", "tuple", "set", "frozenset"})

    def _function_return_shapes(self, tree) -> dict[str, dict[str, bool]]:
        """Collect per-function-name return-shape flags.

        A top-level ``return_statement`` (one not nested inside an inner
        function definition) whose argument is a mapping / sequence /
        constructed-type expression sets the corresponding flag. Multiple
        returns OR together — a single mapping-return is enough.
        """
        out: dict[str, dict[str, bool]] = {}
        for node in self._iter_nodes(tree.root_node):
            if node.type != "function_definition":
                continue
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node is None or body is None:
                continue
            shape = self._collect_return_shape(body)
            if not any(shape.values()):
                continue
            name = _node_text(name_node)
            existing = out.setdefault(
                name, {"mapping": False, "sequence": False, "constructed": False}
            )
            for k, v in shape.items():
                if v:
                    existing[k] = True
        return out

    @classmethod
    def _collect_return_shape(cls, body) -> dict[str, bool]:
        """Walk a function body for top-level ``return X`` shape classification.

        Two passes:
         1. Collect ``local_name → shape`` for assignments whose RHS is
            itself a literal / constructor (``field_dict = {}`` →
            ``field_dict`` is mapping; ``items = []`` → sequence; etc).
            Later assignments overwrite, matching real control flow.
         2. Classify each ``return_statement``. A bare identifier return
            falls back to the local-assignment map — so ``field_dict =
            {}; for f in fields: field_dict[f.name] = f.formfield();
            return field_dict`` correctly registers as a mapping return.

        Skips nested function / class definitions so an inner helper that
        returns a dict doesn't paint the outer function as a mapping
        returner.
        """
        # Pass 1: identifier assignments whose RHS is a recognised shape.
        local_assigns: dict[str, str] = {}
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type in ("function_definition", "lambda"):
                continue
            if n.type == "assignment":
                left = n.child_by_field_name("left")
                right = n.child_by_field_name("right")
                if left is not None and right is not None and left.type == "identifier":
                    kind = cls._classify_return_expr(right)
                    if kind:
                        local_assigns[_node_text(left)] = kind
            for child in n.children:
                stack.append(child)

        # Pass 2: return statements.
        shape = {"mapping": False, "sequence": False, "constructed": False}
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type == "return_statement":
                expr = n.named_children[0] if n.named_children else None
                if expr is None:
                    continue
                kind = cls._classify_return_expr(expr)
                if not kind and expr.type == "identifier":
                    kind = local_assigns.get(_node_text(expr), "")
                if kind:
                    shape[kind] = True
                continue
            if n.type in ("function_definition", "lambda"):
                continue
            for child in n.children:
                stack.append(child)
        return shape

    @classmethod
    def _classify_return_expr(cls, expr) -> str:
        """Return ``"mapping"`` / ``"sequence"`` / ``"constructed"`` / ``""``."""
        t = expr.type
        if t in ("dictionary", "dictionary_comprehension"):
            return "mapping"
        if t in ("list", "list_comprehension", "tuple", "set", "set_comprehension"):
            return "sequence"
        if t == "call":
            fn = expr.child_by_field_name("function")
            if fn is None:
                return ""
            if fn.type == "identifier":
                name = _node_text(fn)
                if name in cls._MAPPING_CTOR_NAMES:
                    return "mapping"
                if name in cls._SEQUENCE_CTOR_NAMES:
                    return "sequence"
                # ``return SomeType(...)`` — a Capitalised identifier
                # is heuristically a constructor call. Lower-case
                # identifiers are functions, not constructed types.
                if name and name[:1].isupper():
                    return "constructed"
            elif fn.type == "attribute":
                # ``return mod.SomeType(...)`` — same heuristic on the
                # last segment.
                attr = fn.child_by_field_name("attribute")
                if attr is not None and attr.type == "identifier":
                    name = _node_text(attr)
                    if name and name[:1].isupper():
                        return "constructed"
        return ""

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

        Tree-sitter based so multi-line base lists and generic bases such as
        ``class C(Base[T], Mixin):`` stay visible. Only the base head is emitted
        (``Base``), while generic parameters are not treated as superclasses.
        """
        if tree is None:
            tree = self._parse(source_code)
        edges: list[InheritanceEdge] = []
        for node in self._iter_nodes(tree.root_node):
            if node.type != "class_definition":
                continue
            args = node.child_by_field_name("superclasses")
            if args is None:
                args = next((c for c in node.children if c.type == "argument_list"), None)
            if args is None:
                continue
            subclass_uid = self._uid_for_node(node, source_code, file_path)
            for base_node in args.named_children:
                if base_node.type == "comment":
                    continue
                base_name = self._inheritance_base_name(base_node)
                if base_name:
                    base_path = self._inheritance_base_path(base_node) or base_name
                    edges.append(
                        InheritanceEdge(
                            subclass_uid=subclass_uid,
                            superclass_name=base_name,
                            is_interface=False,
                            superclass_path=base_path,
                        )
                    )
        return edges

    def extract_proxy_bindings(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """Module-level lazy-proxy bindings: ``X = SomeProxy(...)``.

        Each entry anchors a ProxyBinding node + ``PROXY_OF`` edge so cross-file calls
        on the proxy are forwarded to the real type. Two target sources, both keeping
        the schema identical (ProxyBinding + PROXY_OF + CALLS_DYNAMIC{via_proxy}):

        - ``annotation``: ``current_app: FlaskProxy = LocalProxy(...)`` — the annotation
          names the forwarded type directly (high confidence).
        - ``wrapped_callable``: ``current_app = Proxy(get_current_app)`` — no annotation;
          the target is the class the wrapped callable constructs/imports in its body
          (celery ``Proxy(get_current_app)`` -> ``Celery``). Lower confidence — it is a
          structural points-to approximation, not a declared type.
        """
        if tree is None:
            tree = self._parse(source_code)
        module = module_name_from_path(file_path)
        import_bindings = self._extract_import_bindings(source_code, file_path)
        table = self._build_proxy_binding_table(tree, source_code, import_bindings, module)
        out: list[dict] = []
        for var_name, meta in table.items():
            out.append(
                {
                    "proxy_uid": self._uid(file_path, var_name),
                    "proxy_name": var_name,
                    "proxy_qualified_name": f"{module}.{var_name}",
                    "target_type": meta["target_type"],
                    "target_source": meta["target_source"],
                    "wrapped_callable": meta.get("wrapped_callable", ""),
                    "context_var": meta.get("context_var", ""),
                    "context_type": meta.get("context_type", ""),
                    "context_attr": meta.get("context_attr", ""),
                    "binding_source": meta.get("binding_source", ""),
                    "confidence": meta.get("confidence", 1.0),
                    "file_path": file_path,
                }
            )
        return out

    def extract_self_method_proxy_calls(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[dict]:
        """Relink candidates for ``L = self.M(); L.attr(...)`` where ``M``
        returns a lazy-proxy global.

        The receiver ``L`` has no statically visible type (it is the result of a
        method whose return value flows from a module-global proxy), so the call
        ``L.attr(...)`` is dropped by the normal call resolver. But the chain is
        structural and recoverable once the graph holds the proxy anchor:

            app = self._get_app()      # _get_app: return cls._app; cls._app = current_app
            app.send_task(...)         # current_app -PROXY_OF-> Celery  ⇒  Celery.send_task

        This per-file pass emits one candidate per such call site —
        ``{caller_uid, callee_name, returns_global_qn, call_site_line}`` — where
        ``returns_global_qn`` is the import-resolved qualified name of the global
        that ``M`` returns. The proxy hop (``returns_global_qn`` → ``PROXY_OF`` →
        class ``C`` → ``C.callee_name``) is resolved at graph time, where the
        cross-file ``PROXY_OF`` anchor lives. Precision is gated structurally:
        the candidate fires ONLY when (a) ``M`` is a method in the same class
        with an unambiguous return-of-imported-global, and (b) ``L`` is assigned
        exactly once, directly from ``self.M()`` / ``cls.M()``.
        """
        if tree is None:
            tree = self._parse(source_code)
        import_bindings = self._extract_import_bindings(source_code, file_path)

        out: list[dict] = []
        for cls in self._iter_nodes(tree.root_node):
            if cls.type != "class_definition":
                continue
            body = cls.child_by_field_name("body")
            if body is None:
                continue
            methods: dict[str, object] = {}
            for child in body.children:
                fn = child
                if child.type == "decorated_definition":
                    fn = child.child_by_field_name("definition")
                if fn is None or fn.type != "function_definition":
                    continue
                fn_name = fn.child_by_field_name("name")
                if fn_name is not None:
                    methods[_node_text(fn_name)] = fn

            # Phase 1: which methods return an imported global (directly or via a
            # self/cls attribute alias assigned in the method body)?
            returns_global: dict[str, str] = {}
            for mname, fn in methods.items():
                g = self._method_returns_imported_global(fn, import_bindings)
                if g:
                    returns_global[mname] = g
            if not returns_global:
                continue

            # Phase 2: in every method, find ``L = self.M()`` (M a proxy-return
            # method) then member calls ``L.attr(...)``.
            for fn in methods.values():
                caller_uid = self._uid_for_node(fn, source_code, file_path)
                fn_body = cast(Any, fn).child_by_field_name("body")
                if fn_body is None:
                    continue
                # local -> proxy-return method M (only single, direct binding)
                local_src: dict[str, str] = {}
                reassigned: set[str] = set()
                for assign in self._iter_body_nodes(fn_body):
                    if assign.type != "assignment":
                        continue
                    left = assign.child_by_field_name("left")
                    right = assign.child_by_field_name("right")
                    if left is None or left.type != "identifier":
                        continue
                    lname = _node_text(left)
                    m = self._self_method_call_name(right)
                    if m is not None and m in returns_global:
                        if lname in local_src or lname in reassigned:
                            # ambiguous: assigned more than once — drop it
                            local_src.pop(lname, None)
                            reassigned.add(lname)
                        else:
                            local_src[lname] = m
                    else:
                        # any other binding to the same name poisons it
                        if lname in local_src:
                            local_src.pop(lname, None)
                        reassigned.add(lname)
                if not local_src:
                    continue
                seen_sites: set[tuple[str, int]] = set()
                for call in self._iter_body_nodes(fn_body):
                    if call.type != "call":
                        continue
                    func = call.child_by_field_name("function")
                    if func is None or func.type != "attribute":
                        continue
                    obj = func.child_by_field_name("object")
                    attr = func.child_by_field_name("attribute")
                    if obj is None or obj.type != "identifier" or attr is None:
                        continue
                    recv = _node_text(obj)
                    if recv not in local_src:
                        continue
                    callee_name = _node_text(attr)
                    line = call.start_point[0] + 1
                    key = (callee_name, line)
                    if key in seen_sites:
                        continue
                    seen_sites.add(key)
                    out.append(
                        {
                            "caller_uid": caller_uid,
                            "callee_name": callee_name,
                            "returns_global_qn": returns_global[local_src[recv]],
                            "call_site_line": line,
                            "file_path": file_path,
                        }
                    )
        return out

    def _iter_body_nodes(self, body):
        """Yield nodes under ``body`` but NOT inside a nested function/class —
        so a method's own statements are scanned while a closure's are not."""
        stack = list(body.children)
        while stack:
            node = stack.pop()
            if node.type in ("function_definition", "class_definition"):
                continue
            yield node
            stack.extend(node.children)

    def _self_method_call_name(self, node) -> str | None:
        """If ``node`` is a call ``self.M()`` / ``cls.M()`` with no arguments
        that matter, return ``M``; else ``None``."""
        if node is None or node.type != "call":
            return None
        func = node.child_by_field_name("function")
        if func is None or func.type != "attribute":
            return None
        obj = func.child_by_field_name("object")
        attr = func.child_by_field_name("attribute")
        if obj is None or obj.type != "identifier" or attr is None:
            return None
        if _node_text(obj) not in ("self", "cls"):
            return None
        return _node_text(attr)

    def _method_returns_imported_global(self, fn, import_bindings: dict[str, str]) -> str:
        """Qualified name of the imported global a method returns, else ''.

        Handles ``return G`` (G an imported name) and ``return self.X`` /
        ``return cls.X`` where ``X`` is assigned an imported global ``G`` in the
        method body (``cls._app = current_app``). Returns '' on ambiguity (more
        than one distinct global) — precision over recall.
        """
        body = fn.child_by_field_name("body")
        if body is None:
            return ""

        # Attribute aliases assigned an imported global: ``self.X = G`` / ``cls.X = G``.
        attr_alias: dict[str, str] = {}
        name_alias: dict[str, str] = {}
        for assign in self._iter_body_nodes(body):
            if assign.type != "assignment":
                continue
            left = assign.child_by_field_name("left")
            right = assign.child_by_field_name("right")
            if left is None or right is None or right.type != "identifier":
                continue
            g = import_bindings.get(_node_text(right))
            if not g:
                continue
            if left.type == "attribute":
                lo = left.child_by_field_name("object")
                la = left.child_by_field_name("attribute")
                if lo is not None and la is not None and _node_text(lo) in ("self", "cls"):
                    attr_alias[_node_text(la)] = g
            elif left.type == "identifier":
                name_alias[_node_text(left)] = g

        found: set[str] = set()
        for ret in self._iter_body_nodes(body):
            if ret.type != "return_statement":
                continue
            expr = ret.named_children[0] if ret.named_children else None
            if expr is None:
                continue
            if expr.type == "identifier":
                name = _node_text(expr)
                g = import_bindings.get(name) or name_alias.get(name)
                if g:
                    found.add(g)
            elif expr.type == "attribute":
                lo = expr.child_by_field_name("object")
                la = expr.child_by_field_name("attribute")
                if lo is not None and la is not None and _node_text(lo) in ("self", "cls"):
                    g = attr_alias.get(_node_text(la))
                    if g:
                        found.add(g)
        return next(iter(found)) if len(found) == 1 else ""

    def extract_decorators(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
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
                callable_name = self._decorator_callable_name(deco)
                base = callable_name.rsplit(".", 1)[-1] if callable_name else ""
                if not base or base in _BUILTIN_DECORATORS:
                    continue
                owner_name = callable_name.rsplit(".", 1)[0] if "." in callable_name else ""
                resolved = self._resolve_dotted_name(callable_name or base, import_bindings, module)
                owner_resolved = (
                    self._resolve_dotted_name(owner_name, import_bindings, module)
                    if owner_name
                    else ""
                )
                out.append(
                    {
                        "decorated_uid": decorated_uid,
                        "decorated_name": decorated_name,
                        "decorator_name": base,
                        "decorator_callable_name": callable_name,
                        "decorator_qualified_name": resolved,
                        "decorator_owner_name": owner_name,
                        "decorator_owner_qualified_name": owner_resolved,
                        "file_path": file_path,
                    }
                )
        return out

    def extract_http_endpoints(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """FastAPI/Flask-style route decorator facts for HTTP endpoint bridges."""
        from context_engine.indexer.http_endpoint import (
            HTTP_ROUTE_REGISTER_CALLEES,
            normalize_http_method,
            normalize_http_path,
        )

        _NON_HTTP_DECORATORS = frozenset({"patch", "mock", "Mock", "MagicMock", "spy"})

        if tree is None:
            tree = self._parse(source_code)
        out: list[dict] = []
        seen: set[tuple[str, str, str, str]] = set()

        def emit(site_uid: str, method: str, path: str, via: str) -> None:
            normalized_method = normalize_http_method(method)
            normalized_path = normalize_http_path(path)
            if not site_uid or not normalized_method or not normalized_path:
                return
            key = (site_uid, normalized_method, normalized_path, "implement")
            if key in seen:
                return
            seen.add(key)
            out.append(
                {
                    "site_uid": site_uid,
                    "method": normalized_method,
                    "path": normalized_path,
                    "role": "implement",
                    "via": via,
                    "file_path": file_path,
                }
            )

        for node in self._iter_nodes(tree.root_node):
            if node.type != "decorated_definition":
                continue
            defn = node.child_by_field_name("definition")
            if defn is None or defn.type != "function_definition":
                continue
            site_uid = self._uid_for_node(defn, source_code, file_path)
            if not site_uid:
                continue
            for deco in node.children:
                if deco.type != "decorator":
                    continue
                callable_name = self._decorator_callable_name(deco)
                base = callable_name.rsplit(".", 1)[-1] if callable_name else ""
                if base in _NON_HTTP_DECORATORS:
                    continue
                if base not in HTTP_ROUTE_REGISTER_CALLEES and base != "api_route":
                    continue
                route_path, methods = self._http_route_from_decorator(deco)
                if not route_path:
                    continue
                if not methods:
                    method = normalize_http_method(base if base != "route" else "get")
                    if method:
                        emit(site_uid, method, route_path, f"@{callable_name or base}")
                    continue
                for method in methods:
                    emit(site_uid, method, route_path, f"@{callable_name or base}")
        return out

    def _http_route_from_decorator(self, deco_node) -> tuple[str, list[str]]:
        from context_engine.indexer.http_endpoint import normalize_http_method

        call_node = None
        for child in deco_node.children:
            if child.type == "call":
                call_node = child
                break
        if call_node is None:
            return "", []
        route_path = ""
        methods: list[str] = []
        arg_list = call_node.child_by_field_name("arguments")
        if arg_list is not None:
            positional = 0
            for child in arg_list.named_children:
                if child.type == "string" and positional == 0:
                    raw = self._string_literal_text(child)
                    if not raw.startswith("/"):
                        return "", []
                    route_path = raw
                    positional += 1
                elif child.type == "keyword_argument":
                    key_node = child.child_by_field_name("name")
                    value_node = child.child_by_field_name("value")
                    if key_node is None or value_node is None:
                        continue
                    key = _node_text(key_node)
                    if key == "methods" and value_node.type == "list":
                        for item in value_node.named_children:
                            if item.type == "string":
                                method = normalize_http_method(self._string_literal_text(item))
                                if method:
                                    methods.append(method)
                    elif key == "method" and value_node.type == "string":
                        method = normalize_http_method(self._string_literal_text(value_node))
                        if method:
                            methods.append(method)
                elif child.type == "string":
                    positional += 1
        return route_path, methods

    def extract_hooks(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """Hook/event facts — make named-hook & pub/sub boundaries transparent.

        Each fact carries ``kind`` (config==subscribe / exec==publish),
        ``target_kind`` (the topic shape: ``method`` literal name / ``object``
        signal) and ``via`` (the api token). The linker turns one fact into up to
        two edges — an EVENT edge to the *topic* and a HOOK edge to the *api
        wrapper* (see ``link_hooks``):

        * **config** — a registration call/decorator: ``event.listen(target,
          "before_insert", fn)``, ``@event.listens_for(target, "before_insert")``,
          ``@receiver(post_save)``, ``signal.connect(fn)``. The register site
          (decorated function, or enclosing function of the call) subscribes to
          the topic via the api wrapper.
        * **exec** — a dispatch/publish: ``obj.dispatch.before_insert(...)`` or
          ``signal.send(...)``. The invoke site publishes to the topic.

        Like decorations these are syntactic facts (a string literal arg / a
        signal object / a ``.dispatch.``/``.send`` attribute), so the edge is
        *derived*, not guessed — names are resolved to real declaration nodes by
        the linker (name resolution, as for CALLS). The target type that would
        disambiguate *which* event class lives behind a dynamic dispatch is not
        statically available, so we emit the name only; the linker binds it solely
        when unambiguous (precision over recall — see ``link_hooks``).
        """
        if tree is None:
            tree = self._parse(source_code)
        register_names = frozenset({"listen", "listens_for"})
        out: list[dict] = []
        seen: set[tuple[str, str, str, str, str]] = set()

        def hook_name_from_args(call_node) -> str:
            arg_list = call_node.child_by_field_name("arguments")
            if arg_list is None:
                return ""
            for child in arg_list.named_children:
                if child.type == "string":
                    val = self._string_literal_text(child)
                    if val.isidentifier():
                        return val
                elif child.type == "keyword_argument":
                    break
            return ""

        def first_arg_name(call_node) -> str:
            """First positional arg as an object-signal name (identifier or
            attribute leaf): ``@receiver(post_save)`` / ``@receiver(signals.x)``."""
            arg_list = call_node.child_by_field_name("arguments")
            if arg_list is None:
                return ""
            for child in arg_list.named_children:
                if child.type == "identifier":
                    return _node_text(child)
                if child.type == "attribute":
                    leaf = child.child_by_field_name("attribute")
                    return _node_text(leaf) if leaf is not None else ""
                break  # first positional is not a plain reference
            return ""

        def receiver_name(fn_attr) -> str:
            """Leaf name of the receiver of ``<recv>.connect`` / ``<recv>.send``."""
            recv = fn_attr.child_by_field_name("object")
            if recv is None:
                return ""
            if recv.type == "identifier":
                return _node_text(recv)
            if recv.type == "attribute":
                leaf = recv.child_by_field_name("attribute")
                return _node_text(leaf) if leaf is not None else ""
            return ""

        def emit(site_uid: str, hook_name: str, kind: str, target_kind: str, via: str) -> None:
            if not site_uid or not hook_name:
                return
            key = (site_uid, hook_name, kind, target_kind, via)
            if key in seen:
                return
            seen.add(key)
            out.append(
                {
                    "site_uid": site_uid,
                    "hook_name": hook_name,
                    "kind": kind,
                    "target_kind": target_kind,
                    "via": via,
                    "file_path": file_path,
                }
            )

        def site_uid_for(node, *, decorated: bool) -> str:
            site_node = (
                self._hook_decorated_def(node) if decorated else None
            ) or self._enclosing_def_node(node)
            return (
                self._uid_for_node(site_node, source_code, file_path)
                if site_node is not None
                else ""
            )

        for node in self._iter_nodes(tree.root_node):
            if node.type != "call":
                continue
            fn = node.child_by_field_name("function")
            if fn is None:
                continue
            if fn.type == "identifier":
                base = _node_text(fn)
            elif fn.type == "attribute":
                tail = fn.child_by_field_name("attribute")
                base = _node_text(tail) if tail is not None else ""
            else:
                base = ""

            # --- method-kind config: ``listen``/``listens_for`` string-literal name
            if base in register_names:
                hook_name = hook_name_from_args(node)
                if hook_name:
                    emit(site_uid_for(node, decorated=True), hook_name, "config", "method", base)
                continue

            # --- object-signal config: ``@receiver(<signal>)`` decorator. The
            #     ``receiver`` idiom is unambiguously signal-specific, so the
            #     linker admits it without the connect+send co-occurrence check.
            if base == "receiver":
                sig = first_arg_name(node)
                if sig.isidentifier():
                    site = self._hook_decorated_def(node)
                    if site is not None:
                        emit(
                            self._uid_for_node(site, source_code, file_path),
                            sig,
                            "config",
                            "object",
                            "receiver",
                        )
                continue

            # --- object-signal config/exec: ``<signal>.connect(..)`` registers,
            #     ``<signal>.send(..)`` / ``.send_robust(..)`` emits. ``connect``/
            #     ``send`` are generic verbs (a DB connection, a websocket manager
            #     also ``.connect``), so ``via`` is threaded and the linker keeps
            #     the edge only when the target is BOTH connected and sent-from.
            if base in ("connect", "send", "send_robust") and fn.type == "attribute":
                sig = receiver_name(fn)
                if sig.isidentifier():
                    kind = "config" if base == "connect" else "exec"
                    via = "connect" if base == "connect" else "send"
                    emit(site_uid_for(node, decorated=True), sig, kind, "object", via)
                continue

            # --- method-kind exec: ``<expr>.dispatch.<hook>(...)`` dispatch
            if fn.type == "attribute":
                obj = fn.child_by_field_name("object")
                tail = fn.child_by_field_name("attribute")
                if (
                    obj is not None
                    and obj.type == "attribute"
                    and tail is not None
                    and tail.type == "identifier"
                ):
                    inner_attr = obj.child_by_field_name("attribute")
                    if inner_attr is not None and _node_text(inner_attr) == "dispatch":
                        hook_name = _node_text(tail)
                        if hook_name.isidentifier():
                            emit(
                                site_uid_for(node, decorated=False),
                                hook_name,
                                "exec",
                                "method",
                                "dispatch",
                            )
        return out

    @staticmethod
    def _hook_decorated_def(call_node):
        """If ``call_node`` is a decorator expression, the def/class it decorates.

        ``@event.listens_for(...)`` → the decorated definition node (so the hook
        site is the handler being registered, not its outer scope).
        """
        parent = call_node.parent
        if parent is None or parent.type != "decorator":
            return None
        deco_parent = parent.parent
        if deco_parent is None or deco_parent.type != "decorated_definition":
            return None
        defn = deco_parent.child_by_field_name("definition")
        if defn is not None and defn.type in ("function_definition", "class_definition"):
            return defn
        return None

    def extract_attr_accesses(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """READS_ATTR / WRITES_ATTR edges from a function to attribute Symbols.

        A function reading ``self.x`` or ``local.x`` (where ``local`` has a
        statically visible type via :meth:`_local_value_types`) emits a
        READS_ATTR edge to the attribute symbol; an assignment ``self.x =
        ...`` emits WRITES_ATTR; a subscript assignment ``self.x[k] = v``
        emits WRITES_ATTR with ``kind="subscript"`` (which is the key
        binding-surface signal — function building a mapping by writing
        into an attribute).

        Attribute symbol resolution is best-effort qualified_name first
        (``ClassName.attr``), with the linker falling back to workspace-
        unique-name match. Unresolvable attribute names produce no edge.

        Receivers other than ``self`` and known-typed locals (e.g.
        ``request.method``) emit a name-only target — the linker only
        binds them when the name is unique workspace-wide.
        """
        if tree is None:
            tree = self._parse(source_code)
        module = module_name_from_path(file_path)
        import_bindings = self._extract_import_bindings(source_code, file_path)
        method_returns, function_returns = self._build_return_type_table(
            tree, import_bindings, module
        )
        attr_type_table = self._build_attr_type_table(
            tree,
            import_bindings,
            module,
            method_returns=method_returns,
            function_returns=function_returns,
        )

        out: list[dict] = []
        seen: set[tuple[str, str, str, str]] = set()

        def emit(
            accessor_uid: str, accessor_name: str, attr_name: str, attr_qn: str, kind: str
        ) -> None:
            if not accessor_uid or not attr_name:
                return
            key = (accessor_uid, attr_name, attr_qn, kind)
            if key in seen:
                return
            seen.add(key)
            out.append(
                {
                    "accessor_uid": accessor_uid,
                    "accessor_name": accessor_name,
                    "attr_name": attr_name,
                    "attr_qualified_name": attr_qn,
                    "kind": kind,
                    "file_path": file_path,
                }
            )

        # Walk every function definition; for each, scan its body for
        # attribute reads and writes. We skip nested function bodies via
        # the same boundary the return-shape scan uses, so a helper closure
        # inside a method doesn't credit its outer scope.
        for fn in self._iter_nodes(tree.root_node):
            if fn.type != "function_definition":
                continue
            name_node = fn.child_by_field_name("name")
            body = fn.child_by_field_name("body")
            if name_node is None or body is None:
                continue
            accessor_uid = self._uid_for_node(fn, source_code, file_path)
            accessor_name = _node_text(name_node)
            enclosing_class = self._enclosing_class_name(fn)
            cls_table = attr_type_table.get(enclosing_class, {})
            local_types = self._local_value_types(
                fn,
                cls_table=cls_table,
                enclosing_class=enclosing_class,
                import_bindings=import_bindings,
                module=module,
                method_returns=method_returns,
                function_returns=function_returns,
            )

            def resolve_receiver(
                obj_node, *, ec=enclosing_class, ct=cls_table, lt=local_types
            ) -> str:
                """Best-guess qualified type for the receiver expression.

                Loop-bound locals (``ec`` / ``ct`` / ``lt``) are captured via
                default-argument binding so each function's closure sees its
                own snapshot rather than the loop's last iteration value.
                """
                if obj_node.type == "identifier":
                    name = _node_text(obj_node)
                    if name == "self" and ec:
                        return cast(str, ec)
                    return cast(str, lt.get(name, ""))
                if obj_node.type == "attribute":
                    inner = obj_node.child_by_field_name("object")
                    inner_attr = obj_node.child_by_field_name("attribute")
                    if (
                        inner is not None
                        and inner.type == "identifier"
                        and _node_text(inner) == "self"
                        and inner_attr is not None
                        and inner_attr.type == "identifier"
                    ):
                        return cast(str, ct.get(_node_text(inner_attr), ""))
                return ""

            # 1. Attribute reads — every ``obj.attr`` we can resolve a
            #    receiver type for.
            for node in self._iter_nodes(body):
                if node.type == "function_definition":
                    continue  # nested callable — its accesses belong to it
                if node.type != "attribute":
                    continue
                obj = node.child_by_field_name("object")
                attr = node.child_by_field_name("attribute")
                if obj is None or attr is None or attr.type != "identifier":
                    continue
                # Skip the callee position of a call: ``obj.method(...)`` — the
                # ``obj.method`` attribute is the call's ``function``, a method
                # *invocation*, not a data-shape attribute read. The call
                # resolver owns it (as a CALLS_* edge); emitting a parallel
                # READS_ATTR here only duplicates the site and, when the method
                # name is workspace-ambiguous, name-binds it to the wrong
                # Symbol. The receiver ``obj.attr`` of an outer access (e.g.
                # ``self.config`` in ``self.config.get()``) still emits, since
                # only the directly-called attribute is the ``function`` node.
                parent = node.parent
                if parent is not None and parent.type == "call":
                    fn_node = parent.child_by_field_name("function")
                    if fn_node is not None and fn_node.id == node.id:
                        continue
                # Skip ``self.x`` on the *write* side; the assignment loop
                # below handles writes. A read embedded in an assignment
                # right-hand side still shows up here as a separate node.
                if parent is not None and parent.type == "assignment":
                    lhs = parent.child_by_field_name("left")
                    if lhs is not None and lhs.start_byte == node.start_byte:
                        continue
                attr_name = _node_text(attr)
                receiver_qn = resolve_receiver(obj)
                if receiver_qn:
                    attr_qn = f"{receiver_qn}.{attr_name}"
                else:
                    attr_qn = ""
                emit(accessor_uid, accessor_name, attr_name, attr_qn, "read")

            # 2. Writes — ``self.attr = ...``, ``local.attr = ...``,
            #    ``self.attr[k] = v`` (subscript) and ``local[k] = v``
            #    (subscript on a local of known type — building a mapping).
            for node in self._iter_nodes(body):
                if node.type == "function_definition":
                    continue
                if node.type != "assignment":
                    continue
                left = node.child_by_field_name("left")
                if left is None:
                    continue
                if left.type == "attribute":
                    obj = left.child_by_field_name("object")
                    attr = left.child_by_field_name("attribute")
                    if obj is None or attr is None or attr.type != "identifier":
                        continue
                    attr_name = _node_text(attr)
                    receiver_qn = resolve_receiver(obj)
                    attr_qn = f"{receiver_qn}.{attr_name}" if receiver_qn else ""
                    emit(accessor_uid, accessor_name, attr_name, attr_qn, "write")
                elif left.type == "subscript":
                    base = left.child_by_field_name("value")
                    if base is None:
                        continue
                    if base.type == "attribute":
                        # ``self.attr[k] = v`` — write into mapping owned by
                        # ``self.attr``. The binding signal: function builds a
                        # mapping by writing into a class attribute.
                        obj = base.child_by_field_name("object")
                        attr = base.child_by_field_name("attribute")
                        if obj is None or attr is None or attr.type != "identifier":
                            continue
                        attr_name = _node_text(attr)
                        receiver_qn = resolve_receiver(obj)
                        attr_qn = f"{receiver_qn}.{attr_name}" if receiver_qn else ""
                        emit(accessor_uid, accessor_name, attr_name, attr_qn, "write_subscript")
                    elif base.type == "identifier":
                        # ``local[k] = v`` — only emit when ``local`` has a
                        # statically known mapping type (otherwise it could be
                        # anything). For now this leans on existing local-typing.
                        rname = _node_text(base)
                        local_type = local_types.get(rname, "")
                        if local_type:
                            emit(
                                accessor_uid,
                                accessor_name,
                                rname,
                                local_type,
                                "write_subscript_local",
                            )
        return out

    def extract_type_references(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """USES_TYPE references: a symbol names a project class in an AST-visible
        position → ``referrer`` USES_TYPE ``type``.

        Like an import (``from m import T``) or a decoration (``@deco``), a type
        reference is a *static* fact written in the source — a parameter/return
        annotation, an annotated assignment, or an ``isinstance``/``issubclass``
        check. We extract it as a derived edge rather than re-deriving the same
        connection from name tokens at query time. Resolution to an in-graph
        Symbol happens at link time, so builtins/stdlib types produce no edge
        (precision over recall; project classes only).
        """
        if tree is None:
            tree = self._parse(source_code)
        module = module_name_from_path(file_path)
        import_bindings = self._extract_import_bindings(source_code, file_path)
        out: list[dict] = []
        seen: set[tuple[str, str]] = set()

        def emit(referrer_node, type_node, kind: str) -> None:
            if referrer_node is None or type_node is None:
                return
            referrer_uid = self._uid_for_node(referrer_node, source_code, file_path)
            rname_node = referrer_node.child_by_field_name("name")
            referrer_name = _node_text(rname_node) if rname_node is not None else ""
            for type_name, type_qn in self._type_ref_targets(type_node, import_bindings, module):
                key = (referrer_uid, type_qn)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "referrer_uid": referrer_uid,
                        "referrer_name": referrer_name,
                        "type_name": type_name,
                        "type_qualified_name": type_qn,
                        "kind": kind,
                        "file_path": file_path,
                    }
                )

        for node in self._iter_nodes(tree.root_node):
            if node.type == "function_definition":
                params = node.child_by_field_name("parameters")
                if params is not None:
                    for p in params.named_children:
                        if p.type in ("typed_parameter", "typed_default_parameter"):
                            emit(node, p.child_by_field_name("type"), "param")
                emit(node, node.child_by_field_name("return_type"), "return")
            elif node.type == "call":
                fn = node.child_by_field_name("function")
                if (
                    fn is not None
                    and fn.type == "identifier"
                    and _node_text(fn)
                    in (
                        "isinstance",
                        "issubclass",
                    )
                ):
                    args = node.child_by_field_name("arguments")
                    referrer = self._enclosing_def_node(node)
                    if args is not None and referrer is not None:
                        type_args = [c for c in args.named_children]
                        if len(type_args) >= 2:
                            emit(referrer, type_args[1], "isinstance")
            elif node.type == "assignment":
                typ = node.child_by_field_name("type")
                if typ is not None:
                    referrer = self._enclosing_def_node(node)
                    if referrer is not None:
                        emit(referrer, typ, "annotation")
        return out

    def _type_ref_targets(
        self, type_node, import_bindings: dict[str, str], module: str
    ) -> list[tuple[str, str]]:
        """Collect (bare_name, qualified_name) for every class named in a type node.

        Handles bare names (``Dependant``), attribute access (``params.Depends`` →
        head resolved via imports), unions/optionals and subscripts (``A | B``,
        ``Optional[A]``, ``List[A]``). Generic/builtin heads (``Optional``, ``List``)
        are emitted too but resolve to no Symbol at link time and are dropped there.
        """
        out: list[tuple[str, str]] = []
        seen_local: set[str] = set()

        def walk(n) -> None:
            if n.type == "attribute":
                obj = n.child_by_field_name("object")
                attr = n.child_by_field_name("attribute")
                if attr is not None and attr.type == "identifier":
                    final = _node_text(attr)
                    if obj is not None and obj.type == "identifier":
                        head = _node_text(obj)
                        base = import_bindings.get(head, head)
                        qn = f"{base}.{final}"
                    else:
                        qn = self._resolve_type_name(final, import_bindings, module)
                    if qn not in seen_local:
                        seen_local.add(qn)
                        out.append((final, qn))
                return  # do not descend into the attribute's identifier children
            if n.type == "identifier":
                name = _node_text(n)
                qn = self._resolve_type_name(name, import_bindings, module)
                if qn not in seen_local:
                    seen_local.add(qn)
                    out.append((name, qn))
                return
            for ch in n.children:
                walk(ch)

        walk(type_node)
        return out

    def extract_injections(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """Dependency-injection bindings: ``def f(x = Marker(provider))`` → ``f`` INJECTS
        ``provider``.

        A provider wired into a parameter default (FastAPI ``Depends(get_db)``,
        dependency-injector ``Provide[...]``, pytest-style fixtures) is a static AST
        fact, like an import. Detection is structural: a parameter whose default (or
        ``Annotated[...]`` metadata) is a call whose positional argument is a bare
        symbol reference. The wrapped symbol is the injected provider; resolution to an
        in-graph symbol at link time drops locals/literals (project providers only).
        """
        if tree is None:
            tree = self._parse(source_code)
        module = module_name_from_path(file_path)
        import_bindings = self._extract_import_bindings(source_code, file_path)
        out: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for node in self._iter_nodes(tree.root_node):
            if node.type != "function_definition":
                continue
            params = node.child_by_field_name("parameters")
            if params is None:
                continue
            owner_uid = self._uid_for_node(node, source_code, file_path)
            owner_name_node = node.child_by_field_name("name")
            owner_name = _node_text(owner_name_node) if owner_name_node is not None else ""
            for p in params.named_children:
                if p.type not in ("default_parameter", "typed_default_parameter"):
                    continue
                for call in self._iter_nodes(p):
                    if call.type != "call":
                        continue
                    for prov in self._positional_identifier_arguments(call, source_code):
                        prov_qn = self._resolve_type_name(prov, import_bindings, module)
                        key = (owner_uid, prov_qn)
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append(
                            {
                                "owner_uid": owner_uid,
                                "owner_name": owner_name,
                                "provider_name": prov,
                                "provider_qualified_name": prov_qn,
                                "file_path": file_path,
                            }
                        )
        return out

    def extract_instantiations(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """INSTANTIATES edges: caller symbol -> the project class it constructs.

        Static construction forms (a refinement of a call where the callee is a
        class, distinct from an ordinary call):
          * literal ``X(...)`` where ``X`` names a class (local class def / import);
          * ``v(...)`` where ``v`` is a local directly annotated ``type[X]`` /
            ``Type[X]`` — the held value is the class object ``X``;
          * ``v(...)`` where ``v`` receives a class object through intra-procedural
            copy propagation — a plain ``v = <expr>`` whose ``<expr>`` copies,
            disjoins (``a or b``), or selects (``a if c else b``) an already-known
            class value (P5). E.g. ``route_class = route_class_override or
            self.route_class; route_class(...)`` constructs ``APIRoute`` via the
            ``type[APIRoute]``-typed parameter operand.
        Propagation is flow-insensitive (union of reachable class values) and only
        follows value-carrying operands; a call result, subscript, or unresolved
        ``self.<attr>`` carries no class identity. Resolution to an in-graph class
        happens at link time (kind=class), so names that resolve to no project class
        produce no edge — precision over recall.
        """
        if tree is None:
            tree = self._parse(source_code)
        module = module_name_from_path(file_path)
        import_bindings = self._extract_import_bindings(source_code, file_path)
        symbols = self.extract_symbols(source_code, file_path, tree=tree)
        local_classes = {s.name: s for s in symbols if s.kind == "class"}
        local_class_names = set(local_classes)

        out: list[dict] = []
        seen: set[tuple[str, str, str]] = set()
        typed_local_cache: dict[int, dict[str, list[tuple[str, str]]]] = {}
        value_local_cache: dict[int, dict[str, list[tuple[str, str]]]] = {}
        _, _, module_uid = self._module_symbol_identity(file_path)

        def emit(
            call_node,
            caller_node,
            type_name: str,
            type_qn: str,
            is_external: bool,
        ) -> None:
            if not type_qn:
                return
            # Module-level ``name = SomeCls(...)`` → the Variable Symbol IS the
            # caller: the new object lives under that identifier and is the DFG
            # anchor downstream code (decorators, references) talks to.
            if caller_node is None:
                var_uid = self._module_assignment_variable_uid(call_node, module)
                caller_uid = var_uid if var_uid is not None else module_uid
            else:
                caller_uid = self._uid_for_node(caller_node, source_code, file_path)
            key = (caller_uid, type_qn, "external" if is_external else "internal")
            if key in seen:
                return
            seen.add(key)
            out.append(
                {
                    "caller_uid": caller_uid,
                    "type_name": type_name,
                    "type_qualified_name": type_qn,
                    "is_external": is_external,
                    "file_path": file_path,
                }
            )

        def class_typed_locals(func_node) -> dict[str, list[tuple[str, str]]]:
            """name -> [(class_name, class_qn)] for locals annotated ``type[X]``."""
            if func_node is None:
                return {}
            cached = typed_local_cache.get(func_node.id)
            if cached is not None:
                return cached
            mapping: dict[str, list[tuple[str, str]]] = {}

            def add(name_node, type_node) -> None:
                if name_node is None or type_node is None:
                    return
                if name_node.type != "identifier":
                    return
                classes = self._class_object_targets(type_node, import_bindings, module)
                if classes:
                    mapping.setdefault(_node_text(name_node), []).extend(classes)

            params = func_node.child_by_field_name("parameters")
            if params is not None:
                for p in params.named_children:
                    if p.type == "typed_parameter":
                        ident = next((c for c in p.named_children if c.type == "identifier"), None)
                        add(ident, p.child_by_field_name("type"))
                    elif p.type == "typed_default_parameter":
                        add(p.child_by_field_name("name"), p.child_by_field_name("type"))
            for n in self._iter_nodes(func_node):
                if n.type == "assignment" and n.child_by_field_name("type") is not None:
                    add(n.child_by_field_name("left"), n.child_by_field_name("type"))
            typed_local_cache[func_node.id] = mapping
            return mapping

        def _unwrap(node):
            while node is not None and node.type == "parenthesized_expression":
                inner = node.named_children[0] if node.named_children else None
                if inner is None:
                    break
                node = inner
            return node

        def resolve_class_value(node, mapping) -> list[tuple[str, str]]:
            """Class objects an expression may evaluate to (copy / or-and / ternary).

            Only value-carrying forms propagate a class object: a name already known
            to hold a class, a local class / imported name, or a disjunction/ternary
            of such. A call result, subscript, or attribute access is an instance or
            structurally unknown — it carries no class identity (precision over recall).
            """
            node = _unwrap(node)
            if node is None:
                return []
            if node.type == "identifier":
                nm = _node_text(node)
                if nm in mapping:
                    return list(mapping[nm])
                if nm in local_classes:
                    return [(nm, local_classes[nm].qualified_name)]
                if nm in import_bindings:
                    return [(nm, import_bindings[nm])]
                return []
            if node.type == "boolean_operator":
                return resolve_class_value(
                    node.child_by_field_name("left"), mapping
                ) + resolve_class_value(node.child_by_field_name("right"), mapping)
            if node.type == "conditional_expression":
                kids = node.named_children
                if len(kids) >= 3:
                    return resolve_class_value(kids[0], mapping) + resolve_class_value(
                        kids[2], mapping
                    )
            return []

        def class_value_locals(func_node) -> dict[str, list[tuple[str, str]]]:
            """``class_typed_locals`` plus intra-procedural class-object propagation (P5).

            Seeds from ``type[X]``-annotated names, then propagates the class a local
            holds through plain ``x = <expr>`` assignments whose RHS copies / disjoins
            / selects already-known class values. Flow-insensitive union over a bounded
            fixpoint; ``self.<attr>`` stays unresolved (no instance-attribute typing),
            so only resolvable operands contribute.
            """
            if func_node is None:
                return {}
            cached = value_local_cache.get(func_node.id)
            if cached is not None:
                return cached
            mapping = {k: list(v) for k, v in class_typed_locals(func_node).items()}

            assignments: list[tuple[str, object]] = []
            for n in self._iter_nodes(func_node):
                if n.type != "assignment":
                    continue
                lhs = n.child_by_field_name("left")
                rhs = n.child_by_field_name("right")
                if lhs is None or rhs is None or lhs.type != "identifier":
                    continue
                assignments.append((_node_text(lhs), rhs))

            def merge(name: str, classes: list[tuple[str, str]]) -> bool:
                if not classes:
                    return False
                bucket = mapping.setdefault(name, [])
                changed = False
                for item in classes:
                    if item not in bucket:
                        bucket.append(item)
                        changed = True
                return changed

            for _ in range(len(assignments) + 1):
                changed = False
                for name, rhs in assignments:
                    if merge(name, resolve_class_value(rhs, mapping)):
                        changed = True
                if not changed:
                    break

            value_local_cache[func_node.id] = mapping
            return mapping

        for node in self._iter_nodes(tree.root_node):
            if node.type != "call":
                continue
            fn = node.child_by_field_name("function")
            if fn is None:
                continue
            caller = self._enclosing_def_node(node)
            # Module-level construction: caller is ``None`` here; ``emit``
            # routes the row to the module Symbol or, when the call is the
            # RHS of a module-level assignment, to the Variable anchor.
            locals_map = class_value_locals(caller) if caller is not None else {}

            # Branch A: ``v(...)`` where ``v`` holds a class via type[X]
            # propagation (locals_map). Names that show up here are
            # internal-or-external based on whether the propagated qn is
            # module-local.
            if fn.type == "identifier":
                name = _node_text(fn)
                if name in locals_map:
                    for cname, cqn in locals_map[name]:
                        is_external = not cqn.startswith(f"{module}.")
                        emit(node, caller, cname, cqn, is_external)
                    continue

            # Branch B: bare-name or dotted attribute construction. The
            # resolver decides external vs internal against the imports
            # table; unresolvable names drop silently right here, never
            # reaching the linker. This is the only layer that has both the
            # imports table and the local class set, so it is also the only
            # honest place to make that call.
            resolved = self._resolve_construction_callee(
                fn,
                import_bindings=import_bindings,
                local_classes=local_class_names,
                module=module,
            )
            if resolved is None:
                continue
            type_name, type_qn, is_external = resolved
            emit(node, caller, type_name, type_qn, is_external)
        return out

    def _module_assignment_variable_uid(self, call_node, module: str) -> str | None:
        """Return the Variable Symbol uid for ``name = call`` at module top level.

        Mirrors :meth:`_module_constructor_variables` exactly — same uid
        formula — so the caller_uid on instantiation rows lines up with the
        Variable Symbol the indexer materializes. Returns ``None`` when the
        call is not the RHS of a module-level identifier assignment.
        """
        parent = call_node.parent
        while parent is not None and parent.type == "parenthesized_expression":
            parent = parent.parent
        if parent is None or parent.type != "assignment":
            return None
        grand = parent.parent
        if grand is None or grand.type != "expression_statement":
            return None
        if grand.parent is None or grand.parent.type != "module":
            return None
        lhs = parent.child_by_field_name("left")
        if lhs is None or lhs.type != "identifier":
            return None
        var_name = _node_text(lhs)
        if not var_name:
            return None
        qualified_name = f"{module}.{var_name}"
        signature = f"{var_name}()->_"
        return compute_uid(qualified_name, signature, self.language_name)

    def _class_object_targets(
        self, type_node, import_bindings: dict[str, str], module: str
    ) -> list[tuple[str, str]]:
        """Classes ``X`` named inside a ``type[X]`` / ``Type[X]`` annotation.

        Only ``type``/``Type`` subscripts qualify: the annotated value is a class
        object, so calling it constructs ``X``. Other annotation shapes yield
        nothing — calling a non-``type``-annotated variable is not a construction.
        """
        out: list[tuple[str, str]] = []
        for n in self._iter_nodes(type_node):
            if n.type == "subscript":
                value = n.child_by_field_name("value")
                if (
                    value is None
                    or value.type != "identifier"
                    or _node_text(value) not in ("type", "Type")
                ):
                    continue
                for sub in n.named_children:
                    if sub.id == value.id:
                        continue
                    out.extend(self._type_ref_targets(sub, import_bindings, module))
                continue

            # Current tree-sitter-python represents annotations as
            # ``type -> generic_type -> type_parameter`` rather than the
            # expression ``subscript`` shape. Treat only ``type[X]`` / ``Type[X]``
            # as a class-object annotation; ordinary ``X`` annotations remain
            # instance types and do not imply construction.
            if n.type == "generic_type":
                head = n.named_children[0] if n.named_children else None
                if (
                    head is None
                    or head.type != "identifier"
                    or _node_text(head) not in ("type", "Type")
                ):
                    continue
                for sub in n.named_children[1:]:
                    out.extend(self._type_ref_targets(sub, import_bindings, module))
        return out

    @staticmethod
    def _enclosing_def_node(node):
        """Nearest enclosing function/class definition node, or None."""
        current = node.parent
        while current is not None:
            if current.type in ("function_definition", "class_definition"):
                return current
            current = current.parent
        return None

    @staticmethod
    def _decorator_base_name(decorator_node) -> str:
        """The decorator's leaf callable identifier: ``@app.route(...)`` → route.

        Kept for compatibility; extraction also records the full dotted callable
        path via :meth:`_decorator_callable_name`.
        """
        name = PythonAdapter._decorator_callable_name(decorator_node)
        return name.rsplit(".", 1)[-1] if name else ""

    @staticmethod
    def _decorator_callable_name(decorator_node) -> str:
        """Decorator callable path: ``@x.y.z(...)`` → ``x.y.z``."""
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
            return PythonAdapter._attribute_path(expr)
        return ""

    @staticmethod
    def _attribute_path(node) -> str:
        """Dotted identifier path from a Python ``attribute`` AST node."""
        if node is None:
            return ""
        if node.type == "identifier":
            return _node_text(node)
        if node.type != "attribute":
            return ""
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        if attr is None:
            return ""
        attr_name = _node_text(attr)
        prefix = PythonAdapter._attribute_path(obj)
        return f"{prefix}.{attr_name}" if prefix else attr_name

    @staticmethod
    def _inheritance_base_name(base_node) -> str:
        """Superclass head name from ``Base``, ``pkg.Base`` or ``Base[T]``."""
        if base_node is None:
            return ""
        if base_node.type == "identifier":
            return _node_text(base_node)
        if base_node.type == "attribute":
            attr = base_node.child_by_field_name("attribute")
            return _node_text(attr) if attr is not None else ""
        if base_node.type == "subscript":
            value = base_node.child_by_field_name("value")
            if value is None:
                value = base_node.named_children[0] if base_node.named_children else None
            return PythonAdapter._inheritance_base_name(value)
        if base_node.type == "call":
            fn = base_node.child_by_field_name("function")
            return PythonAdapter._inheritance_base_name(fn)
        return ""

    @staticmethod
    def _inheritance_base_path(base_node) -> str:
        """Dotted superclass expression: ``Base``, ``mod.Base``, ``a.b.Base``.

        Unlike ``_inheritance_base_name`` (which returns the head only), this
        preserves the receiver chain so the EXTENDS_EXTERNAL resolver can
        reconstruct the upstream qualified name through ``IMPORTS_EXTERNAL_SYMBOL``.
        """
        if base_node is None:
            return ""
        if base_node.type == "identifier":
            return _node_text(base_node)
        if base_node.type == "attribute":
            attr = base_node.child_by_field_name("attribute")
            obj = base_node.child_by_field_name("object")
            attr_text = _node_text(attr) if attr is not None else ""
            obj_path = PythonAdapter._inheritance_base_path(obj)
            if obj_path and attr_text:
                return f"{obj_path}.{attr_text}"
            return attr_text or obj_path
        if base_node.type == "subscript":
            value = base_node.child_by_field_name("value")
            if value is None:
                value = base_node.named_children[0] if base_node.named_children else None
            return PythonAdapter._inheritance_base_path(value)
        if base_node.type == "call":
            fn = base_node.child_by_field_name("function")
            return PythonAdapter._inheritance_base_path(fn)
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

        # Flatten captures from matches into (node, tag) tuples
        captures = []
        for _match_id, captures_dict in iter_ts_query_matches(
            self.language, self.call_query, tree.root_node
        ):
            for tag, nodes in captures_dict.items():
                for node in nodes:
                    captures.append((node, tag))

        symbols = self.extract_symbols(source_code, file_path, tree=tree)
        by_name: dict[str, list] = {}
        for symbol in symbols:
            by_name.setdefault(symbol.name, []).append(symbol)
        import_bindings = self._extract_import_bindings(source_code, file_path)
        module = module_name_from_path(file_path)
        method_returns, function_returns = self._build_return_type_table(
            tree, import_bindings, module
        )
        attr_type_table = self._build_attr_type_table(
            tree,
            import_bindings,
            module,
            method_returns=method_returns,
            function_returns=function_returns,
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
                            parent,
                            obj_node,
                            call_name,
                            attr_type_table,
                            alias_cache,
                            method_returns,
                            function_returns,
                            import_bindings,
                            module,
                        )
                        if typed is not None:
                            tier = "typed"
                            confidence = 0.8
                            callee_qualified_name = typed
                elif obj_node.type == "attribute":
                    typed = self._typed_qualified_target(
                        parent,
                        obj_node,
                        call_name,
                        attr_type_table,
                        alias_cache,
                        method_returns,
                        function_returns,
                        import_bindings,
                        module,
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

    def _resolve_dotted_name(self, raw: str, import_bindings: dict[str, str], module: str) -> str:
        """Resolve dotted names through the imported head, preserving the tail."""
        if not raw:
            return ""
        if raw in import_bindings:
            return import_bindings[raw]
        head, dot, tail = raw.partition(".")
        if dot and head in import_bindings:
            return f"{import_bindings[head]}.{tail}"
        return f"{module}.{raw}"

    def _declared_parameter_types(
        self, fn_node, import_bindings: dict[str, str], module: str
    ) -> dict[str, str]:
        """Parameter name -> declared type for typed parameters."""
        out: dict[str, str] = {}
        params_node = fn_node.child_by_field_name("parameters")
        if params_node is None:
            return out
        for prm in params_node.named_children:
            if prm.type not in ("typed_parameter", "typed_default_parameter"):
                continue
            if prm.type == "typed_parameter":
                ident = next((c for c in prm.named_children if c.type == "identifier"), None)
            else:
                ident = prm.child_by_field_name("name")
            ptype = prm.child_by_field_name("type")
            if ident is None or ptype is None:
                continue
            targets = self._type_ref_targets(ptype, import_bindings, module)
            if targets:
                out[_node_text(ident)] = targets[0][1]
        return out

    def _call_result_type(
        self,
        callee,
        *,
        enclosing_class: str = "",
        import_bindings: dict[str, str],
        module: str,
        method_returns: dict[tuple[str, str], str] | None = None,
        function_returns: dict[str, str] | None = None,
        allow_bare_constructor: bool = False,
    ) -> str:
        """Static result type for a call expression, if the type is visible."""
        method_returns = method_returns or {}
        function_returns = function_returns or {}
        if callee is None:
            return ""
        if callee.type == "identifier":
            name = _node_text(callee)
            inferred = function_returns.get(name)
            if inferred:
                return inferred
            if allow_bare_constructor or name[:1].isupper():
                return self._resolve_type_name(name, import_bindings, module)
            return ""
        if callee.type == "attribute":
            obj = callee.child_by_field_name("object")
            attr = callee.child_by_field_name("attribute")
            if obj is None or attr is None:
                return ""
            if obj.type == "identifier" and _node_text(obj) == "self" and enclosing_class:
                inferred = method_returns.get((enclosing_class, _node_text(attr)))
                if inferred:
                    return inferred
            if obj.type == "identifier" and _node_text(attr)[:1].isupper():
                base = import_bindings.get(_node_text(obj), _node_text(obj))
                return f"{base}.{_node_text(attr)}"
        return ""

    def _local_value_types(
        self,
        func_node,
        *,
        cls_table: dict[str, str],
        enclosing_class: str,
        import_bindings: dict[str, str],
        module: str,
        method_returns: dict[tuple[str, str], str] | None = None,
        function_returns: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Flow-insensitive local value types from annotations, params, and copies."""
        mapping = self._declared_parameter_types(func_node, import_bindings, module)
        assignments: list[tuple[str, object]] = []
        for assign in self._iter_nodes(func_node):
            if assign.type != "assignment":
                continue
            left = assign.child_by_field_name("left")
            if left is None or left.type != "identifier":
                continue
            typ = assign.child_by_field_name("type")
            if typ is not None:
                targets = self._type_ref_targets(typ, import_bindings, module)
                if targets:
                    mapping.setdefault(_node_text(left), targets[0][1])
            assignments.append((_node_text(left), assign))

        def resolve_expr(expr) -> str:
            if expr is None:
                return ""
            if expr.type == "identifier":
                return mapping.get(_node_text(expr), "")
            if expr.type == "attribute":
                obj = expr.child_by_field_name("object")
                attr = expr.child_by_field_name("attribute")
                if obj is not None and _node_text(obj) == "self" and attr is not None:
                    return cls_table.get(_node_text(attr), "")
                return ""
            if expr.type == "call":
                return self._call_result_type(
                    expr.child_by_field_name("function"),
                    enclosing_class=enclosing_class,
                    import_bindings=import_bindings,
                    module=module,
                    method_returns=method_returns,
                    function_returns=function_returns,
                )
            return ""

        for _ in range(len(assignments) + 1):
            changed = False
            for name, assign in assignments:
                inferred = resolve_expr(cast(Any, assign).child_by_field_name("right"))
                if inferred and name not in mapping:
                    mapping[name] = inferred
                    changed = True
            if not changed:
                break
        return mapping

    def _build_attr_type_table(
        self,
        tree,
        import_bindings: dict[str, str],
        module: str,
        *,
        method_returns: dict[tuple[str, str], str] | None = None,
        function_returns: dict[str, str] | None = None,
    ) -> dict[str, dict[str, str]]:
        """Infer instance-attribute types per class (structural; no framework literals).

        Sources: ``<base>_cls = 'mod:Class'`` string convention, class-level
        annotation ``x: Class``, and instance-method assignments where the RHS has a
        visible static type: ``self.x: Type``, ``self.x = typed_param``,
        ``self.x = local_alias``, ``self.x = factory()`` with known return type, or
        direct constructor calls.
        """
        method_returns = method_returns or {}
        function_returns = function_returns or {}
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
            # Instance-method assignments: the class shape may be established by
            # __init__, configure/bind methods, or framework hooks. Keep the signal
            # structural: only typed params/locals, explicit annotations, known
            # return types, or constructor-looking calls carry a type.
            for fn in body.children:
                if fn.type != "function_definition":
                    continue
                fn_name = fn.child_by_field_name("name")
                if fn_name is None:
                    continue
                method_name = _node_text(fn_name)
                local_types = self._local_value_types(
                    fn,
                    cls_table=attrs,
                    enclosing_class=cname,
                    import_bindings=import_bindings,
                    module=module,
                    method_returns=method_returns,
                    function_returns=function_returns,
                )
                for assign in self._iter_nodes(fn):
                    if assign.type != "assignment":
                        continue
                    left = assign.child_by_field_name("left")
                    right = assign.child_by_field_name("right")
                    typ = assign.child_by_field_name("type")
                    if left is None or left.type != "attribute":
                        continue
                    obj = left.child_by_field_name("object")
                    attr = left.child_by_field_name("attribute")
                    if obj is None or _node_text(obj) != "self" or attr is None:
                        continue
                    aname = _node_text(attr)
                    # (a) explicit annotation: ``self.x: Type = ...`` — the developer
                    # declared the attribute's type. Use the type-ref resolver so a
                    # qualified annotation (``routing.APIRouter``) keeps its module
                    # (``fastapi.routing.APIRouter``), not the current one.
                    if typ is not None:
                        targets = self._type_ref_targets(typ, import_bindings, module)
                        if targets:
                            attrs.setdefault(aname, targets[0][1])
                            continue
                    # (c) typed value propagation: ``self.x = param`` where ``param``
                    # is type-annotated, or ``self.x = local`` where ``local`` copied
                    # such a value / a known-return factory result.
                    if right is not None and right.type == "identifier":
                        rname = _node_text(right)
                        if rname in local_types:
                            attrs.setdefault(aname, local_types[rname])
                        continue
                    # (d) known return / constructor: ``self.x = factory()`` when
                    # factory has a visible return type, or ``self.x = Class(...)`` /
                    # ``self.x = mod.Class(...)``. Preserve the old __init__ behavior
                    # for bare constructor calls; outside __init__, require a
                    # constructor-looking name or a known return type.
                    if right is None or right.type != "call":
                        continue
                    callee = right.child_by_field_name("function")
                    inferred = self._call_result_type(
                        callee,
                        enclosing_class=cname,
                        import_bindings=import_bindings,
                        module=module,
                        method_returns=method_returns,
                        function_returns=function_returns,
                        allow_bare_constructor=method_name == "__init__",
                    )
                    if inferred:
                        attrs.setdefault(aname, inferred)
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
        self, tree, source_code: str, import_bindings: dict[str, str], module: str
    ) -> dict[str, dict]:
        """Resolve module-level lazy-proxy variables to the type they forward to.

        ``X = SomeProxy(callable)`` is a generic Python idiom (werkzeug ``LocalProxy``,
        celery ``Proxy`` — "stolen from werkzeug"); attribute access forwards to the
        wrapped object. Detection is by class-name convention (ends with ``Proxy``),
        mirroring the ``_cls = 'mod:Class'`` convention, not a receiver name-match.

        Returns ``{var_name: {target_type, target_source, wrapped_callable, confidence}}``.
        Two sources: the ANNOTATED form names the type directly; the BARE form resolves
        through the wrapped callable's body (the class it imports-and-constructs).
        """
        context_var_types = self._build_context_var_type_table(
            tree,
            source_code,
            import_bindings,
            module,
        )

        # name -> function_definition node, and simple ``alias = other`` function aliases.
        func_nodes: dict[str, object] = {}
        func_aliases: dict[str, str] = {}
        for node in self._iter_nodes(tree.root_node):
            if node.type == "function_definition":
                nm = node.child_by_field_name("name")
                if nm is not None:
                    func_nodes[_node_text(nm)] = node
            elif node.type == "assignment":
                lf = node.child_by_field_name("left")
                rt = node.child_by_field_name("right")
                if (
                    lf is not None
                    and lf.type == "identifier"
                    and rt is not None
                    and rt.type == "identifier"
                ):
                    func_aliases[_node_text(lf)] = _node_text(rt)

        table: dict[str, dict] = {}
        for stmt in self._iter_nodes(tree.root_node):
            if stmt.type != "assignment":
                continue
            left = stmt.child_by_field_name("left")
            right = stmt.child_by_field_name("right")
            typ = stmt.child_by_field_name("type")
            if left is None or left.type != "identifier" or right is None:
                continue
            if right.type != "call":
                continue
            callee = right.child_by_field_name("function")
            if callee is None or callee.type != "identifier":
                continue
            if not _node_text(callee).endswith("Proxy"):
                continue
            var_name = _node_text(left)

            # Source 1: annotation names the forwarded type directly.
            if typ is not None:
                type_ident = self._type_identifier(typ)
                if type_ident:
                    context_binding = self._proxy_context_binding(
                        right,
                        context_var_types,
                        source_code,
                    )
                    table[var_name] = {
                        "target_type": self._resolve_type_name(type_ident, import_bindings, module),
                        "target_source": "annotation",
                        "wrapped_callable": "",
                        "confidence": 1.0,
                        **context_binding,
                    }
                continue

            # Source 2: bare ``Proxy(callable)`` — resolve via the wrapped callable's body.
            wrapped = self._first_positional_identifier(right)
            if not wrapped:
                continue
            resolved_wrapped = func_aliases.get(wrapped, wrapped)
            fn = func_nodes.get(resolved_wrapped)
            if fn is None:
                continue
            target_qn = self._constructed_imported_class(fn, source_code, import_bindings, module)
            if not target_qn:
                continue
            table[var_name] = {
                "target_type": target_qn,
                "target_source": "wrapped_callable",
                "wrapped_callable": f"{module}.{resolved_wrapped}",
                "confidence": 0.65,
            }
        return table

    def _build_context_var_type_table(
        self,
        tree,
        source_code: str,
        import_bindings: dict[str, str],
        module: str,
    ) -> dict[str, str]:
        """Map ``ContextVar[T]`` module bindings to their context payload type."""
        table: dict[str, str] = {}
        for stmt in self._iter_nodes(tree.root_node):
            if stmt.type != "assignment":
                continue
            left = stmt.child_by_field_name("left")
            typ = stmt.child_by_field_name("type")
            if left is None or left.type != "identifier" or typ is None:
                continue
            payload = self._context_var_payload_type_identifier(typ)
            if not payload:
                continue
            table[_node_text(left)] = self._resolve_type_name(payload, import_bindings, module)
        return table

    @staticmethod
    def _context_var_payload_type_identifier(type_node) -> str:
        """Return ``T`` for ``ContextVar[T]`` annotations, else ``''``."""
        for node in PythonAdapter._iter_nodes(type_node):
            if node.type != "generic_type":
                continue
            named = [child for child in node.children if child.is_named]
            if len(named) < 2:
                continue
            base = named[0]
            if base.type != "identifier" or _node_text(base) != "ContextVar":
                continue
            payload = next(
                (
                    child
                    for child in named[1:]
                    if child.type in {"type", "type_parameter", "identifier"}
                ),
                None,
            )
            if payload is None:
                continue
            if payload.type == "identifier":
                return _node_text(payload)
            for child in PythonAdapter._iter_nodes(payload):
                if child.type == "identifier":
                    return _node_text(child)
        return ""

    def _proxy_context_binding(
        self,
        call_node,
        context_var_types: dict[str, str],
        source_code: str,
    ) -> dict[str, str]:
        """Binding metadata for ``Proxy(context_var, "attr")`` forms."""
        args = call_node.child_by_field_name("arguments")
        if args is None:
            return {}
        positional = [
            child
            for child in args.named_children
            if child.type not in {"keyword_argument", "comment"}
        ]
        if len(positional) < 2:
            return {}
        context_var_node, attr_node = positional[0], positional[1]
        if context_var_node.type != "identifier" or attr_node.type != "string":
            return {}
        context_var = _node_text(context_var_node)
        context_type = context_var_types.get(context_var, "")
        context_attr = self._literal_string_value(attr_node, source_code)
        if not context_type or not context_attr:
            return {}
        return {
            "context_var": context_var,
            "context_type": context_type,
            "context_attr": context_attr,
            "binding_source": "context_attr",
        }

    @staticmethod
    def _literal_string_value(node, source_code: str) -> str:
        if node is None or node.type != "string":
            return ""
        text = source_code[node.start_byte : node.end_byte]
        try:
            value = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            return ""
        return value if isinstance(value, str) else ""

    @staticmethod
    def _first_positional_identifier(call_node):
        """First positional argument of a call, if it is a bare identifier."""
        args = call_node.child_by_field_name("arguments")
        if args is None:
            return ""
        for child in args.named_children:
            if child.type == "identifier":
                return _node_text(child)
            return ""  # first positional is not a bare identifier (e.g. a lambda)
        return ""

    def _constructed_imported_class(
        self, func_node, source_code: str, import_bindings: dict[str, str], module: str
    ) -> str:
        """The single class a function imports-and-constructs in its body, else ''.

        Structural points-to: a function whose body does ``from m import C`` (module- or
        body-local) and then ``C(...)`` is producing a ``C``. Keyed on the import binding
        (not capitalization). Returns the resolved qualified name only when exactly one
        such class is constructed (ambiguity -> no edge, precision over recall).
        """
        # Body-local from-imports add to the visible bindings for this function.
        bindings = dict(import_bindings)
        for node in self._iter_nodes(func_node):
            if node.type in ("import_from_statement",):
                text = _node_text(node).strip()
                from_parts = split_python_from_import(text)
                if from_parts:
                    import_module, names = from_parts
                    target_module = self._resolve_import_module(
                        import_module, module.rsplit(".", 1)[0] if "." in module else ""
                    )
                    for item in names.split(","):
                        item = item.strip()
                        original, _, alias = item.partition(" as ")
                        local = alias.strip() or original.strip()
                        if local and local != "*":
                            bindings[local] = f"{target_module}.{original.strip()}"

        constructed: set[str] = set()
        for node in self._iter_nodes(func_node):
            if node.type != "call":
                continue
            fn = node.child_by_field_name("function")
            if fn is None or fn.type != "identifier":
                continue
            name = _node_text(fn)
            if name in bindings:
                constructed.add(bindings[name])
        return next(iter(constructed)) if len(constructed) == 1 else ""

    @staticmethod
    def _type_identifier(type_node) -> str:
        """Extract a bare class name from an annotation node, ignoring generics/unions."""
        if type_node.type == "identifier":
            return _node_text(type_node)
        for child in PythonAdapter._iter_nodes(type_node):
            if child.type == "identifier":
                return _node_text(child)
        return ""

    def _typed_qualified_target(
        self,
        parent,
        receiver_node,
        call_name: str,
        attr_type_table: dict[str, dict[str, str]],
        alias_cache: dict[int, dict[str, str]],
        method_returns: dict[tuple[str, str], str] | None = None,
        function_returns: dict[str, str] | None = None,
        import_bindings: dict[str, str] | None = None,
        module: str = "",
    ) -> str | None:
        """Tier 4.5 CALLS_TYPED: resolve ``self.attr.m()`` / ``local.m()`` to ``Type.m``.

        For local receivers, the alias table is the same flow-insensitive map that
        ``_build_attr_type_table`` already uses for ``self.x = …`` — so a typed
        parameter (``def f(x: Foo):``), an annotated local (``v: Foo = …``), a
        bare constructor (``v = Foo()`` / ``v = mod.Foo()``), and a known return
        type (``v = func()`` with ``-> Foo``) all carry through; multi-hop
        ``v = u; w = v`` propagates via the fixpoint inside ``_local_value_types``.
        """
        import_bindings = import_bindings or {}
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
                alias_cache[parent.id] = self._local_value_types(
                    parent,
                    cls_table=cls_table,
                    enclosing_class=enclosing,
                    import_bindings=import_bindings,
                    module=module,
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

    def extract_reexports(self, source_code: str, file_path: str) -> list[dict]:
        """Re-export edges: a package ``__init__`` surfacing a symbol from a submodule.

        ``from .submodule import Name`` (optionally ``as Name``) in an ``__init__``
        brings ``Name`` into the package's public namespace — a re-export, distinct
        from an ordinary import inside a regular module. Only ``__init__`` files are
        treated as package surface. The target is matched to a project symbol during
        linking; names resolving to nothing in-graph (stdlib/external) produce no
        edge — precision over recall, like USES_TYPE.

        Returns dicts with ``init_file``, ``export_name`` (the surfaced local name),
        and ``export_qualified_name`` (best-effort target qn, relative imports
        resolved).
        """
        if Path(file_path).name not in ("__init__.py", "__init__.pyi"):
            return []
        bindings = self._extract_import_bindings(source_code, file_path)
        return [
            {
                "init_file": file_path,
                "export_name": local_name,
                "export_qualified_name": qualified_name,
            }
            for local_name, qualified_name in bindings.items()
        ]

    def _extract_import_bindings(self, source_code: str, file_path: str) -> dict[str, str]:
        """Return local import alias -> best-effort target qualified name."""
        module = module_name_from_path(file_path)
        if Path(file_path).name in ("__init__.py", "__init__.pyi"):
            package = module
        else:
            package = module.rsplit(".", 1)[0] if "." in module else ""
        bindings: dict[str, str] = {}
        for line in source_code.splitlines():
            stripped = line.strip()
            from_parts = split_python_from_import(stripped)
            if from_parts:
                import_module, names = from_parts
                target_module = self._resolve_import_module(import_module, package)
                for item in names.split(","):
                    item = item.strip()
                    if not item or item == "*":
                        continue
                    original, _, alias = item.partition(" as ")
                    local_name = alias.strip() or original.strip()
                    bindings[local_name] = f"{target_module}.{original.strip()}"
                continue

            import_body = split_python_import_clause(stripped)
            if import_body:
                for item in import_body.split(","):
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
