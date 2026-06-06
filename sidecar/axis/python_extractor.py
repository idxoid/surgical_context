"""Python AST extractor for physical CFG/DFG/Structural axis bits."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Iterable

from sidecar.axis.schema import AxisExtraction, AxisFact, AxisName
from sidecar.parser.uid import UNRESOLVED_SIGNATURE, compute_uid, module_name_from_path


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

    def extract(self, source: str, file_path: str, *, project_root: str | None = None) -> AxisExtraction:
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

    @property
    def current_scope(self) -> _SymbolScope:
        return self.scope_stack[-1]

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
        scope = self._class_scope(node)
        self._emit("struct", "class_def", node, scope=scope, payload={"name": node.name})
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

        self.scope_stack.append(scope)
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
            self.scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, async_function=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, async_function=True)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._emit("cfg", "callable_body", node, payload={"callable_kind": "lambda"})
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        payload = {"callee": _call_name(node.func)}
        self._emit("cfg", "call_site", node, payload=payload)
        if isinstance(node.func, ast.Attribute):
            self._emit("cfg", "method_dispatch", node.func, payload=payload)
        if _looks_like_constructor_call(node.func):
            self._emit("cfg", "constructor_call", node, payload=payload)
            self._emit("dfg", "constructor_value", node, payload=payload)
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        self._emit("cfg", "branch_selector", node)
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        self._emit("cfg", "branch_selector", node)
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self._emit("cfg", "branch_selector", node)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self._emit("cfg", "loop_driver", node)
        self._emit_binding_targets(node.target, "loop_target")
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._emit("cfg", "loop_driver", node)
        self._emit("cfg", "async_suspend_resume", node)
        self._emit_binding_targets(node.target, "loop_target")
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self._emit("cfg", "loop_driver", node)
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
        if node.name:
            self._emit("dfg", "exception_value", node, payload={"name": node.name})
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        self._emit("cfg", "exception_transfer", node)
        self.generic_visit(node)

    def visit_Await(self, node: ast.Await) -> None:
        self._emit("cfg", "async_suspend_resume", node)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        self._emit("cfg", "return_exit", node)
        if node.value is not None:
            self._emit("dfg", "return_output", node.value)
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
        for target in node.targets:
            self._emit_assignment_target_bits(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._emit(
            "struct",
            "annotation",
            node.annotation,
            payload={"annotation": _unparse(node.annotation), "target": _unparse(node.target)},
        )
        self._emit("dfg", "assignment_binding", node, payload=_assignment_payload(node))
        if node.value is not None:
            self._emit_assignment_value_bits(node.value)
        self._emit_assignment_target_bits(node.target, node.value)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._emit("dfg", "augmented_mutation", node)
        self._emit_assignment_target_bits(node.target, node.value)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._emit("dfg", "assignment_binding", node, payload={"target": _unparse(node.target)})
        self._emit_assignment_value_bits(node.value)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load) and not self._is_call_func(node):
            self._emit("dfg", "attr_read", node, payload={"attribute": node.attr})
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Load):
            self._emit("dfg", "subscript_read", node)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        self._emit("dfg", "collection_assembly", node, payload={"shape": "dict"})
        self._emit("struct", "literal_shape", node, payload={"shape": "dict"})
        self.generic_visit(node)

    def visit_List(self, node: ast.List) -> None:
        if isinstance(node.ctx, ast.Load):
            self._emit("dfg", "collection_assembly", node, payload={"shape": "list"})
            self._emit("struct", "literal_shape", node, payload={"shape": "list"})
        self.generic_visit(node)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        if isinstance(node.ctx, ast.Load):
            self._emit("dfg", "collection_assembly", node, payload={"shape": "tuple"})
            self._emit("struct", "literal_shape", node, payload={"shape": "tuple"})
        self.generic_visit(node)

    def visit_Set(self, node: ast.Set) -> None:
        self._emit("dfg", "collection_assembly", node, payload={"shape": "set"})
        self._emit("struct", "literal_shape", node, payload={"shape": "set"})
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, *, async_function: bool) -> None:
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
            self._emit(
                "struct",
                "annotation",
                node.returns,
                scope=scope,
                payload={"kind": "return", "annotation": _unparse(node.returns)},
            )

        self.scope_stack.append(scope)
        try:
            for decorator in node.decorator_list:
                self.visit(decorator)
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if default is not None:
                    self.visit(default)
            for stmt in node.body:
                self.visit(stmt)
        finally:
            self.scope_stack.pop()

    def _emit_parameter_facts(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        scope: _SymbolScope,
    ) -> None:
        for arg in _iter_args(node.args):
            payload = {"name": arg.arg}
            self._emit("struct", "parameter_decl", arg, scope=scope, payload=payload)
            self._emit("dfg", "parameter_input", arg, scope=scope, payload=payload)
            if arg.annotation is not None:
                self._emit(
                    "struct",
                    "annotation",
                    arg.annotation,
                    scope=scope,
                    payload={"kind": "parameter", "name": arg.arg, "annotation": _unparse(arg.annotation)},
                )

    def _emit_decorators(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        scope: _SymbolScope,
    ) -> None:
        for decorator in node.decorator_list:
            payload = {"decorator": _unparse(decorator)}
            self._emit("struct", "decorator_attachment", decorator, scope=scope, payload=payload)
            self._emit("cfg", "decorator_application", decorator, scope=scope, payload=payload)

    def _emit_assignment_value_bits(self, value: ast.AST | None) -> None:
        if value is None:
            return
        if isinstance(value, ast.Call):
            payload = {"callee": _call_name(value.func)}
            self._emit("dfg", "call_result_origin", value, payload=payload)
            if _looks_like_constructor_call(value.func):
                self._emit("dfg", "constructor_value", value, payload=payload)
        if isinstance(value, (ast.Dict, ast.List, ast.Tuple, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
            self._emit("dfg", "collection_assembly", value, payload={"shape": _shape_name(value)})

    def _emit_assignment_target_bits(self, target: ast.AST, value: ast.AST | None) -> None:
        for leaf in _flatten_targets(target):
            if isinstance(leaf, ast.Attribute):
                payload = {"attribute": leaf.attr, "target": _unparse(leaf)}
                self._emit("dfg", "attr_write", leaf, payload=payload)
                if _is_self_attribute(leaf):
                    self._emit("struct", "instance_attribute_hint", leaf, payload=payload)
            elif isinstance(leaf, ast.Subscript):
                self._emit("dfg", "subscript_write", leaf, payload={"target": _unparse(leaf)})
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
                self._emit("dfg", "assignment_binding", leaf, payload={"target": leaf.id, "source_kind": source_kind})

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
        payload: dict[str, object] | None = None,
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


def _looks_like_constructor_call(func: ast.AST) -> bool:
    if isinstance(func, ast.Name):
        return bool(func.id[:1].isupper())
    if isinstance(func, ast.Attribute):
        return bool(func.attr[:1].isupper())
    return False


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


def _assignment_payload(node: ast.AST) -> dict[str, object]:
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
