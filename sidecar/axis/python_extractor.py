"""Python AST extractor for physical CFG/DFG/Structural axis bits."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sidecar.axis.schema import AxisExtraction, AxisFact, AxisName
from sidecar.parser.uid import UNRESOLVED_SIGNATURE, compute_uid, module_name_from_path

_CONTAINER_MUTATION_METHODS = frozenset(
    {
        "add",
        "append",
        "extend",
        "insert",
        "setdefault",
        "update",
    }
)
_CONTAINER_READ_METHODS = frozenset({"get"})


@dataclass(frozen=True)
class _SymbolScope:
    uid: str
    qualified_name: str
    kind: str
    is_function: bool = False
    is_class: bool = False


class PythonAxisExtractor:
    """Extract AST-physical axis facts from Python source.

    This class intentionally avoids framework names and semantic role labels.
    It only emits normalized bits that can later feed contracts/query planning.
    """

    language = "python"

    def extract(
        self, source: str, file_path: str, *, project_root: str | None = None
    ) -> AxisExtraction:
        tree = ast.parse(source, filename=file_path)
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        module_name = module_name_from_path(file_path, project_root=project_root)
        module_scope = _SymbolScope(
            uid=compute_uid(module_name, UNRESOLVED_SIGNATURE, self.language),
            qualified_name=module_name,
            kind="module",
        )
        visitor = _AxisVisitor(source, file_path, module_name, module_scope, parents)
        visitor.visit(tree)
        return AxisExtraction(file_path=file_path, facts=visitor.facts)

    def extract_facts(
        self,
        source: str,
        file_path: str,
        *,
        project_root: str | None = None,
    ) -> list[AxisFact]:
        return self.extract(source, file_path, project_root=project_root).facts


class _AxisVisitor(ast.NodeVisitor):
    def __init__(
        self,
        source: str,
        file_path: str,
        module_name: str,
        module_scope: _SymbolScope,
        parents: dict[ast.AST, ast.AST],
    ):
        self.source = source
        self.file_path = file_path
        self.module_name = module_name
        self.language = PythonAxisExtractor.language
        self.parents = parents
        self.facts: list[AxisFact] = []
        self.scope_stack: list[_SymbolScope] = [module_scope]
        self.callable_bindings_stack: list[set[str]] = [set()]

    @property
    def current_scope(self) -> _SymbolScope:
        return self.scope_stack[-1]

    @property
    def current_callable_bindings(self) -> set[str]:
        return self.callable_bindings_stack[-1]

    def visit_Module(self, node: ast.Module) -> None:
        self._emit("struct", "module_scope", node, scope=self.current_scope)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._emit(
                "struct",
                "import_dependency",
                node,
                payload={"module": alias.name, "alias": alias.asname or ""},
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = "." * int(node.level or 0) + (node.module or "")
        for alias in node.names:
            self._emit(
                "struct",
                "import_dependency",
                node,
                payload={"module": module, "name": alias.name, "alias": alias.asname or ""},
            )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.current_callable_bindings.add(node.name)
        scope = self._class_scope(node)
        self._emit("struct", "class_def", node, scope=scope, payload={"name": node.name})
        self._emit(
            "dfg",
            "callable_value",
            node,
            scope=scope,
            payload={"callable_kind": "class", "origin": "definition", "name": node.name},
        )
        if node.decorator_list:
            self._emit_decorators(node, scope)
        for base in node.bases:
            self._emit(
                "struct",
                "inheritance",
                base,
                scope=scope,
                payload={"base": _unparse(base)},
            )
        for keyword in node.keywords:
            if keyword.arg == "metaclass":
                self._emit(
                    "struct",
                    "metaclass",
                    keyword.value,
                    scope=scope,
                    payload={"metaclass": _unparse(keyword.value)},
                )
            else:
                self._emit(
                    "struct",
                    "base_keyword",
                    keyword.value,
                    scope=scope,
                    payload={"keyword": keyword.arg or "**", "value": _unparse(keyword.value)},
                )

        self.scope_stack.append(scope)
        self.callable_bindings_stack.append(set())
        try:
            for decorator in node.decorator_list:
                self.visit(decorator)
            for base in node.bases:
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword.value)
            for stmt in node.body:
                if _is_class_attribute_statement(stmt):
                    self._emit("struct", "class_attribute", stmt, payload=_assignment_payload(stmt))
                self.visit(stmt)
        finally:
            self.callable_bindings_stack.pop()
            self.scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, async_function=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, async_function=True)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._emit("cfg", "callable_body", node, payload={"callable_kind": "lambda"})
        self._emit(
            "dfg",
            "callable_value",
            node,
            payload={"callable_kind": "lambda", "origin": "expression"},
        )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        payload = {"callee": _call_name(node.func)}
        self._emit("cfg", "call_site", node, payload=payload)
        if _is_value_call_callee(node.func):
            self._emit(
                "cfg",
                "value_call",
                node.func,
                payload={"callee": _unparse(node.func), "callee_kind": type(node.func).__name__},
            )
        if isinstance(node.func, ast.Attribute):
            self._emit("cfg", "method_dispatch", node.func, payload=payload)
        if _looks_like_constructor_call(node.func):
            self._emit("cfg", "constructor_call", node, payload=payload)
            self._emit("dfg", "constructor_value", node, payload=payload)
        self._emit_call_argument_facts(node)
        self._emit_container_call_facts(node)
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        self._emit("cfg", "branch_selector", node, payload={"kind": "if"})
        self._emit_branch_condition(node.test, kind="if")
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        self._emit("cfg", "branch_selector", node, payload={"kind": "match"})
        self._emit_branch_condition(node.subject, kind="match_subject")
        for case in node.cases:
            if case.guard is not None:
                self._emit_branch_condition(case.guard, kind="match_guard")
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self._emit("cfg", "branch_selector", node, payload={"kind": "if_expression"})
        self._emit_branch_condition(node.test, kind="if_expression")
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self._emit("cfg", "loop_driver", node)
        self._emit_iteration_source(node.target, node.iter)
        self._emit_binding_targets(node.target, "loop_target")
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._emit("cfg", "loop_driver", node)
        self._emit("cfg", "async_suspend_resume", node)
        self._emit_iteration_source(node.target, node.iter, async_iteration=True)
        self._emit_binding_targets(node.target, "loop_target")
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self._emit("cfg", "loop_driver", node)
        self._emit_branch_condition(node.test, kind="while")
        self.generic_visit(node)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._emit("cfg", "loop_driver", node)
        self._emit("dfg", "collection_assembly", node, payload={"shape": "list"})
        self._emit("struct", "literal_shape", node, payload={"shape": "list"})
        self.generic_visit(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._emit("cfg", "loop_driver", node)
        self._emit("dfg", "collection_assembly", node, payload={"shape": "set"})
        self._emit("struct", "literal_shape", node, payload={"shape": "set"})
        self.generic_visit(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._emit("cfg", "loop_driver", node)
        self._emit("dfg", "collection_assembly", node, payload={"shape": "dict"})
        self._emit("struct", "literal_shape", node, payload={"shape": "dict"})
        self.generic_visit(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._emit("cfg", "loop_driver", node)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        self._emit("cfg", "context_enter_exit", node)
        for item in node.items:
            if item.optional_vars is not None:
                self._emit(
                    "dfg",
                    "context_resource",
                    item.optional_vars,
                    payload={"target": _unparse(item.optional_vars)},
                )
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._emit("cfg", "context_enter_exit", node)
        self._emit("cfg", "async_suspend_resume", node)
        for item in node.items:
            if item.optional_vars is not None:
                self._emit(
                    "dfg",
                    "context_resource",
                    item.optional_vars,
                    payload={"target": _unparse(item.optional_vars)},
                )
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self._emit("cfg", "exception_transfer", node)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._emit("cfg", "exception_transfer", node)
        self._emit("cfg", "exception_handler_type", node, payload=_exception_handler_payload(node))
        if node.name:
            self._emit("dfg", "exception_value", node, payload={"name": node.name})
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        self._emit("cfg", "exception_transfer", node)
        self._emit("cfg", "exception_raise_value", node, payload=_raise_payload(node))
        self.generic_visit(node)

    def visit_Await(self, node: ast.Await) -> None:
        self._emit("cfg", "async_suspend_resume", node)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        self._emit("cfg", "return_exit", node)
        self._emit("dfg", "return_shape_kind", node, payload=_return_shape_payload(node.value))
        if node.value is not None:
            self._emit("dfg", "return_output", node.value)
            self._maybe_emit_callable_value(node.value, source="return")
            if _looks_like_constructed_output(node.value):
                value = node.value
                assert isinstance(value, ast.Call)
                self._emit(
                    "dfg",
                    "constructed_output",
                    value,
                    payload=_constructed_output_payload(value, destination="return"),
                )
            if _contains_attr_read(node.value):
                self._emit("dfg", "projection", node.value)
        self.generic_visit(node)

    def visit_Yield(self, node: ast.Yield) -> None:
        self._emit("cfg", "generator_yield", node)
        if node.value is not None:
            self._emit("dfg", "yield_output", node.value)
        self.generic_visit(node)

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:
        self._emit("cfg", "generator_yield", node)
        self._emit("dfg", "yield_output", node.value)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._emit("dfg", "assignment_binding", node, payload=_assignment_payload(node))
        self._emit_assignment_value_bits(node.value)
        self._maybe_emit_callable_value(node.value, source="assignment_value")
        for target in node.targets:
            self._emit_assignment_target_bits(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._emit_annotation_facts(
            node.annotation,
            payload={"kind": "assignment", "target": _unparse(node.target)},
        )
        self._emit("dfg", "assignment_binding", node, payload=_assignment_payload(node))
        if node.value is not None:
            self._emit_assignment_value_bits(node.value)
            self._maybe_emit_callable_value(node.value, source="assignment_value")
        self._emit_assignment_target_bits(node.target, node.value)
        self.visit(node.target)
        if node.value is not None:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._emit("dfg", "augmented_mutation", node)
        self._emit_assignment_target_bits(node.target, node.value)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._emit("dfg", "assignment_binding", node, payload={"target": _unparse(node.target)})
        self._emit_assignment_value_bits(node.value)
        self._maybe_emit_callable_value(node.value, source="assignment_value")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load) and not self._is_call_func(node):
            self._emit("dfg", "attr_read", node, payload={"attribute": node.attr})
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Load):
            payload = _subscript_key_payload(node)
            self._emit("dfg", "subscript_read", node, payload=payload)
            self._emit("dfg", "container_read_key", node, payload=payload)
            self._emit("dfg", "keyed_read", node, payload=payload)
            self._emit_literal_key(
                node.slice, context="subscript_read", container=_unparse(node.value)
            )
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        self._emit("dfg", "collection_assembly", node, payload={"shape": "dict"})
        self._emit("struct", "literal_shape", node, payload={"shape": "dict"})
        for key, value in zip(node.keys, node.values, strict=False):
            if key is not None:
                self._emit(
                    "dfg",
                    "keyed_write",
                    key,
                    payload=_keyed_write_payload(key=key, value=value, container="dict_literal"),
                )
                self._emit_literal_key(key, context="dict_literal")
            if value is not None:
                if key is not None:
                    self._maybe_emit_callable_value(
                        value,
                        source="keyed_write",
                        payload={"container": "dict_literal", "key": _unparse(key)},
                    )
                self._maybe_emit_callable_value(value, source="collection_value")
        self.generic_visit(node)

    def visit_List(self, node: ast.List) -> None:
        if isinstance(node.ctx, ast.Load):
            self._emit("dfg", "collection_assembly", node, payload={"shape": "list"})
            self._emit("struct", "literal_shape", node, payload={"shape": "list"})
            for elt in node.elts:
                self._maybe_emit_callable_value(elt, source="collection_value")
        self.generic_visit(node)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        if isinstance(node.ctx, ast.Load):
            self._emit("dfg", "collection_assembly", node, payload={"shape": "tuple"})
            self._emit("struct", "literal_shape", node, payload={"shape": "tuple"})
            for elt in node.elts:
                self._maybe_emit_callable_value(elt, source="collection_value")
        self.generic_visit(node)

    def visit_Set(self, node: ast.Set) -> None:
        self._emit("dfg", "collection_assembly", node, payload={"shape": "set"})
        self._emit("struct", "literal_shape", node, payload={"shape": "set"})
        for elt in node.elts:
            self._maybe_emit_callable_value(elt, source="collection_value")
        self.generic_visit(node)

    def _visit_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, *, async_function: bool
    ) -> None:
        self.current_callable_bindings.add(node.name)
        scope = self._function_scope(node)
        self._emit(
            "struct",
            "async_function_def" if async_function else "function_def",
            node,
            scope=scope,
            payload={"name": node.name},
        )
        self._emit(
            "cfg",
            "callable_body",
            node,
            scope=scope,
            payload={"callable_kind": "async_function" if async_function else "function"},
        )
        self._emit(
            "dfg",
            "callable_value",
            node,
            scope=scope,
            payload={
                "callable_kind": "async_function" if async_function else "function",
                "origin": "definition",
                "name": node.name,
                "decorated": bool(node.decorator_list),
            },
        )
        if async_function:
            self._emit("cfg", "async_suspend_resume", node, scope=scope)
        if len(self.scope_stack) >= 2 and self.scope_stack[-1].is_class:
            self._emit(
                "struct",
                "method_member",
                node,
                scope=scope,
                payload={"owner": self.scope_stack[-1].qualified_name},
            )
        if node.decorator_list:
            self._emit_decorators(node, scope)
        self._emit_parameter_facts(node, scope)
        if node.returns is not None:
            self._emit_annotation_facts(
                node.returns,
                scope=scope,
                payload={"kind": "return"},
            )

        self.scope_stack.append(scope)
        self.callable_bindings_stack.append(set())
        try:
            for decorator in node.decorator_list:
                self.visit(decorator)
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if default is not None:
                    self.visit(default)
            for stmt in node.body:
                self.visit(stmt)
        finally:
            self.callable_bindings_stack.pop()
            self.scope_stack.pop()

    def _emit_parameter_facts(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        scope: _SymbolScope,
    ) -> None:
        defaults = {arg.arg: default for arg, default in _iter_parameter_defaults(node.args)}
        for arg in _iter_args(node.args):
            payload = {"name": arg.arg}
            self._emit("struct", "parameter_decl", arg, scope=scope, payload=payload)
            self._emit("dfg", "parameter_input", arg, scope=scope, payload=payload)
            default = defaults.get(arg.arg)
            if default is not None:
                default_payload = {
                    "name": arg.arg,
                    "default": _unparse(default),
                    "default_kind": type(default).__name__,
                }
                self._emit(
                    "struct",
                    "parameter_default",
                    default,
                    scope=scope,
                    payload=default_payload,
                )
                self._emit(
                    "dfg",
                    "parameter_default_value",
                    default,
                    scope=scope,
                    payload=default_payload,
                )
                self._maybe_emit_callable_value(
                    default,
                    scope=scope,
                    source="parameter_default",
                    payload={"parameter": arg.arg},
                )
            if arg.annotation is not None:
                self._emit_annotation_facts(
                    arg.annotation,
                    scope=scope,
                    payload={"kind": "parameter", "name": arg.arg},
                )

    def _emit_decorators(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        scope: _SymbolScope,
    ) -> None:
        for decorator in node.decorator_list:
            payload = {"decorator": _unparse(decorator)}
            self._emit("struct", "decorator_attachment", decorator, scope=scope, payload=payload)
            self._emit(
                "struct",
                "decorator_shape",
                decorator,
                scope=scope,
                payload=_decorator_shape_payload(decorator),
            )
            self._emit("cfg", "decorator_application", decorator, scope=scope, payload=payload)

    def _emit_assignment_value_bits(self, value: ast.AST | None) -> None:
        if value is None:
            return
        if isinstance(value, ast.Call):
            payload = {"callee": _call_name(value.func)}
            self._emit("dfg", "call_result_origin", value, payload=payload)
            if _looks_like_constructor_call(value.func):
                self._emit("dfg", "constructor_value", value, payload=payload)
                self._emit(
                    "dfg",
                    "constructed_output",
                    value,
                    payload=_constructed_output_payload(value, destination="assignment"),
                )
        if isinstance(
            value, (ast.Dict, ast.List, ast.Tuple, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)
        ):
            self._emit("dfg", "collection_assembly", value, payload={"shape": _shape_name(value)})

    def _emit_call_argument_facts(self, node: ast.Call) -> None:
        callee = _unparse(node.func)
        for index, arg in enumerate(node.args):
            expr = arg.value if isinstance(arg, ast.Starred) else arg
            payload = {
                "callee": callee,
                "position": index,
                "argument_kind": "starred" if isinstance(arg, ast.Starred) else "positional",
                **_expr_payload(expr),
            }
            self._emit("dfg", "call_argument", expr, payload=payload)
            self._maybe_emit_callable_value(
                expr,
                source="call_argument",
                payload={"callee": callee, "position": index},
            )
        for keyword in node.keywords:
            expr = keyword.value
            payload = {
                "callee": callee,
                "keyword": keyword.arg or "**",
                "argument_kind": "kwargs" if keyword.arg is None else "keyword",
                **_expr_payload(expr),
            }
            self._emit("dfg", "call_argument", expr, payload=payload)
            self._maybe_emit_callable_value(
                expr,
                source="call_argument",
                payload={"callee": callee, "keyword": keyword.arg or "**"},
            )

    def _emit_container_call_facts(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Attribute):
            return

        method = node.func.attr
        container = _unparse(node.func.value)
        if method in _CONTAINER_MUTATION_METHODS:
            values = _container_mutation_values(method, node)
            key = _container_mutation_key(method, node)
            payload: dict[str, Any] = {
                "container": container,
                "method": method,
                "callee": _unparse(node.func),
                "arguments": [_expr_payload(_call_arg_expr(arg)) for arg in node.args],
                "keywords": [
                    {"name": keyword.arg or "**", **_expr_payload(keyword.value)}
                    for keyword in node.keywords
                ],
            }
            if key is not None:
                payload.update(_key_payload(key))
            if values:
                payload["values"] = [_expr_payload(value) for value in values]
                payload["value"] = _unparse(values[0])
                payload["value_kind"] = type(values[0]).__name__

            self._emit("dfg", "container_write_value", node, payload=payload)
            for value in values:
                self._maybe_emit_callable_value(
                    value,
                    source="container_write_value",
                    payload={"container": container, "method": method},
                )
            if key is not None:
                self._emit_literal_key(key, context="container_method_write", container=container)

        if method in _CONTAINER_READ_METHODS and node.args:
            key = _call_arg_expr(node.args[0])
            payload = {
                "container": container,
                "method": method,
                "callee": _unparse(node.func),
                **_key_payload(key),
            }
            self._emit("dfg", "container_read_key", node, payload=payload)
            self._emit("dfg", "keyed_read", node, payload=payload)
            self._emit_literal_key(key, context="container_method_read", container=container)

    def _emit_branch_condition(self, condition: ast.AST, *, kind: str) -> None:
        payload = {
            "kind": kind,
            "condition": _unparse(condition),
            "condition_kind": type(condition).__name__,
            "reads": _read_expression_payloads(condition),
        }
        self._emit("cfg", "branch_condition", condition, payload=payload)
        self._emit("dfg", "branch_influence", condition, payload=payload)

    def _maybe_emit_callable_value(
        self,
        node: ast.AST,
        *,
        source: str,
        scope: _SymbolScope | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        callable_payload = self._callable_value_payload(node)
        if callable_payload is None:
            return
        if payload:
            callable_payload.update(payload)
        callable_payload["source"] = source
        self._emit("dfg", "callable_value", node, scope=scope, payload=callable_payload)

    def _callable_value_payload(self, node: ast.AST) -> dict[str, Any] | None:
        if isinstance(node, ast.Lambda):
            return {"callable_kind": "lambda", **_expr_payload(node)}
        if isinstance(node, ast.Name) and self._name_is_callable_binding(node.id):
            return {"callable_kind": "known_name", **_expr_payload(node)}
        return None

    def _name_is_callable_binding(self, name: str) -> bool:
        return any(name in bindings for bindings in reversed(self.callable_bindings_stack))

    def _emit_assignment_target_bits(self, target: ast.AST, value: ast.AST | None) -> None:
        for leaf in _flatten_targets(target):
            if isinstance(leaf, ast.Attribute):
                payload = {"attribute": leaf.attr, "target": _unparse(leaf)}
                self._emit("dfg", "attr_write", leaf, payload=payload)
                if _is_self_attribute(leaf):
                    self._emit("struct", "instance_attribute_hint", leaf, payload=payload)
            elif isinstance(leaf, ast.Subscript):
                payload = {"target": _unparse(leaf), **_subscript_key_payload(leaf)}
                self._emit("dfg", "subscript_write", leaf, payload=payload)
                self._emit_literal_key(
                    leaf.slice, context="subscript_write", container=_unparse(leaf.value)
                )
                if value is not None:
                    write_payload = {
                        **payload,
                        "value": _unparse(value),
                        "value_kind": type(value).__name__,
                    }
                    self._emit("dfg", "container_write_value", leaf, payload=write_payload)
                    self._emit(
                        "dfg",
                        "keyed_write",
                        leaf,
                        payload=_keyed_write_payload(
                            key=leaf.slice, value=value, container=_unparse(leaf.value)
                        ),
                    )
                    self._maybe_emit_callable_value(
                        value,
                        source="container_write_value",
                        payload={"container": _unparse(leaf.value), "key": _unparse(leaf.slice)},
                    )
            elif isinstance(leaf, ast.Name) and isinstance(value, ast.Name):
                self._emit(
                    "dfg",
                    "aliasing",
                    leaf,
                    payload={"target": leaf.id, "source": value.id},
                )

    def _emit_binding_targets(self, target: ast.AST, source_kind: str) -> None:
        for leaf in _flatten_targets(target):
            if isinstance(leaf, ast.Name):
                self._emit(
                    "dfg",
                    "assignment_binding",
                    leaf,
                    payload={"target": leaf.id, "source_kind": source_kind},
                )

    def _emit_iteration_source(
        self,
        target: ast.AST,
        iterable: ast.AST,
        *,
        async_iteration: bool = False,
    ) -> None:
        self._emit(
            "dfg",
            "iteration_source",
            iterable,
            payload={
                "target": _unparse(target),
                "target_kind": type(target).__name__,
                "iterable": _unparse(iterable),
                "iterable_kind": type(iterable).__name__,
                "async": async_iteration,
            },
        )

    def _emit_literal_key(
        self,
        key: ast.AST,
        *,
        context: str,
        container: str = "",
    ) -> None:
        payload = _literal_key_payload(key)
        if payload is None:
            return
        payload.update({"context": context})
        if container:
            payload["container"] = container
        self._emit("struct", "literal_key", key, payload=payload)

    def _emit_annotation_facts(
        self,
        annotation: ast.AST,
        *,
        scope: _SymbolScope | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        annotation_payload = {"annotation": _unparse(annotation)}
        if payload:
            annotation_payload.update(payload)
        self._emit("struct", "annotation", annotation, scope=scope, payload=annotation_payload)
        for generic in _iter_generic_shapes(annotation):
            self._emit(
                "struct",
                "generic_shape",
                generic,
                scope=scope,
                payload={**annotation_payload, **_generic_shape_payload(generic)},
            )

    def _class_scope(self, node: ast.ClassDef) -> _SymbolScope:
        qualified_name = self._qualified_child_name(node.name)
        return _SymbolScope(
            uid=compute_uid(qualified_name, f"{node.name}()->_", self.language),
            qualified_name=qualified_name,
            kind="class",
            is_class=True,
        )

    def _function_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> _SymbolScope:
        qualified_name = self._qualified_child_name(node.name)
        return _SymbolScope(
            uid=compute_uid(qualified_name, _signature_for_function(node), self.language),
            qualified_name=qualified_name,
            kind="function",
            is_function=True,
        )

    def _qualified_child_name(self, name: str) -> str:
        parts = [self.module_name]
        for scope in self.scope_stack[1:]:
            if scope.is_function:
                parts.append("<locals>")
            parts.append(scope.qualified_name.rsplit(".", 1)[-1])
        parts.append(name)
        return ".".join(parts)

    def _is_call_func(self, node: ast.Attribute) -> bool:
        parent = self.parents.get(node)
        return isinstance(parent, ast.Call) and parent.func is node

    def _emit(
        self,
        axis: AxisName,
        bit: str,
        node: ast.AST,
        *,
        scope: _SymbolScope | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        owner = scope or self.current_scope
        self.facts.append(
            AxisFact(
                symbol_uid=owner.uid,
                qualified_name=owner.qualified_name,
                symbol_kind=owner.kind,
                axis=axis,
                bit=bit,
                line=int(getattr(node, "lineno", 1) or 1),
                evidence=_evidence(node),
                ast_kind=type(node).__name__,
                payload=payload or {},
            )
        )


def _iter_args(args: ast.arguments) -> Iterable[ast.arg]:
    yield from args.posonlyargs
    yield from args.args
    if args.vararg is not None:
        yield args.vararg
    yield from args.kwonlyargs
    if args.kwarg is not None:
        yield args.kwarg


def _iter_parameter_defaults(args: ast.arguments) -> Iterable[tuple[ast.arg, ast.AST]]:
    positional = [*args.posonlyargs, *args.args]
    offset = len(positional) - len(args.defaults)
    for index, default in enumerate(args.defaults):
        yield positional[offset + index], default
    for index, arg in enumerate(args.kwonlyargs):
        kw_default = args.kw_defaults[index]
        if kw_default is None:
            continue
        yield arg, kw_default


def _signature_for_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    params: list[str] = []
    for arg in _iter_args(node.args):
        prefix = ""
        if arg is node.args.vararg:
            prefix = "*"
        elif arg is node.args.kwarg:
            prefix = "**"
        text = prefix + arg.arg
        if arg.annotation is not None:
            text = f"{text}: {_unparse(arg.annotation)}"
        params.append(text)
    returns = f"->{_unparse(node.returns)}" if node.returns is not None else ""
    return f"{node.name}({', '.join(params)}){returns}"


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return _unparse(func)


def _is_value_call_callee(func: ast.AST) -> bool:
    return not isinstance(func, ast.Attribute)


def _looks_like_constructor_call(func: ast.AST) -> bool:
    if isinstance(func, ast.Name):
        return bool(func.id[:1].isupper())
    if isinstance(func, ast.Attribute):
        return bool(func.attr[:1].isupper())
    return False


def _decorator_shape_payload(decorator: ast.AST) -> dict[str, Any]:
    payload = {"decorator": _unparse(decorator), **_expr_payload(decorator)}
    if isinstance(decorator, ast.Call):
        payload.update(
            {
                "callee": _unparse(decorator.func),
                "callee_kind": type(decorator.func).__name__,
                "args": [_expr_payload(arg) for arg in decorator.args],
                "keywords": [
                    {
                        "name": keyword.arg or "**",
                        **_expr_payload(keyword.value),
                    }
                    for keyword in decorator.keywords
                ],
            }
        )
    return payload


def _expr_payload(node: ast.AST) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "expression": _unparse(node),
        "expression_kind": type(node).__name__,
    }
    if isinstance(node, ast.Name):
        payload["name"] = node.id
    elif isinstance(node, ast.Attribute):
        payload["attribute"] = node.attr
        payload["receiver"] = _unparse(node.value)
    elif isinstance(node, ast.Subscript):
        payload["container"] = _unparse(node.value)
        payload["key"] = _unparse(node.slice)
    elif isinstance(node, ast.Constant):
        payload["literal"] = _json_safe_literal(node.value)
    elif isinstance(node, ast.Call):
        payload["callee"] = _unparse(node.func)
        payload["callee_kind"] = type(node.func).__name__
    return payload


def _read_expression_payloads(node: ast.AST) -> list[dict[str, Any]]:
    reads: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Subscript) and isinstance(child.ctx, ast.Load):
            payload = {
                "read_kind": "subscript",
                **_expr_payload(child),
                **_subscript_key_payload(child),
            }
        elif isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
            payload = {"read_kind": "attribute", **_expr_payload(child)}
        elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            payload = {"read_kind": "name", **_expr_payload(child)}
        else:
            continue
        key = (str(payload["expression"]), str(payload["expression_kind"]))
        if key in seen:
            continue
        seen.add(key)
        reads.append(payload)
    return reads


def _raise_payload(node: ast.Raise) -> dict[str, Any]:
    payload: dict[str, Any] = {"raise_kind": "bare"}
    if node.exc is not None:
        payload = {"raise_kind": "expression", **_expr_payload(node.exc)}
        if isinstance(node.exc, ast.Call):
            payload.update(_constructed_output_payload(node.exc, destination="raise"))
    if node.cause is not None:
        payload["cause"] = _unparse(node.cause)
        payload["cause_kind"] = type(node.cause).__name__
    return payload


def _exception_handler_payload(node: ast.ExceptHandler) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "caught_type": _unparse(node.type),
        "caught_type_kind": type(node.type).__name__ if node.type is not None else "bare",
        "bound_name": node.name or "",
    }
    if isinstance(node.type, ast.Tuple):
        payload["caught_types"] = [_unparse(elt) for elt in node.type.elts]
    return payload


def _return_shape_payload(value: ast.AST | None) -> dict[str, Any]:
    if value is None:
        return {"shape_kind": "none", "expression": "", "expression_kind": "None"}
    payload = {"shape_kind": _return_shape_kind(value), **_expr_payload(value)}
    if isinstance(
        value, (ast.Dict, ast.List, ast.Tuple, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)
    ):
        payload["collection_shape"] = _shape_name(value)
    return payload


def _return_shape_kind(value: ast.AST) -> str:
    if isinstance(value, ast.Dict):
        return "mapping"
    if isinstance(value, (ast.List, ast.Tuple, ast.ListComp)):
        return "sequence"
    if isinstance(value, (ast.Set, ast.SetComp)):
        return "set"
    if _looks_like_constructed_output(value):
        return "constructed"
    if isinstance(value, ast.Call):
        return "call_result"
    if isinstance(value, ast.Lambda):
        return "callable"
    if isinstance(value, ast.Name):
        return "name"
    if isinstance(value, ast.Attribute):
        return "attribute"
    if isinstance(value, ast.Subscript):
        return "subscript"
    if isinstance(value, ast.Constant):
        return "literal"
    return type(value).__name__


def _looks_like_constructed_output(value: ast.AST | None) -> bool:
    return isinstance(value, ast.Call) and _looks_like_constructor_call(value.func)


def _constructed_output_payload(call: ast.Call, *, destination: str) -> dict[str, Any]:
    return {
        "destination": destination,
        "callee": _unparse(call.func),
        "callee_kind": type(call.func).__name__,
        "args": [_expr_payload(_call_arg_expr(arg)) for arg in call.args],
        "keywords": [
            {
                "keyword": keyword.arg or "**",
                **_expr_payload(keyword.value),
            }
            for keyword in call.keywords
        ],
    }


def _iter_generic_shapes(annotation: ast.AST) -> Iterable[ast.Subscript]:
    for node in ast.walk(annotation):
        if isinstance(node, ast.Subscript):
            yield node


def _generic_shape_payload(node: ast.Subscript) -> dict[str, Any]:
    return {
        "generic": _unparse(node),
        "base": _unparse(node.value),
        "args": [_expr_payload(arg) for arg in _generic_args(node.slice)],
    }


def _generic_args(slice_node: ast.AST) -> list[ast.AST]:
    if isinstance(slice_node, ast.Tuple):
        return list(slice_node.elts)
    return [slice_node]


_NO_LITERAL = object()


def _call_arg_expr(node: ast.AST) -> ast.AST:
    return node.value if isinstance(node, ast.Starred) else node


def _key_payload(key: ast.AST) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "key": _unparse(key),
        "key_kind": type(key).__name__,
    }
    literal = _literal_key_value(key)
    if literal is not _NO_LITERAL:
        payload["key_literal"] = literal
    return payload


def _subscript_key_payload(node: ast.Subscript) -> dict[str, Any]:
    return {
        "container": _unparse(node.value),
        "container_kind": type(node.value).__name__,
        "subscript": _unparse(node),
        **_key_payload(node.slice),
    }


def _keyed_write_payload(
    *,
    key: ast.AST,
    value: ast.AST | None,
    container: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"container": container, **_key_payload(key)}
    if value is not None:
        payload["value"] = _unparse(value)
        payload["value_kind"] = type(value).__name__
    return payload


def _literal_key_payload(key: ast.AST) -> dict[str, Any] | None:
    literal = _literal_key_value(key)
    if literal is _NO_LITERAL:
        return None
    return {
        "key": _unparse(key),
        "key_kind": type(key).__name__,
        "key_literal": literal,
        "literal": literal,
    }


def _literal_key_value(key: ast.AST) -> object:
    if isinstance(key, ast.Constant):
        return _json_safe_literal(key.value)
    return _NO_LITERAL


def _container_mutation_values(method: str, node: ast.Call) -> list[ast.AST]:
    args = [_call_arg_expr(arg) for arg in node.args]
    if method in {"add", "append", "extend", "update"}:
        values = args
    elif method == "insert":
        values = args[1:]
    elif method == "setdefault":
        values = args[1:]
    else:
        values = []

    if method == "update":
        values.extend(keyword.value for keyword in node.keywords if keyword.arg is not None)
    return values


def _container_mutation_key(method: str, node: ast.Call) -> ast.AST | None:
    if method in {"insert", "setdefault"} and node.args:
        return _call_arg_expr(node.args[0])
    return None


def _json_safe_literal(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _flatten_targets(target: ast.AST) -> Iterable[ast.AST]:
    if isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _flatten_targets(elt)
    else:
        yield target


def _is_self_attribute(node: ast.Attribute) -> bool:
    return isinstance(node.value, ast.Name) and node.value.id in {"self", "cls"}


def _contains_attr_read(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load)
        for child in ast.walk(node)
    )


def _is_class_attribute_statement(stmt: ast.stmt) -> bool:
    return isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign))


def _assignment_payload(node: ast.AST) -> dict[str, Any]:
    if isinstance(node, ast.Assign):
        return {
            "targets": [_unparse(target) for target in node.targets],
            "value": _unparse(node.value),
        }
    if isinstance(node, ast.AnnAssign):
        return {
            "target": _unparse(node.target),
            "annotation": _unparse(node.annotation),
            "value": _unparse(node.value) if node.value is not None else "",
        }
    if isinstance(node, ast.AugAssign):
        return {"target": _unparse(node.target), "value": _unparse(node.value)}
    return {}


def _shape_name(node: ast.AST) -> str:
    if isinstance(node, (ast.Dict, ast.DictComp)):
        return "dict"
    if isinstance(node, (ast.List, ast.ListComp)):
        return "list"
    if isinstance(node, ast.Tuple):
        return "tuple"
    if isinstance(node, (ast.Set, ast.SetComp)):
        return "set"
    return type(node).__name__


def _evidence(node: ast.AST) -> str:
    text = _unparse(node).replace("\n", " ").strip()
    if len(text) > 160:
        return text[:157] + "..."
    return text


def _unparse(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - ast.unparse is best-effort evidence
        return type(node).__name__
