"""TypeScript language adapter using tree-sitter."""

import re
from pathlib import Path

from context_engine.parser.adapters.treesitter_base import TreeSitterAdapter, iter_ts_query_matches
from context_engine.parser.protocol import ImportEdge, InheritanceEdge, SymbolMetadata
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
    _BODY_CALL_FALLBACK_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*(?:<[^>\n;{}()]*>)?\s*\(")
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
    _TYPE_OWNER_TYPES = {
        "function_declaration",
        "method_definition",
        "class_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "variable_declarator",
    }
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

    @property
    def language_name(self) -> str:
        return "typescript"

    @property
    def file_extensions(self) -> set[str]:
        return {".ts", ".tsx"}

    @property
    def ts_language_name(self) -> str:
        return "typescript"

    @property
    def symbol_query(self) -> str:
        return """
            (function_declaration name: (identifier) @func.name) @func.def
            (method_definition name: (property_identifier) @func.name) @func.def
            (class_declaration name: (type_identifier) @class.name) @class.def
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
        return {"function_declaration", "method_definition", "class_declaration"}

    @property
    def import_query(self) -> str:
        return """
            (import_statement source: (string) @import.source) @import.stmt
            (export_statement source: (string) @import.source) @import.stmt
            (import_specifier (identifier) @import.name) @import.spec
        """

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
        symbols = super().extract_symbols(source_code, file_path, tree=tree)
        object_api_ranges = self._exported_object_api_ranges(source_code)
        if object_api_ranges:
            symbols = [
                symbol
                for symbol in symbols
                if not self._is_nested_object_api_member(symbol, object_api_ranges)
            ]
            symbols = self._merge_exported_object_api_symbols(
                symbols,
                source_code,
                file_path,
                object_api_ranges,
            )
        existing_names = {symbol.name for symbol in symbols}

        for match in self._EXPORTED_FUNC_FALLBACK_RE.finditer(source_code):
            name = match.group(1)
            if name in existing_names:
                continue

            start_line, end_line, content = self._fallback_symbol_span(
                source_code,
                match.start(),
            )
            signature = normalize_signature(f"{name}()->_", self.language_name)
            qualified_name = f"{module_name_from_path(file_path)}.{name}"
            symbols.append(
                SymbolMetadata(
                    uid=compute_uid(qualified_name, signature, self.language_name),
                    name=name,
                    kind="function",
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

        for match in self._EXPORTED_VAR_FALLBACK_RE.finditer(source_code):
            name = match.group(1)
            if name in existing_names:
                continue
            tail = source_code[match.end() : match.end() + 24]
            if re.match(r"\s*=\s*\{", tail):
                continue

            start_line, end_line, content = self._fallback_symbol_span(
                source_code,
                match.start(),
            )
            signature = normalize_signature(f"{name}()->_", self.language_name)
            qualified_name = f"{module_name_from_path(file_path)}.{name}"
            symbols.append(
                SymbolMetadata(
                    uid=compute_uid(qualified_name, signature, self.language_name),
                    name=name,
                    kind="variable",
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

        for match in self._EXPORTED_TYPE_FALLBACK_RE.finditer(source_code):
            name = match.group(1)
            if name in existing_names:
                continue
            start_line, end_line, content = self._fallback_symbol_span(
                source_code,
                match.start(),
            )
            signature = normalize_signature(f"{name}()->_", self.language_name)
            qualified_name = f"{module_name_from_path(file_path)}.{name}"
            symbols.append(
                SymbolMetadata(
                    uid=compute_uid(qualified_name, signature, self.language_name),
                    name=name,
                    kind="class",
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
        # Parse once for the higher-order-factory walk if the caller didn't
        # hand us a tree — the base extract_symbols may have parsed internally
        # without returning the result.
        if tree is None:
            tree = self._parse(source_code)
        higher_order_factory_names = self._higher_order_factory_names(tree)
        if higher_order_factory_names:
            for symbol in symbols:
                if symbol.name in higher_order_factory_names:
                    symbol.returns_function_expression = True
        return symbols

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

    @staticmethod
    def _is_nested_object_api_member(
        symbol: SymbolMetadata,
        object_api_ranges: dict[str, tuple[int, int]],
    ) -> bool:
        for name, (start_line, end_line) in object_api_ranges.items():
            if symbol.name == name:
                return False
            if start_line <= symbol.start_line <= end_line:
                return True
        return False

    def _merge_exported_object_api_symbols(
        self,
        symbols: list[SymbolMetadata],
        source_code: str,
        file_path: str,
        object_api_ranges: dict[str, tuple[int, int]],
    ) -> list[SymbolMetadata]:
        by_name = {symbol.name: symbol for symbol in symbols}
        lines = source_code.splitlines()
        for name, (start_line, end_line) in object_api_ranges.items():
            if end_line < start_line or start_line < 1:
                continue
            content = "\n".join(lines[start_line - 1 : end_line])
            signature = normalize_signature(f"{name}()->_", self.language_name)
            qualified_name = f"{module_name_from_path(file_path)}.{name}"
            by_name[name] = SymbolMetadata(
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
        return list(by_name.values())

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
        captures = []
        for _match_id, captures_dict in iter_ts_query_matches(
            self.language, self.import_query, tree.root_node
        ):
            for tag, nodes in captures_dict.items():
                for node in nodes:
                    captures.append((node, tag))

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
            if line.startswith("class "):
                extends_match = re.search(r"extends\s+(\w+)", line)
                implements_match = re.search(r"implements\s+([^{]+)", line)

                class_match = re.match(r"class\s+(\w+)", line)
                if class_match:
                    class_name = class_match.group(1)

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
            parent = deco.parent
            if parent is None:
                continue
            # Two tree-sitter placements for a ``@deco`` prefix:
            #   1. Sibling-before form (export class, class body methods): the
            #      decorator and the declaration share a parent
            #      (``export_statement`` or ``class_body``).
            #   2. Inner-prefix form (bare ``@deco\nclass A {}`` / function /
            #      method without an enclosing ``export_statement``): tree-sitter
            #      tucks the decorator *inside* the declaration node itself.
            # The same scan handles both.
            if parent.type in self._DECORATABLE_NODE_TYPES:
                decorated = parent
            else:
                decorated = self._decoratable_sibling_after(parent, deco)
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

    def _decoratable_name(self, node) -> str:
        if node.type == "class_declaration":
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
        signal for the "declarative metadata composition" pattern documented
        in role_signature_findings as subtype 2 of composition_surface.

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
            parent = deco.parent
            if parent is None:
                continue
            if parent.type in self._DECORATABLE_NODE_TYPES:
                decorated = parent
            else:
                decorated = self._decoratable_sibling_after(parent, deco)
            if decorated is None or decorated.type != "class_declaration":
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
            elif owner.type == "class_declaration":
                emit(owner, owner, "annotation")
            elif owner.type == "variable_declarator":
                self._emit_ts_type_annotations(owner, "annotation", emit, owner)
        return out

    def extract_calls_from_source(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[dict]:
        """Extract TypeScript calls with direct vs dynamic dispatch classification."""
        if tree is None:
            tree = self._parse(source_code)

        # Flatten captures from matches into (node, tag) tuples
        captures = []
        for _match_id, captures_dict in iter_ts_query_matches(
            self.language, "(call_expression) @call", tree.root_node
        ):
            for tag, nodes in captures_dict.items():
                for node in nodes:
                    captures.append((node, tag))

        symbols = self.extract_symbols(source_code, file_path, tree=tree)
        by_name: dict[str, list] = {}
        for symbol in symbols:
            by_name.setdefault(symbol.name, []).append(symbol)

        import_bindings, module_aliases = self._extract_import_bindings(source_code, file_path)
        calls = []
        for node, tag in captures:
            if tag != "call":
                continue

            func_node = node.child_by_field_name("function")
            if not func_node:
                continue

            parent = self._enclosing_symbol_owner(node)
            if not parent:
                continue

            caller_uid = self._caller_uid_for_owner(parent, source_code, file_path)
            if not caller_uid:
                continue
            callee_uid = None
            call_name = ""
            rel_type = "CALLS_DIRECT"
            tier = "direct"
            confidence = 1.0

            if func_node.type == "identifier":
                call_name = source_code[func_node.start_byte : func_node.end_byte]
                if call_name in import_bindings:
                    rel_type = "CALLS_IMPORTED"
                    tier = "imported"
                    confidence = 0.9
            elif func_node.type == "member_expression":
                named_children = [child for child in func_node.children if child.is_named]
                if len(named_children) < 2:
                    continue
                receiver_node = named_children[0]
                method_node = named_children[-1]
                receiver_text = source_code[receiver_node.start_byte : receiver_node.end_byte]
                call_name = source_code[method_node.start_byte : method_node.end_byte]
                rel_type = "CALLS_DYNAMIC"
                tier = "dynamic"
                confidence = 0.7
                if receiver_text == "this":
                    callee_uid = self._resolve_method_uid(parent, call_name, by_name)
                elif receiver_text in import_bindings:
                    rel_type = "CALLS_IMPORTED"
                    tier = "imported"
                    confidence = 0.9
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
                "resolver": "ts-scope-v1",
                "call_site_line": node.start_point[0] + 1,
            }
            if callee_uid:
                call["callee_uid"] = callee_uid
            if rel_type == "CALLS_IMPORTED":
                if func_node.type == "identifier":
                    call["callee_qualified_name"] = import_bindings[call_name]
                else:
                    receiver_node = [child for child in func_node.children if child.is_named][0]
                    receiver_text = source_code[receiver_node.start_byte : receiver_node.end_byte]
                    base = import_bindings.get(receiver_text, "")
                    if base:
                        if receiver_text in module_aliases:
                            call["callee_qualified_name"] = f"{base}.{call_name}"
                        else:
                            base_leaf = base.rsplit(".", 1)[-1]
                            if base_leaf == receiver_text:
                                call["callee_qualified_name"] = base
                            else:
                                call["callee_qualified_name"] = f"{base}.{call_name}"
            calls.append(call)

        self._append_exported_initializer_call_fallbacks(
            calls,
            source_code,
            file_path,
            import_bindings,
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
    ) -> None:
        """Recover ``export const x = call(...)`` edges when TS AST owner recovery fails."""
        seen = {
            (
                call.get("caller_uid"),
                call.get("callee_name"),
                call.get("rel_type"),
                call.get("call_site_line"),
                call.get("callee_qualified_name", ""),
            )
            for call in calls
        }
        for match in self._EXPORTED_CALL_INITIALIZER_RE.finditer(source_code):
            caller_name = match.group(1)
            callee_name = match.group(2)
            if not caller_name or not callee_name:
                continue
            caller_uid = self._uid(file_path, caller_name)
            rel_type = "CALLS_IMPORTED" if callee_name in import_bindings else "CALLS_DIRECT"
            line = source_code.count("\n", 0, match.start(2)) + 1
            callee_qn = import_bindings.get(callee_name, "") if rel_type == "CALLS_IMPORTED" else ""
            key = (caller_uid, callee_name, rel_type, line, callee_qn)
            if key in seen:
                continue
            call = {
                "caller_uid": caller_uid,
                "callee_name": callee_name,
                "rel_type": rel_type,
                "tier": "imported" if rel_type == "CALLS_IMPORTED" else "direct",
                "confidence": 0.9 if rel_type == "CALLS_IMPORTED" else 1.0,
                "resolver": "ts-export-initializer-fallback-v1",
                "call_site_line": line,
            }
            if callee_qn:
                call["callee_qualified_name"] = callee_qn
            calls.append(call)
            seen.add(key)

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
        seen = {
            (
                call.get("caller_uid"),
                call.get("callee_name"),
                call.get("rel_type"),
                call.get("call_site_line"),
                call.get("callee_qualified_name", ""),
            )
            for call in calls
        }
        lines = source_code.splitlines(keepends=True)
        line_offsets: list[int] = []
        offset = 0
        for src_line in lines:
            line_offsets.append(offset)
            offset += len(src_line)

        for symbol in symbols:
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
            for match in self._BODY_CALL_FALLBACK_RE.finditer(body):
                callee_name = match.group(1)
                if (
                    not callee_name
                    or callee_name == symbol.name
                    or callee_name in self._BODY_CALL_FALLBACK_SKIP
                    or callee_name not in known_names
                ):
                    continue
                prefix = body[: match.start()]
                previous = prefix.rstrip()[-1:] if prefix.rstrip() else ""
                if previous in {".", ":"}:
                    continue
                if re.search(r"\bfunction\s+$", prefix[-32:]):
                    continue

                rel_type = "CALLS_IMPORTED" if callee_name in import_bindings else "CALLS_DIRECT"
                line = source_code.count("\n", 0, body_start + match.start(1)) + 1
                callee_qn = (
                    import_bindings.get(callee_name, "") if rel_type == "CALLS_IMPORTED" else ""
                )
                key = (caller_uid, callee_name, rel_type, line, callee_qn)
                if key in seen:
                    continue
                call = {
                    "caller_uid": caller_uid,
                    "callee_name": callee_name,
                    "rel_type": rel_type,
                    "tier": "imported" if rel_type == "CALLS_IMPORTED" else "direct",
                    "confidence": 0.75 if rel_type == "CALLS_IMPORTED" else 0.7,
                    "resolver": "ts-symbol-body-fallback-v1",
                    "call_site_line": line,
                }
                if callee_qn:
                    call["callee_qualified_name"] = callee_qn
                calls.append(call)
                seen.add(key)

    def _extract_import_bindings(
        self, source_code: str, file_path: str
    ) -> tuple[dict[str, str], set[str]]:
        bindings: dict[str, str] = {}
        module_aliases: set[str] = set()
        for match in re.finditer(
            r"import\s+([^;]+?)\s+from\s+['\"]([^'\"]+)['\"]",
            source_code,
        ):
            spec = match.group(1).strip()
            source = self._normalize_import_source(file_path, match.group(2).strip())
            if not spec or not source:
                continue
            if spec.startswith("{") and spec.endswith("}"):
                self._parse_named_import_bindings(spec[1:-1], source, bindings)
            elif spec.startswith("* as "):
                alias = spec[len("* as ") :].strip()
                if alias:
                    bindings[alias] = source
                    module_aliases.add(alias)
            elif "," in spec:
                default_alias, rest = spec.split(",", 1)
                default_alias = default_alias.strip()
                if default_alias:
                    bindings[default_alias] = source
                rest = rest.strip()
                if rest.startswith("{") and rest.endswith("}"):
                    self._parse_named_import_bindings(rest[1:-1], source, bindings)
            else:
                bindings[spec] = source
        for match in re.finditer(
            r"const\s+\{\s*([^}]+)\s*\}\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)",
            source_code,
        ):
            source = self._normalize_import_source(file_path, match.group(2).strip())
            self._parse_named_import_bindings(match.group(1), source, bindings)
        for match in re.finditer(
            r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)",
            source_code,
        ):
            alias = match.group(1).strip()
            source = self._normalize_import_source(file_path, match.group(2).strip())
            if alias and source:
                bindings[alias] = source
        return bindings, module_aliases

    @staticmethod
    def _parse_named_import_bindings(spec: str, source: str, out: dict[str, str]) -> None:
        for part in spec.split(","):
            token = part.strip()
            if not token:
                continue
            if " as " in token:
                imported, alias = token.split(" as ", 1)
                imported = imported.strip()
                alias = alias.strip()
            elif ":" in token:
                imported, alias = token.split(":", 1)
                imported = imported.strip()
                alias = alias.strip()
            else:
                imported = token
                alias = token
            if alias and imported:
                out[alias] = f"{source}.{imported}"

    def _normalize_import_source(self, file_path: str, source: str) -> str:
        if not source:
            return ""
        if not source.startswith("."):
            return source.replace("/", ".")
        base = Path(file_path).parent
        resolved = (base / source).resolve()
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
        return source.lstrip("./").replace("/", ".")

    def _fallback_symbol_span(
        self,
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
        raw_signature, _ = signature_from_node(node, source_code, self.language_name)
        return compute_uid(qualified_name, raw_signature, self.language_name)

    def _enclosing_symbol_owner(self, node):
        parent = node.parent
        while parent:
            if parent.type == "method_definition":
                var_owner = self._object_literal_owner_variable(parent)
                if var_owner is not None:
                    return var_owner
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
            if parent.type in {"function_declaration", "method_definition", "class_declaration"}:
                return False
            parent = parent.parent
        return False

    def _resolve_method_uid(
        self, caller_node, method_name: str, by_name: dict[str, list]
    ) -> str | None:
        candidates = by_name.get(method_name, [])
        if not candidates:
            return None

        class_node = caller_node
        while class_node and class_node.type != "class_declaration":
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
