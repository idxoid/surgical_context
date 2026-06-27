"""Python language adapter using tree-sitter."""

import ast
import importlib.metadata
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from context_engine.parser.adapters.treesitter_base import TreeSitterAdapter
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

_INIT_PY = "__init__.py"

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
    def _resolve_identifier_construction_callee(
        cls,
        name: str,
        *,
        import_bindings: dict[str, str],
        local_classes: set[str],
        module: str,
    ) -> tuple[str, str, bool] | None:
        if not name:
            return None
        if name in local_classes:
            return (name, f"{module}.{name}", False)
        if name in import_bindings:
            return (name, import_bindings[name], True)
        if name in cls._BUILTIN_CALLABLE_NAMES:
            return None
        return None

    @classmethod
    def _resolve_attribute_construction_callee(
        cls,
        fn_node,
        *,
        import_bindings: dict[str, str],
    ) -> tuple[str, str, bool] | None:
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
            return cls._resolve_identifier_construction_callee(
                _node_text(fn_node),
                import_bindings=import_bindings,
                local_classes=local_classes,
                module=module,
            )
        if fn_node.type == "attribute":
            return cls._resolve_attribute_construction_callee(
                fn_node,
                import_bindings=import_bindings,
            )
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

    @staticmethod
    def _apply_return_shape_markers(symbol, shape: dict[str, bool]) -> None:
        if shape.get("mapping"):
            symbol.returns_mapping = True
        if shape.get("sequence"):
            symbol.returns_sequence = True
        if shape.get("constructed"):
            symbol.returns_constructed_type = True

    @staticmethod
    def _apply_iteration_shape_markers(symbol, it: dict[str, bool]) -> None:
        if it.get("iterates_attr_call"):
            symbol.iterates_attr_call = True
        if it.get("assembles_mapping_in_loop"):
            symbol.assembles_mapping_in_loop = True

    @staticmethod
    def _apply_python_symbol_shape_markers(
        symbols: list,
        shapes: dict[str, dict[str, bool]] | None,
        iteration: dict[str, dict[str, bool]] | None,
    ) -> None:
        if not shapes and not iteration:
            return
        for symbol in symbols:
            if shapes is not None:
                shape = shapes.get(symbol.name)
                if shape is not None:
                    PythonAdapter._apply_return_shape_markers(symbol, shape)
            if iteration is not None:
                it = iteration.get(symbol.name)
                if it is not None:
                    PythonAdapter._apply_iteration_shape_markers(symbol, it)

    def _finalize_python_symbol_metadata(
        self,
        symbols: list,
        source_code: str,
        file_path: str,
        *,
        tree,
    ) -> None:
        if tree is None:
            tree = self._parse(source_code)
        module_var_symbols = self._module_constructor_variables(
            source_code, file_path, tree, base_symbols=symbols
        )
        symbols.extend(module_var_symbols)
        shapes = self._function_return_shapes(tree)
        iteration = self._function_iteration_shapes(tree)
        self._apply_python_symbol_shape_markers(symbols, shapes, iteration)
        from context_engine.parser.docstring_extract import attach_docstrings

        attach_docstrings(
            symbols,
            source_code,
            tree=tree,
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
        self._finalize_python_symbol_metadata(
            symbols,
            source_code,
            file_path,
            tree=tree,
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
    def _apply_for_statement_iteration_flags(cls, n, flags: dict[str, bool]) -> None:
        left = n.child_by_field_name("left")
        right = n.child_by_field_name("right")
        fbody = n.child_by_field_name("body")
        if fbody is None:
            return
        if right is not None and right.type == "attribute":
            loop_var = _node_text(left) if (left is not None and left.type == "identifier") else ""
            if cls._for_body_calls_on(fbody, loop_var):
                flags["iterates_attr_call"] = True
        if cls._for_body_writes_subscript(fbody):
            flags["assembles_mapping_in_loop"] = True

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
                continue
            if n.type == "for_statement":
                fbody = n.child_by_field_name("body")
                if fbody is None:
                    for child in n.children:
                        stack.append(child)
                    continue
                cls._apply_for_statement_iteration_flags(n, flags)
            for child in n.children:
                stack.append(child)
        return flags

    @classmethod
    def _is_nested_callable_boundary(cls, node) -> bool:
        return node.type in ("function_definition", "lambda")

    @classmethod
    def _call_uses_loop_var(cls, fn, loop_var: str) -> bool:
        if fn is None or fn.type != "attribute":
            return False
        obj = fn.child_by_field_name("object")
        return obj is not None and obj.type == "identifier" and _node_text(obj) == loop_var

    @classmethod
    def _for_body_calls_on(cls, body, loop_var: str) -> bool:
        """``loop_var.method(...)`` anywhere inside ``body`` (not nested fn)."""
        if not loop_var:
            return False
        stack = [body]
        while stack:
            n = stack.pop()
            if cls._is_nested_callable_boundary(n):
                continue
            if n.type == "call":
                fn = n.child_by_field_name("function")
                if cls._call_uses_loop_var(fn, loop_var):
                    return True
            stack.extend(n.children)
        return False

    @classmethod
    def _is_subscript_assignment_target(cls, left) -> bool:
        if left is None or left.type != "subscript":
            return False
        base = left.child_by_field_name("value")
        return base is not None and base.type in ("identifier", "attribute")

    @classmethod
    def _for_body_writes_subscript(cls, body) -> bool:
        """``result[k] = …`` (subscript assignment on local or self.attr)."""
        stack = [body]
        while stack:
            n = stack.pop()
            if cls._is_nested_callable_boundary(n):
                continue
            if n.type == "assignment":
                left = n.child_by_field_name("left")
                if cls._is_subscript_assignment_target(left):
                    return True
            stack.extend(n.children)
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
    def _collect_local_return_assigns(cls, body) -> dict[str, str]:
        local_assigns: dict[str, str] = {}
        stack = [body]
        while stack:
            n = stack.pop()
            if cls._is_nested_callable_boundary(n):
                continue
            if n.type == "assignment":
                left = n.child_by_field_name("left")
                right = n.child_by_field_name("right")
                if left is not None and right is not None and left.type == "identifier":
                    kind = cls._classify_return_expr(right)
                    if kind:
                        local_assigns[_node_text(left)] = kind
            stack.extend(n.children)
        return local_assigns

    @classmethod
    def _return_shape_from_statement(cls, expr, local_assigns: dict[str, str]) -> str:
        if expr is None:
            return ""
        kind = cls._classify_return_expr(expr)
        if not kind and expr.type == "identifier":
            kind = local_assigns.get(_node_text(expr), "")
        return kind

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
        local_assigns = cls._collect_local_return_assigns(body)
        shape = {"mapping": False, "sequence": False, "constructed": False}
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type == "return_statement":
                expr = n.named_children[0] if n.named_children else None
                kind = cls._return_shape_from_statement(expr, local_assigns)
                if kind:
                    shape[kind] = True
                continue
            if cls._is_nested_callable_boundary(n):
                continue
            stack.extend(n.children)
        return shape

    @classmethod
    def _classify_identifier_call_return(cls, fn) -> str:
        name = _node_text(fn)
        if name in cls._MAPPING_CTOR_NAMES:
            return "mapping"
        if name in cls._SEQUENCE_CTOR_NAMES:
            return "sequence"
        if name and name[:1].isupper():
            return "constructed"
        return ""

    @classmethod
    def _classify_attribute_call_return(cls, fn) -> str:
        attr = fn.child_by_field_name("attribute")
        if attr is None or attr.type != "identifier":
            return ""
        name = _node_text(attr)
        if name and name[:1].isupper():
            return "constructed"
        return ""

    @classmethod
    def _classify_return_expr(cls, expr) -> str:
        """Return ``"mapping"`` / ``"sequence"`` / ``"constructed"`` / ``""``."""
        t = expr.type
        if t in ("dictionary", "dictionary_comprehension"):
            return "mapping"
        if t in ("list", "list_comprehension", "tuple", "set", "set_comprehension"):
            return "sequence"
        if t != "call":
            return ""
        fn = expr.child_by_field_name("function")
        if fn is None:
            return ""
        if fn.type == "identifier":
            return cls._classify_identifier_call_return(fn)
        if fn.type == "attribute":
            return cls._classify_attribute_call_return(fn)
        return ""

    def _direct_import_edges(self, body: str, file_path: str) -> list[ImportEdge]:
        imports: list[ImportEdge] = []
        for part in body.split(","):
            module = part.strip().split(" as ")[0].strip()
            if module and not self._is_external(module, file_path=file_path):
                imports.append(ImportEdge(file_path, module, "direct"))
        return imports

    def _from_import_edge(self, line: str, file_path: str) -> ImportEdge | None:
        match = line.split(" import ")
        if len(match) != 2:
            return None
        module = match[0][5:].strip()
        if not module or module == ".":
            return None
        if self._is_external(module.lstrip("."), file_path=file_path):
            return None
        return ImportEdge(file_path, module, "from_package")

    def _import_edges_from_line(self, stripped: str, file_path: str) -> list[ImportEdge]:
        if stripped.startswith("import "):
            return self._direct_import_edges(stripped[7:], file_path)
        if stripped.startswith("from "):
            edge = self._from_import_edge(stripped, file_path)
            return [edge] if edge is not None else []
        return []

    def extract_imports(self, source_code: str, file_path: str, *, tree=None) -> list[ImportEdge]:
        """Extract only intra-project import statements (skips stdlib and third-party).

        Imports are line-based regex; ``tree`` is unused but accepted for
        ``extract_all`` parity.
        """
        _ = tree
        imports: list[ImportEdge] = []
        for line in source_code.split("\n"):
            imports.extend(self._import_edges_from_line(line.strip(), file_path))
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
            if (resolved / top / _INIT_PY).exists() or (resolved / f"{top}.py").exists():
                return True
            src_root = resolved / "src"
            if (src_root / top / _INIT_PY).exists() or (src_root / f"{top}.py").exists():
                return True
        return False

    @staticmethod
    @lru_cache(maxsize=1)
    def _installed_top_level_packages() -> frozenset[str]:
        try:
            return frozenset(importlib.metadata.packages_distributions().keys())
        except Exception:
            return frozenset()

    def _inheritance_edges_for_class(
        self,
        node,
        *,
        file_path: str,
    ) -> list[InheritanceEdge]:
        args = node.child_by_field_name("superclasses")
        if args is None:
            args = next((c for c in node.children if c.type == "argument_list"), None)
        if args is None:
            return []
        subclass_uid = self._uid_for_node(node, file_path)
        edges: list[InheritanceEdge] = []
        for base_node in args.named_children:
            if base_node.type == "comment":
                continue
            base_name = self._inheritance_base_name(base_node)
            if not base_name:
                continue
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
            edges.extend(self._inheritance_edges_for_class(node, file_path=file_path))
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

    @staticmethod
    def _class_method_map(body) -> dict[str, object]:
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
        return methods

    def _proxy_return_methods(
        self,
        methods: dict[str, object],
        import_bindings: dict[str, str],
    ) -> dict[str, str]:
        returns_global: dict[str, str] = {}
        for mname, fn in methods.items():
            global_qn = self._method_returns_imported_global(fn, import_bindings)
            if global_qn:
                returns_global[mname] = global_qn
        return returns_global

    def _track_proxy_local_assignment(
        self,
        lname: str,
        method_name: str | None,
        returns_global: dict[str, str],
        local_src: dict[str, str],
        reassigned: set[str],
    ) -> bool:
        if method_name is None or method_name not in returns_global:
            return False
        if lname in local_src or lname in reassigned:
            local_src.pop(lname, None)
            reassigned.add(lname)
        else:
            local_src[lname] = method_name
        return True

    @staticmethod
    def _track_proxy_local_reassignment(
        lname: str,
        local_src: dict[str, str],
        reassigned: set[str],
    ) -> None:
        if lname in local_src:
            local_src.pop(lname, None)
        reassigned.add(lname)

    def _local_proxy_sources(
        self,
        fn_body,
        returns_global: dict[str, str],
    ) -> dict[str, str]:
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
            method_name = self._self_method_call_name(right)
            if self._track_proxy_local_assignment(
                lname, method_name, returns_global, local_src, reassigned
            ):
                continue
            self._track_proxy_local_reassignment(lname, local_src, reassigned)
        return local_src

    def _proxy_member_calls_from_method(
        self,
        fn,
        *,
        local_src: dict[str, str],
        returns_global: dict[str, str],
        caller_uid: str,
        file_path: str,
        seen_sites: set[tuple[str, int]],
        out: list[dict],
    ) -> None:
        fn_body = fn.child_by_field_name("body")
        if fn_body is None:
            return
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

    def _self_method_proxy_calls_in_class(
        self,
        cls,
        *,
        file_path: str,
        import_bindings: dict[str, str],
    ) -> list[dict]:
        body = cls.child_by_field_name("body")
        if body is None:
            return []
        methods = self._class_method_map(body)
        returns_global = self._proxy_return_methods(methods, import_bindings)
        if not returns_global:
            return []

        out: list[dict] = []
        seen_sites: set[tuple[str, int]] = set()
        for fn in methods.values():
            fn_body = cast(Any, fn).child_by_field_name("body")
            if fn_body is None:
                continue
            caller_uid = self._uid_for_node(fn, file_path)
            local_src = self._local_proxy_sources(fn_body, returns_global)
            if not local_src:
                continue
            self._proxy_member_calls_from_method(
                fn,
                local_src=local_src,
                returns_global=returns_global,
                caller_uid=caller_uid,
                file_path=file_path,
                seen_sites=seen_sites,
                out=out,
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
            out.extend(
                self._self_method_proxy_calls_in_class(
                    cls,
                    file_path=file_path,
                    import_bindings=import_bindings,
                )
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

    @staticmethod
    def _is_self_or_cls(obj) -> bool:
        return obj is not None and _node_text(obj) in ("self", "cls")

    def _import_global_aliases_in_method(
        self,
        body,
        import_bindings: dict[str, str],
    ) -> tuple[dict[str, str], dict[str, str]]:
        attr_alias: dict[str, str] = {}
        name_alias: dict[str, str] = {}
        for assign in self._iter_body_nodes(body):
            if assign.type != "assignment":
                continue
            left = assign.child_by_field_name("left")
            right = assign.child_by_field_name("right")
            if left is None or right is None or right.type != "identifier":
                continue
            global_qn = import_bindings.get(_node_text(right))
            if not global_qn:
                continue
            if left.type == "attribute":
                lo = left.child_by_field_name("object")
                la = left.child_by_field_name("attribute")
                if self._is_self_or_cls(lo) and la is not None:
                    attr_alias[_node_text(la)] = global_qn
            elif left.type == "identifier":
                name_alias[_node_text(left)] = global_qn
        return attr_alias, name_alias

    def _returned_import_global_from_return_expr(
        self,
        expr,
        import_bindings: dict[str, str],
        attr_alias: dict[str, str],
        name_alias: dict[str, str],
    ) -> str | None:
        if expr.type == "identifier":
            name = _node_text(expr)
            return import_bindings.get(name) or name_alias.get(name) or None
        if expr.type != "attribute":
            return None
        lo = expr.child_by_field_name("object")
        la = expr.child_by_field_name("attribute")
        if self._is_self_or_cls(lo) and la is not None:
            return attr_alias.get(_node_text(la)) or None
        return None

    def _returned_import_globals_from_body(
        self,
        body,
        import_bindings: dict[str, str],
        attr_alias: dict[str, str],
        name_alias: dict[str, str],
    ) -> set[str]:
        found: set[str] = set()
        for ret in self._iter_body_nodes(body):
            if ret.type != "return_statement":
                continue
            expr = ret.named_children[0] if ret.named_children else None
            if expr is None:
                continue
            global_qn = self._returned_import_global_from_return_expr(
                expr,
                import_bindings,
                attr_alias,
                name_alias,
            )
            if global_qn:
                found.add(global_qn)
        return found

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
        attr_alias, name_alias = self._import_global_aliases_in_method(body, import_bindings)
        found = self._returned_import_globals_from_body(
            body,
            import_bindings,
            attr_alias,
            name_alias,
        )
        return next(iter(found)) if len(found) == 1 else ""

    def _decorator_record(
        self,
        deco,
        *,
        decorated_uid: str,
        decorated_name: str,
        import_bindings: dict[str, str],
        module: str,
        file_path: str,
    ) -> dict | None:
        callable_name = self._decorator_callable_name(deco)
        base = callable_name.rsplit(".", 1)[-1] if callable_name else ""
        if not base or base in _BUILTIN_DECORATORS:
            return None
        owner_name = callable_name.rsplit(".", 1)[0] if "." in callable_name else ""
        resolved = self._resolve_dotted_name(callable_name or base, import_bindings, module)
        owner_resolved = (
            self._resolve_dotted_name(owner_name, import_bindings, module) if owner_name else ""
        )
        return {
            "decorated_uid": decorated_uid,
            "decorated_name": decorated_name,
            "decorator_name": base,
            "decorator_callable_name": callable_name,
            "decorator_qualified_name": resolved,
            "decorator_owner_name": owner_name,
            "decorator_owner_qualified_name": owner_resolved,
            "file_path": file_path,
        }

    def _decorator_records_for_definition(
        self,
        node,
        *,
        file_path: str,
        import_bindings: dict[str, str],
        module: str,
    ) -> list[dict]:
        defn = node.child_by_field_name("definition")
        if defn is None or defn.type not in ("function_definition", "class_definition"):
            return []
        name_node = defn.child_by_field_name("name")
        if name_node is None:
            return []
        decorated_uid = self._uid_for_node(defn, file_path)
        decorated_name = _node_text(name_node)
        records: list[dict] = []
        for deco in node.children:
            if deco.type != "decorator":
                continue
            record = self._decorator_record(
                deco,
                decorated_uid=decorated_uid,
                decorated_name=decorated_name,
                import_bindings=import_bindings,
                module=module,
                file_path=file_path,
            )
            if record is not None:
                records.append(record)
        return records

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
            out.extend(
                self._decorator_records_for_definition(
                    node,
                    file_path=file_path,
                    import_bindings=import_bindings,
                    module=module,
                )
            )
        return out

    def _emit_http_endpoint_from_decorator(
        self,
        deco,
        *,
        site_uid: str,
        non_http_decorators: frozenset[str],
        emit,
    ) -> None:
        from context_engine.indexer.http_endpoint import (
            HTTP_ROUTE_REGISTER_CALLEES,
            normalize_http_method,
        )

        callable_name = self._decorator_callable_name(deco)
        base = callable_name.rsplit(".", 1)[-1] if callable_name else ""
        if base in non_http_decorators:
            return
        if base not in HTTP_ROUTE_REGISTER_CALLEES and base != "api_route":
            return
        route_path, methods = self._http_route_from_decorator(deco)
        if not route_path:
            return
        via = f"@{callable_name or base}"
        if not methods:
            method = normalize_http_method(base if base != "route" else "get")
            if method:
                emit(site_uid, method, route_path, via)
            return
        for method in methods:
            emit(site_uid, method, route_path, via)

    def _http_endpoint_records_for_definition(
        self,
        node,
        *,
        file_path: str,
        non_http_decorators: frozenset[str],
        emit,
    ) -> None:
        defn = node.child_by_field_name("definition")
        if defn is None or defn.type != "function_definition":
            return
        site_uid = self._uid_for_node(defn, file_path)
        if not site_uid:
            return
        for deco in node.children:
            if deco.type != "decorator":
                continue
            self._emit_http_endpoint_from_decorator(
                deco,
                site_uid=site_uid,
                non_http_decorators=non_http_decorators,
                emit=emit,
            )

    def extract_http_endpoints(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """FastAPI/Flask-style route decorator facts for HTTP endpoint bridges."""
        from context_engine.indexer.http_endpoint import (
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
            self._http_endpoint_records_for_definition(
                node,
                file_path=file_path,
                non_http_decorators=_NON_HTTP_DECORATORS,
                emit=emit,
            )
        return out

    def _http_route_keyword_methods(self, key: str, value_node) -> list[str]:
        from context_engine.indexer.http_endpoint import normalize_http_method

        if key == "methods" and value_node.type == "list":
            methods: list[str] = []
            for item in value_node.named_children:
                if item.type != "string":
                    continue
                method = normalize_http_method(self._string_literal_text(item))
                if method:
                    methods.append(method)
            return methods
        if key == "method" and value_node.type == "string":
            method = normalize_http_method(self._string_literal_text(value_node))
            return [method] if method else []
        return []

    @staticmethod
    def _decorator_call_node(deco_node):
        for child in deco_node.children:
            if child.type == "call":
                return child
        return None

    def _http_route_positional_path(self, child, positional: int) -> tuple[str, int] | None:
        if child.type != "string" or positional != 0:
            return None
        raw = self._string_literal_text(child)
        if not raw.startswith("/"):
            return ("", 0)
        return raw, positional + 1

    def _http_route_keyword_methods_from_arg(self, child) -> list[str]:
        if child.type != "keyword_argument":
            return []
        key_node = child.child_by_field_name("name")
        value_node = child.child_by_field_name("value")
        if key_node is None or value_node is None:
            return []
        return self._http_route_keyword_methods(_node_text(key_node), value_node)

    def _http_route_apply_decorator_arg(
        self,
        child,
        *,
        route_path: str,
        positional: int,
    ) -> tuple[str, int, list[str]] | None:
        path_update = self._http_route_positional_path(child, positional)
        if path_update is not None:
            route_path, positional = path_update
            if not route_path and positional == 0:
                return None
            return route_path, positional, []
        if child.type == "keyword_argument":
            return route_path, positional, self._http_route_keyword_methods_from_arg(child)
        if child.type == "string":
            return route_path, positional + 1, []
        return route_path, positional, []

    def _http_route_from_decorator(self, deco_node) -> tuple[str, list[str]]:
        call_node = self._decorator_call_node(deco_node)
        if call_node is None:
            return "", []
        route_path = ""
        methods: list[str] = []
        arg_list = call_node.child_by_field_name("arguments")
        if arg_list is None:
            return route_path, methods
        positional = 0
        for child in arg_list.named_children:
            applied = self._http_route_apply_decorator_arg(
                child,
                route_path=route_path,
                positional=positional,
            )
            if applied is None:
                return "", []
            route_path, positional, arg_methods = applied
            methods.extend(arg_methods)
        return route_path, methods

    def _hook_call_base_name(self, fn_node) -> str:
        if fn_node.type == "identifier":
            return _node_text(fn_node)
        if fn_node.type == "attribute":
            tail = fn_node.child_by_field_name("attribute")
            return _node_text(tail) if tail is not None else ""
        return ""

    def _hook_name_from_call_args(self, call_node) -> str:
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

    def _hook_first_arg_name(self, call_node) -> str:
        arg_list = call_node.child_by_field_name("arguments")
        if arg_list is None:
            return ""
        for child in arg_list.named_children:
            if child.type == "identifier":
                return _node_text(child)
            if child.type == "attribute":
                leaf = child.child_by_field_name("attribute")
                return _node_text(leaf) if leaf is not None else ""
            break
        return ""

    def _hook_receiver_name(self, fn_attr) -> str:
        recv = fn_attr.child_by_field_name("object")
        if recv is None:
            return ""
        if recv.type == "identifier":
            return _node_text(recv)
        if recv.type == "attribute":
            leaf = recv.child_by_field_name("attribute")
            return _node_text(leaf) if leaf is not None else ""
        return ""

    def _emit_hook_fact(
        self,
        out: list[dict],
        seen: set[tuple[str, str, str, str, str]],
        *,
        site_uid: str,
        hook_name: str,
        kind: str,
        target_kind: str,
        via: str,
        file_path: str,
    ) -> None:
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

    def _hook_site_uid(
        self,
        node,
        file_path: str,
        *,
        decorated: bool,
    ) -> str:
        site_node = (
            self._hook_decorated_def(node) if decorated else None
        ) or self._enclosing_def_node(node)
        return self._uid_for_node(site_node, file_path) if site_node is not None else ""

    def _process_hook_register_call(
        self,
        node,
        *,
        base: str,
        file_path: str,
        out: list[dict],
        seen: set[tuple[str, str, str, str, str]],
    ) -> None:
        hook_name = self._hook_name_from_call_args(node)
        if not hook_name:
            return
        self._emit_hook_fact(
            out,
            seen,
            site_uid=self._hook_site_uid(node, file_path, decorated=True),
            hook_name=hook_name,
            kind="config",
            target_kind="method",
            via=base,
            file_path=file_path,
        )

    def _process_hook_receiver_call(
        self,
        node,
        *,
        file_path: str,
        out: list[dict],
        seen: set[tuple[str, str, str, str, str]],
    ) -> None:
        sig = self._hook_first_arg_name(node)
        if not sig.isidentifier():
            return
        site = self._hook_decorated_def(node)
        if site is None:
            return
        self._emit_hook_fact(
            out,
            seen,
            site_uid=self._uid_for_node(site, file_path),
            hook_name=sig,
            kind="config",
            target_kind="object",
            via="receiver",
            file_path=file_path,
        )

    def _process_hook_signal_connect_call(
        self,
        node,
        fn,
        *,
        base: str,
        file_path: str,
        out: list[dict],
        seen: set[tuple[str, str, str, str, str]],
    ) -> None:
        sig = self._hook_receiver_name(fn)
        if not sig.isidentifier():
            return
        kind = "config" if base == "connect" else "exec"
        via = "connect" if base == "connect" else "send"
        self._emit_hook_fact(
            out,
            seen,
            site_uid=self._hook_site_uid(node, file_path, decorated=True),
            hook_name=sig,
            kind=kind,
            target_kind="object",
            via=via,
            file_path=file_path,
        )

    def _process_hook_dispatch_call(
        self,
        node,
        fn,
        *,
        file_path: str,
        out: list[dict],
        seen: set[tuple[str, str, str, str, str]],
    ) -> None:
        obj = fn.child_by_field_name("object")
        tail = fn.child_by_field_name("attribute")
        if obj is None or tail is None or obj.type != "attribute" or tail.type != "identifier":
            return
        inner_attr = obj.child_by_field_name("attribute")
        if inner_attr is None or _node_text(inner_attr) != "dispatch":
            return
        hook_name = _node_text(tail)
        if not hook_name.isidentifier():
            return
        self._emit_hook_fact(
            out,
            seen,
            site_uid=self._hook_site_uid(node, file_path, decorated=False),
            hook_name=hook_name,
            kind="exec",
            target_kind="method",
            via="dispatch",
            file_path=file_path,
        )

    def _process_hook_call(
        self,
        node,
        *,
        file_path: str,
        register_names: frozenset[str],
        out: list[dict],
        seen: set[tuple[str, str, str, str, str]],
    ) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        base = self._hook_call_base_name(fn)

        if base in register_names:
            self._process_hook_register_call(
                node,
                base=base,
                file_path=file_path,
                out=out,
                seen=seen,
            )
            return

        if base == "receiver":
            self._process_hook_receiver_call(
                node,
                file_path=file_path,
                out=out,
                seen=seen,
            )
            return

        if base in ("connect", "send", "send_robust") and fn.type == "attribute":
            self._process_hook_signal_connect_call(
                node,
                fn,
                base=base,
                file_path=file_path,
                out=out,
                seen=seen,
            )
            return

        if fn.type != "attribute":
            return
        self._process_hook_dispatch_call(
            node,
            fn,
            file_path=file_path,
            out=out,
            seen=seen,
        )

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
        for node in self._iter_nodes(tree.root_node):
            if node.type != "call":
                continue
            self._process_hook_call(
                node,
                file_path=file_path,
                register_names=register_names,
                out=out,
                seen=seen,
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

    @staticmethod
    def _attr_access_receiver_type(
        obj_node,
        *,
        enclosing_class: str,
        cls_table: dict[str, str],
        local_types: dict[str, str],
    ) -> str:
        if obj_node.type == "identifier":
            name = _node_text(obj_node)
            if name == "self" and enclosing_class:
                return enclosing_class
            return local_types.get(name, "")
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
                return cls_table.get(_node_text(inner_attr), "")
        return ""

    @staticmethod
    def _attr_access_skip_read(node, parent) -> bool:
        if parent is not None and parent.type == "call":
            fn_node = parent.child_by_field_name("function")
            if fn_node is not None and fn_node.id == node.id:
                return True
        if parent is not None and parent.type == "assignment":
            lhs = parent.child_by_field_name("left")
            if lhs is not None and lhs.start_byte == node.start_byte:
                return True
        return False

    def _attr_access_reads_in_body(
        self,
        body,
        *,
        accessor_uid: str,
        accessor_name: str,
        enclosing_class: str,
        cls_table: dict[str, str],
        local_types: dict[str, str],
        emit,
    ) -> None:
        for node in self._iter_nodes(body):
            if node.type == "function_definition":
                continue
            if node.type != "attribute":
                continue
            obj = node.child_by_field_name("object")
            attr = node.child_by_field_name("attribute")
            if obj is None or attr is None or attr.type != "identifier":
                continue
            if self._attr_access_skip_read(node, node.parent):
                continue
            attr_name = _node_text(attr)
            receiver_qn = self._attr_access_receiver_type(
                obj,
                enclosing_class=enclosing_class,
                cls_table=cls_table,
                local_types=local_types,
            )
            attr_qn = f"{receiver_qn}.{attr_name}" if receiver_qn else ""
            emit(accessor_uid, accessor_name, attr_name, attr_qn, "read")

    def _attr_access_write_attribute_lhs(
        self,
        left,
        *,
        accessor_uid: str,
        accessor_name: str,
        enclosing_class: str,
        cls_table: dict[str, str],
        local_types: dict[str, str],
        emit,
    ) -> None:
        obj = left.child_by_field_name("object")
        attr = left.child_by_field_name("attribute")
        if obj is None or attr is None or attr.type != "identifier":
            return
        attr_name = _node_text(attr)
        receiver_qn = self._attr_access_receiver_type(
            obj,
            enclosing_class=enclosing_class,
            cls_table=cls_table,
            local_types=local_types,
        )
        attr_qn = f"{receiver_qn}.{attr_name}" if receiver_qn else ""
        emit(accessor_uid, accessor_name, attr_name, attr_qn, "write")

    def _attr_access_write_subscript_lhs(
        self,
        left,
        *,
        accessor_uid: str,
        accessor_name: str,
        enclosing_class: str,
        cls_table: dict[str, str],
        local_types: dict[str, str],
        emit,
    ) -> None:
        base = left.child_by_field_name("value")
        if base is None:
            return
        if base.type == "attribute":
            obj = base.child_by_field_name("object")
            attr = base.child_by_field_name("attribute")
            if obj is None or attr is None or attr.type != "identifier":
                return
            attr_name = _node_text(attr)
            receiver_qn = self._attr_access_receiver_type(
                obj,
                enclosing_class=enclosing_class,
                cls_table=cls_table,
                local_types=local_types,
            )
            attr_qn = f"{receiver_qn}.{attr_name}" if receiver_qn else ""
            emit(accessor_uid, accessor_name, attr_name, attr_qn, "write_subscript")
            return
        if base.type == "identifier":
            rname = _node_text(base)
            local_type = local_types.get(rname, "")
            if local_type:
                emit(accessor_uid, accessor_name, rname, local_type, "write_subscript_local")

    def _attr_access_writes_in_body(
        self,
        body,
        *,
        accessor_uid: str,
        accessor_name: str,
        enclosing_class: str,
        cls_table: dict[str, str],
        local_types: dict[str, str],
        emit,
    ) -> None:
        for node in self._iter_nodes(body):
            if node.type == "function_definition":
                continue
            if node.type != "assignment":
                continue
            left = node.child_by_field_name("left")
            if left is None:
                continue
            if left.type == "attribute":
                self._attr_access_write_attribute_lhs(
                    left,
                    accessor_uid=accessor_uid,
                    accessor_name=accessor_name,
                    enclosing_class=enclosing_class,
                    cls_table=cls_table,
                    local_types=local_types,
                    emit=emit,
                )
            elif left.type == "subscript":
                self._attr_access_write_subscript_lhs(
                    left,
                    accessor_uid=accessor_uid,
                    accessor_name=accessor_name,
                    enclosing_class=enclosing_class,
                    cls_table=cls_table,
                    local_types=local_types,
                    emit=emit,
                )

    def _attr_accesses_for_function(
        self,
        fn,
        *,
        file_path: str,
        import_bindings: dict[str, str],
        module: str,
        method_returns,
        function_returns,
        attr_type_table: dict[str, dict[str, str]],
        emit,
    ) -> None:
        name_node = fn.child_by_field_name("name")
        body = fn.child_by_field_name("body")
        if name_node is None or body is None:
            return
        accessor_uid = self._uid_for_node(fn, file_path)
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
        ctx = {
            "accessor_uid": accessor_uid,
            "accessor_name": accessor_name,
            "enclosing_class": enclosing_class,
            "cls_table": cls_table,
            "local_types": local_types,
            "emit": emit,
        }
        self._attr_access_reads_in_body(body, **ctx)
        self._attr_access_writes_in_body(body, **ctx)

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

        for fn in self._iter_nodes(tree.root_node):
            if fn.type != "function_definition":
                continue
            self._attr_accesses_for_function(
                fn,
                file_path=file_path,
                import_bindings=import_bindings,
                module=module,
                method_returns=method_returns,
                function_returns=function_returns,
                attr_type_table=attr_type_table,
                emit=emit,
            )
        return out

    def _type_ref_records_for_function(
        self,
        node,
        *,
        emit,
    ) -> None:
        params = node.child_by_field_name("parameters")
        if params is not None:
            for p in params.named_children:
                if p.type in ("typed_parameter", "typed_default_parameter"):
                    emit(node, p.child_by_field_name("type"), "param")
        emit(node, node.child_by_field_name("return_type"), "return")

    def _type_ref_records_for_call(
        self,
        node,
        *,
        emit,
    ) -> None:
        fn = node.child_by_field_name("function")
        if (
            fn is None
            or fn.type != "identifier"
            or _node_text(fn) not in ("isinstance", "issubclass")
        ):
            return
        args = node.child_by_field_name("arguments")
        referrer = self._enclosing_def_node(node)
        if args is None or referrer is None:
            return
        type_args = list(args.named_children)
        if len(type_args) >= 2:
            emit(referrer, type_args[1], "isinstance")

    def _type_ref_records_for_assignment(
        self,
        node,
        *,
        emit,
    ) -> None:
        typ = node.child_by_field_name("type")
        if typ is None:
            return
        referrer = self._enclosing_def_node(node)
        if referrer is not None:
            emit(referrer, typ, "annotation")

    def _emit_type_reference(
        self,
        out: list[dict],
        seen: set[tuple[str, str]],
        referrer_node,
        type_node,
        kind: str,
        *,
        file_path: str,
        import_bindings: dict[str, str],
        module: str,
    ) -> None:
        if referrer_node is None or type_node is None:
            return
        referrer_uid = self._uid_for_node(referrer_node, file_path)
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

    def _collect_type_references_from_tree(
        self,
        tree,
        emit,
    ) -> None:
        for node in self._iter_nodes(tree.root_node):
            if node.type == "function_definition":
                self._type_ref_records_for_function(node, emit=emit)
            elif node.type == "call":
                self._type_ref_records_for_call(node, emit=emit)
            elif node.type == "assignment":
                self._type_ref_records_for_assignment(node, emit=emit)

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
            self._emit_type_reference(
                out,
                seen,
                referrer_node,
                type_node,
                kind,
                file_path=file_path,
                import_bindings=import_bindings,
                module=module,
            )

        self._collect_type_references_from_tree(tree, emit)
        return out

    def _append_type_ref_attribute(
        self,
        n,
        *,
        import_bindings: dict[str, str],
        module: str,
        out: list[tuple[str, str]],
        seen_local: set[str],
    ) -> None:
        obj = n.child_by_field_name("object")
        attr = n.child_by_field_name("attribute")
        if attr is None or attr.type != "identifier":
            return
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

    def _append_type_ref_identifier(
        self,
        n,
        *,
        import_bindings: dict[str, str],
        module: str,
        out: list[tuple[str, str]],
        seen_local: set[str],
    ) -> None:
        name = _node_text(n)
        qn = self._resolve_type_name(name, import_bindings, module)
        if qn not in seen_local:
            seen_local.add(qn)
            out.append((name, qn))

    def _walk_type_ref_node(
        self,
        n,
        *,
        import_bindings: dict[str, str],
        module: str,
        out: list[tuple[str, str]],
        seen_local: set[str],
    ) -> None:
        if n.type == "attribute":
            self._append_type_ref_attribute(
                n,
                import_bindings=import_bindings,
                module=module,
                out=out,
                seen_local=seen_local,
            )
            return
        if n.type == "identifier":
            self._append_type_ref_identifier(
                n,
                import_bindings=import_bindings,
                module=module,
                out=out,
                seen_local=seen_local,
            )
            return
        for ch in n.children:
            self._walk_type_ref_node(
                ch,
                import_bindings=import_bindings,
                module=module,
                out=out,
                seen_local=seen_local,
            )

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
        self._walk_type_ref_node(
            type_node,
            import_bindings=import_bindings,
            module=module,
            out=out,
            seen_local=seen_local,
        )
        return out

    def _injection_record_for_provider(
        self,
        *,
        owner_uid: str,
        owner_name: str,
        provider: str,
        file_path: str,
        import_bindings: dict[str, str],
        module: str,
        seen: set[tuple[str, str]],
    ) -> dict | None:
        prov_qn = self._resolve_type_name(provider, import_bindings, module)
        key = (owner_uid, prov_qn)
        if key in seen:
            return None
        seen.add(key)
        return {
            "owner_uid": owner_uid,
            "owner_name": owner_name,
            "provider_name": provider,
            "provider_qualified_name": prov_qn,
            "file_path": file_path,
        }

    def _injection_records_for_default_parameter(
        self,
        param_node,
        *,
        owner_uid: str,
        owner_name: str,
        source_code: str,
        file_path: str,
        import_bindings: dict[str, str],
        module: str,
        seen: set[tuple[str, str]],
    ) -> list[dict]:
        records: list[dict] = []
        for call in self._iter_nodes(param_node):
            if call.type != "call":
                continue
            for prov in self._positional_identifier_arguments(call):
                record = self._injection_record_for_provider(
                    owner_uid=owner_uid,
                    owner_name=owner_name,
                    provider=prov,
                    file_path=file_path,
                    import_bindings=import_bindings,
                    module=module,
                    seen=seen,
                )
                if record is not None:
                    records.append(record)
        return records

    def _function_injection_owner(
        self,
        node,
        *,
        file_path: str,
    ) -> tuple[str, str] | None:
        params = node.child_by_field_name("parameters")
        if params is None:
            return None
        owner_uid = self._uid_for_node(node, file_path)
        owner_name_node = node.child_by_field_name("name")
        owner_name = _node_text(owner_name_node) if owner_name_node is not None else ""
        return owner_uid, owner_name

    @staticmethod
    def _default_parameter_nodes(params) -> list:
        return [
            p
            for p in params.named_children
            if p.type in ("default_parameter", "typed_default_parameter")
        ]

    def _injection_records_for_function(
        self,
        node,
        *,
        source_code: str,
        file_path: str,
        import_bindings: dict[str, str],
        module: str,
        seen: set[tuple[str, str]],
    ) -> list[dict]:
        owner = self._function_injection_owner(node, file_path=file_path)
        if owner is None:
            return []
        owner_uid, owner_name = owner
        params = node.child_by_field_name("parameters")
        records: list[dict] = []
        for p in self._default_parameter_nodes(params):
            records.extend(
                self._injection_records_for_default_parameter(
                    p,
                    owner_uid=owner_uid,
                    owner_name=owner_name,
                    source_code=source_code,
                    file_path=file_path,
                    import_bindings=import_bindings,
                    module=module,
                    seen=seen,
                )
            )
        return records

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
            out.extend(
                self._injection_records_for_function(
                    node,
                    source_code=source_code,
                    file_path=file_path,
                    import_bindings=import_bindings,
                    module=module,
                    seen=seen,
                )
            )
        return out

    @staticmethod
    def _unwrap_parenthesized_expr(node):
        while node is not None and node.type == "parenthesized_expression":
            inner = node.named_children[0] if node.named_children else None
            if inner is None:
                break
            node = inner
        return node

    def _add_class_typed_local(
        self,
        mapping: dict[str, list[tuple[str, str]]],
        name_node,
        type_node,
        *,
        import_bindings: dict[str, str],
        module: str,
    ) -> None:
        if name_node is None or type_node is None or name_node.type != "identifier":
            return
        classes = self._class_object_targets(type_node, import_bindings, module)
        if classes:
            mapping.setdefault(_node_text(name_node), []).extend(classes)

    def _class_typed_locals_map(
        self,
        func_node,
        *,
        import_bindings: dict[str, str],
        module: str,
        typed_local_cache: dict[int, dict[str, list[tuple[str, str]]]],
    ) -> dict[str, list[tuple[str, str]]]:
        if func_node is None:
            return {}
        cached = typed_local_cache.get(func_node.id)
        if cached is not None:
            return cached
        mapping: dict[str, list[tuple[str, str]]] = {}
        params = func_node.child_by_field_name("parameters")
        if params is not None:
            for p in params.named_children:
                if p.type == "typed_parameter":
                    ident = next((c for c in p.named_children if c.type == "identifier"), None)
                    self._add_class_typed_local(
                        mapping,
                        ident,
                        p.child_by_field_name("type"),
                        import_bindings=import_bindings,
                        module=module,
                    )
                elif p.type == "typed_default_parameter":
                    self._add_class_typed_local(
                        mapping,
                        p.child_by_field_name("name"),
                        p.child_by_field_name("type"),
                        import_bindings=import_bindings,
                        module=module,
                    )
        for n in self._iter_nodes(func_node):
            if n.type == "assignment" and n.child_by_field_name("type") is not None:
                self._add_class_typed_local(
                    mapping,
                    n.child_by_field_name("left"),
                    n.child_by_field_name("type"),
                    import_bindings=import_bindings,
                    module=module,
                )
        typed_local_cache[func_node.id] = mapping
        return mapping

    def _resolve_class_value_expr(
        self,
        node,
        mapping: dict[str, list[tuple[str, str]]],
        *,
        local_classes: dict,
        import_bindings: dict[str, str],
    ) -> list[tuple[str, str]]:
        node = self._unwrap_parenthesized_expr(node)
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
            return self._resolve_class_value_expr(
                node.child_by_field_name("left"),
                mapping,
                local_classes=local_classes,
                import_bindings=import_bindings,
            ) + self._resolve_class_value_expr(
                node.child_by_field_name("right"),
                mapping,
                local_classes=local_classes,
                import_bindings=import_bindings,
            )
        if node.type == "conditional_expression":
            kids = node.named_children
            if len(kids) >= 3:
                return self._resolve_class_value_expr(
                    kids[0],
                    mapping,
                    local_classes=local_classes,
                    import_bindings=import_bindings,
                ) + self._resolve_class_value_expr(
                    kids[2],
                    mapping,
                    local_classes=local_classes,
                    import_bindings=import_bindings,
                )
        return []

    @staticmethod
    def _merge_class_value_local(
        mapping: dict[str, list[tuple[str, str]]],
        name: str,
        classes: list[tuple[str, str]],
    ) -> bool:
        if not classes:
            return False
        bucket = mapping.setdefault(name, [])
        changed = False
        for item in classes:
            if item not in bucket:
                bucket.append(item)
                changed = True
        return changed

    def _collect_identifier_assignments(self, func_node) -> list[tuple[str, object]]:
        assignments: list[tuple[str, object]] = []
        for n in self._iter_nodes(func_node):
            if n.type != "assignment":
                continue
            lhs = n.child_by_field_name("left")
            rhs = n.child_by_field_name("right")
            if lhs is None or rhs is None or lhs.type != "identifier":
                continue
            assignments.append((_node_text(lhs), rhs))
        return assignments

    def _propagate_class_value_locals(
        self,
        mapping: dict[str, list[tuple[str, str]]],
        assignments: list[tuple[str, object]],
        *,
        local_classes: dict,
        import_bindings: dict[str, str],
    ) -> None:
        for _ in range(len(assignments) + 1):
            changed = False
            for name, rhs in assignments:
                classes = self._resolve_class_value_expr(
                    rhs,
                    mapping,
                    local_classes=local_classes,
                    import_bindings=import_bindings,
                )
                if self._merge_class_value_local(mapping, name, classes):
                    changed = True
            if not changed:
                break

    def _initial_class_value_locals_mapping(
        self,
        func_node,
        *,
        import_bindings: dict[str, str],
        module: str,
        typed_local_cache: dict[int, dict[str, list[tuple[str, str]]]],
    ) -> dict[str, list[tuple[str, str]]]:
        return {
            k: list(v)
            for k, v in self._class_typed_locals_map(
                func_node,
                import_bindings=import_bindings,
                module=module,
                typed_local_cache=typed_local_cache,
            ).items()
        }

    def _class_value_locals_map(
        self,
        func_node,
        *,
        import_bindings: dict[str, str],
        module: str,
        local_classes: dict,
        typed_local_cache: dict[int, dict[str, list[tuple[str, str]]]],
        value_local_cache: dict[int, dict[str, list[tuple[str, str]]]],
    ) -> dict[str, list[tuple[str, str]]]:
        if func_node is None:
            return {}
        cached = value_local_cache.get(func_node.id)
        if cached is not None:
            return cached
        mapping = self._initial_class_value_locals_mapping(
            func_node,
            import_bindings=import_bindings,
            module=module,
            typed_local_cache=typed_local_cache,
        )
        assignments = self._collect_identifier_assignments(func_node)
        self._propagate_class_value_locals(
            mapping,
            assignments,
            local_classes=local_classes,
            import_bindings=import_bindings,
        )
        value_local_cache[func_node.id] = mapping
        return mapping

    def _emit_instantiation_record(
        self,
        out: list[dict],
        seen: set[tuple[str, str, str]],
        *,
        call_node,
        caller_node,
        type_name: str,
        type_qn: str,
        is_external: bool,
        file_path: str,
        module: str,
        module_uid: str,
    ) -> None:
        if not type_qn:
            return
        if caller_node is None:
            var_uid = self._module_assignment_variable_uid(call_node, module)
            caller_uid = var_uid if var_uid is not None else module_uid
        else:
            caller_uid = self._uid_for_node(caller_node, file_path)
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

    def _instantiation_from_call_node(
        self,
        node,
        *,
        source_code: str,
        file_path: str,
        module: str,
        import_bindings: dict[str, str],
        local_class_names: set[str],
        local_classes: dict,
        module_uid: str,
        typed_local_cache: dict[int, dict[str, list[tuple[str, str]]]],
        value_local_cache: dict[int, dict[str, list[tuple[str, str]]]],
        out: list[dict],
        seen: set[tuple[str, str, str]],
    ) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        caller = self._enclosing_def_node(node)
        locals_map = (
            self._class_value_locals_map(
                caller,
                import_bindings=import_bindings,
                module=module,
                local_classes=local_classes,
                typed_local_cache=typed_local_cache,
                value_local_cache=value_local_cache,
            )
            if caller is not None
            else {}
        )
        emit_kwargs = {
            "file_path": file_path,
            "module": module,
            "module_uid": module_uid,
        }
        if fn.type == "identifier":
            name = _node_text(fn)
            if name in locals_map:
                for cname, cqn in locals_map[name]:
                    is_external = not cqn.startswith(f"{module}.")
                    self._emit_instantiation_record(
                        out,
                        seen,
                        call_node=node,
                        caller_node=caller,
                        type_name=cname,
                        type_qn=cqn,
                        is_external=is_external,
                        **emit_kwargs,
                    )
                return
        resolved = self._resolve_construction_callee(
            fn,
            import_bindings=import_bindings,
            local_classes=local_class_names,
            module=module,
        )
        if resolved is None:
            return
        type_name, type_qn, is_external = resolved
        self._emit_instantiation_record(
            out,
            seen,
            call_node=node,
            caller_node=caller,
            type_name=type_name,
            type_qn=type_qn,
            is_external=is_external,
            **emit_kwargs,
        )

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

        for node in self._iter_nodes(tree.root_node):
            if node.type != "call":
                continue
            self._instantiation_from_call_node(
                node,
                source_code=source_code,
                file_path=file_path,
                module=module,
                import_bindings=import_bindings,
                local_class_names=local_class_names,
                local_classes=local_classes,
                module_uid=module_uid,
                typed_local_cache=typed_local_cache,
                value_local_cache=value_local_cache,
                out=out,
                seen=seen,
            )
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

    def _class_object_targets_from_subscript(
        self,
        n,
        *,
        import_bindings: dict[str, str],
        module: str,
    ) -> list[tuple[str, str]]:
        value = n.child_by_field_name("value")
        if value is None or value.type != "identifier" or _node_text(value) not in ("type", "Type"):
            return []
        out: list[tuple[str, str]] = []
        for sub in n.named_children:
            if sub.id == value.id:
                continue
            out.extend(self._type_ref_targets(sub, import_bindings, module))
        return out

    def _class_object_targets_from_generic_type(
        self,
        n,
        *,
        import_bindings: dict[str, str],
        module: str,
    ) -> list[tuple[str, str]]:
        head = n.named_children[0] if n.named_children else None
        if head is None or head.type != "identifier" or _node_text(head) not in ("type", "Type"):
            return []
        out: list[tuple[str, str]] = []
        for sub in n.named_children[1:]:
            out.extend(self._type_ref_targets(sub, import_bindings, module))
        return out

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
                out.extend(
                    self._class_object_targets_from_subscript(
                        n,
                        import_bindings=import_bindings,
                        module=module,
                    )
                )
                continue
            if n.type == "generic_type":
                out.extend(
                    self._class_object_targets_from_generic_type(
                        n,
                        import_bindings=import_bindings,
                        module=module,
                    )
                )
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
    def _inheritance_base_path_wrapped(base_node) -> str | None:
        if base_node.type == "subscript":
            value = base_node.child_by_field_name("value")
            if value is None:
                value = base_node.named_children[0] if base_node.named_children else None
            return PythonAdapter._inheritance_base_path(value)
        if base_node.type == "call":
            fn = base_node.child_by_field_name("function")
            return PythonAdapter._inheritance_base_path(fn)
        return None

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
        wrapped = PythonAdapter._inheritance_base_path_wrapped(base_node)
        if wrapped is not None:
            return wrapped
        return ""

    def _positional_identifier_arguments(self, call_node, *, limit: int = 8) -> list[str]:
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

    def _py_call_from_identifier(
        self,
        *,
        call_name: str,
        import_bindings: dict[str, str],
        by_name: dict[str, list],
    ) -> tuple[str, str, float, str | None, str | None]:
        rel_type = self._classify_direct_call(call_name)
        tier = "direct" if rel_type == "CALLS_DIRECT" else "guess"
        confidence = 1.0 if rel_type == "CALLS_DIRECT" else 0.4
        callee_uid = None
        callee_qualified_name = None

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
        return rel_type, tier, confidence, callee_uid, callee_qualified_name

    def _py_call_from_attribute(
        self,
        func_node,
        parent,
        *,
        import_bindings: dict[str, str],
        by_name: dict[str, list],
        attr_type_table,
        alias_cache: dict[int, dict[str, str]],
        method_returns,
        function_returns,
        module: str,
    ) -> tuple[str, str, float, str, str | None, str | None] | None:
        obj_node = func_node.child_by_field_name("object")
        method_node = func_node.child_by_field_name("attribute")
        if obj_node is None or method_node is None or method_node.type != "identifier":
            return None
        call_name = _node_text(method_node)
        rel_type = "CALLS_DYNAMIC"
        tier = "dynamic"
        confidence = 0.7
        callee_uid = None
        callee_qualified_name = None

        if obj_node.type == "identifier":
            receiver_text = _node_text(obj_node)
            if receiver_text == "self":
                callee_uid = self._resolve_method_uid(parent, call_name, by_name)
            elif receiver_text in import_bindings:
                callee_qualified_name = f"{import_bindings[receiver_text]}.{call_name}"
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
                return None
            tier = "typed"
            confidence = 0.8
            callee_qualified_name = typed
        else:
            return None
        return rel_type, tier, confidence, call_name, callee_uid, callee_qualified_name

    def _append_py_call_record(
        self,
        calls: list[dict],
        *,
        node,
        caller_uid: str,
        call_name: str,
        rel_type: str,
        tier: str,
        confidence: float,
        callee_uid: str | None,
        callee_qualified_name: str | None,
    ) -> None:
        if callee_uid == caller_uid:
            return
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
        pos_args = self._positional_identifier_arguments(node)
        if pos_args:
            call["arguments"] = pos_args
        calls.append(call)

    def _py_enclosing_call_parent(self, node):
        parent = node.parent
        while parent and parent.type not in self.parent_types:
            parent = parent.parent
        return parent

    def _py_call_from_capture(
        self,
        node,
        *,
        file_path: str,
        import_bindings: dict[str, str],
        by_name: dict[str, list],
        attr_type_table,
        alias_cache: dict[int, dict[str, str]],
        method_returns,
        function_returns,
        module: str,
    ) -> tuple[str, str, str, str, float, str | None, str | None] | None:
        func_node = node.child_by_field_name("function")
        if func_node is None:
            return None

        parent = self._py_enclosing_call_parent(node)
        if parent is None:
            return None

        caller_uid = self._uid_for_node(parent, file_path)
        if func_node.type == "identifier":
            call_name = _node_text(func_node)
            rel_type, tier, confidence, callee_uid, callee_qualified_name = (
                self._py_call_from_identifier(
                    call_name=call_name,
                    import_bindings=import_bindings,
                    by_name=by_name,
                )
            )
        elif func_node.type == "attribute":
            resolved = self._py_call_from_attribute(
                func_node,
                parent,
                import_bindings=import_bindings,
                by_name=by_name,
                attr_type_table=attr_type_table,
                alias_cache=alias_cache,
                method_returns=method_returns,
                function_returns=function_returns,
                module=module,
            )
            if resolved is None:
                return None
            rel_type, tier, confidence, call_name, callee_uid, callee_qualified_name = resolved
        else:
            return None

        return (
            caller_uid,
            call_name,
            rel_type,
            tier,
            confidence,
            callee_uid,
            callee_qualified_name,
        )

    def _py_call_query_captures(self, tree) -> list[tuple[object, str]]:
        return self._flatten_ts_query_captures(self.call_query, tree.root_node)

    def _py_call_resolution_context(
        self,
        source_code: str,
        file_path: str,
        *,
        tree,
    ) -> tuple[
        dict[str, list],
        dict[str, str],
        str,
        dict,
        dict,
        dict[str, dict[str, str]],
    ]:
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
        return by_name, import_bindings, module, method_returns, function_returns, attr_type_table

    def _append_resolved_py_calls(
        self,
        calls: list[dict],
        captures: list[tuple[object, str]],
        *,
        file_path: str,
        by_name: dict[str, list],
        import_bindings: dict[str, str],
        module: str,
        method_returns,
        function_returns,
        attr_type_table,
        alias_cache: dict[int, dict[str, str]],
    ) -> None:
        for node, tag in captures:
            if tag != "call":
                continue
            resolved = self._py_call_from_capture(
                node,
                file_path=file_path,
                import_bindings=import_bindings,
                by_name=by_name,
                attr_type_table=attr_type_table,
                alias_cache=alias_cache,
                method_returns=method_returns,
                function_returns=function_returns,
                module=module,
            )
            if resolved is None:
                continue
            caller_uid, call_name, rel_type, tier, confidence, callee_uid, callee_qualified_name = (
                resolved
            )
            self._append_py_call_record(
                calls,
                node=node,
                caller_uid=caller_uid,
                call_name=call_name,
                rel_type=rel_type,
                tier=tier,
                confidence=confidence,
                callee_uid=callee_uid,
                callee_qualified_name=callee_qualified_name,
            )

    def extract_calls_from_source(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[dict]:
        """Extract function calls and attach resolver metadata when statically resolvable."""
        if tree is None:
            tree = self._parse(source_code)

        captures = self._py_call_query_captures(tree)
        (
            by_name,
            import_bindings,
            module,
            method_returns,
            function_returns,
            attr_type_table,
        ) = self._py_call_resolution_context(source_code, file_path, tree=tree)
        alias_cache: dict[int, dict[str, str]] = {}

        calls: list[dict] = []
        self._append_resolved_py_calls(
            calls,
            captures,
            file_path=file_path,
            by_name=by_name,
            import_bindings=import_bindings,
            module=module,
            method_returns=method_returns,
            function_returns=function_returns,
            attr_type_table=attr_type_table,
            alias_cache=alias_cache,
        )

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

    def _uid_for_node(self, node, file_path: str) -> str:
        qualified_name = qualified_name_for(node, file_path)
        raw_signature, _ = signature_from_node(node, self.language_name)
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

    def _call_result_type_from_identifier(
        self,
        callee,
        *,
        import_bindings: dict[str, str],
        module: str,
        function_returns: dict[str, str],
        allow_bare_constructor: bool,
    ) -> str:
        name = _node_text(callee)
        inferred = function_returns.get(name)
        if inferred:
            return inferred
        if allow_bare_constructor or name[:1].isupper():
            return self._resolve_type_name(name, import_bindings, module)
        return ""

    def _call_result_type_from_attribute(
        self,
        callee,
        *,
        enclosing_class: str,
        import_bindings: dict[str, str],
        method_returns: dict[tuple[str, str], str],
    ) -> str:
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
            return self._call_result_type_from_identifier(
                callee,
                import_bindings=import_bindings,
                module=module,
                function_returns=function_returns,
                allow_bare_constructor=allow_bare_constructor,
            )
        if callee.type == "attribute":
            return self._call_result_type_from_attribute(
                callee,
                enclosing_class=enclosing_class,
                import_bindings=import_bindings,
                method_returns=method_returns,
            )
        return ""

    def _collect_local_value_assignments(
        self,
        func_node,
        mapping: dict[str, str],
        *,
        import_bindings: dict[str, str],
        module: str,
    ) -> list[tuple[str, object]]:
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
        return assignments

    def _resolve_local_value_expr_type(
        self,
        expr,
        mapping: dict[str, str],
        *,
        cls_table: dict[str, str],
        enclosing_class: str,
        import_bindings: dict[str, str],
        module: str,
        method_returns: dict[tuple[str, str], str] | None,
        function_returns: dict[str, str] | None,
    ) -> str:
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

    def _propagate_local_value_types(
        self,
        mapping: dict[str, str],
        assignments: list[tuple[str, object]],
        *,
        cls_table: dict[str, str],
        enclosing_class: str,
        import_bindings: dict[str, str],
        module: str,
        method_returns: dict[tuple[str, str], str] | None,
        function_returns: dict[str, str] | None,
    ) -> None:
        for _ in range(len(assignments) + 1):
            changed = False
            for name, assign in assignments:
                inferred = self._resolve_local_value_expr_type(
                    cast(Any, assign).child_by_field_name("right"),
                    mapping,
                    cls_table=cls_table,
                    enclosing_class=enclosing_class,
                    import_bindings=import_bindings,
                    module=module,
                    method_returns=method_returns,
                    function_returns=function_returns,
                )
                if inferred and name not in mapping:
                    mapping[name] = inferred
                    changed = True
            if not changed:
                break

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
        assignments = self._collect_local_value_assignments(
            func_node,
            mapping,
            import_bindings=import_bindings,
            module=module,
        )
        self._propagate_local_value_types(
            mapping,
            assignments,
            cls_table=cls_table,
            enclosing_class=enclosing_class,
            import_bindings=import_bindings,
            module=module,
            method_returns=method_returns,
            function_returns=function_returns,
        )
        return mapping

    def _class_body_attr_from_cls_string(
        self,
        left,
        right,
    ) -> tuple[str, str] | None:
        lname = _node_text(left)
        if not lname.endswith("_cls") or right is None or right.type != "string":
            return None
        literal = self._string_literal_text(right)
        if ":" not in literal:
            return None
        return lname[:-4], literal.replace(":", ".")

    def _class_body_attr_from_annotation(
        self,
        left,
        typ,
        *,
        import_bindings: dict[str, str],
        module: str,
    ) -> tuple[str, str] | None:
        if typ is None:
            return None
        type_ident = self._type_identifier(typ)
        if not type_ident:
            return None
        return _node_text(left), self._resolve_type_name(type_ident, import_bindings, module)

    @staticmethod
    def _iter_class_body_assignments(body):
        for stmt in body.children:
            if stmt.type != "expression_statement":
                continue
            for assign in stmt.children:
                if assign.type == "assignment":
                    yield assign

    def _parse_class_body_assignment(
        self,
        assign,
        *,
        import_bindings: dict[str, str],
        module: str,
    ) -> tuple[str, str] | None:
        left = assign.child_by_field_name("left")
        right = assign.child_by_field_name("right")
        typ = assign.child_by_field_name("type")
        if left is None or left.type != "identifier":
            return None
        parsed = self._class_body_attr_from_cls_string(left, right)
        if parsed is None:
            parsed = self._class_body_attr_from_annotation(
                left,
                typ,
                import_bindings=import_bindings,
                module=module,
            )
        return parsed

    def _class_body_level_attrs(
        self,
        body,
        *,
        import_bindings: dict[str, str],
        module: str,
    ) -> dict[str, str]:
        attrs: dict[str, str] = {}
        for assign in self._iter_class_body_assignments(body):
            parsed = self._parse_class_body_assignment(
                assign,
                import_bindings=import_bindings,
                module=module,
            )
            if parsed is not None:
                attrs.setdefault(parsed[0], parsed[1])
        return attrs

    def _self_attr_type_from_method_assignment(
        self,
        assign,
        *,
        cname: str,
        local_types: dict[str, str],
        import_bindings: dict[str, str],
        module: str,
        method_returns: dict[tuple[str, str], str],
        function_returns: dict[str, str],
        method_name: str,
    ) -> str | None:
        left = assign.child_by_field_name("left")
        right = assign.child_by_field_name("right")
        typ = assign.child_by_field_name("type")
        if left is None or left.type != "attribute":
            return None
        obj = left.child_by_field_name("object")
        attr = left.child_by_field_name("attribute")
        if obj is None or _node_text(obj) != "self" or attr is None:
            return None
        if typ is not None:
            targets = self._type_ref_targets(typ, import_bindings, module)
            if targets:
                return targets[0][1]
        if right is not None and right.type == "identifier":
            rname = _node_text(right)
            if rname in local_types:
                return local_types[rname]
        if right is None or right.type != "call":
            return None
        callee = right.child_by_field_name("function")
        return (
            self._call_result_type(
                callee,
                enclosing_class=cname,
                import_bindings=import_bindings,
                module=module,
                method_returns=method_returns,
                function_returns=function_returns,
                allow_bare_constructor=method_name == "__init__",
            )
            or None
        )

    @staticmethod
    def _self_attr_name_from_assignment(assign) -> str | None:
        left = assign.child_by_field_name("left")
        if left is None or left.type != "attribute":
            return None
        obj = left.child_by_field_name("object")
        attr = left.child_by_field_name("attribute")
        if obj is None or _node_text(obj) != "self" or attr is None:
            return None
        return _node_text(attr)

    def _infer_instance_attrs_from_method(
        self,
        fn,
        *,
        cname: str,
        attrs: dict[str, str],
        import_bindings: dict[str, str],
        module: str,
        method_returns: dict[tuple[str, str], str],
        function_returns: dict[str, str],
    ) -> None:
        fn_name = fn.child_by_field_name("name")
        if fn_name is None:
            return
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
            aname = self._self_attr_name_from_assignment(assign)
            if aname is None:
                continue
            inferred = self._self_attr_type_from_method_assignment(
                assign,
                cname=cname,
                local_types=local_types,
                import_bindings=import_bindings,
                module=module,
                method_returns=method_returns,
                function_returns=function_returns,
                method_name=method_name,
            )
            if inferred:
                attrs.setdefault(aname, inferred)

    @staticmethod
    def _class_method_definitions(body):
        for fn in body.children:
            if fn.type == "function_definition":
                yield fn

    def _infer_instance_attrs_from_methods(
        self,
        body,
        *,
        cname: str,
        attrs: dict[str, str],
        import_bindings: dict[str, str],
        module: str,
        method_returns: dict[tuple[str, str], str],
        function_returns: dict[str, str],
    ) -> None:
        for fn in self._class_method_definitions(body):
            self._infer_instance_attrs_from_method(
                fn,
                cname=cname,
                attrs=attrs,
                import_bindings=import_bindings,
                module=module,
                method_returns=method_returns,
                function_returns=function_returns,
            )

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
            attrs = self._class_body_level_attrs(
                body,
                import_bindings=import_bindings,
                module=module,
            )
            self._infer_instance_attrs_from_methods(
                body,
                cname=cname,
                attrs=attrs,
                import_bindings=import_bindings,
                module=module,
                method_returns=method_returns,
                function_returns=function_returns,
            )
            if attrs:
                table.setdefault(cname, {}).update(attrs)
        return table

    def _return_type_from_annotation(
        self,
        fn_node,
        *,
        import_bindings: dict[str, str],
        module: str,
    ) -> str:
        ret = fn_node.child_by_field_name("return_type")
        if ret is None:
            return ""
        ident = self._type_identifier(ret)
        if not ident:
            return ""
        return self._resolve_type_name(ident, import_bindings, module)

    def _return_type_from_constructor_return(
        self,
        fn_node,
        *,
        import_bindings: dict[str, str],
        module: str,
    ) -> str:
        body = fn_node.child_by_field_name("body")
        if body is None:
            return ""
        for node in self._iter_nodes(body):
            if node.type != "return_statement":
                continue
            expr = node.named_children[0] if node.named_children else None
            if expr is None or expr.type != "call":
                continue
            callee = expr.child_by_field_name("function")
            if callee is None or callee.type != "identifier":
                continue
            name = _node_text(callee)
            if name[:1].isupper():
                return self._resolve_type_name(name, import_bindings, module)
        return ""

    def _inferred_function_return_type(
        self,
        fn_node,
        *,
        import_bindings: dict[str, str],
        module: str,
    ) -> str:
        annotated = self._return_type_from_annotation(
            fn_node,
            import_bindings=import_bindings,
            module=module,
        )
        if annotated:
            return annotated
        return self._return_type_from_constructor_return(
            fn_node,
            import_bindings=import_bindings,
            module=module,
        )

    def _record_inferred_method_return(
        self,
        cname: str,
        fn,
        *,
        import_bindings: dict[str, str],
        module: str,
        method_returns: dict[tuple[str, str], str],
    ) -> None:
        fname_node = fn.child_by_field_name("name")
        if fname_node is None:
            return
        rtype = self._inferred_function_return_type(
            fn,
            import_bindings=import_bindings,
            module=module,
        )
        if rtype:
            method_returns.setdefault((cname, _node_text(fname_node)), rtype)

    def _collect_method_returns_for_class(
        self,
        cls,
        *,
        import_bindings: dict[str, str],
        module: str,
        method_returns: dict[tuple[str, str], str],
    ) -> None:
        cname_node = cls.child_by_field_name("name")
        body = cls.child_by_field_name("body")
        if cname_node is None or body is None:
            return
        cname = _node_text(cname_node)
        for fn in self._class_method_definitions(body):
            self._record_inferred_method_return(
                cname,
                fn,
                import_bindings=import_bindings,
                module=module,
                method_returns=method_returns,
            )

    @staticmethod
    def _iter_class_definitions(root):
        for node in PythonAdapter._iter_nodes(root):
            if node.type == "class_definition":
                yield node

    def _collect_class_method_returns(
        self,
        tree,
        *,
        import_bindings: dict[str, str],
        module: str,
        method_returns: dict[tuple[str, str], str],
    ) -> None:
        for cls in self._iter_class_definitions(tree.root_node):
            self._collect_method_returns_for_class(
                cls,
                import_bindings=import_bindings,
                module=module,
                method_returns=method_returns,
            )

    def _collect_module_function_returns(
        self,
        tree,
        *,
        import_bindings: dict[str, str],
        module: str,
        function_returns: dict[str, str],
    ) -> None:
        for fn in self._iter_nodes(tree.root_node):
            if fn.type != "function_definition":
                continue
            fname_node = fn.child_by_field_name("name")
            if fname_node is None:
                continue
            rtype = self._inferred_function_return_type(
                fn,
                import_bindings=import_bindings,
                module=module,
            )
            if rtype:
                function_returns.setdefault(_node_text(fname_node), rtype)

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
        self._collect_class_method_returns(
            tree,
            import_bindings=import_bindings,
            module=module,
            method_returns=method_returns,
        )
        self._collect_module_function_returns(
            tree,
            import_bindings=import_bindings,
            module=module,
            function_returns=function_returns,
        )
        return method_returns, function_returns

    @staticmethod
    def _collect_function_nodes_and_aliases(tree) -> tuple[dict[str, object], dict[str, str]]:
        func_nodes: dict[str, object] = {}
        func_aliases: dict[str, str] = {}
        for node in PythonAdapter._iter_nodes(tree.root_node):
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
        return func_nodes, func_aliases

    def _annotated_proxy_binding(
        self,
        *,
        right,
        typ,
        import_bindings: dict[str, str],
        module: str,
        context_var_types: dict[str, str],
        source_code: str,
    ) -> dict | None:
        type_ident = self._type_identifier(typ)
        if not type_ident:
            return None
        context_binding = self._proxy_context_binding(
            right,
            context_var_types,
            source_code,
        )
        return {
            "target_type": self._resolve_type_name(type_ident, import_bindings, module),
            "target_source": "annotation",
            "wrapped_callable": "",
            "confidence": 1.0,
            **context_binding,
        }

    def _wrapped_callable_proxy_binding(
        self,
        *,
        right,
        func_nodes: dict[str, object],
        func_aliases: dict[str, str],
        source_code: str,
        import_bindings: dict[str, str],
        module: str,
    ) -> dict | None:
        wrapped = self._first_positional_identifier(right)
        if not wrapped:
            return None
        resolved_wrapped = func_aliases.get(wrapped, wrapped)
        fn = func_nodes.get(resolved_wrapped)
        if fn is None:
            return None
        target_qn = self._constructed_imported_class(fn, import_bindings, module)
        if not target_qn:
            return None
        return {
            "target_type": target_qn,
            "target_source": "wrapped_callable",
            "wrapped_callable": f"{module}.{resolved_wrapped}",
            "confidence": 0.65,
        }

    def _proxy_binding_from_assignment(
        self,
        stmt,
        *,
        import_bindings: dict[str, str],
        module: str,
        context_var_types: dict[str, str],
        source_code: str,
        func_nodes: dict[str, object],
        func_aliases: dict[str, str],
    ) -> tuple[str, dict] | None:
        left = stmt.child_by_field_name("left")
        right = stmt.child_by_field_name("right")
        typ = stmt.child_by_field_name("type")
        if left is None or left.type != "identifier" or right is None or right.type != "call":
            return None
        callee = right.child_by_field_name("function")
        if callee is None or callee.type != "identifier":
            return None
        if not _node_text(callee).endswith("Proxy"):
            return None
        var_name = _node_text(left)
        if typ is not None:
            record = self._annotated_proxy_binding(
                right=right,
                typ=typ,
                import_bindings=import_bindings,
                module=module,
                context_var_types=context_var_types,
                source_code=source_code,
            )
            return (var_name, record) if record is not None else None
        record = self._wrapped_callable_proxy_binding(
            right=right,
            func_nodes=func_nodes,
            func_aliases=func_aliases,
            source_code=source_code,
            import_bindings=import_bindings,
            module=module,
        )
        return (var_name, record) if record is not None else None

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
            import_bindings,
            module,
        )
        func_nodes, func_aliases = self._collect_function_nodes_and_aliases(tree)

        table: dict[str, dict] = {}
        for stmt in self._iter_nodes(tree.root_node):
            if stmt.type != "assignment":
                continue
            binding = self._proxy_binding_from_assignment(
                stmt,
                import_bindings=import_bindings,
                module=module,
                context_var_types=context_var_types,
                source_code=source_code,
                func_nodes=func_nodes,
                func_aliases=func_aliases,
            )
            if binding is None:
                continue
            var_name, record = binding
            table[var_name] = record
        return table

    def _build_context_var_type_table(
        self,
        tree,
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
    def _context_var_payload_from_generic_type(node) -> str:
        named = [child for child in node.children if child.is_named]
        if len(named) < 2:
            return ""
        base = named[0]
        if base.type != "identifier" or _node_text(base) != "ContextVar":
            return ""
        payload = next(
            (
                child
                for child in named[1:]
                if child.type in {"type", "type_parameter", "identifier"}
            ),
            None,
        )
        if payload is None:
            return ""
        if payload.type == "identifier":
            return _node_text(payload)
        for child in PythonAdapter._iter_nodes(payload):
            if child.type == "identifier":
                return _node_text(child)
        return ""

    @staticmethod
    def _context_var_payload_type_identifier(type_node) -> str:
        """Return ``T`` for ``ContextVar[T]`` annotations, else ``''``."""
        for node in PythonAdapter._iter_nodes(type_node):
            if node.type != "generic_type":
                continue
            payload = PythonAdapter._context_var_payload_from_generic_type(node)
            if payload:
                return payload
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

    def _function_local_import_bindings(
        self,
        func_node,
        import_bindings: dict[str, str],
        module: str,
    ) -> dict[str, str]:
        bindings = dict(import_bindings)
        package = module.rsplit(".", 1)[0] if "." in module else ""
        for node in self._iter_nodes(func_node):
            if node.type not in ("import_from_statement",):
                continue
            text = _node_text(node).strip()
            from_parts = split_python_from_import(text)
            if not from_parts:
                continue
            import_module, names = from_parts
            target_module = self._resolve_import_module(import_module, package)
            for item in names.split(","):
                item = item.strip()
                original, _, alias = item.partition(" as ")
                local = alias.strip() or original.strip()
                if local and local != "*":
                    bindings[local] = f"{target_module}.{original.strip()}"
        return bindings

    @staticmethod
    def _constructed_classes_in_body(func_node, bindings: dict[str, str]) -> set[str]:
        constructed: set[str] = set()
        for node in PythonAdapter._iter_nodes(func_node):
            if node.type != "call":
                continue
            fn = node.child_by_field_name("function")
            if fn is None or fn.type != "identifier":
                continue
            name = _node_text(fn)
            if name in bindings:
                constructed.add(bindings[name])
        return constructed

    def _constructed_imported_class(
        self, func_node, import_bindings: dict[str, str], module: str
    ) -> str:
        """The single class a function imports-and-constructs in its body, else ''.

        Structural points-to: a function whose body does ``from m import C`` (module- or
        body-local) and then ``C(...)`` is producing a ``C``. Keyed on the import binding
        (not capitalization). Returns the resolved qualified name only when exactly one
        such class is constructed (ambiguity -> no edge, precision over recall).
        """
        bindings = self._function_local_import_bindings(func_node, import_bindings, module)
        constructed = self._constructed_classes_in_body(func_node, bindings)
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

    @staticmethod
    def _method_uid_for_class_name(
        candidates: list,
        class_name: str,
        method_name: str,
    ) -> str | None:
        for candidate in candidates:
            if f".{class_name}.{method_name}" in candidate.qualified_name:
                return str(candidate.uid)
        return None

    @staticmethod
    def _single_method_candidate_uid(candidates: list) -> str | None:
        return str(candidates[0].uid) if len(candidates) == 1 else None

    @staticmethod
    def _enclosing_class_definition(node):
        class_node = node
        while class_node and class_node.type != "class_definition":
            class_node = class_node.parent
        return class_node

    def _method_uid_for_class_node(
        self,
        class_node,
        method_name: str,
        candidates: list,
    ) -> str | None:
        class_name_node = class_node.child_by_field_name("name")
        if not class_name_node:
            return None
        class_name = class_name_node.text.decode("utf-8")
        resolved = self._method_uid_for_class_name(candidates, class_name, method_name)
        if resolved:
            return resolved
        return self._single_method_candidate_uid(candidates)

    def _resolve_method_uid(
        self, caller_node, method_name: str, by_name: dict[str, list]
    ) -> str | None:
        candidates = by_name.get(method_name, [])
        if not candidates:
            return None

        class_node = self._enclosing_class_definition(caller_node)
        if not class_node:
            return self._single_method_candidate_uid(candidates)

        return self._method_uid_for_class_node(class_node, method_name, candidates)

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
        if Path(file_path).name not in (_INIT_PY, "__init__.pyi"):
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

    def _bindings_from_from_import_line(
        self,
        import_module: str,
        names: str,
        *,
        package: str,
    ) -> dict[str, str]:
        bindings: dict[str, str] = {}
        target_module = self._resolve_import_module(import_module, package)
        for item in names.split(","):
            item = item.strip()
            if not item or item == "*":
                continue
            original, _, alias = item.partition(" as ")
            local_name = alias.strip() or original.strip()
            bindings[local_name] = f"{target_module}.{original.strip()}"
        return bindings

    def _bindings_from_import_clause(self, import_body: str) -> dict[str, str]:
        bindings: dict[str, str] = {}
        for item in import_body.split(","):
            item = item.strip()
            original, _, alias = item.partition(" as ")
            target_module = original.strip()
            if not target_module:
                continue
            local_name = alias.strip() or target_module.split(".")[0]
            bindings[local_name] = target_module
        return bindings

    def _extract_import_bindings(self, source_code: str, file_path: str) -> dict[str, str]:
        """Return local import alias -> best-effort target qualified name."""
        module = module_name_from_path(file_path)
        if Path(file_path).name in (_INIT_PY, "__init__.pyi"):
            package = module
        else:
            package = module.rsplit(".", 1)[0] if "." in module else ""
        bindings: dict[str, str] = {}
        for line in source_code.splitlines():
            stripped = line.strip()
            from_parts = split_python_from_import(stripped)
            if from_parts:
                import_module, names = from_parts
                bindings.update(
                    self._bindings_from_from_import_line(import_module, names, package=package)
                )
                continue
            import_body = split_python_import_clause(stripped)
            if import_body:
                bindings.update(self._bindings_from_import_clause(import_body))
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
