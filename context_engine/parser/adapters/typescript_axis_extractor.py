"""TypeScript AST extractor for physical CFG/DFG/Structural axis bits."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from context_engine.axis.schema import AxisExtraction, AxisFact, AxisName
from context_engine.parser.uid import (
    UNRESOLVED_SIGNATURE,
    compute_uid,
    module_name_from_path,
    qualified_name_for,
)

if TYPE_CHECKING:
    from context_engine.parser.adapters.javascript_adapter import JavaScriptAdapter
    from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter

_CONTAINER_MUTATION_METHODS = frozenset(
    {"push", "pop", "shift", "unshift", "splice", "set", "delete"}
)
_CONTAINER_READ_METHODS = frozenset({"get", "has"})


class TypeScriptAxisExtractor:
    """Extract adapter-owned TypeScript axis facts.

    Emits AST-visible bits only; framework roles stay in the axis compiler.
    """

    _CLASS_TYPES = frozenset({"class_declaration", "abstract_class_declaration"})
    _CALLABLE_TYPES = frozenset(
        {
            "function_declaration",
            "method_definition",
            "function_expression",
            "arrow_function",
        }
    )
    _OWNER_TYPES = _CLASS_TYPES | _CALLABLE_TYPES | frozenset({"variable_declarator"})
    _PARAMETER_TYPES = frozenset(
        {
            "required_parameter",
            "optional_parameter",
            "rest_pattern",
            "assignment_pattern",
        }
    )
    _SHAPE_TYPES = {"object": "object", "array": "array"}
    _BRANCH_TYPES = frozenset({"if_statement", "switch_statement", "ternary_expression"})
    _LOOP_TYPES = frozenset(
        {
            "for_statement",
            "for_in_statement",
            "for_of_statement",
            "while_statement",
            "do_statement",
        }
    )

    def __init__(self, adapter: TypeScriptAdapter | JavaScriptAdapter) -> None:
        self.adapter = adapter

    @classmethod
    def _axis_node_handler_map(cls) -> dict[str, str]:
        cached = getattr(cls, "_CACHED_AXIS_NODE_HANDLER_MAP", None)
        if cached is not None:
            return cached
        handlers: dict[str, str] = {
            "import_statement": "_axis_node_import",
            "variable_declarator": "_axis_node_variable_declarator",
            "decorator": "_axis_node_decorator",
            "call_expression": "_axis_node_call",
            "new_expression": "_axis_node_new",
            "return_statement": "_axis_node_return",
            "assignment_expression": "_axis_node_assignment",
            "augmented_assignment_expression": "_axis_node_augmented_assignment",
            "await_expression": "_axis_node_await",
            "yield_expression": "_axis_node_yield",
            "try_statement": "_axis_node_try",
            "catch_clause": "_axis_node_catch",
            "throw_statement": "_axis_node_throw",
            "member_expression": "_axis_node_member",
            "subscript_expression": "_axis_node_subscript",
        }
        for node_type in cls._CLASS_TYPES:
            handlers[node_type] = "_axis_node_class"
        for node_type in cls._CALLABLE_TYPES:
            handlers[node_type] = "_axis_node_callable"
        for node_type in cls._SHAPE_TYPES:
            handlers[node_type] = "_axis_node_shape"
        for node_type in cls._BRANCH_TYPES:
            handlers[node_type] = "_axis_node_branch"
        for node_type in cls._LOOP_TYPES:
            handlers[node_type] = "_axis_node_loop"
        cls._CACHED_AXIS_NODE_HANDLER_MAP = handlers
        return handlers

    def _dispatch_axis_node(
        self,
        node,
        *,
        source: str,
        file_path: str,
        module_scope: tuple[str, str, str],
        emit,
        emit_scope,
        seen_decorators: set[int],
    ) -> None:
        handler_name = self._axis_node_handler_map().get(node.type)
        if handler_name is None:
            return
        getattr(self, handler_name)(
            node,
            source=source,
            file_path=file_path,
            module_scope=module_scope,
            emit=emit,
            emit_scope=emit_scope,
            seen_decorators=seen_decorators,
        )

    def extract(
        self,
        source: str,
        file_path: str,
        *,
        tree=None,
    ) -> AxisExtraction:
        if tree is None:
            tree = self.adapter._parse(source)

        facts: list[AxisFact] = []
        seen_decorators: set[int] = set()
        module_scope = self._module_scope(file_path)
        emitted_module_scope = False

        def emit_scope(
            scope: tuple[str, str, str],
            node,
            axis: AxisName,
            bit: str,
            *,
            ast_kind: str | None = None,
            payload: dict[str, object] | None = None,
        ) -> None:
            uid, qn, kind = scope
            facts.append(
                AxisFact(
                    symbol_uid=uid,
                    qualified_name=qn,
                    symbol_kind=kind,
                    axis=axis,
                    bit=bit,
                    line=int(getattr(node, "start_point", (0,))[0]) + 1,
                    evidence=self._evidence(node, source),
                    ast_kind=ast_kind or node.type,
                    payload=payload or {},
                )
            )

        def emit(
            owner,
            node,
            axis: AxisName,
            bit: str,
            *,
            ast_kind: str | None = None,
            payload: dict[str, object] | None = None,
        ) -> None:
            scope = self._scope(owner, source, file_path)
            if scope is None:
                return
            emit_scope(scope, node, axis, bit, ast_kind=ast_kind, payload=payload)

        for node in self.adapter._iter_nodes(tree.root_node):
            if not emitted_module_scope and node.type == "program":
                emit_scope(module_scope, node, "struct", "module_scope")
                emitted_module_scope = True

            self._dispatch_axis_node(
                node,
                source=source,
                file_path=file_path,
                module_scope=module_scope,
                emit=emit,
                emit_scope=emit_scope,
                seen_decorators=seen_decorators,
            )

        return AxisExtraction(file_path=file_path, facts=facts)

    def _axis_node_import(self, node, *, source, module_scope, emit_scope, **_) -> None:
        self._emit_import_facts(node, source, module_scope, emit_scope)

    def _axis_node_class(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_class_def_facts(node, source, file_path, emit)
        self._emit_inheritance_facts(node, source, file_path, emit)

    def _axis_node_callable(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_callable_def_facts(node, source, file_path, emit)
        self._emit_parameter_facts(node, source, file_path, emit)
        self._emit_return_annotation(node, source, file_path, emit)

    def _axis_node_variable_declarator(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_variable_declarator_facts(node, source, file_path, emit)

    def _axis_node_decorator(self, node, *, source, file_path, emit, seen_decorators, **_) -> None:
        if node.start_byte in seen_decorators:
            return
        seen_decorators.add(node.start_byte)
        self._emit_decorator_facts(node, source, file_path, emit)

    def _axis_node_call(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_call_facts(node, source, file_path, emit)

    def _axis_node_new(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_new_facts(node, source, file_path, emit)

    def _axis_node_return(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_return_facts(node, source, file_path, emit)

    def _axis_node_shape(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_literal_shape_facts(node, source, file_path, emit)

    def _axis_node_assignment(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_assignment_facts(node, source, file_path, emit)

    def _axis_node_augmented_assignment(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_augmented_assignment_facts(node, source, file_path, emit)

    def _axis_node_branch(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_branch_facts(node, source, file_path, emit)

    def _axis_node_loop(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_loop_facts(node, source, file_path, emit)

    def _axis_node_await(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_await_facts(node, source, file_path, emit)

    def _axis_node_yield(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_yield_facts(node, source, file_path, emit)

    def _axis_node_try(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_try_facts(node, source, file_path, emit)

    def _axis_node_catch(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_catch_facts(node, source, file_path, emit)

    def _axis_node_throw(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_throw_facts(node, source, file_path, emit)

    def _axis_node_member(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_member_read_facts(node, source, file_path, emit)

    def _axis_node_subscript(self, node, *, source, file_path, emit, **_) -> None:
        self._emit_subscript_read_facts(node, source, file_path, emit)

    def extract_facts(self, source: str, file_path: str, *, tree=None) -> list[AxisFact]:
        return self.extract(source, file_path, tree=tree).facts

    def _module_scope(self, file_path: str) -> tuple[str, str, str]:
        module = module_name_from_path(file_path)
        uid = compute_uid(module, UNRESOLVED_SIGNATURE, self.adapter.language_name)
        return uid, module, "module"

    def _scope(self, node, source: str, file_path: str) -> tuple[str, str, str] | None:
        if node is None:
            return None
        if node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return None
            name = self.adapter._node_text(name_node)
            module = module_name_from_path(file_path)
            return self.adapter._uid(file_path, name), f"{module}.{name}", "variable"
        if node.type in self._CLASS_TYPES:
            return (
                self.adapter._uid_for_node(node, source, file_path),
                qualified_name_for(node, source, file_path),
                "class",
            )
        if node.type in self._CALLABLE_TYPES:
            owner = self.adapter._enclosing_symbol_owner(node)
            if node.type in {"function_expression", "arrow_function"} and owner is not None:
                return self._scope(owner, source, file_path)
            return (
                self.adapter._uid_for_node(node, source, file_path),
                qualified_name_for(node, source, file_path),
                "function",
            )
        return None

    def _owner_for_fact(self, node):
        owner = self.adapter._enclosing_symbol_owner(node)
        if owner is not None:
            return owner
        parent = node.parent
        while parent is not None:
            if parent.type in self._OWNER_TYPES:
                return parent
            parent = parent.parent
        return None

    def _import_module_from_statement(self, node) -> str:
        source_node = next((c for c in node.children if c.type == "string"), None)
        if source_node is None:
            return ""
        return self.adapter._string_literal_text(source_node)

    def _emit_import_dependency_fact(
        self,
        node,
        module_scope,
        emit_scope,
        *,
        module: str,
        name: str = "",
        alias: str = "",
    ) -> None:
        payload: dict[str, object] = {"module": module, "alias": alias}
        if name:
            payload["name"] = name
        emit_scope(module_scope, node, "struct", "import_dependency", payload=payload)

    def _emit_named_import_specifier(
        self,
        spec,
        *,
        module: str,
        module_scope,
        emit_scope,
    ) -> None:
        if spec.type != "import_specifier":
            return
        name = spec.child_by_field_name("name")
        alias = spec.child_by_field_name("alias")
        imported = self.adapter._node_text(name) if name else ""
        local = self.adapter._node_text(alias) if alias else imported
        self._emit_import_dependency_fact(
            spec,
            module_scope,
            emit_scope,
            module=module,
            name=imported,
            alias=local,
        )

    def _emit_import_clause_child(
        self,
        child,
        *,
        module: str,
        module_scope,
        emit_scope,
    ) -> None:
        if child.type == "identifier":
            self._emit_import_dependency_fact(
                child,
                module_scope,
                emit_scope,
                module=module,
                name=self.adapter._node_text(child),
                alias="",
            )
            return
        if child.type == "namespace_import":
            alias_node = child.child_by_field_name("name")
            self._emit_import_dependency_fact(
                child,
                module_scope,
                emit_scope,
                module=module,
                alias=self.adapter._node_text(alias_node) if alias_node else "",
            )
            return
        if child.type != "named_imports":
            return
        for spec in child.named_children:
            self._emit_named_import_specifier(
                spec,
                module=module,
                module_scope=module_scope,
                emit_scope=emit_scope,
            )

    def _emit_import_facts(self, node, source: str, module_scope, emit_scope) -> None:
        module = self._import_module_from_statement(node)
        clause = next((c for c in node.children if c.type == "import_clause"), None)
        if clause is None:
            self._emit_import_dependency_fact(
                node,
                module_scope,
                emit_scope,
                module=module,
                alias="",
            )
            return
        for child in clause.named_children:
            self._emit_import_clause_child(
                child,
                module=module,
                module_scope=module_scope,
                emit_scope=emit_scope,
            )

    def _emit_class_def_facts(self, node, source: str, file_path: str, emit) -> None:
        name_node = node.child_by_field_name("name")
        payload = {"name": self.adapter._node_text(name_node) if name_node else ""}
        emit(node, node, "struct", "class_def", payload=payload)
        emit(
            node,
            node,
            "dfg",
            "callable_value",
            payload={"callable_kind": "class", "origin": "definition", **payload},
        )

    def _emit_callable_def_facts(self, node, source: str, file_path: str, emit) -> None:
        name_node = node.child_by_field_name("name")
        name = self.adapter._node_text(name_node) if name_node else ""
        is_async = any(child.type == "async" for child in node.children)
        is_method = node.type == "method_definition"
        struct_bit = "async_function_def" if is_async else "function_def"
        callable_kind = "async_function" if is_async else "function"
        if is_method:
            struct_bit = "function_def"
        emit(node, node, "struct", struct_bit, payload={"name": name})
        emit(
            node,
            node,
            "cfg",
            "callable_body",
            payload={"callable_kind": callable_kind if not is_method else "method"},
        )
        emit(
            node,
            node,
            "dfg",
            "callable_value",
            payload={
                "callable_kind": callable_kind,
                "origin": "definition",
                "name": name,
                "decorated": self._node_has_decorators(node),
            },
        )
        if is_async:
            emit(node, node, "cfg", "async_suspend_resume")
        if is_method:
            class_owner = self._enclosing_class(node)
            owner_qn = ""
            if class_owner is not None:
                owner_qn = qualified_name_for(class_owner, source, file_path)
            emit(
                node,
                node,
                "struct",
                "method_member",
                payload={"owner": owner_qn, "name": name},
            )

    def _emit_variable_declarator_facts(self, node, source: str, file_path: str, emit) -> None:
        owner = self._owner_for_fact(node) or node
        name_node = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        target = self.adapter._node_text(name_node) if name_node else ""
        emit(
            owner,
            node,
            "dfg",
            "assignment_binding",
            payload={"target": target, "source_kind": "declarator"},
        )
        if self._is_class_body_declarator(node):
            emit(owner, node, "struct", "class_attribute", payload={"target": target})
        if value is not None:
            self._emit_value_origin(owner, value, emit, destination="assignment")
            self._maybe_emit_callable_value(owner, value, emit, source="assignment_value")

    def _emit_decorator_facts(self, deco, source: str, file_path: str, emit) -> None:
        base_name_fn = getattr(self.adapter, "_decorator_base_name", None)
        if not callable(base_name_fn):
            return
        decorated = self._decorated_node_for(deco)
        if decorated is None:
            return
        callee = base_name_fn(deco)
        if not callee:
            return
        payload = {
            "callee": callee,
            "decorator": callee,
            "arguments": self._decorator_arguments(deco),
        }
        emit(decorated, deco, "struct", "decorator_attachment", payload=payload)
        emit(decorated, deco, "struct", "decorator_shape", payload=payload)
        emit(decorated, deco, "cfg", "decorator_application", payload=payload)

    def _emit_call_facts(self, node, source: str, file_path: str, emit) -> None:
        owner = self._owner_for_fact(node)
        if owner is None:
            return
        fn = node.child_by_field_name("function")
        callee = self._callee_name(fn)
        payload: dict[str, object] = {"callee": callee} if callee else {}
        emit(owner, node, "cfg", "call_site", payload=payload)
        if self._is_member_call(node):
            emit(owner, node, "cfg", "method_dispatch", payload=payload)
        elif fn is not None and fn.type != "member_expression":
            emit(
                owner,
                fn,
                "cfg",
                "value_call",
                payload={"callee": callee, "callee_kind": fn.type},
            )
        self._emit_container_call_facts(owner, node, emit, payload)
        for arg_payload in self._argument_payloads(node):
            emit(owner, node, "dfg", "call_argument", payload={**payload, **arg_payload})

    def _emit_new_facts(self, node, source: str, file_path: str, emit) -> None:
        owner = self._owner_for_fact(node)
        if owner is None:
            return
        ctor = self._callee_name(node.child_by_field_name("constructor"))
        payload = {"callee": ctor, "call_kind": "construct"} if ctor else {"call_kind": "construct"}
        emit(owner, node, "cfg", "call_site", payload=payload)
        emit(owner, node, "cfg", "constructor_call", payload=payload)
        emit(owner, node, "dfg", "constructor_value", payload=payload)
        emit(
            owner,
            node,
            "dfg",
            "constructed_output",
            payload={**payload, "destination": "expression"},
        )
        for arg_payload in self._argument_payloads(node):
            emit(owner, node, "dfg", "call_argument", payload={**payload, **arg_payload})

    def _emit_return_facts(self, node, source: str, file_path: str, emit) -> None:
        owner = self._owner_for_fact(node)
        if owner is None:
            return
        emit(owner, node, "cfg", "return_exit")
        value = self._first_named_child(node)
        if value is None:
            emit(owner, node, "dfg", "return_shape_kind", payload={"shape_kind": "none"})
            return
        emit(owner, value, "dfg", "return_output", payload={"value_kind": value.type})
        emit(
            owner,
            value,
            "dfg",
            "return_shape_kind",
            payload=self._return_shape_payload(value),
        )
        self._emit_value_shape(owner, value, emit)
        self._emit_value_origin(owner, value, emit, destination="return")
        if self._contains_attr_read(value):
            emit(owner, value, "dfg", "projection")

    def _emit_literal_shape_facts(self, node, source: str, file_path: str, emit) -> None:
        owner = self._owner_for_fact(node)
        if owner is None:
            return
        self._emit_value_shape(owner, node, emit)
        if node.type != "object":
            return
        for pair in node.named_children:
            if pair.type != "pair":
                continue
            key = pair.child_by_field_name("key")
            value = pair.child_by_field_name("value")
            if key is None:
                continue
            key_payload = {"key": self._literal_text(key), "key_literal": self._literal_text(key)}
            emit(owner, key, "struct", "literal_key", payload=key_payload)
            if value is not None:
                emit(
                    owner,
                    pair,
                    "dfg",
                    "keyed_write",
                    payload={
                        "container": "object_literal",
                        **key_payload,
                        "value": self._expr_text(value, source),
                        "value_kind": value.type,
                    },
                )
                self._maybe_emit_callable_value(
                    owner,
                    value,
                    emit,
                    source="keyed_write",
                    payload={"container": "object_literal", **key_payload},
                )

    def _emit_assignment_facts(self, node, source: str, file_path: str, emit) -> None:
        if self._is_using_declaration(node):
            self._emit_using_facts(node, source, file_path, emit)
            return
        owner = self._owner_for_fact(node)
        if owner is None:
            return
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        emit(
            owner,
            node,
            "dfg",
            "assignment_binding",
            payload={"target": self._expr_text(left, source), "source_kind": "assign"},
        )
        if left is not None and left.type == "member_expression":
            target = self._dotted_name(left)
            payload = {"attribute": self._member_property(left), "target": target}
            emit(owner, left, "dfg", "attr_write", payload=payload)
            if self._is_this_member(left):
                emit(owner, left, "struct", "instance_attribute_hint", payload=payload)
        elif left is not None and left.type == "subscript_expression":
            container = self._dotted_name(left.child_by_field_name("object"))
            key = left.child_by_field_name("index")
            key_payload = self._key_payload(key, source)
            subscript_payload = cast(
                dict[str, object],
                {
                    "target": self._expr_text(left, source),
                    "container": container,
                    **key_payload,
                },
            )
            emit(owner, left, "dfg", "subscript_write", payload=subscript_payload)
            emit(owner, left, "dfg", "container_write_value", payload=subscript_payload)
            emit(
                owner,
                left,
                "dfg",
                "keyed_write",
                payload={
                    **subscript_payload,
                    **self._keyed_write_payload(key, right, source, container),
                },
            )
            if key_payload.get("key_literal"):
                emit(
                    owner,
                    key,
                    "struct",
                    "literal_key",
                    payload={**key_payload, "context": "subscript_write"},
                )
        elif (
            left is not None
            and right is not None
            and left.type == "identifier"
            and right.type == "identifier"
        ):
            emit(
                owner,
                left,
                "dfg",
                "aliasing",
                payload={
                    "target": self.adapter._node_text(left),
                    "source": self.adapter._node_text(right),
                },
            )
        if right is not None:
            self._emit_value_origin(owner, right, emit, destination="assignment")
            self._emit_value_shape(owner, right, emit)
            self._maybe_emit_callable_value(owner, right, emit, source="assignment_value")

    def _emit_branch_facts(self, node, source: str, file_path: str, emit) -> None:
        owner = self._owner_for_fact(node)
        if owner is None:
            return
        kind = node.type.replace("_statement", "").replace("_expression", "")
        emit(owner, node, "cfg", "branch_selector", payload={"kind": kind})
        condition = self._branch_condition_node(node)
        if condition is not None:
            payload = {
                "kind": kind,
                "condition": self._expr_text(condition, source),
                "condition_kind": condition.type,
                "reads": self._read_expression_payloads(condition, source),
            }
            emit(owner, condition, "cfg", "branch_condition", payload=payload)
            emit(owner, condition, "dfg", "branch_influence", payload=payload)

    def _emit_loop_facts(self, node, source: str, file_path: str, emit) -> None:
        owner = self._owner_for_fact(node)
        if owner is None:
            return
        emit(owner, node, "cfg", "loop_driver")
        left = node.child_by_field_name("left") or node.child_by_field_name("initializer")
        right = node.child_by_field_name("right") or node.child_by_field_name("condition")
        if node.type in {"for_in_statement", "for_of_statement"}:
            right = node.child_by_field_name("right")
            left = node.child_by_field_name("left")
        if left is not None:
            emit(
                owner,
                left,
                "dfg",
                "assignment_binding",
                payload={"target": self._expr_text(left, source), "source_kind": "loop_target"},
            )
        if right is not None and node.type in {
            "for_in_statement",
            "for_of_statement",
            "for_statement",
        }:
            emit(
                owner,
                right,
                "dfg",
                "iteration_source",
                payload={
                    "target": self._expr_text(left, source) if left else "",
                    "iterable": self._expr_text(right, source),
                    "async": False,
                },
            )

    def _emit_await_facts(self, node, source: str, file_path: str, emit) -> None:
        owner = self._owner_for_fact(node)
        if owner is None:
            return
        inner = self._first_named_child(node)
        if (
            inner is not None
            and inner.type == "assignment_expression"
            and self._is_using_declaration(inner)
        ):
            self._emit_using_facts(inner, source, file_path, emit, is_async=True, await_node=node)
            return
        emit(owner, node, "cfg", "async_suspend_resume")

    def _emit_using_facts(
        self,
        node,
        source: str,
        file_path: str,
        emit,
        *,
        is_async: bool = False,
        await_node=None,
    ) -> None:
        owner = self._owner_for_fact(node)
        if owner is None:
            return
        anchor = await_node or node
        emit(
            owner, anchor, "cfg", "context_enter_exit", payload={"kind": "using", "async": is_async}
        )
        if is_async and await_node is not None:
            emit(owner, await_node, "cfg", "async_suspend_resume")
        name = self._using_binding_name(node)
        if name:
            emit(owner, node, "dfg", "context_resource", payload={"target": name})

    def _emit_augmented_assignment_facts(self, node, source: str, file_path: str, emit) -> None:
        owner = self._owner_for_fact(node)
        if owner is None:
            return
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        operator = node.child_by_field_name("operator")
        emit(
            owner,
            node,
            "dfg",
            "augmented_mutation",
            payload={"operator": self.adapter._node_text(operator) if operator else ""},
        )
        if left is not None and left.type == "member_expression":
            target = self._dotted_name(left)
            payload = {"attribute": self._member_property(left), "target": target}
            emit(owner, left, "dfg", "attr_write", payload=payload)
            if self._is_this_member(left):
                emit(owner, left, "struct", "instance_attribute_hint", payload=payload)
        elif left is not None and left.type == "subscript_expression":
            container = self._dotted_name(left.child_by_field_name("object"))
            key = left.child_by_field_name("index")
            key_payload = self._key_payload(key, source)
            subscript_payload = cast(
                dict[str, object],
                {
                    "target": self._expr_text(left, source),
                    "container": container,
                    **key_payload,
                },
            )
            emit(owner, left, "dfg", "subscript_write", payload=subscript_payload)
            emit(owner, left, "dfg", "container_write_value", payload=subscript_payload)
            emit(
                owner,
                left,
                "dfg",
                "keyed_write",
                payload={
                    **subscript_payload,
                    **self._keyed_write_payload(key, right, source, container),
                },
            )
        if right is not None:
            self._emit_value_shape(owner, right, emit)

    def _emit_yield_facts(self, node, source: str, file_path: str, emit) -> None:
        owner = self._owner_for_fact(node)
        if owner is None:
            return
        emit(owner, node, "cfg", "generator_yield")
        value = self._first_named_child(node)
        if value is not None:
            emit(owner, value, "dfg", "yield_output", payload={"value_kind": value.type})

    def _emit_try_facts(self, node, source: str, file_path: str, emit) -> None:
        owner = self._owner_for_fact(node)
        if owner is None:
            return
        emit(owner, node, "cfg", "exception_transfer")

    def _emit_catch_facts(self, node, source: str, file_path: str, emit) -> None:
        owner = self._owner_for_fact(node)
        if owner is None:
            return
        emit(owner, node, "cfg", "exception_transfer")
        param = node.child_by_field_name("parameter") or node.child_by_field_name("name")
        type_node = node.child_by_field_name("type")
        payload = {
            "caught_type": self._expr_text(type_node, source) if type_node else "",
            "bound_name": self._expr_text(param, source) if param else "",
        }
        emit(owner, node, "cfg", "exception_handler_type", payload=payload)
        if param is not None:
            emit(owner, param, "dfg", "exception_value", payload={"name": payload["bound_name"]})

    def _emit_throw_facts(self, node, source: str, file_path: str, emit) -> None:
        owner = self._owner_for_fact(node)
        if owner is None:
            return
        emit(owner, node, "cfg", "exception_transfer")
        value = self._first_named_child(node)
        payload: dict[str, object] = {"raise_kind": "bare" if value is None else "expression"}
        if value is not None:
            payload.update(self._expr_payload(value, source))
            if value.type == "new_expression":
                payload["destination"] = "raise"
                payload["callee"] = self._callee_name(value.child_by_field_name("constructor"))
        emit(owner, node, "cfg", "exception_raise_value", payload=payload)

    def _emit_member_read_facts(self, node, source: str, file_path: str, emit) -> None:
        if not self._is_attr_read(node):
            return
        owner = self._owner_for_fact(node)
        if owner is None:
            return
        emit(
            owner,
            node,
            "dfg",
            "attr_read",
            payload={
                "attribute": self._member_property(node),
                "receiver": self._dotted_name(node.child_by_field_name("object")),
            },
        )

    def _emit_subscript_read_facts(self, node, source: str, file_path: str, emit) -> None:
        if not self._is_subscript_read(node):
            return
        owner = self._owner_for_fact(node)
        if owner is None:
            return
        key = node.child_by_field_name("index")
        container = self._dotted_name(node.child_by_field_name("object"))
        payload = {"container": container, **self._key_payload(key, source)}
        emit(owner, node, "dfg", "subscript_read", payload=payload)
        emit(owner, node, "dfg", "container_read_key", payload=payload)
        emit(owner, node, "dfg", "keyed_read", payload=payload)
        if payload.get("key_literal"):
            emit(
                owner,
                key,
                "struct",
                "literal_key",
                payload={**payload, "context": "subscript_read"},
            )

    def _emit_parameter_facts(self, node, source: str, file_path: str, emit) -> None:
        params = self._parameters_node(node)
        if params is None:
            return
        index = 0
        for param in params.named_children:
            if param.type not in self._PARAMETER_TYPES:
                continue
            name_node = self._parameter_name_node(param)
            name = self.adapter._node_text(name_node) if name_node is not None else ""
            payload = {"name": name, "index": index}
            emit(node, param, "struct", "parameter_decl", payload=payload)
            emit(node, param, "dfg", "parameter_input", payload=payload)
            default = self._parameter_default_node(param)
            if default is not None:
                default_payload = {
                    "name": name,
                    "default": self._expr_text(default, source),
                    "default_kind": default.type,
                }
                emit(node, default, "struct", "parameter_default", payload=default_payload)
                emit(node, default, "dfg", "parameter_default_value", payload=default_payload)
                self._maybe_emit_callable_value(
                    node,
                    default,
                    emit,
                    source="parameter_default",
                    payload={"parameter": name},
                )
            type_node = self._type_annotation_node(param)
            if type_node is not None:
                self._emit_annotation_facts(node, type_node, emit, target=name)
            index += 1

    def _emit_return_annotation(self, node, source: str, file_path: str, emit) -> None:
        return_type = node.child_by_field_name("return_type")
        if return_type is None:
            lookup = getattr(self.adapter, "_node_field_by_type", None)
            return_type = lookup(node, "type_annotation") if callable(lookup) else None
        if return_type is None:
            return
        self._emit_annotation_facts(node, return_type, emit, target="return")

    def _emit_inheritance_facts(self, node, source: str, file_path: str, emit) -> None:
        seen: set[str] = set()
        for child in node.children:
            if child.type not in {"class_heritage", "extends_clause", "implements_clause"}:
                continue
            for ref in self.adapter._iter_nodes(child):
                if ref.type not in {"identifier", "type_identifier"}:
                    continue
                name = self.adapter._node_text(ref)
                if not name or name in seen:
                    continue
                seen.add(name)
                emit(node, ref, "struct", "inheritance", payload={"base": name})

    def _emit_annotation_facts(self, owner, type_node, emit, *, target: str) -> None:
        annotation = self._annotation_text(type_node)
        payload = {"target": target, "annotation": annotation}
        emit(owner, type_node, "struct", "annotation", payload=payload)
        for generic in self._generic_type_nodes(type_node):
            emit(
                owner,
                generic,
                "struct",
                "generic_shape",
                payload={
                    **payload,
                    "generic": self._expr_text(generic, ""),
                    "base": self._expr_text(generic.child_by_field_name("name"), ""),
                },
            )

    def _emit_value_origin(self, owner, value, emit, *, destination: str) -> None:
        if value.type == "call_expression":
            emit(
                owner,
                value,
                "dfg",
                "call_result_origin",
                payload={
                    "callee": self._callee_name(value.child_by_field_name("function")),
                    "destination": destination,
                },
            )
        if value.type == "new_expression":
            payload = {
                "callee": self._callee_name(value.child_by_field_name("constructor")),
                "destination": destination,
            }
            emit(owner, value, "dfg", "constructor_value", payload=payload)
            emit(owner, value, "dfg", "constructed_output", payload=payload)

    def _emit_value_shape(self, owner, node, emit) -> None:
        shape = self._shape_for_value(node)
        if not shape:
            return
        emit(owner, node, "dfg", "collection_assembly", payload={"shape": shape})
        emit(owner, node, "struct", "literal_shape", payload={"shape": shape})

    def _emit_container_call_facts(
        self, owner, node, emit, base_payload: dict[str, object]
    ) -> None:
        fn = node.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            return
        method = self._member_property(fn)
        container = self._dotted_name(fn.child_by_field_name("object"))
        if method in _CONTAINER_MUTATION_METHODS:
            payload = {**base_payload, "container": container, "method": method}
            emit(owner, node, "dfg", "container_write_value", payload=payload)
            if node.named_child_count and method in {"set", "delete"}:
                args = node.child_by_field_name("arguments")
                if args and args.named_child_count:
                    key = args.named_children[0]
                    key_payload = self._key_payload(key, "")
                    emit(
                        owner,
                        key,
                        "struct",
                        "literal_key",
                        payload={**key_payload, "context": "container_method_write"},
                    )
        if method in _CONTAINER_READ_METHODS:
            args = node.child_by_field_name("arguments")
            key = args.named_children[0] if args and args.named_child_count else None
            payload = {
                **base_payload,
                "container": container,
                "method": method,
                **self._key_payload(key, ""),
            }
            emit(owner, node, "dfg", "container_read_key", payload=payload)
            emit(owner, node, "dfg", "keyed_read", payload=payload)

    def _maybe_emit_callable_value(
        self,
        owner,
        node,
        emit,
        *,
        source: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        callable_payload = self._callable_value_payload(node)
        if callable_payload is None:
            return
        merged = dict(callable_payload)
        if payload:
            merged.update(payload)
        merged["source"] = source
        emit(owner, node, "dfg", "callable_value", payload=merged)

    def _callable_value_payload(self, node) -> dict[str, object] | None:
        if node.type in {"arrow_function", "function_expression"}:
            return {"callable_kind": node.type, **self._expr_payload(node, "")}
        if node.type == "identifier":
            return {"callable_kind": "known_name", "name": self.adapter._node_text(node)}
        return None

    def _decorated_node_for(self, deco):
        decoratable = getattr(self.adapter, "_DECORATABLE_NODE_TYPES", frozenset())
        sibling_after = getattr(self.adapter, "_decoratable_sibling_after", None)
        parent = deco.parent
        if parent is None:
            return None
        if parent.type in decoratable:
            return parent
        if callable(sibling_after):
            return sibling_after(parent, deco)
        return None

    def _decorator_arguments(self, deco) -> list[dict[str, object]]:
        call = next((child for child in deco.children if child.type == "call_expression"), None)
        if call is None:
            return []
        return self._argument_payloads(call)

    def _shape_for_value(self, node) -> str:
        if node is None:
            return ""
        if node.type in self._SHAPE_TYPES:
            return self._SHAPE_TYPES[node.type]
        if node.type == "new_expression":
            ctor = self._callee_name(node.child_by_field_name("constructor"))
            if ctor in {"Map", "WeakMap", "Record"}:
                return "mapping"
            if ctor in {"Set", "WeakSet", "Array"}:
                return "sequence"
        if node.type == "call_expression":
            callee = self._callee_name(node.child_by_field_name("function"))
            if callee in {"Object.fromEntries", "Object.assign"}:
                return "mapping"
            if callee in {"Array.from"}:
                return "sequence"
        return ""

    def _return_shape_payload(self, node) -> dict[str, object]:
        shape = self._shape_for_value(node)
        payload = self._expr_payload(node, "")
        if shape:
            payload["shape_kind"] = shape
            payload["collection_shape"] = shape
        elif node.type == "new_expression":
            payload["shape_kind"] = "constructed"
        elif node.type == "call_expression":
            payload["shape_kind"] = "call_result"
        elif node.type in {"arrow_function", "function_expression"}:
            payload["shape_kind"] = "callable"
        elif node.type == "identifier":
            payload["shape_kind"] = "name"
        elif node.type == "member_expression":
            payload["shape_kind"] = "attribute"
        elif node.type == "subscript_expression":
            payload["shape_kind"] = "subscript"
        elif self._literal_text(node):
            payload["shape_kind"] = "literal"
        else:
            payload["shape_kind"] = node.type
        return payload

    def _argument_payloads(self, call_node) -> list[dict[str, object]]:
        args = call_node.child_by_field_name("arguments")
        if args is None:
            return []
        payloads: list[dict[str, object]] = []
        for index, arg in enumerate(args.named_children):
            payload: dict[str, object] = {
                "position": index,
                "kind": arg.type,
                **self._expr_payload(arg, ""),
            }
            literal = self._literal_text(arg)
            if literal:
                payload["literal"] = literal
            payloads.append(payload)
        return payloads

    def _parameters_node(self, node):
        return next((child for child in node.children if child.type == "formal_parameters"), None)

    def _parameter_name_node(self, param):
        direct = param.child_by_field_name("name")
        if direct is not None:
            return direct
        pattern = param.child_by_field_name("pattern")
        if pattern is not None:
            return pattern
        for child in param.named_children:
            if child.type in {"identifier", "property_identifier"}:
                return child
        return None

    def _parameter_default_node(self, param):
        if param.type == "assignment_pattern":
            return param.child_by_field_name("right")
        return param.child_by_field_name("value")

    def _type_annotation_node(self, node):
        type_node = node.child_by_field_name("type")
        if type_node is not None:
            return type_node
        return next(
            (child for child in node.named_children if child.type == "type_annotation"), None
        )

    def _generic_type_nodes(self, type_node):
        for child in self.adapter._iter_nodes(type_node):
            if child.type in {"generic_type", "type_arguments"}:
                yield child

    def _first_named_child(self, node):
        return next(iter(node.named_children), None)

    def _is_using_declaration(self, node) -> bool:
        return any(child.type == "using" for child in node.children)

    def _using_binding_name(self, node) -> str:
        for child in node.named_children:
            if child.type == "identifier":
                return self.adapter._node_text(child)
        return ""

    def _node_has_decorators(self, node) -> bool:
        if any(child.type == "decorator" for child in node.children):
            return True
        parent = node.parent
        if parent is None:
            return False
        for sibling in parent.children:
            if sibling is node:
                break
            if sibling.type == "decorator":
                return True
        return False

    def _is_member_call(self, call_node) -> bool:
        fn = call_node.child_by_field_name("function")
        return fn is not None and fn.type == "member_expression"

    def _is_attr_read(self, node) -> bool:
        parent = node.parent
        if parent is not None:
            if parent.type == "call_expression" and parent.child_by_field_name("function") is node:
                return False
            if (
                parent.type == "assignment_expression"
                and parent.child_by_field_name("left") is node
            ):
                return False
            if parent.type == "member_expression" and parent.child_by_field_name("object") is node:
                return False
        return True

    def _is_subscript_read(self, node) -> bool:
        parent = node.parent
        if parent is not None:
            if (
                parent.type == "assignment_expression"
                and parent.child_by_field_name("left") is node
            ):
                return False
            if parent.type == "member_expression" and parent.child_by_field_name("object") is node:
                return False
        return True

    def _is_this_member(self, node) -> bool:
        obj = node.child_by_field_name("object")
        return obj is not None and obj.type == "this"

    def _is_class_body_declarator(self, node) -> bool:
        parent = node.parent
        while parent is not None:
            if parent.type in self._CLASS_TYPES:
                return True
            if parent.type in self._CALLABLE_TYPES:
                return False
            parent = parent.parent
        return False

    def _enclosing_class(self, node):
        parent = node.parent
        while parent is not None:
            if parent.type in self._CLASS_TYPES:
                return parent
            parent = parent.parent
        return None

    def _branch_condition_node(self, node):
        if node.type == "if_statement":
            return node.child_by_field_name("condition")
        if node.type == "switch_statement":
            return node.child_by_field_name("value")
        if node.type == "ternary_expression":
            return node.child_by_field_name("condition")
        return None

    def _contains_attr_read(self, node) -> bool:
        for child in self.adapter._iter_nodes(node):
            if child.type == "member_expression" and self._is_attr_read(child):
                return True
        return False

    def _read_expression_payloads(self, node, source: str) -> list[dict[str, object]]:
        reads: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for child in self.adapter._iter_nodes(node):
            if child.type == "subscript_expression" and self._is_subscript_read(child):
                payload = {
                    "read_kind": "subscript",
                    **self._expr_payload(child, source),
                    **self._key_payload(child.child_by_field_name("index"), source),
                }
            elif child.type == "member_expression" and self._is_attr_read(child):
                payload = {"read_kind": "attribute", **self._expr_payload(child, source)}
            elif child.type == "identifier":
                payload = {"read_kind": "name", **self._expr_payload(child, source)}
            else:
                continue
            key = (str(payload.get("expression", "")), str(payload.get("expression_kind", "")))
            if key in seen:
                continue
            seen.add(key)
            reads.append(payload)
        return reads

    def _callee_name(self, node) -> str:
        if node is None:
            return ""
        if node.type in {"identifier", "property_identifier", "type_identifier"}:
            return self.adapter._node_text(node)
        if node.type == "member_expression":
            return self._dotted_name(node)
        return self.adapter._node_text(node)

    def _member_property(self, node) -> str:
        prop = node.child_by_field_name("property")
        return self.adapter._node_text(prop) if prop is not None else ""

    def _dotted_name(self, node) -> str:
        if node is None:
            return ""
        if node.type in {"identifier", "property_identifier", "type_identifier", "this"}:
            return self.adapter._node_text(node)
        if node.type == "member_expression":
            obj = node.child_by_field_name("object")
            prop = node.child_by_field_name("property")
            left = self._dotted_name(obj)
            right = self._dotted_name(prop)
            if left and right:
                return f"{left}.{right}"
            return right or left
        if node.type == "subscript_expression":
            obj = node.child_by_field_name("object")
            return self._dotted_name(obj)
        return self.adapter._node_text(node)

    def _annotation_text(self, node) -> str:
        text = self.adapter._node_text(node).strip()
        return text[1:].strip() if text.startswith(":") else text

    def _literal_text(self, node) -> str:
        if node is None:
            return ""
        if node.type in {"string", "number", "true", "false", "null", "undefined"}:
            return self.adapter._node_text(node)
        if node.type == "template_string":
            return self.adapter._node_text(node)
        return ""

    def _expr_text(self, node, source: str) -> str:
        if node is None:
            return ""
        return self.adapter._node_text(node)

    def _expr_payload(self, node, source: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "expression": self._expr_text(node, source),
            "expression_kind": node.type,
        }
        if node.type == "identifier":
            payload["name"] = self.adapter._node_text(node)
        elif node.type == "member_expression":
            payload["attribute"] = self._member_property(node)
            payload["receiver"] = self._dotted_name(node.child_by_field_name("object"))
        elif node.type == "subscript_expression":
            payload["container"] = self._dotted_name(node.child_by_field_name("object"))
            payload["key"] = self._expr_text(node.child_by_field_name("index"), source)
        elif node.type == "call_expression":
            payload["callee"] = self._callee_name(node.child_by_field_name("function"))
        return payload

    def _key_payload(self, key, source: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "key": self._expr_text(key, source),
            "key_kind": key.type if key is not None else "",
        }
        literal = self._literal_text(key)
        if literal:
            payload["key_literal"] = literal
            payload["literal"] = literal
        return payload

    def _keyed_write_payload(self, key, value, source: str, container: str) -> dict[str, object]:
        payload = {"container": container, **self._key_payload(key, source)}
        if value is not None:
            payload["value"] = self._expr_text(value, source)
            payload["value_kind"] = value.type
        return payload

    def _evidence(self, node, source: str) -> str:
        text = self._expr_text(node, source).replace("\n", " ").strip()
        if len(text) > 160:
            return text[:157] + "..."
        if text:
            return text
        return f"<ts:{node.type}>"


__all__ = ["TypeScriptAxisExtractor"]
