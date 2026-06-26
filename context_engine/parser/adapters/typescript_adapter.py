"""TypeScript language adapter using tree-sitter."""

import re
from pathlib import Path

from context_engine.parser.adapters.js_ts_fallback_patterns import (
    CHAINED_PROPERTY_FUNC_API_RE,
    PROPERTY_ARROW_API_RE,
    PROPERTY_FUNC_API_RE,
)
from context_engine.parser.adapters.treesitter_base import (
    TreeSitterAdapter,
    flatten_ts_query_captures,
)
from context_engine.parser.adapters.ts_reexport_resolver import TsReexportResolver
from context_engine.parser.adapters.ts_scope_graph import TsBinding, TsScopeGraph
from context_engine.parser.import_scan import (
    collect_js_ts_import_bindings,
    iter_typescript_body_call_fallback_names,
    resolve_import_module_name,
)
from context_engine.parser.protocol import ClassApiEdge, ImportEdge, InheritanceEdge, SymbolMetadata
from context_engine.parser.uid import (
    compute_uid,
    module_name_from_path,
    normalize_signature,
    qualified_name_for,
    signature_from_node,
    signature_hash,
)


class TypeScriptAdapter(TreeSitterAdapter):
    """TypeScript parser adapter."""

    _EXPORTED_VAR_FALLBACK_RE = re.compile(
        r"(?m)^export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\b"
    )
    _EXPORTED_FUNC_FALLBACK_RE = re.compile(
        r"(?m)^export\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\b"
    )
    _EXPORTED_TYPE_FALLBACK_RE = re.compile(
        r"(?m)^export\s+(?:type|interface)\s+([A-Za-z_$][\w$]*)\b"
    )
    _EXPORTED_OBJECT_API_RE = re.compile(r"(?m)^export\s+const\s+([A-Za-z_$][\w$]*)\s*=\s*\{")
    _EXPORTED_CALL_INITIALIZER_RE = re.compile(
        r"(?m)^export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\b[^=\n;]*=\s*"
        r"(?:(?:/\*.*?\*/)\s*)*(?:await\s+)?([A-Za-z_$][\w$]*)\s*\("
    )
    _BODY_CALL_FALLBACK_SKIP = {
        "catch",
        "for",
        "function",
        "if",
        "new",
        "return",
        "super",
        "switch",
        "throw",
        "typeof",
        "while",
    }
    # tree-sitter emits ``abstract class`` as ``abstract_class_declaration`` —
    # a DIFFERENT node type from ``class_declaration``. Abstract bases are the
    # backbone of NestJS/Angular/TypeORM, so every place that handles a class
    # must accept both, or the base is dropped from the index entirely (no
    # Symbol, no ``DEPENDS_ON`` inheritance edge, no inherited API).
    _CLASS_DECL_TYPES = frozenset({"class_declaration", "abstract_class_declaration"})
    _TYPE_OWNER_TYPES = {
        "function_declaration",
        "method_definition",
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "variable_declarator",
    }
    _PROPERTY_FUNC_API_RE = PROPERTY_FUNC_API_RE
    _PROPERTY_ARROW_API_RE = PROPERTY_ARROW_API_RE
    _CHAINED_PROPERTY_FUNC_API_RE = CHAINED_PROPERTY_FUNC_API_RE
    _TYPE_REF_SKIP_NAMES = {
        "any",
        "bigint",
        "boolean",
        "false",
        "never",
        "null",
        "number",
        "object",
        "string",
        "symbol",
        "true",
        "undefined",
        "unknown",
        "void",
    }
    # Framework lifecycle hook method names — structural convention (NestJS,
    # Angular, Rollup/Vite plugin API), not query-specific answer keys.
    _LIFECYCLE_METHOD_NAMES = frozenset(
        {
            "onModuleInit",
            "onApplicationBootstrap",
            "onModuleDestroy",
            "beforeApplicationShutdown",
            "onApplicationShutdown",
            "ngOnInit",
            "ngOnDestroy",
            "ngOnChanges",
            "ngDoCheck",
            "ngAfterContentInit",
            "ngAfterContentChecked",
            "ngAfterViewInit",
            "ngAfterViewChecked",
            "buildStart",
            "buildEnd",
            "resolveId",
            "load",
            "transform",
            "generateBundle",
            "writeBundle",
            "closeBundle",
            "configureServer",
            "configurePreviewServer",
        }
    )
    _HOOK_REGISTER_CALLEE_NAMES = frozenset({"use", "subscribe"})
    _CONTROLLER_DECORATORS = frozenset({"Controller"})
    # Node ``EventEmitter``, DOM events, RxJS ``Subject.next`` — pub/sub surfaces.
    _EVENT_CONFIG_CALLEES = frozenset({"on", "once", "addListener", "addEventListener"})
    _EVENT_EXEC_CALLEES = frozenset({"emit", "dispatchEvent", "next"})
    _EVENT_DISPATCH_BASES = frozenset(
        {
            "EventEmitter",
            "Subject",
            "BehaviorSubject",
            "ReplaySubject",
            "AsyncSubject",
        }
    )
    # Builtins / ambient globals — not emitted as CALLS_GUESS (precision over recall).
    _STANDARD_JS_GLOBALS = frozenset(
        {
            "console",
            "window",
            "document",
            "globalThis",
            "self",
            "global",
            "fetch",
            "Promise",
            "setTimeout",
            "setInterval",
            "clearTimeout",
            "clearInterval",
            "queueMicrotask",
            "requestAnimationFrame",
            "cancelAnimationFrame",
            "JSON",
            "Math",
            "Object",
            "Array",
            "String",
            "Number",
            "Boolean",
            "Symbol",
            "BigInt",
            "Date",
            "RegExp",
            "Map",
            "Set",
            "WeakMap",
            "WeakSet",
            "Proxy",
            "Reflect",
            "Intl",
            "Atomics",
            "Error",
            "parseInt",
            "parseFloat",
            "isNaN",
            "isFinite",
            "undefined",
            "NaN",
            "Infinity",
            "process",
            "Buffer",
            "__dirname",
            "__filename",
            "module",
            "exports",
            "require",
        }
    )
    _CALLABLE_OWNER_TYPES = frozenset(
        {
            "function_declaration",
            "method_definition",
            "arrow_function",
            "function_expression",
            "function",
        }
    )
    # reflect-metadata bridge: ``Reflect.defineMetadata(KEY, …)`` (producer) and
    # ``Reflect.getMetadata(KEY, …)`` / ``Reflector.get(KEY, …)`` (consumer) are
    # connected ONLY through the shared metadata KEY constant — the structural
    # analog of the hook/event archetype's ``hook_name`` identity. ``Reflect`` is
    # an unambiguous ambient global so these member names are safe to key on.
    _REFLECT_DEFINE_METHODS = frozenset({"defineMetadata"})
    _REFLECT_READ_METHODS = frozenset(
        {"getMetadata", "getOwnMetadata", "hasMetadata", "hasOwnMetadata"}
    )
    # NestJS ``Reflector`` service. ``getAllAndOverride`` / ``getAllAndMerge`` are
    # distinctive; bare ``get`` / ``getAll`` are generic (Map/Array) so they are
    # only treated as reads when the key arg is an imported/module constant.
    _REFLECTOR_DISTINCT_METHODS = frozenset({"getAllAndOverride", "getAllAndMerge"})
    _REFLECTOR_GENERIC_METHODS = frozenset({"get", "getAll"})
    _METADATA_DEFINE_HELPERS = frozenset({"SetMetadata", "extendArrayMetadata"})
    _METADATA_READ_HELPERS = frozenset({"createContext"})

    @property
    def language_name(self) -> str:
        return "typescript"

    @property
    def file_extensions(self) -> set[str]:
        return {".ts", ".tsx"}

    @property
    def ts_language_name(self) -> str:
        return "typescript"

    def extract_axis_facts(
        self,
        source_code: str,
        file_path: str,
        *,
        tree=None,
        symbols: list[SymbolMetadata] | None = None,
        project_root: str | None = None,
    ):
        """Return common symbol facts plus TypeScript AST-physical axis facts."""
        from context_engine.parser.adapters.typescript_axis_extractor import (
            TypeScriptAxisExtractor,
        )

        facts = super().extract_axis_facts(
            source_code,
            file_path,
            tree=tree,
            symbols=symbols,
            project_root=project_root,
        )
        if tree is None:
            tree = self._parse(source_code)
        ts_facts = TypeScriptAxisExtractor(self).extract_facts(
            source_code,
            file_path,
            tree=tree,
        )
        return [*facts, *ts_facts]

    @property
    def symbol_query(self) -> str:
        return """
            (function_declaration name: (identifier) @func.name) @func.def
            (method_definition name: (property_identifier) @func.name) @func.def
            (class_declaration name: (type_identifier) @class.name) @class.def
            (abstract_class_declaration name: (type_identifier) @class.name) @class.def
            (program (lexical_declaration (variable_declarator name: (identifier) @var.name) @var.def))
            (program (export_statement (lexical_declaration (variable_declarator name: (identifier) @var.name) @var.exported_def)))
        """

    @property
    def call_query(self) -> str:
        return """
            (call_expression function: (identifier) @call.name)
            (call_expression function: (member_expression property: (property_identifier) @call.name))
        """

    @property
    def parent_types(self) -> set[str]:
        return {
            "function_declaration",
            "method_definition",
            "class_declaration",
            "abstract_class_declaration",
        }

    @property
    def import_query(self) -> str:
        return """
            (import_statement source: (string) @import.source) @import.stmt
            (export_statement source: (string) @import.source) @import.stmt
            (import_specifier (identifier) @import.name) @import.spec
        """

    def _append_typescript_export_fallback_symbols(
        self,
        symbols: list[SymbolMetadata],
        existing_names: set[str],
        file_path: str,
        source_code: str,
    ) -> None:
        for match in self._EXPORTED_FUNC_FALLBACK_RE.finditer(source_code):
            self._append_module_fallback_symbol(
                symbols,
                existing_names,
                file_path,
                source_code,
                start_offset=match.start(),
                name=match.group(1),
                kind="function",
            )
        for match in self._EXPORTED_VAR_FALLBACK_RE.finditer(source_code):
            name = match.group(1)
            if name in existing_names:
                continue
            tail = source_code[match.end() : match.end() + 24]
            if re.match(r"\s*=\s*\{", tail):
                continue
            self._append_module_fallback_symbol(
                symbols,
                existing_names,
                file_path,
                source_code,
                start_offset=match.start(),
                name=name,
                kind="variable",
            )
        for match in self._EXPORTED_TYPE_FALLBACK_RE.finditer(source_code):
            self._append_module_fallback_symbol(
                symbols,
                existing_names,
                file_path,
                source_code,
                start_offset=match.start(),
                name=match.group(1),
                kind="class",
            )

    def _apply_typescript_symbol_annotations(
        self,
        symbols: list[SymbolMetadata],
        tree,
        source_code: str,
        file_path: str,
    ) -> None:
        higher_order_factory_names = self._higher_order_factory_names(tree)
        if higher_order_factory_names:
            for symbol in symbols:
                if symbol.name in higher_order_factory_names:
                    symbol.returns_function_expression = True
        self._mark_property_accessor_symbols(symbols, tree, source_code, file_path)
        self._mark_react_hook_symbols(symbols)
        self._mark_behavioral_shape_symbols(symbols, tree)

    def _apply_typescript_export_enrichments(
        self,
        symbols: list[SymbolMetadata],
        source_code: str,
        file_path: str,
        *,
        tree,
    ) -> list[SymbolMetadata]:
        object_api_ranges = self._exported_object_api_ranges(source_code)
        if object_api_ranges:
            symbols = self._qualify_exported_object_api_members(
                symbols,
                source_code,
                file_path,
                tree,
            )
            symbols = self._merge_exported_object_api_symbols(
                symbols,
                source_code,
                file_path,
                object_api_ranges,
            )
        existing_names = {symbol.name for symbol in symbols}
        self._append_typescript_export_fallback_symbols(
            symbols, existing_names, file_path, source_code
        )
        self._apply_typescript_symbol_annotations(symbols, tree, source_code, file_path)
        from context_engine.parser.docstring_extract import attach_docstrings

        attach_docstrings(
            symbols,
            source_code,
            file_path,
            tree=tree,
            language=self.language_name,
        )
        return symbols

    def extract_symbols(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[SymbolMetadata]:
        """Extract TS symbols with a fallback for exported lexical APIs.

        Tree-sitter can recover imperfectly on very type-heavy files and skip
        otherwise simple `export const foo = ...` declarations. We still want
        those public API surfaces indexed, so we add a conservative text
        fallback for top-level exported lexical declarations that were not
        surfaced by the AST query.
        """
        if tree is None:
            tree = self._parse(source_code)
        symbols = super().extract_symbols(source_code, file_path, tree=tree)
        return self._apply_typescript_export_enrichments(
            symbols,
            source_code,
            file_path,
            tree=tree,
        )

    # Return-shape constructor names (``return new Map()`` → mapping). Plain
    # ``return new Foo()`` is a constructed type; collection builtins are the
    # mapping / sequence shapes instead.
    _SHAPE_MAPPING_CTORS = frozenset({"Map", "WeakMap"})
    _SHAPE_SEQUENCE_CTORS = frozenset({"Array", "Set", "WeakSet"})
    _SHAPE_FUNC_TYPES = frozenset(
        {"function_declaration", "method_definition", "function_expression", "arrow_function"}
    )
    _SHAPE_WRAP_TYPES = frozenset(
        {
            "as_expression",
            "satisfies_expression",
            "parenthesized_expression",
            "non_null_expression",
            "type_assertion",
        }
    )

    def _mark_behavioral_shape_symbols(self, symbols: list[SymbolMetadata], tree) -> None:
        """Stamp return-/iteration-shape flags (Python adapter parity for TS).

        These five booleans are the evidence for the structural
        ``binding_surface`` role (``role_cascade``): a function that *returns a
        mapping / constructed type* or *assembles a mapping in a loop* is a
        transformer/binder, not a generic orchestrator. The TS adapter never
        computed them, so TS transformers collapsed into coarse roles and the
        per-role seed selector could not surface them.
        """
        table = self._function_shape_table(tree)
        if not table:
            return
        for symbol in symbols:
            if symbol.kind not in {"function", "method"}:
                continue
            shape = table.get(symbol.name)
            if not shape:
                continue
            if shape["mapping"]:
                symbol.returns_mapping = True
            if shape["sequence"]:
                symbol.returns_sequence = True
            if shape["constructed"]:
                symbol.returns_constructed_type = True
            if shape["iterates"]:
                symbol.iterates_attr_call = True
            if shape["assembles"]:
                symbol.assembles_mapping_in_loop = True

    def _function_shape_table(self, tree) -> dict[str, dict[str, bool]]:
        """``function_name → shape flags`` (ORed across same-named functions)."""
        out: dict[str, dict[str, bool]] = {}
        for node in self._iter_nodes(tree.root_node):
            if node.type not in self._SHAPE_FUNC_TYPES:
                continue
            name = self._shape_function_name(node)
            if not name:
                continue
            body = node.child_by_field_name("body")
            if body is None:
                continue
            ret = self._collect_ts_return_shape(body)
            it = self._collect_ts_iteration_shape(body)
            if not (any(ret.values()) or any(it.values())):
                continue
            slot = out.setdefault(
                name,
                {
                    "mapping": False,
                    "sequence": False,
                    "constructed": False,
                    "iterates": False,
                    "assembles": False,
                },
            )
            for k, v in {**ret, **it}.items():
                if v:
                    slot[k] = True
        return out

    def _shape_function_name(self, node) -> str:
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            return self._node_text(name_node)
        # ``const x = () => …`` / ``const x = function () {}`` — name from the
        # enclosing declarator so the flag lands on the same Symbol.
        parent = node.parent
        if parent is not None and parent.type == "variable_declarator":
            decl_name = parent.child_by_field_name("name")
            if decl_name is not None and decl_name.type == "identifier":
                return self._node_text(decl_name)
        return ""

    @classmethod
    def _unwrap_shape_expr(cls, expr):
        while expr is not None and expr.type in cls._SHAPE_WRAP_TYPES:
            inner = expr.named_children[0] if expr.named_children else None
            if inner is None:
                break
            expr = inner
        return expr

    @classmethod
    def _classify_ts_return_expr(cls, expr) -> str:
        expr = cls._unwrap_shape_expr(expr)
        if expr is None:
            return ""
        t = expr.type
        if t == "object":
            return "mapping"
        if t == "array":
            return "sequence"
        if t == "new_expression":
            ctor = expr.child_by_field_name("constructor")
            if ctor is not None and ctor.type == "identifier":
                name = cls._node_text(ctor)
                if name in cls._SHAPE_MAPPING_CTORS:
                    return "mapping"
                if name in cls._SHAPE_SEQUENCE_CTORS:
                    return "sequence"
            return "constructed"
        if t == "call_expression":
            fn = expr.child_by_field_name("function")
            if fn is not None and fn.type == "member_expression":
                obj = fn.child_by_field_name("object")
                prop = fn.child_by_field_name("property")
                if obj is not None and prop is not None and obj.type == "identifier":
                    head, tail = cls._node_text(obj), cls._node_text(prop)
                    if head == "Object" and tail in {"fromEntries", "assign"}:
                        return "mapping"
                    if head == "Array" and tail in {"from", "of"}:
                        return "sequence"
        return ""

    @classmethod
    def _collect_ts_return_shape(cls, body) -> dict[str, bool]:
        # Pass 1: local bindings whose initializer is itself a known shape.
        local: dict[str, str] = {}
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type in cls._SHAPE_FUNC_TYPES:
                continue
            if n.type == "variable_declarator":
                name_node = n.child_by_field_name("name")
                value = n.child_by_field_name("value")
                if name_node is not None and value is not None and name_node.type == "identifier":
                    kind = cls._classify_ts_return_expr(value)
                    if kind:
                        local[cls._node_text(name_node)] = kind
            elif n.type == "assignment_expression":
                left = n.child_by_field_name("left")
                right = n.child_by_field_name("right")
                if left is not None and right is not None and left.type == "identifier":
                    kind = cls._classify_ts_return_expr(right)
                    if kind:
                        local[cls._node_text(left)] = kind
            for child in n.children:
                stack.append(child)

        # Pass 2: return statements (bare identifier falls back to Pass 1).
        shape = {"mapping": False, "sequence": False, "constructed": False}
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type == "return_statement":
                expr = n.named_children[0] if n.named_children else None
                if expr is not None:
                    kind = cls._classify_ts_return_expr(expr)
                    if not kind:
                        unwrapped = cls._unwrap_shape_expr(expr)
                        if unwrapped is not None and unwrapped.type == "identifier":
                            kind = local.get(cls._node_text(unwrapped), "")
                    if kind:
                        shape[kind] = True
                continue
            if n.type in cls._SHAPE_FUNC_TYPES:
                continue
            for child in n.children:
                stack.append(child)
        return shape

    @classmethod
    def _collect_ts_iteration_shape(cls, body) -> dict[str, bool]:
        """``for (… of obj.attr) x.m()`` and subscript-assembly in a loop."""
        flags = {"iterates": False, "assembles": False}
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type in cls._SHAPE_FUNC_TYPES:
                continue
            if n.type in ("for_in_statement", "for_statement"):
                fbody = n.child_by_field_name("body")
                if n.type == "for_in_statement":
                    left = n.child_by_field_name("left")
                    right = n.child_by_field_name("right")
                    if (
                        fbody is not None
                        and right is not None
                        and right.type == "member_expression"
                        and left is not None
                        and left.type == "identifier"
                        and cls._for_body_calls_on(fbody, cls._node_text(left))
                    ):
                        flags["iterates"] = True
                if fbody is not None and cls._for_body_writes_subscript(fbody):
                    flags["assembles"] = True
            for child in n.children:
                stack.append(child)
        return flags

    @classmethod
    def _for_body_calls_on(cls, body, loop_var: str) -> bool:
        if not loop_var:
            return False
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type in cls._SHAPE_FUNC_TYPES:
                continue
            if n.type == "call_expression":
                fn = n.child_by_field_name("function")
                if fn is not None and fn.type == "member_expression":
                    obj = fn.child_by_field_name("object")
                    if (
                        obj is not None
                        and obj.type == "identifier"
                        and cls._node_text(obj) == loop_var
                    ):
                        return True
            for child in n.children:
                stack.append(child)
        return False

    @classmethod
    def _for_body_writes_subscript(cls, body) -> bool:
        """``acc[k] = …`` / ``map.set(…)`` / ``arr.push(…)`` inside the loop body."""
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type in cls._SHAPE_FUNC_TYPES:
                continue
            if n.type == "assignment_expression":
                left = n.child_by_field_name("left")
                if left is not None and left.type == "subscript_expression":
                    return True
            if n.type == "call_expression":
                fn = n.child_by_field_name("function")
                if fn is not None and fn.type == "member_expression":
                    prop = fn.child_by_field_name("property")
                    if prop is not None and cls._node_text(prop) in {"set", "push", "add"}:
                        return True
            for child in n.children:
                stack.append(child)
        return False

    @staticmethod
    def _is_react_hook_name(name: str) -> bool:
        return (
            len(name) > 3 and name.startswith("use") and name[3].isupper() and name.isidentifier()
        )

    def _mark_react_hook_symbols(self, symbols: list[SymbolMetadata]) -> None:
        for symbol in symbols:
            if symbol.kind not in {"function", "method"}:
                continue
            if self._is_react_hook_name(symbol.name):
                symbol.is_react_hook = True

    def _mark_property_accessor_symbols(
        self,
        symbols: list[SymbolMetadata],
        tree,
        source_code: str,
        file_path: str,
    ) -> None:
        """Stamp ``is_getter`` / ``is_setter`` on class accessor method symbols."""
        by_uid = {symbol.uid: symbol for symbol in symbols}
        for node in self._iter_nodes(tree.root_node):
            if node.type != "method_definition":
                continue
            is_get = any(child.type == "get" for child in node.children)
            is_set = any(child.type == "set" for child in node.children)
            if not is_get and not is_set:
                continue
            uid = self._uid_for_node(node, source_code, file_path)
            symbol = by_uid.get(uid)
            if symbol is None:
                continue
            symbol.is_getter = is_get
            symbol.is_setter = is_set

    def _higher_order_factory_names(self, tree) -> set[str]:
        """Collect names of functions whose body returns a function expression.

        A higher-order factory has the shape ``def f(...) { ... return (x)=>...
        }`` — its return value is itself callable. NestJS's ``Controller``,
        ``Module``, ``Injectable``, and ``RequestMapping`` follow this shape,
        as does anything that synthesises a decorator. The signal is purely
        AST-syntactic: the body has a ``return_statement`` whose immediate
        argument is an ``arrow_function`` or ``function_expression``.

        Two surface forms are recognised: a ``function`` declaration, and a
        ``variable_declarator`` whose initializer is an arrow function (the
        common ``export const Foo = (opts) => { return (target) => {...} }``
        pattern). A variable initialised by a call (``const Get =
        makeDecorator(...)``) is not detected — its return shape requires
        cross-function dataflow.
        """
        names: set[str] = set()
        for node in self._iter_nodes(tree.root_node):
            if node.type == "function_declaration":
                name = node.child_by_field_name("name")
                body = node.child_by_field_name("body")
                if (
                    name is not None
                    and body is not None
                    and self._body_returns_function_expression(body)
                ):
                    names.add(self._node_text(name))
            elif node.type == "variable_declarator":
                name = node.child_by_field_name("name")
                value = node.child_by_field_name("value")
                if name is None or value is None or value.type != "arrow_function":
                    continue
                body = value.child_by_field_name("body")
                if body is not None and self._body_returns_function_expression(body):
                    names.add(self._node_text(name))
        return names

    @staticmethod
    def _body_returns_function_expression(body) -> bool:
        """A ``return arrow_function``/``return function_expression`` anywhere
        in this body, *not* descending into nested function definitions."""
        stack = [body]
        while stack:
            node = stack.pop()
            if node.type == "return_statement":
                for child in node.named_children:
                    if child.type in ("arrow_function", "function_expression"):
                        return True
                continue
            # Don't cross into a nested callable's body — only the *current*
            # function's return shape matters.
            for child in node.children:
                if child.type in (
                    "arrow_function",
                    "function_expression",
                    "function_declaration",
                    "method_definition",
                ):
                    continue
                stack.append(child)
        return False

    def _exported_object_api_ranges(self, source_code: str) -> dict[str, tuple[int, int]]:
        ranges: dict[str, tuple[int, int]] = {}
        for match in self._EXPORTED_OBJECT_API_RE.finditer(source_code):
            name = match.group(1)
            brace_index = source_code.find("{", match.end() - 1)
            if brace_index < 0:
                continue
            end_index = self._find_matching_brace(source_code, brace_index)
            if end_index is None:
                continue
            start_line = source_code.count("\n", 0, match.start()) + 1
            end_line = source_code.count("\n", 0, end_index) + 1
            ranges[name] = (start_line, end_line)
        return ranges

    @staticmethod
    def _find_matching_brace(source_code: str, open_index: int) -> int | None:
        depth = 0
        for idx in range(open_index, len(source_code)):
            char = source_code[idx]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return idx
        return None

    def _qualify_exported_object_api_members(
        self,
        symbols: list[SymbolMetadata],
        source_code: str,
        file_path: str,
        tree,
    ) -> list[SymbolMetadata]:
        """Keep exported object methods as addressable symbols.

        Tree-sitter exposes ``export const Client = { ask() {} }`` members as
        ordinary ``method_definition`` nodes, but their default qualified name
        omits the object owner (``module.ask``).  Preserve the aggregate
        ``Client`` surface and qualify each direct member as
        ``module.Client.ask`` so editor targets, calls, and HTTP endpoint facts
        all share one stable UID.
        """
        owner_by_span: dict[tuple[str, int, int], tuple[str, str | None]] = {}
        for node in self._iter_nodes(tree.root_node):
            if node.type != "method_definition":
                continue
            owner = self._object_literal_owner_variable(node)
            if owner is None:
                continue
            owner_name_node = owner.child_by_field_name("name")
            method_name_node = node.child_by_field_name("name")
            if owner_name_node is None or method_name_node is None:
                continue
            owner_name = self._node_text(owner_name_node)
            method_name = self._node_text(method_name_node)
            if owner_name and method_name:
                raw_signature, _ = signature_from_node(
                    node,
                    source_code,
                    self.language_name,
                )
                owner_by_span[(method_name, node.start_point[0] + 1, node.end_point[0] + 1)] = (
                    owner_name,
                    raw_signature,
                )

        if not owner_by_span:
            return symbols

        module = module_name_from_path(file_path)
        qualified: list[SymbolMetadata] = []
        for symbol in symbols:
            owner = owner_by_span.get((symbol.name, symbol.start_line, symbol.end_line))
            if not owner:
                qualified.append(symbol)
                continue
            owner_name, raw_signature = owner
            qualified_name = f"{module}.{owner_name}.{symbol.name}"
            qualified.append(
                symbol.model_copy(
                    update={
                        "uid": compute_uid(
                            qualified_name,
                            raw_signature,
                            self.language_name,
                        ),
                        "qualified_name": qualified_name,
                    }
                )
            )
        return qualified

    def _merge_exported_object_api_symbols(
        self,
        symbols: list[SymbolMetadata],
        source_code: str,
        file_path: str,
        object_api_ranges: dict[str, tuple[int, int]],
    ) -> list[SymbolMetadata]:
        merged = [
            symbol
            for symbol in symbols
            if not (
                symbol.name in object_api_ranges
                and symbol.start_line == object_api_ranges[symbol.name][0]
            )
        ]
        lines = source_code.splitlines()
        for name, (start_line, end_line) in object_api_ranges.items():
            if end_line < start_line or start_line < 1:
                continue
            content = "\n".join(lines[start_line - 1 : end_line])
            signature = normalize_signature(f"{name}()->_", self.language_name)
            qualified_name = f"{module_name_from_path(file_path)}.{name}"
            merged.append(
                SymbolMetadata(
                    uid=compute_uid(qualified_name, signature, self.language_name),
                    name=name,
                    kind="object_api",
                    start_line=start_line,
                    end_line=end_line,
                    content_hash=self._hash(content),
                    file_path=file_path,
                    qualified_name=qualified_name,
                    signature=signature,
                    signature_hash=signature_hash(signature, self.language_name),
                    signature_status="object_api_export",
                    language=self.language_name,
                )
            )
        return merged

    def should_include_variable_symbol(
        self,
        node,
        tag: str,
        name: str,
        *,
        source_code: str,
        file_path: str,
    ) -> bool:
        """Treat exported lexical declarations as public API symbols.

        TypeScript libraries commonly publish their top-level API as
        ``export const foo = ...`` rather than ``function foo()``. Indexing
        those declarations makes retrieval work across TS codebases without
        hard-coding framework names like Redux Toolkit.
        """
        if super().should_include_variable_symbol(
            node, tag, name, source_code=source_code, file_path=file_path
        ):
            return True
        return tag == "var.exported_def"

    @property
    def inheritance_query(self) -> str:
        return ""

    def extract_imports(self, source_code: str, file_path: str, *, tree=None) -> list[ImportEdge]:
        """Extract import statements from TypeScript source."""
        if tree is None:
            tree = self._parse(source_code)

        # Flatten captures from matches into (node, tag) tuples
        captures = flatten_ts_query_captures(self.language, self.import_query, tree.root_node)

        imports = []
        for node, tag in captures:
            if tag == "import.source":
                source = (node.text or b"").decode("utf-8").strip("\"'")
                import_type = "relative" if source.startswith(".") else "from_package"
                imports.append(ImportEdge(file_path, source, import_type))

        return imports

    def extract_inheritance(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[InheritanceEdge]:
        """Extract class inheritance and interface implementation from TypeScript source.

        Line-based regex; ``tree`` is accepted for ``extract_all`` parity.
        """
        import re

        edges = []
        lines = source_code.split("\n")
        for line in lines:
            line = line.strip()
            class_match = re.match(
                r"^(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+(\w+)", line
            )
            if not class_match:
                continue
            class_name = class_match.group(1)
            extends_match = re.search(r"extends\s+(\w+)", line)
            implements_match = re.search(r"implements\s+([^{]+)", line)

            if extends_match:
                extends = extends_match.group(1)
                subclass_uid = self._uid(file_path, class_name)
                edges.append(InheritanceEdge(subclass_uid, extends, False))

            if implements_match:
                implements = implements_match.group(1)
                for impl in implements.split(","):
                    impl = impl.strip()
                    if impl:
                        subclass_uid = self._uid(file_path, class_name)
                        edges.append(InheritanceEdge(subclass_uid, impl, True))

        return edges

    # Decorated tree-sitter node types that can carry an `@deco` prefix in TS.
    _DECORATABLE_NODE_TYPES = frozenset(
        {
            "class_declaration",
            "abstract_class_declaration",
            "method_definition",
            "function_declaration",
            "public_field_definition",
            "abstract_method_signature",
            "method_signature",
            "property_signature",
            "accessor_signature",
        }
    )

    def extract_decorators(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """TS decorator extraction → DECORATED_BY edges, matching Python's shape.

        The ``@deco`` and ``@deco(args)`` forms are static, AST-visible facts: a
        ``decorator`` node sits as a sibling immediately *before* the
        decorated declaration. Tree-sitter places that decorator under one of
        two parents: ``export_statement`` (class-level: ``@Module export class``)
        or ``class_body`` (member-level: method, public field, signature). We
        scan the tree for every ``decorator`` node and pair it with the next
        decoratable sibling, ignoring intervening ``decorator`` / ``export``
        tokens — so a class with multiple stacked decorators credits each.

        Resolves the decorator name through the import bindings to a qualified
        name where possible; bare same-module names fall back to ``module.name``.
        The dict shape matches ``PythonAdapter.extract_decorators`` so the same
        ``Neo4jClient.link_decorators`` linker handles both languages.
        """
        if tree is None:
            tree = self._parse(source_code)
        module = module_name_from_path(file_path)
        import_bindings, _ = self._extract_import_bindings(source_code, file_path)
        out: list[dict] = []
        for deco in self._iter_nodes(tree.root_node):
            if deco.type != "decorator":
                continue
            decorated = self._decorated_node_from_decorator(deco)
            if decorated is None:
                continue
            decorated_name = self._decoratable_name(decorated)
            if not decorated_name:
                continue
            decorated_uid = self._uid_for_node(decorated, source_code, file_path)
            if not decorated_uid:
                continue
            base = self._decorator_base_name(deco)
            if not base:
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

    @classmethod
    def _decoratable_sibling_after(cls, parent, deco):
        """First sibling after ``deco`` that is a decoratable declaration.

        Compares siblings by start_byte rather than identity — tree-sitter
        Python bindings hand out fresh node wrappers on each access, so
        ``sib is deco`` never matches.
        """
        deco_start = deco.start_byte
        for sib in parent.children:
            if sib.start_byte <= deco_start:
                continue
            if sib.type == "decorator":
                continue
            if sib.type in cls._DECORATABLE_NODE_TYPES:
                return sib
            # Skip TS keywords (anonymous tokens) that may appear between
            # decorators and the declaration (e.g. `export`, `abstract`,
            # `public`, …). A named sibling that is not decoratable means
            # the decorator does not attach to a recognised declaration —
            # give up rather than reach across an unrelated node.
            if sib.is_named:
                return None
        return None

    def _decorated_node_from_decorator(self, deco, *, class_only: bool = False):
        parent = deco.parent
        if parent is None:
            return None
        if parent.type in self._DECORATABLE_NODE_TYPES:
            decorated = parent
        else:
            decorated = self._decoratable_sibling_after(parent, deco)
        if decorated is None:
            return None
        if class_only and decorated.type not in self._CLASS_DECL_TYPES:
            return None
        return decorated

    def _decoratable_name(self, node) -> str:
        if node.type in self._CLASS_DECL_TYPES:
            name = node.child_by_field_name("name")
            return self._node_text(name) if name is not None else ""
        if node.type in {"function_declaration", "method_definition"}:
            name = node.child_by_field_name("name")
            return self._node_text(name) if name is not None else ""
        if node.type in {
            "public_field_definition",
            "abstract_method_signature",
            "method_signature",
            "property_signature",
            "accessor_signature",
        }:
            name = node.child_by_field_name("name")
            return self._node_text(name) if name is not None else ""
        return ""

    def _decorator_base_name(self, deco) -> str:
        """Resolve ``@Foo`` / ``@Foo(args)`` / ``@a.b`` / ``@a.b(args)`` to a name.

        For member-expression decorators returns ``a.b`` (dotted), letting the
        linker fall back to the leaf name when needed via ``decorator_name``.
        """
        for child in deco.children:
            if child.type == "identifier":
                return self._node_text(child)
            if child.type == "member_expression":
                return self._member_expression_dotted(child)
            if child.type == "call_expression":
                fn = child.child_by_field_name("function")
                if fn is None:
                    continue
                if fn.type == "identifier":
                    return self._node_text(fn)
                if fn.type == "member_expression":
                    return self._member_expression_dotted(fn)
        return ""

    def extract_decorator_compositions(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[dict]:
        """Decorator-arg composition refs → COMPOSES edges (subtype 2 signal).

        A class decorated with ``@Module({ imports, providers, controllers })``
        names the components it composes inline as identifiers inside arrays
        of the object literal. Each such identifier is a static AST reference
        from the decorated class to a composed symbol — the cleanest possible
        signal for the declarative metadata composition pattern (composition_surface
        subtype 2).

        Emitted as edges from the decorated class to each referenced symbol,
        carrying the decorator name and the property key (``imports``,
        ``providers``, …) for diagnostics. Spread elements (``...providers``)
        are skipped — the expanded contents are not statically visible.

        Only class-level decorators contribute; method/field decorators name
        request/lifecycle metadata, not composition.
        """
        if tree is None:
            tree = self._parse(source_code)
        module = module_name_from_path(file_path)
        import_bindings, _ = self._extract_import_bindings(source_code, file_path)
        out: list[dict] = []
        for deco in self._iter_nodes(tree.root_node):
            if deco.type != "decorator":
                continue
            decorated = self._decorated_node_from_decorator(deco, class_only=True)
            if decorated is None:
                continue
            decorated_name = self._decoratable_name(decorated)
            if not decorated_name:
                continue
            decorated_uid = self._uid_for_node(decorated, source_code, file_path)
            if not decorated_uid:
                continue
            base = self._decorator_base_name(deco)
            if not base:
                continue
            for key, ref_name in self._decorator_arg_object_refs(deco):
                qn = self._resolve_type_name(ref_name, import_bindings, module)
                out.append(
                    {
                        "decorated_uid": decorated_uid,
                        "decorated_name": decorated_name,
                        "decorator_name": base,
                        "decorator_key": key,
                        "referenced_name": ref_name,
                        "referenced_qualified_name": qn,
                        "file_path": file_path,
                    }
                )
        return out

    def _decorator_arg_object_refs(self, deco):
        """Yield ``(property_key, identifier)`` for every identifier inside an
        object-literal-of-arrays decorator argument.

        Matches the shape: ``@Foo({ key1: [Id, Id], key2: [Id], … })``. Spread
        elements (``...spread``) are skipped — their expansion is not visible
        in the AST. Returns nothing for decorators with no args, with non-object
        args, or with arrays of non-identifier values (strings, calls, …)."""
        call = next(
            (c for c in deco.children if c.type == "call_expression"),
            None,
        )
        if call is None:
            return
        args = call.child_by_field_name("arguments")
        if args is None:
            return
        # First object argument: ``@Foo({...}, other)`` only the first object counts.
        obj = next(
            (c for c in args.named_children if c.type == "object"),
            None,
        )
        if obj is None:
            return
        for pair in obj.named_children:
            if pair.type != "pair":
                continue
            key_node = pair.child_by_field_name("key")
            value_node = pair.child_by_field_name("value")
            if key_node is None or value_node is None:
                continue
            key = self._node_text(key_node) if key_node.type == "property_identifier" else ""
            if not key:
                continue
            if value_node.type != "array":
                continue
            for elem in value_node.named_children:
                if elem.type == "identifier":
                    yield key, self._node_text(elem)

    def _member_expression_dotted(self, node) -> str:
        obj = node.child_by_field_name("object")
        prop = node.child_by_field_name("property")
        if obj is None or prop is None:
            return ""
        if obj.type == "identifier" and prop.type == "property_identifier":
            return f"{self._node_text(obj)}.{self._node_text(prop)}"
        # Nested `a.b.c` is rare for decorators; fall back to leaf name.
        if prop.type == "property_identifier":
            return self._node_text(prop)
        return ""

    def extract_type_references(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """Extract TypeScript ``USES_TYPE`` references from AST-visible type syntax.

        Type annotations, generic constraints/defaults, interface/type bodies, and
        annotated top-level variables are static facts. Resolution is left to Neo4j,
        so builtin/external names are naturally dropped unless the project indexes a
        matching type symbol.
        """
        if tree is None:
            tree = self._parse(source_code)
        module = module_name_from_path(file_path)
        import_bindings, _ = self._extract_import_bindings(source_code, file_path)
        out: list[dict] = []
        seen: set[tuple[str, str, str]] = set()

        def emit(owner, type_node, kind: str) -> None:
            owner_uid = self._owner_uid_for_type_reference(owner, source_code, file_path)
            owner_name = self._owner_name_for_type_reference(owner, source_code)
            if not owner_uid or not owner_name:
                return
            skip_names = self._type_parameter_names(owner, source_code) | {owner_name}
            for type_name, type_qn in self._type_ref_targets(
                type_node,
                import_bindings,
                module,
                skip_names=skip_names,
            ):
                key = (owner_uid, type_qn, kind)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "referrer_uid": owner_uid,
                        "referrer_name": owner_name,
                        "type_name": type_name,
                        "type_qualified_name": type_qn,
                        "kind": kind,
                        "file_path": file_path,
                    }
                )

        for owner in self._iter_nodes(tree.root_node):
            if owner.type not in self._TYPE_OWNER_TYPES:
                continue
            if owner.type == "variable_declarator" and not self._is_top_level_variable_declarator(
                owner
            ):
                continue
            if owner.type in {"function_declaration", "method_definition"}:
                params = owner.child_by_field_name("parameters")
                if params is not None:
                    for param in params.named_children:
                        self._emit_ts_type_annotations(param, "param", emit, owner)
                type_params = next(
                    (child for child in owner.named_children if child.type == "type_parameters"),
                    None,
                )
                if type_params is not None:
                    emit(owner, type_params, "annotation")
                return_type = owner.child_by_field_name("return_type")
                if return_type is None:
                    return_type = self._node_field_by_type(owner, "type_annotation")
                if return_type is not None:
                    emit(owner, return_type, "return")
                body = owner.child_by_field_name("body")
                if body is not None:
                    for node in self._iter_nodes(body):
                        if node.type in {"lexical_declaration", "variable_declarator"}:
                            self._emit_ts_type_annotations(node, "annotation", emit, owner)
            elif owner.type in {"interface_declaration", "type_alias_declaration"}:
                emit(owner, owner, "annotation")
            elif owner.type in self._CLASS_DECL_TYPES:
                emit(owner, owner, "annotation")
            elif owner.type == "variable_declarator":
                self._emit_ts_type_annotations(owner, "annotation", emit, owner)
        return out

    def extract_injections(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """Dependency-injection bindings from parameter decorators.

        ``constructor(@Inject(Provider) …)`` is a static AST fact parallel to
        Python's ``def f(x = Marker(provider))``: the decorator call's first
        positional identifier names the wired provider. Type-only constructor
        parameters (no inject decorator) stay on ``USES_TYPE`` — same split as
        Python (marker-wrapped default vs plain annotation).
        """
        if tree is None:
            tree = self._parse(source_code)
        module = module_name_from_path(file_path)
        import_bindings, _ = self._extract_import_bindings(source_code, file_path)
        out: list[dict] = []
        seen: set[tuple[str, str]] = set()

        for class_node in self._iter_nodes(tree.root_node):
            if class_node.type not in self._CLASS_DECL_TYPES:
                continue
            for child in class_node.named_children:
                if child.type != "class_body":
                    continue
                for member in child.named_children:
                    if member.type != "method_definition":
                        continue
                    name_node = member.child_by_field_name("name")
                    if name_node is None or self._node_text(name_node) != "constructor":
                        continue
                    owner_uid = self._uid_for_node(member, source_code, file_path)
                    owner_name_node = class_node.child_by_field_name("name")
                    owner_name = (
                        self._node_text(owner_name_node) if owner_name_node is not None else ""
                    )
                    params = member.child_by_field_name("parameters")
                    if params is None:
                        continue
                    for param in params.named_children:
                        for provider_name in self._parameter_decorator_provider_names(param):
                            prov_qn = self._resolve_type_name(
                                provider_name, import_bindings, module
                            )
                            key = (owner_uid, prov_qn)
                            if key in seen:
                                continue
                            seen.add(key)
                            out.append(
                                {
                                    "owner_uid": owner_uid,
                                    "owner_name": owner_name,
                                    "provider_name": provider_name,
                                    "provider_qualified_name": prov_qn,
                                    "file_path": file_path,
                                }
                            )
        return out

    def extract_instantiations(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """INSTANTIATES edges: caller symbol -> the project class it constructs.

        ``new Foo()`` / ``new imported.Foo()`` are static construction sites,
        distinct from ordinary calls. Resolution to in-graph classes (and
        external-package routing) happens at link time; unresolvable names are
        dropped here.
        """
        if tree is None:
            tree = self._parse(source_code)
        module = module_name_from_path(file_path)
        import_bindings, _ = self._extract_import_bindings(source_code, file_path)
        symbols = self.extract_symbols(source_code, file_path, tree=tree)
        local_classes = {s.name for s in symbols if s.kind == "class"}
        out: list[dict] = []
        seen: set[tuple[str, str, str]] = set()

        for node in self._iter_nodes(tree.root_node):
            if node.type != "new_expression":
                continue
            ctor = node.child_by_field_name("constructor")
            if ctor is None:
                continue
            owner = self._enclosing_symbol_owner(node)
            if owner is None:
                continue
            caller_uid = self._caller_uid_for_owner(owner, source_code, file_path)
            if not caller_uid:
                continue
            typed_locals = self._class_typed_locals(
                owner,
                source_code,
                import_bindings,
                module,
                local_classes,
            )
            resolved = None
            if ctor.type == "identifier":
                local_name = self._node_text(ctor)
                if local_name in typed_locals:
                    resolved = typed_locals[local_name]
            if resolved is None:
                resolved = self._resolve_new_callee(
                    ctor,
                    import_bindings=import_bindings,
                    local_classes=local_classes,
                    module=module,
                )
            if resolved is None:
                continue
            type_name, type_qn, is_external = resolved
            bucket = "external" if is_external else "internal"
            key = (caller_uid, type_qn, bucket)
            if key in seen:
                continue
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
        return out

    def extract_hooks(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """Hook / registration facts for JS/TS middleware, lifecycle, and event surfaces.

        Emits the same fact shape as :meth:`PythonAdapter.extract_hooks` so
        ``Neo4jClient.link_hooks`` can materialize EVENT_* + HOOK_* edges:

        * **handler** — ``app.use(mw)``, ``interceptors.request.use(fn)``,
          ``obs.subscribe(handler)``, ``emitter.on('evt', handler)`` (handler arg).
        * **method** — lifecycle method names (``onModuleInit``, ``ngOnInit``, …)
          and string-literal event topics on ``emit`` / ``addEventListener`` when
          the topic is a valid identifier (mirrors Python ``listens_for``).
        * **object** — ``subject.next(value)`` publishes through an instantiated
          RxJS subject variable (same channel as Python ``signal.send``).
        * **wrapper-only** — ``emit('user:login')`` and other non-identifier
          topics still emit HOOK_EXEC/HOOK_CONFIG to the dispatch API (``emit``,
          ``on``, …) even when no topic Symbol resolves.
        """
        if tree is None:
            tree = self._parse(source_code)
        out: list[dict] = []
        seen: set[tuple[str, str, str, str, str]] = set()

        def emit(
            site_uid: str,
            hook_name: str,
            *,
            kind: str,
            target_kind: str,
            via: str,
            wrapper_only: bool = False,
        ) -> None:
            if not site_uid or (not hook_name and not wrapper_only):
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

        for node in self._iter_nodes(tree.root_node):
            if node.type == "method_definition":
                name_node = node.child_by_field_name("name")
                if name_node is None:
                    continue
                method_name = self._node_text(name_node)
                if method_name not in self._LIFECYCLE_METHOD_NAMES:
                    continue
                site_uid = self._uid_for_node(node, source_code, file_path)
                emit(
                    site_uid,
                    method_name,
                    kind="config",
                    target_kind="method",
                    via=method_name,
                )
                continue

            if node.type != "call_expression":
                continue
            func = node.child_by_field_name("function")
            if func is None or func.type != "member_expression":
                continue
            path = self._member_expression_path(func)
            if not path:
                continue
            callee = path[-1]
            owner = self._enclosing_symbol_owner(node)
            if owner is None:
                continue
            call_site_uid = self._caller_uid_for_owner(owner, source_code, file_path)
            if not call_site_uid:
                continue

            if callee in self._EVENT_CONFIG_CALLEES or callee in self._EVENT_EXEC_CALLEES:
                kind = "config" if callee in self._EVENT_CONFIG_CALLEES else "exec"
                if callee == "next":
                    receiver = path[0] if path else ""
                    if receiver.isidentifier():
                        emit(
                            call_site_uid,
                            receiver,
                            kind="exec",
                            target_kind="object",
                            via="next",
                        )
                    else:
                        emit(
                            call_site_uid,
                            "",
                            kind="exec",
                            target_kind="object",
                            via="next",
                            wrapper_only=True,
                        )
                    continue

                topic = self._first_positional_string_literal(node)
                topic_is_identifier = bool(topic) and topic.isidentifier()
                if topic_is_identifier:
                    emit(
                        call_site_uid,
                        topic,
                        kind=kind,
                        target_kind="method",
                        via=callee,
                    )
                if kind == "config":
                    handler_name = self._second_positional_identifier(node)
                    if handler_name:
                        emit(
                            call_site_uid,
                            handler_name,
                            kind="config",
                            target_kind="handler",
                            via=callee,
                        )
                if not topic_is_identifier:
                    emit(
                        call_site_uid,
                        "",
                        kind=kind,
                        target_kind="method",
                        via=callee,
                        wrapper_only=True,
                    )
                continue

            if callee not in self._HOOK_REGISTER_CALLEE_NAMES:
                continue
            handler_name = self._first_positional_identifier(node)
            if not handler_name:
                continue
            via = "interceptors" if "interceptors" in path else callee
            emit(
                call_site_uid,
                handler_name,
                kind="config",
                target_kind="handler",
                via=via,
            )
        return out

    def extract_metadata_bridges(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[dict]:
        """Reflect-metadata producer/consumer facts keyed on the shared key constant.

        NestJS (and any ``reflect-metadata`` framework) wires a decorator to its
        scanner ONLY through a metadata KEY constant: the decorator runs
        ``Reflect.defineMetadata(KEY, …)`` / ``SetMetadata(KEY, …)`` (producer)
        and a context-creator / explorer runs ``Reflect.getMetadata(KEY, …)`` /
        ``reflector.getAllAndOverride(KEY, …)`` (consumer). No call / import /
        inheritance edge connects the two — the only structural link is that both
        sides import the same ``KEY`` from a constants module, so its resolved
        qualified name is the bridge identity (cf. ``hook_name`` in
        :meth:`extract_hooks`). The linker pairs ``define`` sites to ``read``
        sites by ``key_qn``; reads whose key has no producer drop out (precision).
        """
        if tree is None:
            tree = self._parse(source_code)
        module = module_name_from_path(file_path)
        import_bindings, _ = self._extract_import_bindings(source_code, file_path)
        out: list[dict] = []
        seen: set[tuple[str, str, str, str]] = set()

        def emit(site_uid: str, key_qn: str, key_name: str, role: str, via: str) -> None:
            if not site_uid or not key_qn:
                return
            key = (site_uid, key_qn, role, via)
            if key in seen:
                return
            seen.add(key)
            out.append(
                {
                    "site_uid": site_uid,
                    "key_qn": key_qn,
                    "key_name": key_name,
                    "role": role,
                    "via": via,
                    "file_path": file_path,
                }
            )

        for node in self._iter_nodes(tree.root_node):
            if node.type != "call_expression":
                continue
            func = node.child_by_field_name("function")
            if func is None:
                continue
            role = ""
            via = ""
            require_constant_key = False
            key_arg_index = 0
            if func.type == "member_expression":
                path = self._member_expression_path(func)
                if len(path) < 2:
                    continue
                head, callee = path[0], path[-1]
                if head == "Reflect" and callee in self._REFLECT_DEFINE_METHODS:
                    role, via = "define", f"Reflect.{callee}"
                elif head == "Reflect" and callee in self._REFLECT_READ_METHODS:
                    role, via = "read", f"Reflect.{callee}"
                elif callee in self._REFLECTOR_DISTINCT_METHODS:
                    role, via = "read", f"reflector.{callee}"
                elif callee in self._REFLECTOR_GENERIC_METHODS:
                    role, via, require_constant_key = "read", f"reflector.{callee}", True
                elif callee in self._METADATA_READ_HELPERS:
                    role, via, require_constant_key = "read", callee, True
                    key_arg_index = 2
                else:
                    continue
            elif (
                func.type == "identifier" and self._node_text(func) in self._METADATA_DEFINE_HELPERS
            ):
                role, via = "define", self._node_text(func)
            else:
                continue

            key_arg = self._nth_positional_argument(node, key_arg_index)
            key_qn, key_name, is_constant = self._resolve_metadata_key(
                key_arg, import_bindings, module
            )
            if not key_qn:
                continue
            if require_constant_key and not is_constant:
                continue
            owner = self._enclosing_symbol_owner(node)
            if owner is None:
                continue
            bridge_site_uid = self._caller_uid_for_owner(owner, source_code, file_path)
            if not bridge_site_uid:
                continue
            emit(bridge_site_uid, key_qn, key_name, role, via)
        return out

    def extract_http_endpoints(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """HTTP client/server endpoint facts for cross-language graph bridges.

        Emits ``implement`` facts for route handlers (NestJS decorators, Express
        ``app.get('/path', handler)``) and ``call`` facts for literal-path HTTP
        clients (``post('/ask')``, ``fetch('/health')``, ``axios.post(...)``).
        """
        from context_engine.indexer.http_endpoint import (
            HTTP_CLIENT_CALLEES,
            HTTP_ROUTE_REGISTER_CALLEES,
            combine_controller_path,
            normalize_http_method,
            normalize_http_path,
        )

        if tree is None:
            tree = self._parse(source_code)
        out: list[dict] = []
        seen: set[tuple[str, str, str, str]] = set()
        controller_prefix_by_class: dict[str, str] = {}

        def emit(site_uid: str, method: str, path: str, role: str, via: str) -> None:
            normalized_method = normalize_http_method(method)
            normalized_path = normalize_http_path(path)
            if not site_uid or not normalized_method or not normalized_path:
                return
            key = (site_uid, normalized_method, normalized_path, role)
            if key in seen:
                return
            seen.add(key)
            out.append(
                {
                    "site_uid": site_uid,
                    "method": normalized_method,
                    "path": normalized_path,
                    "role": role,
                    "via": via,
                    "file_path": file_path,
                }
            )

        for deco in self._iter_nodes(tree.root_node):
            if deco.type != "decorator":
                continue
            base = self._decorator_base_name(deco)
            if base not in self._CONTROLLER_DECORATORS:
                continue
            decorated = self._decorated_node_from_decorator(deco, class_only=True)
            if decorated is None:
                continue
            class_uid = self._uid_for_node(decorated, source_code, file_path)
            prefix = self._http_path_from_decorator(deco)
            if class_uid and prefix is not None:
                controller_prefix_by_class[class_uid] = prefix

        for deco in self._iter_nodes(tree.root_node):
            if deco.type != "decorator":
                continue
            base = self._decorator_base_name(deco)
            method = normalize_http_method(base)
            if not method:
                continue
            decorated = self._decorated_node_from_decorator(deco)
            if decorated is None:
                continue
            site_uid = self._uid_for_node(decorated, source_code, file_path)
            if not site_uid:
                continue
            subpath = self._http_path_from_decorator(deco) or ""
            class_uid = self._enclosing_class_uid(decorated, source_code, file_path)
            prefix = controller_prefix_by_class.get(class_uid, "")
            path = (
                combine_controller_path(prefix, subpath)
                if prefix
                else normalize_http_path(subpath or "/")
            )
            emit(site_uid, method, path, "implement", f"@{base}")

        for node in self._iter_nodes(tree.root_node):
            if node.type != "call_expression":
                continue
            func = node.child_by_field_name("function")
            if func is None:
                continue

            if func.type == "member_expression":
                path_parts = self._member_expression_path(func)
                if not path_parts:
                    continue
                callee = path_parts[-1]
                if callee in HTTP_ROUTE_REGISTER_CALLEES:
                    method = normalize_http_method(callee)
                    route_path = self._http_path_from_call_argument(node, 0)
                    if not method or not route_path:
                        continue
                    handler_uid = self._http_handler_uid_from_call(node, source_code, file_path)
                    if handler_uid:
                        emit(
                            handler_uid,
                            method,
                            route_path,
                            "implement",
                            ".".join(path_parts),
                        )
                    continue
                if callee in HTTP_CLIENT_CALLEES or (
                    len(path_parts) >= 2 and path_parts[-2] in {"axios", "http", "HttpService"}
                ):
                    method = normalize_http_method(callee)
                    if callee == "fetch":
                        method = method or "GET"
                    if not method:
                        continue
                    route_path = self._http_path_from_call_argument(node, 0)
                    if not route_path:
                        continue
                    owner = self._enclosing_symbol_owner(node)
                    if owner is None:
                        continue
                    site_uid = self._http_call_site_uid(node, source_code, file_path)
                    emit(site_uid, method, route_path, "call", ".".join(path_parts))
                continue

            if func.type == "identifier":
                callee = self._node_text(func)
                if callee == "fetch":
                    method = "GET"
                elif callee in HTTP_CLIENT_CALLEES:
                    method = normalize_http_method(callee)
                else:
                    continue
                route_path = self._http_path_from_call_argument(node, 0)
                if not route_path:
                    continue
                owner = self._enclosing_symbol_owner(node)
                if owner is None:
                    continue
                site_uid = self._http_call_site_uid(node, source_code, file_path)
                emit(site_uid, method, route_path, "call", callee)

        return out

    def _http_call_site_uid(self, node, source_code: str, file_path: str) -> str:
        owner = self._enclosing_symbol_owner(node)
        if owner is None:
            return ""
        return self._caller_uid_for_owner(owner, source_code, file_path) or ""

    def _enclosing_class_uid(self, node, source_code: str, file_path: str) -> str:
        parent = node.parent
        while parent:
            if parent.type in self._CLASS_DECL_TYPES:
                return self._uid_for_node(parent, source_code, file_path)
            parent = parent.parent
        return ""

    def _http_path_from_decorator(self, deco) -> str | None:
        for child in deco.children:
            if child.type != "call_expression":
                continue
            path = self._http_path_from_call_argument(child, 0)
            return path if path is not None else ""
        return ""

    def _http_path_from_call_argument(self, call_node, index: int) -> str:
        from context_engine.indexer.http_endpoint import (
            normalize_http_path,
            path_from_template_text,
        )

        arg = self._nth_positional_argument(call_node, index)
        if arg is None:
            return ""
        if arg.type == "string":
            return normalize_http_path(self._string_literal_text(arg))
        if arg.type == "template_string":
            fragments: list[str] = []
            for child in arg.children:
                if child.type == "string_fragment":
                    fragments.append(self._node_text(child))
            return path_from_template_text("".join(fragments))
        return ""

    def _http_handler_uid_from_call(self, call_node, source_code: str, file_path: str) -> str:
        handler_arg = self._nth_positional_argument(call_node, 1)
        if handler_arg is None:
            return ""
        if handler_arg.type == "identifier":
            return self._uid_for_symbol_name(handler_arg, source_code, file_path)
        owner = self._enclosing_symbol_owner(call_node)
        if owner is None:
            return ""
        return self._caller_uid_for_owner(owner, source_code, file_path) or ""

    def _uid_for_symbol_name(self, name_node, source_code: str, file_path: str) -> str:
        name = self._node_text(name_node)
        if not name:
            return ""
        tree = self._parse(source_code)
        for node in self._iter_nodes(tree.root_node):
            if node.type not in {"function_declaration", "method_definition"}:
                continue
            name_field = node.child_by_field_name("name")
            if name_field is None or self._node_text(name_field) != name:
                continue
            return self._uid_for_node(node, source_code, file_path)
        return ""

    def _resolve_metadata_key(
        self, arg, import_bindings: dict[str, str], module: str
    ) -> tuple[str, str, bool]:
        """Resolve a metadata-key argument to ``(qualified_name, bare_name, is_constant)``.

        ``is_constant`` flags an imported/module constant identifier (or member of
        one) — the shape that gates generic ``reflector.get`` reads from Map/Array
        false positives. String-literal keys are namespaced under ``str:``.
        """
        if arg is None:
            return "", "", False
        if arg.type == "identifier":
            name = self._node_text(arg)
            return (
                self._resolve_type_name(name, import_bindings, module),
                name,
                name in import_bindings,
            )
        if arg.type == "member_expression":
            path = self._member_expression_path(arg)
            if len(path) < 2:
                return "", "", False
            head = path[0]
            base = import_bindings.get(head, f"{module}.{head}")
            tail = ".".join(path[1:])
            return f"{base}.{tail}", path[-1], head in import_bindings
        if arg.type == "string":
            value = self._string_literal_text(arg)
            if not value:
                return "", "", False
            return f"str:{value}", value, False
        return "", "", False

    def _member_expression_path(self, member_node) -> list[str]:
        parts: list[str] = []
        node = member_node
        while node is not None and node.type == "member_expression":
            prop = node.child_by_field_name("property")
            if prop is None:
                break
            parts.append(self._node_text(prop))
            node = node.child_by_field_name("object")
        if node is not None and node.type in {"identifier", "this"}:
            parts.append(self._node_text(node))
        parts.reverse()
        return parts

    def _nth_positional_argument(self, call_node, index: int):
        args = call_node.child_by_field_name("arguments")
        if args is None:
            return None
        pos = 0
        for child in args.named_children:
            if child.type == ",":
                continue
            if pos == index:
                return child
            pos += 1
        return None

    def _first_positional_identifier(self, call_node) -> str:
        arg = self._nth_positional_argument(call_node, 0)
        if arg is not None and arg.type == "identifier":
            return self._node_text(arg)
        return ""

    def _second_positional_identifier(self, call_node) -> str:
        arg = self._nth_positional_argument(call_node, 1)
        if arg is not None and arg.type == "identifier":
            return self._node_text(arg)
        return ""

    def _first_positional_string_literal(self, call_node) -> str:
        arg = self._nth_positional_argument(call_node, 0)
        if arg is not None and arg.type == "string":
            return self._string_literal_text(arg)
        return ""

    def extract_proxy_bindings(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """Native ``Proxy`` bindings: ``const x = new Proxy(target, handler)``.

        Each top-level binding anchors a ``proxy_binding`` Symbol + ``PROXY_OF``
        edge to the statically visible target type (first constructor argument).
        Mirrors Python lazy-proxy extraction but for the ECMAScript ``Proxy``
        constructor instead of ``LocalProxy``/``Proxy(get_current_app)`` idioms.
        """
        if tree is None:
            tree = self._parse(source_code)
        module = module_name_from_path(file_path)
        import_bindings, _ = self._extract_import_bindings(source_code, file_path)
        symbols = self.extract_symbols(source_code, file_path, tree=tree)
        local_classes = {s.name for s in symbols if s.kind == "class"}
        out: list[dict] = []
        seen: set[str] = set()

        for decl in self._iter_nodes(tree.root_node):
            if decl.type != "variable_declarator":
                continue
            if not self._is_top_level_variable_declarator(decl):
                continue
            name_node = decl.child_by_field_name("name")
            value = decl.child_by_field_name("value")
            if name_node is None or value is None or value.type != "new_expression":
                continue
            ctor = value.child_by_field_name("constructor")
            if ctor is None or ctor.type != "identifier" or self._node_text(ctor) != "Proxy":
                continue
            var_name = self._node_text(name_node)
            if var_name in seen:
                continue
            resolved = self._resolve_proxy_target_arg(
                value,
                import_bindings=import_bindings,
                local_classes=local_classes,
                module=module,
            )
            if resolved is None:
                continue
            _type_name, target_qn, confidence = resolved
            seen.add(var_name)
            out.append(
                {
                    "proxy_uid": self._uid(file_path, var_name),
                    "proxy_name": var_name,
                    "proxy_qualified_name": f"{module}.{var_name}",
                    "target_type": target_qn,
                    "target_source": "native_proxy",
                    "wrapped_callable": "",
                    "confidence": confidence,
                    "file_path": file_path,
                }
            )
        return out

    def _resolve_proxy_target_arg(
        self,
        new_expr,
        *,
        import_bindings: dict[str, str],
        local_classes: set[str],
        module: str,
    ) -> tuple[str, str, float] | None:
        args = new_expr.child_by_field_name("arguments")
        if args is None:
            return None
        for arg in args.named_children:
            if arg.type == ",":
                continue
            return self._resolve_proxy_target_node(
                arg,
                import_bindings=import_bindings,
                local_classes=local_classes,
                module=module,
            )
        return None

    def _resolve_proxy_target_node(
        self,
        node,
        *,
        import_bindings: dict[str, str],
        local_classes: set[str],
        module: str,
    ) -> tuple[str, str, float] | None:
        if node.type == "identifier":
            resolved = self._resolve_new_callee(
                node,
                import_bindings=import_bindings,
                local_classes=local_classes,
                module=module,
            )
            if resolved is None:
                return None
            type_name, type_qn, _is_external = resolved
            return type_name, type_qn, 0.95
        if node.type == "as_expression":
            type_node = next(
                (c for c in node.named_children if c.type == "type_identifier"),
                None,
            )
            if type_node is None:
                return None
            type_name = self._node_text(type_node)
            type_qn = self._resolve_type_name(type_name, import_bindings, module)
            return type_name, type_qn, 0.9
        if node.type == "type_identifier":
            type_name = self._node_text(node)
            type_qn = self._resolve_type_name(type_name, import_bindings, module)
            return type_name, type_qn, 0.9
        return None

    def extract_attr_accesses(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """READS_ATTR / WRITES_ATTR from ``this.member`` in method bodies."""
        if tree is None:
            tree = self._parse(source_code)
        module = module_name_from_path(file_path)
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
            if fn.type not in ("method_definition", "function_declaration"):
                continue
            body = fn.child_by_field_name("body")
            name_node = fn.child_by_field_name("name")
            if body is None or name_node is None:
                continue
            accessor_uid = self._uid_for_node(fn, source_code, file_path)
            accessor_name = self._node_text(name_node)
            class_name = self._enclosing_class_name(fn)
            receiver_qn = f"{module}.{class_name}" if class_name else ""

            for node in self._iter_nodes(body):
                if node.type in ("method_definition", "function_declaration", "arrow_function"):
                    if node is not fn:
                        continue
                if node.type == "member_expression":
                    obj = node.child_by_field_name("object")
                    prop = node.child_by_field_name("property")
                    if obj is None or prop is None:
                        continue
                    if not self._is_this_receiver(obj):
                        continue
                    parent = node.parent
                    if parent is not None and parent.type == "call_expression":
                        fn_node = parent.child_by_field_name("function")
                        if fn_node is not None and fn_node.start_byte == node.start_byte:
                            continue
                    if parent is not None and parent.type == "assignment_expression":
                        lhs = parent.child_by_field_name("left")
                        if lhs is not None and lhs.start_byte == node.start_byte:
                            continue
                    attr_name = self._node_text(prop)
                    attr_qn = f"{receiver_qn}.{attr_name}" if receiver_qn else ""
                    emit(accessor_uid, accessor_name, attr_name, attr_qn, "read")
                elif node.type == "assignment_expression":
                    left = node.child_by_field_name("left")
                    if left is None or left.type != "member_expression":
                        continue
                    obj = left.child_by_field_name("object")
                    prop = left.child_by_field_name("property")
                    if obj is None or prop is None:
                        continue
                    if not self._is_this_receiver(obj):
                        continue
                    attr_name = self._node_text(prop)
                    attr_qn = f"{receiver_qn}.{attr_name}" if receiver_qn else ""
                    emit(accessor_uid, accessor_name, attr_name, attr_qn, "write")

        return out

    def extract_property_api_edges(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[ClassApiEdge]:
        """HAS_API edges from static ``owner.prop = function`` assignments."""
        edges: list[ClassApiEdge] = []
        seen: set[tuple[str, str]] = set()

        def add(owner: str, prop: str, method_name: str) -> None:
            owner = owner.strip()
            method_name = (method_name or prop).strip()
            if not owner or not method_name or owner in {"exports", "module"}:
                return
            key = (owner, method_name)
            if key in seen:
                return
            seen.add(key)
            edges.append(
                ClassApiEdge(
                    class_uid=self._uid(file_path, owner),
                    method_uid=self._property_method_uid(file_path, owner, method_name),
                    edge_type="HAS_API",
                )
            )

        for match in self._CHAINED_PROPERTY_FUNC_API_RE.finditer(source_code):
            add(match.group(1), match.group(2), match.group(2))
            add(match.group(3), match.group(4), match.group(5) or match.group(4))
        for match in self._PROPERTY_FUNC_API_RE.finditer(source_code):
            add(match.group(1), match.group(2), match.group(3) or match.group(2))
        for match in self._PROPERTY_ARROW_API_RE.finditer(source_code):
            add(match.group(1), match.group(2), match.group(2))
        return edges

    def _reexport_resolver(self) -> TsReexportResolver:
        cached = getattr(self, "_reexport_resolver_instance", None)
        if cached is None:
            cached = TsReexportResolver(self)
            self._reexport_resolver_instance = cached
        return cached

    def extract_reexports(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """RE_EXPORTS from barrel ``index.ts`` / ``index.tsx`` export-from statements."""
        if Path(file_path).name not in ("index.ts", "index.tsx"):
            return []
        if tree is None:
            tree = self._parse(source_code)
        surface = self._reexport_resolver()._surface_from_source(
            source_code,
            file_path,
            tree=tree,
            depth=0,
        )
        return [
            {
                "init_file": file_path,
                "export_name": export_name,
                "export_qualified_name": export_qn,
            }
            for export_name, export_qn in surface.items()
        ]

    def extract_symbol_aliases(self, source_code: str, file_path: str, *, tree=None) -> list[dict]:
        """REFERENCES from renamed export-from surfaces (``export {{ X as Y }} from``)."""
        if tree is None:
            tree = self._parse(source_code)
        out: list[dict] = []
        seen: set[tuple[str, str, str, str]] = set()

        for stmt in self._iter_nodes(tree.root_node):
            if stmt.type != "export_statement":
                continue
            source_node = next((c for c in stmt.children if c.type == "string"), None)
            if source_node is None:
                continue
            import_source = self._string_literal_text(source_node)
            if not import_source:
                continue
            target_module = self._normalize_import_source(file_path, import_source)
            export_clause = next(
                (c for c in stmt.children if c.type == "export_clause"),
                None,
            )
            if export_clause is None:
                continue
            for spec in export_clause.named_children:
                if spec.type != "export_specifier":
                    continue
                name_node = spec.child_by_field_name("name")
                alias_node = spec.child_by_field_name("alias")
                if name_node is None or alias_node is None:
                    continue
                original = self._node_text(name_node)
                alias = self._node_text(alias_node)
                if not original or not alias or original == alias or original == "default":
                    continue
                target_qn = f"{target_module}.{original}"
                key = (alias, original, target_qn, "ts_export_alias")
                if key in seen:
                    continue
                seen.add(key)
                line = source_code.count("\n", 0, spec.start_byte) + 1
                out.append(
                    {
                        "source_uid": self._uid(file_path, alias),
                        "source_name": alias,
                        "target_name": original,
                        "target_qualified_name": target_qn,
                        "file_path": file_path,
                        "kind": "ts_export_alias",
                        "confidence": 0.85,
                        "line": line,
                        "match_by_name": False,
                    }
                )
        return out

    def _ts_classify_identifier_call_node(
        self,
        func_node,
        *,
        source_code: str,
        call_at_byte: int,
        import_bindings: dict[str, str],
        by_name: dict[str, list],
        scope_graph,
    ) -> tuple[str, str, str, float, str, str | None, bool, str]:
        call_name = source_code[func_node.start_byte : func_node.end_byte]
        rel_type, tier, confidence, resolver, callee_uid, skip_call, callee_qn = (
            self._classify_identifier_call(
                call_name,
                import_bindings=import_bindings,
                by_name=by_name,
                scope_graph=scope_graph,
                at_byte=call_at_byte,
            )
        )
        return call_name, rel_type, tier, confidence, resolver, callee_uid, skip_call, callee_qn

    def _ts_classify_member_call_node(
        self,
        func_node,
        *,
        parent,
        source_code: str,
        call_at_byte: int,
        import_bindings: dict[str, str],
        by_name: dict[str, list],
        scope_graph,
    ) -> tuple[str, str, str, float, str, str | None, bool, str] | None:
        named_children = [child for child in func_node.children if child.is_named]
        if len(named_children) < 2:
            return None
        receiver_node = named_children[0]
        method_node = named_children[-1]
        receiver_text = source_code[receiver_node.start_byte : receiver_node.end_byte]
        call_name = source_code[method_node.start_byte : method_node.end_byte]
        rel_type, tier, confidence, resolver, callee_uid, skip_call, callee_qn = (
            self._classify_member_call(
                receiver_text,
                call_name,
                parent=parent,
                import_bindings=import_bindings,
                by_name=by_name,
                scope_graph=scope_graph,
                at_byte=call_at_byte,
            )
        )
        return call_name, rel_type, tier, confidence, resolver, callee_uid, skip_call, callee_qn

    def _ts_apply_imported_callee_qn(
        self,
        call: dict,
        *,
        func_node,
        call_name: str,
        rel_type: str,
        callee_qn: str,
        import_bindings: dict[str, str],
        source_code: str,
    ) -> None:
        if rel_type != "CALLS_IMPORTED":
            return
        if callee_qn:
            call["callee_qualified_name"] = callee_qn
            return
        if func_node.type == "identifier":
            call["callee_qualified_name"] = import_bindings[call_name]
            return
        receiver_node = [child for child in func_node.children if child.is_named][0]
        receiver_text = source_code[receiver_node.start_byte : receiver_node.end_byte]
        base = import_bindings.get(receiver_text, "")
        if base:
            call["callee_qualified_name"] = f"{base}.{call_name}"

    def _ts_call_from_capture(
        self,
        node,
        tag: str,
        *,
        source_code: str,
        file_path: str,
        import_bindings: dict[str, str],
        by_name: dict[str, list],
        scope_graph,
    ) -> dict | None:
        if tag != "call":
            return None

        func_node = node.child_by_field_name("function")
        if func_node is None and node.type == "new_expression":
            func_node = node.child_by_field_name("constructor")
        if func_node is None:
            return None

        parent = self._enclosing_symbol_owner(node)
        if parent is None:
            return None

        caller_uid = self._caller_uid_for_owner(parent, source_code, file_path)
        if not caller_uid:
            return None

        call_at_byte = node.start_byte
        call_kind = "construct" if node.type == "new_expression" else "call"
        classified = None
        if func_node.type == "identifier":
            classified = self._ts_classify_identifier_call_node(
                func_node,
                source_code=source_code,
                call_at_byte=call_at_byte,
                import_bindings=import_bindings,
                by_name=by_name,
                scope_graph=scope_graph,
            )
        elif func_node.type == "member_expression":
            classified = self._ts_classify_member_call_node(
                func_node,
                parent=parent,
                source_code=source_code,
                call_at_byte=call_at_byte,
                import_bindings=import_bindings,
                by_name=by_name,
                scope_graph=scope_graph,
            )
        else:
            return None
        if classified is None:
            return None

        call_name, rel_type, tier, confidence, resolver, callee_uid, skip_call, callee_qn = (
            classified
        )
        if skip_call or callee_uid == caller_uid:
            return None

        call = {
            "caller_uid": caller_uid,
            "callee_name": call_name,
            "rel_type": rel_type,
            "tier": tier,
            "confidence": confidence,
            "resolver": resolver,
            "call_site_line": node.start_point[0] + 1,
            "call_kind": call_kind,
        }
        if callee_uid:
            call["callee_uid"] = callee_uid
        self._ts_apply_imported_callee_qn(
            call,
            func_node=func_node,
            call_name=call_name,
            rel_type=rel_type,
            callee_qn=callee_qn,
            import_bindings=import_bindings,
            source_code=source_code,
        )
        return call

    def _append_resolved_ts_calls(
        self,
        calls: list[dict],
        captures,
        *,
        source_code: str,
        file_path: str,
        import_bindings: dict[str, str],
        by_name: dict[str, list],
        scope_graph,
    ) -> None:
        for node, tag in captures:
            call = self._ts_call_from_capture(
                node,
                tag,
                source_code=source_code,
                file_path=file_path,
                import_bindings=import_bindings,
                by_name=by_name,
                scope_graph=scope_graph,
            )
            if call is not None:
                calls.append(call)

    def extract_calls_from_source(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[dict]:
        """Extract TypeScript/JavaScript calls with ambiguity-gated resolution.

        Unresolved identifier calls and member calls on ``any``/unknown/ambient
        receivers emit ``CALLS_GUESS`` for workspace-wide unique-name linking
        (mirrors Python ``CALLS_GUESS`` + ``link_calls`` name fallback).
        """
        if tree is None:
            tree = self._parse(source_code)

        captures = flatten_ts_query_captures(
            self.language,
            """
            (call_expression) @call
            (new_expression) @call
            """,
            tree.root_node,
        )

        symbols = self.extract_symbols(source_code, file_path, tree=tree)
        by_name: dict[str, list] = {}
        for symbol in symbols:
            by_name.setdefault(symbol.name, []).append(symbol)

        import_bindings, module_aliases = self._extract_import_bindings(source_code, file_path)
        scope_graph = TsScopeGraph.build(
            tree.root_node,
            import_bindings=import_bindings,
            node_text=self._node_text,
            normalize_require=lambda path: self._normalize_import_source(file_path, path),
        )
        calls: list[dict] = []
        self._append_resolved_ts_calls(
            calls,
            captures,
            source_code=source_code,
            file_path=file_path,
            import_bindings=import_bindings,
            by_name=by_name,
            scope_graph=scope_graph,
        )

        self._append_exported_initializer_call_fallbacks(
            calls,
            source_code,
            file_path,
            import_bindings,
            symbols,
        )
        self._append_symbol_body_call_fallbacks(
            calls,
            source_code,
            file_path,
            symbols,
            import_bindings,
        )
        return calls

    def _append_exported_initializer_call_fallbacks(
        self,
        calls: list[dict],
        source_code: str,
        file_path: str,
        import_bindings: dict[str, str],
        symbols: list[SymbolMetadata],
    ) -> None:
        """Recover ``export const x = call(...)`` edges when TS AST owner recovery fails."""
        seen = self._call_dedupe_keys(calls)
        for match in self._EXPORTED_CALL_INITIALIZER_RE.finditer(source_code):
            caller_name = match.group(1)
            callee_name = match.group(2)
            if not caller_name or not callee_name:
                continue
            rel_type, callee_qn = self._classify_text_fallback_call(
                callee_name,
                import_bindings,
                symbols,
            )
            self._append_text_fallback_call(
                calls,
                seen,
                caller_uid=self._uid(file_path, caller_name),
                callee_name=callee_name,
                rel_type=rel_type,
                line=source_code.count("\n", 0, match.start(2)) + 1,
                callee_qn=callee_qn,
                resolver="ts-export-initializer-fallback-v1",
                imported_confidence=0.9,
                scoped_confidence=0.9,
            )

    def _append_symbol_body_call_fallbacks(
        self,
        calls: list[dict],
        source_code: str,
        file_path: str,
        symbols: list[SymbolMetadata],
        import_bindings: dict[str, str],
    ) -> None:
        """Recover calls inside large exported TS bodies when tree-sitter loses owners."""
        known_names = {symbol.name for symbol in symbols} | set(import_bindings)
        if not known_names:
            return
        existing_callers = {str(call.get("caller_uid", "")) for call in calls}
        seen = self._call_dedupe_keys(calls)
        lines = source_code.splitlines(keepends=True)
        line_offsets: list[int] = []
        offset = 0
        for src_line in lines:
            line_offsets.append(offset)
            offset += len(src_line)

        for symbol in symbols:
            if symbol.kind == "class":
                continue
            if symbol.end_line <= symbol.start_line:
                continue
            if symbol.signature_status != "fallback_export" and symbol.uid in existing_callers:
                continue
            start_idx = max(0, symbol.start_line - 1)
            end_idx = min(len(lines), symbol.end_line)
            if start_idx >= end_idx:
                continue
            body_start = line_offsets[start_idx]
            body = "".join(lines[start_idx:end_idx])
            caller_uid = str(symbol.uid)
            for callee_name, name_pos in iter_typescript_body_call_fallback_names(body):
                if (
                    not callee_name
                    or callee_name == symbol.name
                    or callee_name in self._BODY_CALL_FALLBACK_SKIP
                    or callee_name not in known_names
                ):
                    continue
                prefix = body[:name_pos]
                previous = prefix.rstrip()[-1:] if prefix.rstrip() else ""
                if previous in {".", ":"}:
                    continue
                if re.search(r"\bfunction\s+$", prefix[-32:]):
                    continue

                rel_type, callee_qn = self._classify_text_fallback_call(
                    callee_name,
                    import_bindings,
                    symbols,
                )
                self._append_text_fallback_call(
                    calls,
                    seen,
                    caller_uid=caller_uid,
                    callee_name=callee_name,
                    rel_type=rel_type,
                    line=source_code.count("\n", 0, body_start + name_pos) + 1,
                    callee_qn=callee_qn,
                    resolver="ts-symbol-body-fallback-v1",
                    imported_confidence=0.75,
                    scoped_confidence=0.75,
                )

    def _extract_import_bindings(
        self, source_code: str, file_path: str
    ) -> tuple[dict[str, str], set[str]]:
        bindings, module_aliases = collect_js_ts_import_bindings(
            source_code, file_path, self._normalize_import_source
        )
        TsReexportResolver.enrich_bindings(
            bindings,
            file_path,
            self._reexport_resolver().resolve_binding_qn,
        )
        return bindings, module_aliases

    def _normalize_import_source(self, file_path: str, source: str) -> str:
        return resolve_import_module_name(
            file_path,
            source,
            module_for_resolved=self._module_for_resolved_path,
        )

    @staticmethod
    def _module_for_resolved_path(resolved: Path) -> str | None:
        """Module qname for a resolved (suffix-less or suffixed) import path, or None."""
        candidates = [resolved]
        if resolved.suffix in {".ts", ".tsx", ".js", ".jsx"}:
            candidates.append(resolved)
        else:
            # ``Path.with_suffix(".ts")`` turns ``shared.utils`` into
            # ``shared.ts``. TypeScript projects commonly import dotted
            # basenames like ``shared.utils`` or ``module-metadata.interface``,
            # so append language suffixes to the full unresolved path first.
            candidates.extend(Path(f"{resolved}{suffix}") for suffix in (".ts", ".tsx"))
            candidates.extend([resolved.with_suffix(".ts"), resolved.with_suffix(".tsx")])
        candidates.extend([resolved / "index.ts", resolved / "index.tsx"])
        for candidate in candidates:
            if candidate.exists():
                return module_name_from_path(str(candidate))
        return None

    def _append_module_fallback_symbol(
        self,
        symbols: list[SymbolMetadata],
        existing_names: set[str],
        file_path: str,
        source_code: str,
        *,
        start_offset: int,
        name: str | None,
        kind: str,
    ) -> None:
        if not name or name in existing_names:
            return
        start_line, end_line, content = self._fallback_symbol_span(source_code, start_offset)
        signature = normalize_signature(f"{name}()->_", self.language_name)
        qualified_name = f"{module_name_from_path(file_path)}.{name}"
        symbols.append(
            SymbolMetadata(
                uid=compute_uid(qualified_name, signature, self.language_name),
                name=name,
                kind=kind,
                start_line=start_line,
                end_line=end_line,
                content_hash=self._hash(content),
                file_path=file_path,
                qualified_name=qualified_name,
                signature=signature,
                signature_hash=signature_hash(signature, self.language_name),
                signature_status="fallback_export",
                language=self.language_name,
            )
        )
        existing_names.add(name)

    @staticmethod
    def _call_dedupe_keys(calls: list[dict]) -> set[tuple]:
        return {
            (
                call.get("caller_uid"),
                call.get("callee_name"),
                call.get("rel_type"),
                call.get("call_site_line"),
                call.get("callee_qualified_name", ""),
            )
            for call in calls
        }

    @staticmethod
    def _classify_text_fallback_call(
        callee_name: str,
        import_bindings: dict[str, str],
        symbols: list[SymbolMetadata],
    ) -> tuple[str, str]:
        if callee_name in import_bindings:
            return "CALLS_IMPORTED", import_bindings.get(callee_name, "")
        if len([symbol for symbol in symbols if symbol.name == callee_name]) == 1:
            return "CALLS_SCOPED", ""
        return "CALLS_GUESS", ""

    @staticmethod
    def _build_text_fallback_call(
        *,
        caller_uid: str,
        callee_name: str,
        rel_type: str,
        line: int,
        callee_qn: str,
        resolver: str,
        imported_confidence: float,
        scoped_confidence: float,
        guess_confidence: float = 0.4,
    ) -> dict:
        if rel_type == "CALLS_IMPORTED":
            confidence = imported_confidence
        elif rel_type == "CALLS_SCOPED":
            confidence = scoped_confidence
        else:
            confidence = guess_confidence

        call = {
            "caller_uid": caller_uid,
            "callee_name": callee_name,
            "rel_type": rel_type,
            "tier": (
                "imported"
                if rel_type == "CALLS_IMPORTED"
                else ("scoped" if rel_type == "CALLS_SCOPED" else "guess")
            ),
            "confidence": confidence,
            "resolver": resolver if rel_type != "CALLS_GUESS" else "ts-ambiguity-gate-v1",
            "call_site_line": line,
        }
        if callee_qn:
            call["callee_qualified_name"] = callee_qn
        return call

    def _append_text_fallback_call(
        self,
        calls: list[dict],
        seen: set[tuple],
        *,
        caller_uid: str,
        callee_name: str,
        rel_type: str,
        line: int,
        callee_qn: str,
        resolver: str,
        imported_confidence: float,
        scoped_confidence: float,
    ) -> None:
        key = (caller_uid, callee_name, rel_type, line, callee_qn)
        if key in seen:
            return
        calls.append(
            self._build_text_fallback_call(
                caller_uid=caller_uid,
                callee_name=callee_name,
                rel_type=rel_type,
                line=line,
                callee_qn=callee_qn,
                resolver=resolver,
                imported_confidence=imported_confidence,
                scoped_confidence=scoped_confidence,
            )
        )
        seen.add(key)

    @staticmethod
    def _fallback_symbol_span(
        source_code: str,
        start_offset: int,
    ) -> tuple[int, int, str]:
        """Best-effort line span for exported symbol text fallbacks.

        For simple `export const` wrappers we keep the single line. For exported
        functions, we try to capture the full brace-delimited body so prompt
        resolution can recover implementation context even when tree-sitter is
        in error-recovery mode.
        """
        line_start = source_code.rfind("\n", 0, start_offset) + 1
        start_line = source_code.count("\n", 0, line_start) + 1
        line_end = source_code.find("\n", start_offset)
        search_from = start_offset
        close_paren = source_code.find(")", start_offset)
        if close_paren != -1 and (line_end == -1 or close_paren <= line_end + 200):
            search_from = close_paren
        brace_start = source_code.find("{", search_from)

        if brace_start == -1 or (line_end != -1 and brace_start > line_end):
            if line_end == -1:
                line_end = len(source_code)
            content = source_code[line_start:line_end]
            return start_line, start_line, content

        depth = 0
        end_offset = len(source_code)
        for idx in range(brace_start, len(source_code)):
            char = source_code[idx]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end_offset = idx + 1
                    break

        end_line = source_code.count("\n", 0, end_offset) + 1
        content = source_code[line_start:end_offset]
        return start_line, end_line, content

    def _emit_ts_type_annotations(self, node, kind: str, emit, owner) -> None:
        for child in self._iter_nodes(node):
            if child.type == "type_annotation":
                emit(owner, child, kind)

    def _type_ref_targets(
        self,
        type_node,
        import_bindings: dict[str, str],
        module: str,
        *,
        skip_names: set[str] | None = None,
    ) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        skip = self._TYPE_REF_SKIP_NAMES | set(skip_names or ())
        for node in self._iter_nodes(type_node):
            if node.type != "type_identifier":
                continue
            name = self._node_text(node)
            if not name or name in skip:
                continue
            qn = self._resolve_type_name(name, import_bindings, module)
            if qn in seen:
                continue
            seen.add(qn)
            out.append((name, qn))
        return out

    @staticmethod
    def _resolve_type_name(name: str, import_bindings: dict[str, str], module: str) -> str:
        return import_bindings.get(name, f"{module}.{name}")

    def _owner_uid_for_type_reference(
        self,
        owner,
        source_code: str,
        file_path: str,
    ) -> str:
        if owner.type in {"interface_declaration", "type_alias_declaration", "variable_declarator"}:
            name = self._owner_name_for_type_reference(owner, source_code)
            return self._uid(file_path, name) if name else ""
        return self._uid_for_node(owner, source_code, file_path)

    @staticmethod
    def _owner_name_for_type_reference(owner, source_code: str) -> str:
        name_node = owner.child_by_field_name("name")
        if name_node is None and owner.type == "variable_declarator":
            name_node = owner.child_by_field_name("name")
        if name_node is None:
            return ""
        return source_code[name_node.start_byte : name_node.end_byte]

    @staticmethod
    def _node_field_by_type(node, node_type: str):
        for child in node.named_children:
            if child.type == node_type:
                return child
        return None

    def _type_parameter_names(self, owner, source_code: str) -> set[str]:
        names: set[str] = set()
        type_params = next(
            (child for child in owner.named_children if child.type == "type_parameters"),
            None,
        )
        if type_params is None:
            return names
        for node in self._iter_nodes(type_params):
            if node.type != "type_parameter":
                continue
            name_node = next(
                (child for child in node.named_children if child.type == "type_identifier"),
                None,
            )
            if name_node is not None:
                names.add(source_code[name_node.start_byte : name_node.end_byte])
        return names

    @staticmethod
    def _iter_nodes(node):
        yield node
        for child in node.children:
            yield from TypeScriptAdapter._iter_nodes(child)

    @staticmethod
    def _node_text(node) -> str:
        return (node.text or b"").decode("utf-8")

    def _uid(self, file_path: str, name: str) -> str:
        qualified_name = f"{module_name_from_path(file_path)}.{name}"
        return compute_uid(qualified_name, f"{name}()->_", self.language_name)

    def _uid_for_node(self, node, source_code: str, file_path: str) -> str:
        qualified_name = qualified_name_for(node, source_code, file_path)
        if node.type == "method_definition":
            owner = self._object_literal_owner_variable(node)
            if owner is not None:
                owner_name_node = owner.child_by_field_name("name")
                method_name_node = node.child_by_field_name("name")
                if owner_name_node is not None and method_name_node is not None:
                    owner_name = self._node_text(owner_name_node)
                    method_name = self._node_text(method_name_node)
                    qualified_name = (
                        f"{module_name_from_path(file_path)}.{owner_name}.{method_name}"
                    )
        raw_signature, _ = signature_from_node(node, source_code, self.language_name)
        return compute_uid(qualified_name, raw_signature, self.language_name)

    @staticmethod
    def _imported_scope_graph_call(
        confidence: float = 0.85,
    ) -> tuple[str, str, float, str, None, bool, str]:
        return "CALLS_IMPORTED", "imported", confidence, "ts-scope-graph-v1", None, False, ""

    def _classify_identifier_call(
        self,
        call_name: str,
        *,
        import_bindings: dict[str, str],
        by_name: dict[str, list],
        scope_graph: TsScopeGraph,
        at_byte: int,
    ) -> tuple[str, str, float, str, str | None, bool, str]:
        """Classify a bare identifier call. Returns optional ``callee_qualified_name``."""
        if call_name in self._STANDARD_JS_GLOBALS:
            return "", "", 0.0, "", None, True, ""
        binding = scope_graph.resolve_name(call_name, at_byte)
        if binding is not None:
            return self._classify_bound_call(
                call_name,
                binding,
                import_bindings=import_bindings,
                by_name=by_name,
            )
        if call_name in import_bindings:
            return (
                "CALLS_IMPORTED",
                "imported",
                0.9,
                "ts-scope-v1",
                None,
                False,
                import_bindings[call_name],
            )
        matches = by_name.get(call_name, [])
        if len(matches) == 1:
            return (
                "CALLS_SCOPED",
                "scoped",
                0.9,
                "ts-scope-v1",
                str(matches[0].uid),
                False,
                "",
            )
        return "CALLS_GUESS", "guess", 0.4, "ts-ambiguity-gate-v1", None, False, ""

    def _classify_bound_call(
        self,
        call_name: str,
        binding: TsBinding,
        *,
        import_bindings: dict[str, str],
        by_name: dict[str, list],
    ) -> tuple[str, str, float, str, str | None, bool, str]:
        if binding.init_import_qn:
            if binding.require_alias:
                qn = binding.init_import_qn
            elif binding.kind == "destructure":
                qn = f"{binding.init_import_qn}.{call_name}"
            else:
                qn = binding.init_import_qn
            return (
                "CALLS_IMPORTED",
                "imported",
                0.9,
                "ts-scope-graph-v1",
                None,
                False,
                qn,
            )
        if binding.init_callee:
            if binding.init_callee in import_bindings:
                return (
                    "CALLS_IMPORTED",
                    "imported",
                    0.85,
                    "ts-scope-graph-v1",
                    None,
                    False,
                    import_bindings[binding.init_callee],
                )
            origin = by_name.get(binding.init_callee, [])
            if len(origin) == 1:
                return (
                    "CALLS_SCOPED",
                    "scoped",
                    0.9,
                    "ts-scope-graph-v1",
                    str(origin[0].uid),
                    False,
                    "",
                )
        if binding.kind == "import" and call_name in import_bindings:
            return (
                "CALLS_IMPORTED",
                "imported",
                0.9,
                "ts-scope-graph-v1",
                None,
                False,
                import_bindings[call_name],
            )
        matches = by_name.get(call_name, [])
        if len(matches) == 1:
            return (
                "CALLS_SCOPED",
                "scoped",
                0.9,
                "ts-scope-graph-v1",
                str(matches[0].uid),
                False,
                "",
            )
        if binding.kind in {"local", "function", "param", "destructure"}:
            return "CALLS_DIRECT", "direct", 1.0, "ts-scope-graph-v1", None, False, ""
        return "CALLS_GUESS", "guess", 0.4, "ts-ambiguity-gate-v1", None, False, ""

    def _classify_member_call(
        self,
        receiver_text: str,
        method_name: str,
        *,
        parent,
        import_bindings: dict[str, str],
        by_name: dict[str, list],
        scope_graph: TsScopeGraph,
        at_byte: int,
    ) -> tuple[str, str, float, str, str | None, bool, str]:
        if receiver_text in self._STANDARD_JS_GLOBALS:
            return "", "", 0.0, "", None, True, ""
        if receiver_text == "this":
            callee_uid = self._resolve_method_uid(parent, method_name, by_name)
            if callee_uid:
                return "CALLS_SCOPED", "scoped", 0.9, "ts-scope-v1", str(callee_uid), False, ""
            return "CALLS_GUESS", "guess", 0.4, "ts-ambiguity-gate-v1", None, False, ""
        if receiver_text in import_bindings:
            return "CALLS_IMPORTED", "imported", 0.9, "ts-scope-v1", None, False, ""
        recv_binding = scope_graph.resolve_name(receiver_text, at_byte)
        if recv_binding is not None:
            if recv_binding.init_import_qn:
                return self._imported_scope_graph_call()
            if recv_binding.init_callee and recv_binding.init_callee in import_bindings:
                return self._imported_scope_graph_call()
            if recv_binding.kind == "param" and recv_binding.ambiguous:
                return "CALLS_GUESS", "guess", 0.4, "ts-ambiguity-gate-v1", None, False, ""
            if recv_binding.kind in {"param", "local", "function", "destructure"}:
                return "CALLS_DYNAMIC", "dynamic", 0.7, "ts-scope-graph-v1", None, False, ""
            if not recv_binding.ambiguous:
                return "CALLS_DYNAMIC", "dynamic", 0.7, "ts-scope-graph-v1", None, False, ""
        return "CALLS_GUESS", "guess", 0.4, "ts-ambiguity-gate-v1", None, False, ""

    def _enclosing_symbol_owner(self, node):
        parent = node.parent
        while parent:
            if parent.type in self.parent_types:
                return parent
            if parent.type == "variable_declarator" and self._is_top_level_variable_declarator(
                parent
            ):
                return parent
            parent = parent.parent
        return None

    def _object_literal_owner_variable(self, node):
        parent = node.parent
        while parent:
            if parent.type == "variable_declarator" and self._is_top_level_variable_declarator(
                parent
            ):
                return parent
            if parent.type in self.parent_types:
                return None
            parent = parent.parent
        return None

    def _caller_uid_for_owner(self, node, source_code: str, file_path: str) -> str | None:
        if node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            if not name_node:
                return None
            name = source_code[name_node.start_byte : name_node.end_byte]
            return self._uid(file_path, name)
        return self._uid_for_node(node, source_code, file_path)

    @staticmethod
    def _is_top_level_variable_declarator(node) -> bool:
        parent = node.parent
        while parent:
            if parent.type == "program":
                return True
            if parent.type in {
                "function_declaration",
                "method_definition",
                "class_declaration",
                "abstract_class_declaration",
            }:
                return False
            parent = parent.parent
        return False

    @staticmethod
    def _is_this_receiver(obj) -> bool:
        if obj is None:
            return False
        if obj.type == "this":
            return True
        return obj.type == "identifier" and TypeScriptAdapter._node_text(obj) == "this"

    def _string_literal_text(self, node) -> str:
        text = self._node_text(node)
        if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
            return text[1:-1]
        return text

    def _enclosing_class_name(self, node) -> str:
        parent = node.parent
        while parent:
            if parent.type in self._CLASS_DECL_TYPES:
                name_node = parent.child_by_field_name("name")
                return self._node_text(name_node) if name_node is not None else ""
            parent = parent.parent
        return ""

    def _class_type_from_type_node(
        self,
        type_node,
        *,
        import_bindings: dict[str, str],
        module: str,
        local_classes: set[str],
    ) -> tuple[str, str, bool] | None:
        if type_node is None:
            return None
        if type_node.type == "type_annotation":
            return self._class_type_from_type_node(
                type_node.named_children[0] if type_node.named_children else None,
                import_bindings=import_bindings,
                module=module,
                local_classes=local_classes,
            )
        if type_node.type == "type_query":
            inner = type_node.named_children[0] if type_node.named_children else None
            if inner is None:
                return None
            if inner.type == "identifier":
                return self._resolve_new_callee(
                    inner,
                    import_bindings=import_bindings,
                    local_classes=local_classes,
                    module=module,
                )
            return None
        if type_node.type == "type_identifier":
            name = self._node_text(type_node)
            if name in self._TYPE_REF_SKIP_NAMES:
                return None
            if name in local_classes:
                return name, f"{module}.{name}", False
            if name in import_bindings:
                qn = import_bindings[name]
                return name, qn, not qn.startswith(f"{module}.")
            return name, f"{module}.{name}", False
        return None

    def _class_typed_locals(
        self,
        owner,
        source_code: str,
        import_bindings: dict[str, str],
        module: str,
        local_classes: set[str],
    ) -> dict[str, tuple[str, str, bool]]:
        body = owner.child_by_field_name("body")
        if body is None:
            return {}
        mapping: dict[str, tuple[str, str, bool]] = {}
        for node in self._iter_nodes(body):
            if node.type in ("method_definition", "function_declaration", "arrow_function"):
                if node is not owner:
                    continue
            if node.type != "lexical_declaration":
                continue
            for declarator in node.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                type_node = declarator.child_by_field_name("type")
                if type_node is None:
                    type_node = self._node_field_by_type(declarator, "type_annotation")
                if name_node is None or type_node is None:
                    continue
                resolved = self._class_type_from_type_node(
                    type_node,
                    import_bindings=import_bindings,
                    module=module,
                    local_classes=local_classes,
                )
                if resolved is not None:
                    mapping[self._node_text(name_node)] = resolved
        return mapping

    def _property_method_uid(self, file_path: str, owner: str, name: str) -> str:
        qualified_name = f"{module_name_from_path(file_path)}.{owner}.{name}"
        return compute_uid(qualified_name, f"{name}()->_", self.language_name)

    def _parameter_decorator_provider_names(self, param) -> list[str]:
        """First positional identifier arg of each parameter decorator call."""
        providers: list[str] = []
        for child in param.children:
            if child.type != "decorator":
                continue
            call = next(
                (c for c in child.children if c.type == "call_expression"),
                None,
            )
            if call is None:
                continue
            args = call.child_by_field_name("arguments")
            if args is None:
                continue
            for arg in args.named_children:
                if arg.type == "identifier":
                    providers.append(self._node_text(arg))
                    break
        return providers

    def _resolve_new_callee(
        self,
        ctor_node,
        *,
        import_bindings: dict[str, str],
        local_classes: set[str],
        module: str,
    ) -> tuple[str, str, bool] | None:
        if ctor_node.type == "identifier":
            name = self._node_text(ctor_node)
            if name in local_classes:
                return name, f"{module}.{name}", False
            if name in import_bindings:
                qn = import_bindings[name]
                return name, qn, not qn.startswith(f"{module}.")
            return name, f"{module}.{name}", False
        if ctor_node.type == "member_expression":
            obj = ctor_node.child_by_field_name("object")
            attr = ctor_node.child_by_field_name("property")
            if obj is None or attr is None:
                return None
            final = self._node_text(attr)
            if obj.type == "identifier":
                head = self._node_text(obj)
                base = import_bindings.get(head, head)
                qn = f"{base}.{final}"
                is_external = head in import_bindings and not qn.startswith(f"{module}.")
                return final, qn, is_external
        return None

    def _resolve_method_uid(
        self, caller_node, method_name: str, by_name: dict[str, list]
    ) -> str | None:
        candidates = by_name.get(method_name, [])
        if not candidates:
            return None

        class_node = caller_node
        while class_node and class_node.type not in self._CLASS_DECL_TYPES:
            class_node = class_node.parent
        if not class_node:
            return str(candidates[0].uid) if len(candidates) == 1 else None

        class_name_node = class_node.child_by_field_name("name")
        if not class_name_node:
            return None
        class_name = class_name_node.text.decode("utf-8")
        for candidate in candidates:
            if f".{class_name}.{method_name}" in candidate.qualified_name:
                return str(candidate.uid)
        return str(candidates[0].uid) if len(candidates) == 1 else None


def make_adapter() -> TypeScriptAdapter:
    """Factory function for adapter discovery."""
    return TypeScriptAdapter()
