"""Lexical scope graph for TypeScript / JavaScript call resolution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class TsBinding:
    name: str
    kind: str = "local"  # local | param | function | destructure | import
    ambiguous: bool = False
    init_callee: str = ""
    init_import_qn: str = ""
    require_alias: bool = False
    decl_byte: int = 0


@dataclass
class _ScopeLayer:
    kind: str
    bindings: dict[str, TsBinding] = field(default_factory=dict)


_SCOPE_PUSH_TYPES = frozenset(
    {
        "statement_block",
        "class_body",
        "function_declaration",
        "method_definition",
        "arrow_function",
        "function_expression",
        "function",
        "for_statement",
        "for_in_statement",
        "for_of_statement",
        "while_statement",
        "do_statement",
        "catch_clause",
        "switch_statement",
    }
)

_CALLABLE_TYPES = frozenset(
    {
        "function_declaration",
        "method_definition",
        "arrow_function",
        "function_expression",
        "function",
    }
)


class TsScopeGraph:
    def __init__(self) -> None:
        self._layers: list[_ScopeLayer] = [_ScopeLayer("module")]
        self._snapshots: list[tuple[int, list[_ScopeLayer]]] = [(0, self._snapshot())]

    @classmethod
    def build(
        cls,
        root,
        *,
        import_bindings: dict[str, str],
        node_text: Callable[[object], str],
        normalize_require: Callable[[str], str] | None = None,
    ) -> TsScopeGraph:
        graph = cls()
        graph._walk(root, import_bindings, node_text, normalize_require)
        return graph

    def resolve_name(self, name: str, at_byte: int) -> TsBinding | None:
        layers = self._layers_at(at_byte)
        for layer in reversed(layers):
            binding = layer.bindings.get(name)
            if binding is not None and binding.decl_byte <= at_byte:
                return binding
        return None

    def _layers_at(self, at_byte: int) -> list[_ScopeLayer]:
        chosen = self._snapshots[0][1]
        for snap_byte, layers in self._snapshots:
            if snap_byte <= at_byte:
                chosen = layers
            else:
                break
        return chosen

    def _snapshot(self) -> list[_ScopeLayer]:
        return [
            _ScopeLayer(kind=layer.kind, bindings=dict(layer.bindings)) for layer in self._layers
        ]

    def _commit(self, decl_byte: int) -> None:
        self._snapshots.append((decl_byte, self._snapshot()))

    def _walk_function_declaration(self, node, node_text: Callable[[object], str]) -> None:
        if node.type != "function_declaration":
            return
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        fn_name = node_text(name_node)
        self._declare(
            fn_name,
            TsBinding(name=fn_name, kind="function", decl_byte=node.start_byte),
        )

    def _walk_catch_clause(self, node, node_text: Callable[[object], str]) -> None:
        if node.type != "catch_clause":
            return
        param = node.child_by_field_name("parameter")
        if param is None:
            return
        for bound_name in _pattern_names(param, node_text):
            self._declare(
                bound_name,
                TsBinding(name=bound_name, kind="param", decl_byte=node.start_byte),
            )

    def _walk_variable_declarations(
        self,
        node,
        import_bindings: dict[str, str],
        node_text: Callable[[object], str],
        normalize_require: Callable[[str], str] | None,
    ) -> None:
        if node.type not in {"lexical_declaration", "variable_declaration"}:
            return
        type_node = node.child_by_field_name("type")
        for child in node.named_children:
            if child.type == "variable_declarator":
                self._register_variable_declarator(
                    child,
                    import_bindings,
                    node_text,
                    normalize_require,
                    type_node=type_node,
                )

    def _walk(
        self,
        node,
        import_bindings: dict[str, str],
        node_text: Callable[[object], str],
        normalize_require: Callable[[str], str] | None,
    ) -> None:
        self._walk_function_declaration(node, node_text)
        push = node.type in _SCOPE_PUSH_TYPES
        if push:
            self._layers.append(_ScopeLayer(node.type))

        if node.type in _CALLABLE_TYPES:
            self._register_function_params(node, node_text)
        self._walk_catch_clause(node, node_text)
        self._walk_variable_declarations(
            node, import_bindings, node_text, normalize_require
        )

        for child in node.children:
            if child.is_named:
                self._walk(child, import_bindings, node_text, normalize_require)

        if push:
            self._layers.pop()
            self._commit(node.end_byte)

    def _register_function_params(self, fn_node, node_text: Callable[[object], str]) -> None:
        params = fn_node.child_by_field_name("parameters")
        if params is None:
            return
        for child in params.named_children:
            if child.type not in {"required_parameter", "optional_parameter", "rest_parameter"}:
                continue
            pattern = child.child_by_field_name("pattern")
            type_node = child.child_by_field_name("type")
            ambiguous = _type_node_is_ambiguous(type_node, node_text)
            for bound_name in _pattern_names(pattern, node_text):
                self._declare(
                    bound_name,
                    TsBinding(
                        name=bound_name,
                        kind="param",
                        ambiguous=ambiguous,
                        decl_byte=child.start_byte,
                    ),
                )

    def _register_variable_declarator(
        self,
        decl,
        import_bindings: dict[str, str],
        node_text: Callable[[object], str],
        normalize_require: Callable[[str], str] | None,
        *,
        type_node,
    ) -> None:
        name_node = decl.child_by_field_name("name")
        value = decl.child_by_field_name("value")
        if name_node is None:
            return
        typ = decl.child_by_field_name("type") or type_node
        ambiguous = _type_node_is_ambiguous(typ, node_text)
        init_callee, init_import_qn = _extract_init_origin(
            value, import_bindings, node_text, normalize_require
        )

        if name_node.type == "identifier":
            name = node_text(name_node)
            require_alias = bool(init_import_qn)
            kind = "destructure" if init_callee or init_import_qn else "local"
            if name in import_bindings and not init_callee and not init_import_qn:
                kind = "import"
                require_alias = False
            self._declare(
                name,
                TsBinding(
                    name=name,
                    kind=kind,
                    ambiguous=ambiguous,
                    init_callee=init_callee,
                    init_import_qn=init_import_qn,
                    require_alias=require_alias,
                    decl_byte=decl.start_byte,
                ),
            )
            return

        if name_node.type == "object_pattern":
            for bound_name in _object_pattern_bindings(name_node, node_text):
                self._declare(
                    bound_name,
                    TsBinding(
                        name=bound_name,
                        kind="destructure",
                        ambiguous=ambiguous or bool(init_import_qn),
                        init_callee=init_callee,
                        init_import_qn=init_import_qn,
                        decl_byte=decl.start_byte,
                    ),
                )

    def _declare(self, name: str, binding: TsBinding) -> None:
        if not name:
            return
        self._layers[-1].bindings[name] = binding
        self._commit(binding.decl_byte)


def _type_node_is_ambiguous(type_node, node_text: Callable[[object], str]) -> bool:
    if type_node is None:
        return True
    if type_node.type == "type_annotation":
        inner = type_node.child_by_field_name("type")
        if inner is None and type_node.named_children:
            inner = type_node.named_children[0]
        return _type_node_is_ambiguous(inner, node_text)
    if type_node.type in {"predefined_type", "type_identifier"}:
        return node_text(type_node) in {"any", "unknown"}
    return False


def _pattern_names(pattern, node_text: Callable[[object], str]) -> list[str]:
    if pattern is None:
        return []
    if pattern.type == "identifier":
        return [node_text(pattern)]
    if pattern.type == "object_pattern":
        return _object_pattern_bindings(pattern, node_text)
    return []


def _object_pattern_bindings(pattern, node_text: Callable[[object], str]) -> list[str]:
    names: list[str] = []
    for child in pattern.named_children:
        if child.type == "shorthand_property_identifier_pattern":
            names.append(node_text(child))
        elif child.type == "pair_pattern":
            key = child.child_by_field_name("key")
            if key is not None and key.type in {"identifier", "property_identifier"}:
                names.append(node_text(key))
    return names


def _extract_init_origin(
    value_node,
    import_bindings: dict[str, str],
    node_text: Callable[[object], str],
    normalize_require: Callable[[str], str] | None,
) -> tuple[str, str]:
    if value_node is None:
        return "", ""
    if value_node.type == "identifier":
        name = node_text(value_node)
        if name in import_bindings:
            return "", import_bindings[name]
        return name, ""
    if value_node.type != "call_expression":
        return "", ""
    func = value_node.child_by_field_name("function")
    if func is None:
        return "", ""
    if func.type == "identifier":
        callee = node_text(func)
        if callee == "require":
            path = _first_string_arg(value_node, node_text)
            if path and normalize_require:
                return "", normalize_require(path)
            return "", path
        if callee in import_bindings:
            return "", import_bindings[callee]
        return callee, ""
    return "", ""


def _first_string_arg(call_node, node_text: Callable[[object], str]) -> str:
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return ""
    for child in args.named_children:
        if child.type == "string":
            text = node_text(child)
            if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
                return text[1:-1]
            return text
        break
    return ""
