"""Tree-sitter twin of ``python_axis_extractor`` (same facts, no ``ast.parse``).

Emits the same physical CFG/DFG/Structural axis bits as the ``ast``-based
``PythonAxisExtractor`` but walks the tree-sitter CST that ``extract_all``
already produced, so the axis pass costs zero extra parses and keeps working
on files with syntax errors (partial trees instead of zero facts).

Parity contract with the ast twin (enforced by ``scripts/axis_ts_parity.py``):
  - ``qualified_name`` / scope nesting (including ``<locals>``) — exact;
  - per-scope (axis, bit) fact multisets and line numbers — exact;
  - ``ast_kind`` vocabulary — exact (tree-sitter node types are mapped back
    to Python ``ast`` class names, since ``ast_kind_bits`` is persisted);
  - payload fields read structurally by ``container_kind`` predicates
    (``key`` / ``key_literal`` / ``key_kind`` / ``container`` / ``name`` /
    ``default_kind`` / ``expression_kind`` / literals) — exact;
  - rendered expression text (``evidence`` / ``expression`` / ``callee`` /
    ``condition`` …) — matches ``ast.unparse`` for the expression grammar
    covered by ``_Renderer``; statements and f-strings fall back to a
    whitespace-normalized source slice (cosmetic-only divergence).

Known accepted divergences (harness-verified across 5651 QA files, 4.5M facts):
  - body-carrying statement evidence (def/if/for/try/module) keeps source
    quoting/comments instead of the ast.unparse re-render — display text only;
  - a column-0 comment splitting a decorator from its def trips tree-sitter
    ERROR recovery and drops the 3 decorator facts (1 machine-generated
    fixture file in the whole QA corpus);
  - files ``ast.parse`` rejects now yield partial facts instead of none.

``ast`` is imported only for ``ast.literal_eval`` on individual literal
tokens — constant folding there is what makes ``0x10`` render as ``16`` and
string escapes match ``ast.unparse`` byte-for-byte.
"""

from __future__ import annotations

import ast
from typing import Any

from context_engine.axis.schema import AxisExtraction, AxisFact, AxisName
from context_engine.parser.adapters.python_axis_extractor import (
    _CONTAINER_MUTATION_METHODS,
    _CONTAINER_READ_METHODS,
    _NO_LITERAL,
    _json_safe_literal,
    _SymbolScope,
)
from context_engine.parser.uid import UNRESOLVED_SIGNATURE, compute_uid, module_name_from_path

# ---------------------------------------------------------------------------
# Node classification
# ---------------------------------------------------------------------------

# tree-sitter node type -> Python ast class name (persisted as ast_kind).
_TS_TO_AST_KIND: dict[str, str] = {
    "identifier": "Name",
    "attribute": "Attribute",
    "subscript": "Subscript",
    "call": "Call",
    "integer": "Constant",
    "float": "Constant",
    "true": "Constant",
    "false": "Constant",
    "none": "Constant",
    "ellipsis": "Constant",
    "binary_operator": "BinOp",
    "boolean_operator": "BoolOp",
    "comparison_operator": "Compare",
    "not_operator": "UnaryOp",
    "unary_operator": "UnaryOp",
    "lambda": "Lambda",
    "list": "List",
    "set": "Set",
    "tuple": "Tuple",
    "dictionary": "Dict",
    "list_comprehension": "ListComp",
    "set_comprehension": "SetComp",
    "dictionary_comprehension": "DictComp",
    "generator_expression": "GeneratorExp",
    "conditional_expression": "IfExp",
    "named_expression": "NamedExpr",
    "await": "Await",
    "list_splat": "Starred",
    "list_splat_pattern": "Starred",
    "slice": "Slice",
    "expression_list": "Tuple",
    "pattern_list": "Tuple",
    "tuple_pattern": "Tuple",
    "list_pattern": "List",
    "dotted_name": "Attribute",
    # type-annotation sublanguage (tree-sitter parses types specially)
    "generic_type": "Subscript",
    "union_type": "BinOp",
    "member_type": "Attribute",
    "constrained_type": "BinOp",
    "keyword_argument": "keyword",
    "if_statement": "If",
    "elif_clause": "If",
    "while_statement": "While",
    "match_statement": "Match",
    "try_statement": "Try",
    "except_clause": "ExceptHandler",
    "raise_statement": "Raise",
    "return_statement": "Return",
    "class_definition": "ClassDef",
    "import_statement": "Import",
    "import_from_statement": "ImportFrom",
    "future_import_statement": "ImportFrom",
    "augmented_assignment": "AugAssign",
    "module": "Module",
}

_COLLECTION_SHAPES: dict[str, str] = {
    "dictionary": "dict",
    "dictionary_comprehension": "dict",
    "list": "list",
    "list_comprehension": "list",
    "expression_list": "tuple",
    "tuple": "tuple",
    "set": "set",
    "set_comprehension": "set",
}

_COMPREHENSION_TYPES = frozenset(
    {"list_comprehension", "set_comprehension", "dictionary_comprehension"}
)
_TARGET_LIST_TYPES = frozenset(
    {"pattern_list", "tuple_pattern", "list_pattern", "expression_list", "tuple", "list"}
)
_CONSTANT_TYPES = frozenset(
    {"integer", "float", "true", "false", "none", "ellipsis", "string", "concatenated_string"}
)


def _text(node) -> str:
    return (node.text or b"").decode("utf-8", "ignore")


def _line(node) -> int:
    return int(node.start_point[0]) + 1


_IGNORED_CHILD_TYPES = frozenset({"comment", "line_continuation"})


def _named_children(node) -> list:
    return [ch for ch in node.named_children if ch.type not in _IGNORED_CHILD_TYPES]


def _unwrap(node):
    """Strip parenthesized_expression wrappers (transparent in ast)."""
    while node is not None and node.type == "parenthesized_expression":
        inner = _named_children(node)
        if len(inner) != 1:
            break
        node = inner[0]
    return node


def _field(node, name: str):
    return _unwrap(node.child_by_field_name(name))


def _fields(node, name: str) -> list:
    return [_unwrap(ch) for ch in node.children_by_field_name(name) if ch.type not in _IGNORED_CHILD_TYPES]


def _is_starred_chain(node) -> bool:
    """True when a postfix chain (attribute/subscript/call) is rooted at a
    splat the grammar swallowed: ``*a.b``, ``*d[k]``, ``*f(x)``, ``*t``."""
    obj = node
    while obj is not None and obj.type in ("attribute", "subscript", "call"):
        field = {"attribute": "object", "subscript": "value", "call": "function"}[obj.type]
        nxt = obj.child_by_field_name(field)
        obj = _unwrap(nxt) if nxt is not None else None
    return obj is not None and obj.type in ("list_splat", "list_splat_pattern")


def _subscript_indexes_form_tuple(indexes: list) -> bool:
    """ast wraps multi-element AND starred single-element slices in a Tuple."""
    if len(indexes) > 1:
        return True
    return len(indexes) == 1 and _is_starred_chain(indexes[0])


def _is_fstring(node) -> bool:
    if node.type == "concatenated_string":
        return any(_is_fstring(ch) for ch in _named_children(node))
    if node.type != "string":
        return False
    start = node.child_by_field_name("string_start") or (
        node.children[0] if node.children else None
    )
    prefix = _text(start).lower() if start is not None else ""
    return "f" in prefix or any(ch.type == "interpolation" for ch in node.named_children)


def _ast_kind(node) -> str:
    node = _unwrap(node)
    t = node.type
    if t == "string" or t == "concatenated_string":
        return "JoinedStr" if _is_fstring(node) else "Constant"
    if t == "yield":
        return "YieldFrom" if any(ch.type == "from" for ch in node.children) else "Yield"
    if t == "assignment":
        return "AnnAssign" if node.child_by_field_name("type") is not None else "Assign"
    if t == "function_definition":
        return (
            "AsyncFunctionDef"
            if node.children and node.children[0].type == "async"
            else "FunctionDef"
        )
    if t == "for_statement":
        return "AsyncFor" if node.children and node.children[0].type == "async" else "For"
    if t == "with_statement":
        return "AsyncWith" if node.children and node.children[0].type == "async" else "With"
    mapped = _TS_TO_AST_KIND.get(t)
    if mapped:
        return mapped
    # Unknown node — CamelCase the tree-sitter type so the harness flags it.
    return "".join(part.capitalize() for part in t.split("_"))


def _is_async(node) -> bool:
    return bool(node.children) and node.children[0].type == "async"


# ---------------------------------------------------------------------------
# Expression renderer — ast.unparse-compatible text
# ---------------------------------------------------------------------------

# Precedence ladder mirroring CPython ast._Unparser._Precedence.
_P_NAMED = 0
_P_TUPLE = 1
_P_YIELD = 2
_P_TEST = 3
_P_OR = 4
_P_AND = 5
_P_NOT = 6
_P_CMP = 7
_P_BOR = 8
_P_BXOR = 9
_P_BAND = 10
_P_SHIFT = 11
_P_ARITH = 12
_P_TERM = 13
_P_FACTOR = 14
_P_POWER = 15
_P_AWAIT = 16
_P_ATOM = 17

_BINOP_PREC: dict[str, int] = {
    "|": _P_BOR,
    "^": _P_BXOR,
    "&": _P_BAND,
    "<<": _P_SHIFT,
    ">>": _P_SHIFT,
    "+": _P_ARITH,
    "-": _P_ARITH,
    "*": _P_TERM,
    "@": _P_TERM,
    "/": _P_TERM,
    "//": _P_TERM,
    "%": _P_TERM,
    "**": _P_POWER,
}


class _Renderer:
    """Render tree-sitter expression nodes to ``ast.unparse``-style text.

    Statement nodes and anything unhandled fall back to a whitespace-collapsed
    source slice; renders memoize per (node, context-precedence).
    """

    __slots__ = ("_memo",)

    def __init__(self) -> None:
        self._memo: dict[tuple[int, int], str] = {}

    # -- public -------------------------------------------------------------

    def render(self, node, ctx: int = _P_TEST) -> str:
        if node is None:
            return ""
        key = (node.id, ctx)
        cached = self._memo.get(key)
        if cached is not None:
            return cached
        try:
            out = self._render(node, ctx)
        except Exception:
            out = self.fallback(node)
        self._memo[key] = out
        return out

    @staticmethod
    def fallback(node) -> str:
        return " ".join(_text(node).split())

    def literal_value(self, node) -> object:
        """Constant value for a literal token, or ``_NO_LITERAL``."""
        node = _unwrap(node)
        t = node.type
        if t == "true":
            return True
        if t == "false":
            return False
        if t == "none":
            return None
        if t == "ellipsis":
            return ...
        if t in ("integer", "float") or (
            t in ("string", "concatenated_string") and not _is_fstring(node)
        ):
            try:
                return ast.literal_eval(f"({_text(node)})")
            except Exception:
                return _NO_LITERAL
        return _NO_LITERAL

    # -- internals ------------------------------------------------------------

    def _render(self, node, ctx: int) -> str:
        node = _unwrap(node)
        t = node.type
        if t in ("identifier", "true", "false", "none", "dotted_name"):
            return _text(node)
        if t == "ellipsis":
            return "..."
        if t in ("integer", "float", "string", "concatenated_string"):
            if _is_fstring(node):
                return self._render_fstring(node)
            value = self.literal_value(node)
            if value is _NO_LITERAL:
                return self.fallback(node)
            return repr(value)
        if t == "type":
            children = _named_children(node)
            return self.render(children[0], ctx) if children else self.fallback(node)
        if t == "generic_type":
            children = _named_children(node)
            if len(children) == 2 and children[1].type == "type_parameter":
                base = self.render(children[0], _P_ATOM)
                args = ", ".join(
                    self.render(arg, _P_TEST) for arg in _named_children(children[1])
                )
                return f"{base}[{args}]"
            return self.fallback(node)
        if t == "union_type":
            children = _named_children(node)
            rendered = " | ".join(self.render(ch, _P_BOR) for ch in children)
            return self._parens(rendered, _P_BOR, ctx)
        if t == "member_type":
            children = _named_children(node)
            if len(children) == 2:
                return f"{self.render(children[0], _P_ATOM)}.{_text(children[1])}"
            return self.fallback(node)
        if t == "attribute":
            obj = _field(node, "object")
            attr = node.child_by_field_name("attribute")
            return f"{self.render(obj, _P_ATOM)}.{_text(attr)}"
        if t == "call":
            return self._render_call(node)
        if t == "subscript":
            value = _field(node, "value")
            return f"{self.render(value, _P_ATOM)}[{self.render_subscript_index(node, bare=True)}]"
        if t == "slice":
            parts: list[str] = []
            for ch in node.children:
                if ch.type == ":":
                    parts.append(":")
                elif ch.is_named and ch.type not in _IGNORED_CHILD_TYPES:
                    parts.append(self.render(ch, _P_TEST))
            return "".join(parts)
        if t == "binary_operator":
            op = _text(node.child_by_field_name("operator"))
            prec = _BINOP_PREC.get(op, _P_ARITH)
            if op == "**":
                left = self.render(_field(node, "left"), prec + 1)
                right = self.render(_field(node, "right"), prec)
            else:
                left = self.render(_field(node, "left"), prec)
                right = self.render(_field(node, "right"), prec + 1)
            return self._parens(f"{left} {op} {right}", prec, ctx)
        if t == "boolean_operator":
            op = _text(node.child_by_field_name("operator"))
            prec = _P_AND if op == "and" else _P_OR
            left = self.render(_field(node, "left"), prec)
            right = self.render(_field(node, "right"), prec + 1)
            return self._parens(f"{left} {op} {right}", prec, ctx)
        if t == "comparison_operator":
            return self._parens(self._render_comparison(node), _P_CMP, ctx)
        if t == "not_operator":
            inner = self.render(_field(node, "argument"), _P_NOT)
            return self._parens(f"not {inner}", _P_NOT, ctx)
        if t == "unary_operator":
            op = _text(node.child_by_field_name("operator"))
            inner = self.render(_field(node, "argument"), _P_FACTOR)
            return self._parens(f"{op}{inner}", _P_FACTOR, ctx)
        if t == "lambda":
            params = node.child_by_field_name("parameters")
            body = self.render(_field(node, "body"), _P_TEST)
            head = f"lambda {self._render_params(params)}" if params is not None else "lambda"
            return self._parens(f"{head}: {body}", _P_TEST, ctx)
        if t == "conditional_expression":
            children = _named_children(node)
            if len(children) == 3:
                body = self.render(children[0], _P_TEST + 1)
                test = self.render(children[1], _P_TEST + 1)
                orelse = self.render(children[2], _P_TEST)
                return self._parens(f"{body} if {test} else {orelse}", _P_TEST, ctx)
            return self.fallback(node)
        if t == "named_expression":
            name = _text(node.child_by_field_name("name"))
            value = self.render(_field(node, "value"), _P_TEST)
            return self._parens(f"{name} := {value}", _P_NAMED, ctx)
        if t == "await":
            children = _named_children(node)
            inner = self.render(children[0], _P_ATOM) if children else ""
            return self._parens(f"await {inner}", _P_AWAIT, ctx)
        if t == "yield":
            children = _named_children(node)
            if any(ch.type == "from" for ch in node.children):
                return self._parens(f"yield from {self.render(children[0])}", _P_YIELD, ctx)
            if children:
                return self._parens(f"yield {self.render(children[0])}", _P_YIELD, ctx)
            return self._parens("yield", _P_YIELD, ctx)
        if t in ("list_splat", "list_splat_pattern"):
            children = _named_children(node)
            return "*" + (self.render(children[0], _P_TEST) if children else "")
        if t in ("dictionary_splat", "dictionary_splat_pattern"):
            children = _named_children(node)
            return "**" + (self.render(children[0], _P_TEST) if children else "")
        if t in ("tuple", "expression_list", "pattern_list", "tuple_pattern"):
            elements = [self.render(ch, _P_TEST) for ch in _named_children(node)]
            if len(elements) == 1:
                body = f"{elements[0]},"
            else:
                body = ", ".join(elements)
            if ctx > _P_TUPLE:
                return f"({body})"
            return body
        if t in ("list", "list_pattern"):
            return "[" + ", ".join(self.render(ch, _P_TEST) for ch in _named_children(node)) + "]"
        if t == "set":
            return "{" + ", ".join(self.render(ch, _P_TEST) for ch in _named_children(node)) + "}"
        if t == "dictionary":
            parts = []
            for ch in _named_children(node):
                if ch.type == "pair":
                    key = self.render(_field(ch, "key"), _P_TEST)
                    value = self.render(_field(ch, "value"), _P_TEST)
                    parts.append(f"{key}: {value}")
                else:
                    parts.append(self.render(ch, _P_TEST))
            return "{" + ", ".join(parts) + "}"
        if t == "pair":
            key = self.render(_field(node, "key"), _P_TEST)
            value = self.render(_field(node, "value"), _P_TEST)
            return f"{key}: {value}"
        if t == "keyword_argument":
            name = _text(node.child_by_field_name("name"))
            return f"{name}={self.render(_field(node, 'value'), _P_TEST)}"
        if t in _COMPREHENSION_TYPES or t == "generator_expression":
            return self._render_comprehension(node)
        if t == "assignment":
            return self._render_assignment(node)
        if t == "augmented_assignment":
            op = _text(node.child_by_field_name("operator"))
            left = self.render(_field(node, "left"), _P_TUPLE)
            right = self.render(_field(node, "right"), _P_TEST)
            return f"{left} {op} {right}"
        if t == "return_statement":
            children = _named_children(node)
            if not children:
                return "return"
            return f"return {self.render(children[0], _P_TEST)}"
        if t == "raise_statement":
            return self._render_raise(node)
        if t == "import_statement":
            return self._render_import(node)
        if t in ("import_from_statement", "future_import_statement"):
            return self._render_import_from(node)
        return self.fallback(node)

    @staticmethod
    def _parens(text: str, prec: int, ctx: int) -> str:
        return f"({text})" if prec < ctx else text

    def _render_assignment(self, node) -> str:
        type_node = node.child_by_field_name("type")
        left = self.render(_field(node, "left"), _P_TUPLE)
        if type_node is not None:
            inner = _named_children(type_node)
            annotation = self.render(inner[0], _P_TEST) if inner else ""
            right = node.child_by_field_name("right")
            head = f"{left}: {annotation}"
            if right is None:
                return head
            return f"{head} = {self.render(_unwrap(right), _P_TEST)}"
        targets = [left]
        current = node.child_by_field_name("right")
        while current is not None and current.type == "assignment":
            targets.append(self.render(_field(current, "left"), _P_TUPLE))
            current = current.child_by_field_name("right")
        value = self.render(_unwrap(current), _P_TEST) if current is not None else ""
        return " = ".join([*targets, value])

    def _render_raise(self, node) -> str:
        raw_cause = node.child_by_field_name("cause")
        exc = None
        for ch in _named_children(node):
            if raw_cause is not None and ch.id == raw_cause.id:
                continue
            exc = ch
            break
        if exc is None:
            return "raise"
        text = f"raise {self.render(exc, _P_TEST)}"
        if raw_cause is not None:
            text = f"{text} from {self.render(raw_cause, _P_TEST)}"
        return text

    def _render_import(self, node) -> str:
        parts = []
        for ch in _named_children(node):
            if ch.type == "dotted_name":
                parts.append(_text(ch))
            elif ch.type == "aliased_import":
                name = _text(ch.child_by_field_name("name"))
                alias = _text(ch.child_by_field_name("alias"))
                parts.append(f"{name} as {alias}")
        return f"import {', '.join(parts)}"

    def _render_import_from(self, node) -> str:
        if node.type == "future_import_statement":
            module = "__future__"
        else:
            module_node = node.child_by_field_name("module_name")
            module = _text(module_node) if module_node is not None else ""
        parts = []
        for ch in node.children_by_field_name("name"):
            if ch.type == "dotted_name":
                parts.append(_text(ch))
            elif ch.type == "aliased_import":
                name = _text(ch.child_by_field_name("name"))
                alias = _text(ch.child_by_field_name("alias"))
                parts.append(f"{name} as {alias}")
        if not parts and any(ch.type == "wildcard_import" for ch in _named_children(node)):
            parts = ["*"]
        return f"from {module} import {', '.join(parts)}"

    def _render_fstring(self, node) -> str:
        """Match ast.unparse for simple f-strings; fall back when escaping gets hairy."""
        if node.type == "concatenated_string":
            return self.fallback(node)
        start = node.children[0] if node.children else None
        prefix = _text(start).lower() if start is not None else ""
        if "r" in prefix or "b" in prefix:
            return self.fallback(node)
        parts: list[str] = []
        literal_chunks: list[str] = []
        for ch in node.children:
            if ch.type == "string_content":
                chunk = _text(ch)
                if "\\" in chunk or "\n" in chunk:
                    return self.fallback(node)
                literal_chunks.append(chunk)
                parts.append(chunk)
            elif ch.type == "interpolation":
                if any(sub.type == "=" for sub in ch.children):
                    return self.fallback(node)  # f"{x=}" debug spec
                expr = ch.child_by_field_name("expression")
                rendered = self.render(_unwrap(expr), _P_TEST) if expr is not None else ""
                if "'" in rendered or '"' in rendered or "\n" in rendered:
                    return self.fallback(node)
                conversion = ch.child_by_field_name("type_conversion")
                spec = ch.child_by_field_name("format_specifier")
                spec_text = _text(spec) if spec is not None else ""
                if "'" in spec_text or '"' in spec_text or "\\" in spec_text:
                    return self.fallback(node)
                parts.append(
                    "{"
                    + rendered
                    + (_text(conversion) if conversion is not None else "")
                    + spec_text
                    + "}"
                )
        body = "".join(parts)
        literal_text = "".join(literal_chunks)
        if "'" in literal_text and '"' in literal_text:
            return self.fallback(node)
        quote = '"' if "'" in literal_text else "'"
        return f"f{quote}{body}{quote}"

    def _render_call(self, node) -> str:
        func = _field(node, "function")
        args_node = node.child_by_field_name("arguments")
        func_text = self.render(func, _P_ATOM)
        if args_node is None:
            return f"{func_text}()"
        if args_node.type == "generator_expression":
            return f"{func_text}({self.render(args_node)})"
        parts = [self.render(ch, _P_TEST) for ch in _named_children(args_node)]
        return f"{func_text}({', '.join(parts)})"

    def _render_comparison(self, node) -> str:
        parts: list[str] = []
        pending_ops: list[str] = []
        for ch in node.children:
            if ch.type in _IGNORED_CHILD_TYPES:
                continue
            if ch.is_named:
                if pending_ops:
                    parts.append(" ".join(pending_ops))
                    pending_ops = []
                parts.append(self.render(ch, _P_CMP + 1))
            else:
                pending_ops.append(ch.type)
        return " ".join(parts)

    def _render_comprehension(self, node) -> str:
        t = node.type
        body = node.child_by_field_name("body")
        parts = [self.render(body, _P_TEST)]
        for ch in _named_children(node):
            if ch.type == "for_in_clause":
                left = self.render(_field(ch, "left"), _P_TUPLE)
                right = self.render(_field(ch, "right"), _P_TEST + 1)
                prefix = "async for" if _is_async(ch) else "for"
                parts.append(f"{prefix} {left} in {right}")
            elif ch.type == "if_clause":
                cond = _named_children(ch)
                parts.append(f"if {self.render(cond[0], _P_TEST + 1) if cond else ''}")
        inner = " ".join(parts)
        if t == "list_comprehension":
            return f"[{inner}]"
        if t == "set_comprehension":
            return "{" + inner + "}"
        if t == "dictionary_comprehension":
            return "{" + inner + "}"
        return f"({inner})"

    def _render_params(self, params_node) -> str:
        parts: list[str] = []
        for ch in _named_children(params_node):
            t = ch.type
            if t == "identifier":
                parts.append(_text(ch))
            elif t == "default_parameter":
                name = _text(ch.child_by_field_name("name"))
                parts.append(f"{name}={self.render(_field(ch, 'value'), _P_TEST)}")
            elif t == "list_splat_pattern":
                parts.append(self.render(ch))
            elif t == "dictionary_splat_pattern":
                parts.append(self.render(ch))
            elif t in ("keyword_separator", "positional_separator"):
                parts.append(_text(ch))
            else:
                parts.append(self.fallback(ch))
        return ", ".join(parts)

    def render_subscript_index(self, subscript_node, *, bare: bool) -> str:
        """The ``[...]`` interior; ``bare=False`` renders it standalone (parens tuple)."""
        indexes = _fields(subscript_node, "subscript")
        if _subscript_indexes_form_tuple(indexes):
            body = ", ".join(self.render(ix, _P_TEST) for ix in indexes)
            if len(indexes) == 1:
                body = f"{body},"  # ast renders the implicit singleton Tuple
            return body if bare else f"({body})"
        if not indexes:
            return ""
        return self.render(indexes[0], _P_TEST)


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


class PythonAxisExtractorTS:
    """Extract AST-physical axis facts from an existing tree-sitter tree."""

    language = "python"

    def extract(
        self,
        source: str,
        file_path: str,
        *,
        project_root: str | None = None,
        tree=None,
    ) -> AxisExtraction:
        if tree is None:
            tree = _default_parser().parse(bytes(source, "utf8"))
        module_name = module_name_from_path(file_path, project_root=project_root)
        module_scope = _SymbolScope(
            uid=compute_uid(module_name, UNRESOLVED_SIGNATURE, self.language),
            qualified_name=module_name,
            kind="module",
        )
        walker = _TsAxisWalker(file_path, module_name, module_scope)
        walker.walk_module(tree.root_node)
        return AxisExtraction(file_path=file_path, facts=walker.facts)

    def extract_facts(
        self,
        source: str,
        file_path: str,
        *,
        project_root: str | None = None,
        tree=None,
    ) -> list[AxisFact]:
        return self.extract(source, file_path, project_root=project_root, tree=tree).facts


_PARSER = None


def _default_parser():
    global _PARSER
    if _PARSER is None:
        from tree_sitter import Language, Parser
        from tree_sitter_python import language as lang_ptr

        _PARSER = Parser(Language(lang_ptr()))
    return _PARSER


class _TsAxisWalker:
    """Mirror of ``_AxisVisitor`` over the tree-sitter CST.

    Handler structure, emission order, and scope bookkeeping intentionally
    follow the ast visitor line-by-line so the parity harness can diff the
    two implementations fact-by-fact.
    """

    def __init__(self, file_path: str, module_name: str, module_scope: _SymbolScope):
        self.file_path = file_path
        self.module_name = module_name
        self.language = PythonAxisExtractorTS.language
        self.facts: list[AxisFact] = []
        self.scope_stack: list[_SymbolScope] = [module_scope]
        self.callable_bindings_stack: list[set[str]] = [set()]
        self.render = _Renderer()
        self._dispatch = {
            "import_statement": self._visit_import,
            "import_from_statement": self._visit_import_from,
            "future_import_statement": self._visit_import_from,
            "decorated_definition": self._visit_decorated,
            "class_definition": lambda n: self._visit_class(n, []),
            "function_definition": lambda n: self._visit_function(n, []),
            "lambda": self._visit_lambda,
            "call": self._visit_call,
            "if_statement": self._visit_if,
            "match_statement": self._visit_match,
            "conditional_expression": self._visit_ifexp,
            "for_statement": self._visit_for,
            "while_statement": self._visit_while,
            "list_comprehension": self._visit_comprehension,
            "set_comprehension": self._visit_comprehension,
            "dictionary_comprehension": self._visit_comprehension,
            "generator_expression": self._visit_generator,
            "with_statement": self._visit_with,
            "try_statement": self._visit_try,
            "raise_statement": self._visit_raise,
            "await": self._visit_await,
            "return_statement": self._visit_return,
            "yield": self._visit_yield,
            "assignment": self._visit_assignment,
            "augmented_assignment": self._visit_aug_assignment,
            "named_expression": self._visit_named_expression,
            "attribute": self._visit_attribute,
            "subscript": self._visit_subscript,
            "dictionary": self._visit_dict,
            "list": self._visit_list_or_tuple,
            "tuple": self._visit_list_or_tuple,
            "expression_list": self._visit_list_or_tuple,
            "set": self._visit_set,
            "delete_statement": self._visit_delete,
            "parenthesized_expression": self._visit_children,
            "expression_statement": self._visit_expression_statement,
            "type_alias_statement": self._visit_type_alias,
        }
        # keeps re-parsed helper trees alive while their nodes are in use
        self._reparse_keepalive: list = []

    # -- scope helpers --------------------------------------------------------

    @property
    def current_scope(self) -> _SymbolScope:
        return self.scope_stack[-1]

    @property
    def current_callable_bindings(self) -> set[str]:
        return self.callable_bindings_stack[-1]

    def _name_is_callable_binding(self, name: str) -> bool:
        return any(name in bindings for bindings in reversed(self.callable_bindings_stack))

    def _qualified_child_name(self, name: str) -> str:
        parts = [self.module_name]
        for scope in self.scope_stack[1:]:
            if scope.is_function:
                parts.append("<locals>")
            parts.append(scope.qualified_name.rsplit(".", 1)[-1])
        parts.append(name)
        return ".".join(parts)

    # -- emission -------------------------------------------------------------

    def _emit(
        self,
        axis: AxisName,
        bit: str,
        node,
        *,
        scope: _SymbolScope | None = None,
        payload: dict[str, Any] | None = None,
        ast_kind: str | None = None,
    ) -> None:
        owner = scope or self.current_scope
        self.facts.append(
            AxisFact(
                symbol_uid=owner.uid,
                qualified_name=owner.qualified_name,
                symbol_kind=owner.kind,
                axis=axis,
                bit=bit,
                line=_line(node),
                evidence=self._evidence(node),
                ast_kind=ast_kind if ast_kind is not None else _ast_kind(node),
                payload=payload or {},
            )
        )

    _RENDER_EVIDENCE_TYPES = frozenset(
        {
            "identifier",
            "attribute",
            "call",
            "subscript",
            "slice",
            "lambda",
            "conditional_expression",
            "named_expression",
            "await",
            "yield",
            "dictionary",
            "pair",
            "list",
            "tuple",
            "set",
            "expression_list",
            "pattern_list",
            "tuple_pattern",
            "list_pattern",
            "list_splat",
            "dictionary_splat",
            "list_comprehension",
            "set_comprehension",
            "dictionary_comprehension",
            "generator_expression",
            "binary_operator",
            "boolean_operator",
            "comparison_operator",
            "not_operator",
            "unary_operator",
            "integer",
            "float",
            "string",
            "concatenated_string",
            "true",
            "false",
            "none",
            "ellipsis",
            "keyword_argument",
            "type",
            "dotted_name",
            "parenthesized_expression",
            "assignment",
            "augmented_assignment",
            "return_statement",
            "raise_statement",
            "import_statement",
            "import_from_statement",
            "future_import_statement",
        }
    )

    def _evidence(self, node) -> str:
        # Expressions go through the unparse-compatible renderer; statements
        # use a normalized slice (cosmetic divergence from ast.unparse).
        if node.type in self._RENDER_EVIDENCE_TYPES:
            text = self.render.render(node).replace("\n", " ").strip()
        else:
            raw = (node.text or b"")[:512].decode("utf-8", "ignore")
            text = " ".join(raw.split())
        if len(text) > 160:
            return text[:157] + "..."
        return text

    # -- generic walk -----------------------------------------------------------

    def walk_module(self, root) -> None:
        owner = self.current_scope
        # ast.Module has no lineno — the ast twin always stamps line 1.
        self.facts.append(
            AxisFact(
                symbol_uid=owner.uid,
                qualified_name=owner.qualified_name,
                symbol_kind=owner.kind,
                axis="struct",
                bit="module_scope",
                line=1,
                evidence=self._evidence(root),
                ast_kind="Module",
                payload={},
            )
        )
        self._visit_children(root)

    def visit(self, node) -> None:
        handler = self._dispatch.get(node.type)
        if handler is not None:
            handler(node)
        else:
            self._visit_children(node)

    def _visit_children(self, node) -> None:
        for ch in node.named_children:
            if ch.type not in _IGNORED_CHILD_TYPES:
                self.visit(ch)

    def _visit_target(self, node) -> None:
        """Store-context descent: no read facts at the target node itself."""
        if node is None:
            return
        node = _unwrap(node)
        t = node.type
        if t in _TARGET_LIST_TYPES:
            for ch in _named_children(node):
                self._visit_target(ch)
        elif t in ("list_splat_pattern", "list_splat"):
            for ch in _named_children(node):
                self._visit_target(ch)
        elif t == "attribute":
            obj = _field(node, "object")
            if obj is not None:
                self.visit(obj)
        elif t == "subscript":
            value = _field(node, "value")
            if value is not None:
                self.visit(value)
            indexes = _fields(node, "subscript")
            if _subscript_indexes_form_tuple(indexes):
                # the slice Tuple of d[a, b] = v is a Load Tuple in ast
                self._emit_synthetic_tuple(indexes)
            for ix in indexes:
                self.visit(ix)
        # identifiers and case patterns: nothing to emit

    # -- imports ---------------------------------------------------------------

    def _visit_import(self, node) -> None:
        for ch in _named_children(node):
            if ch.type == "dotted_name":
                self._emit(
                    "struct",
                    "import_dependency",
                    node,
                    payload={"module": _text(ch), "alias": ""},
                    ast_kind="Import",
                )
            elif ch.type == "aliased_import":
                self._emit(
                    "struct",
                    "import_dependency",
                    node,
                    payload={
                        "module": _text(ch.child_by_field_name("name")),
                        "alias": _text(ch.child_by_field_name("alias")),
                    },
                    ast_kind="Import",
                )

    def _visit_import_from(self, node) -> None:
        if node.type == "future_import_statement":
            module = "__future__"
        else:
            module_node = node.child_by_field_name("module_name")
            module = _text(module_node) if module_node is not None else ""
        for ch in node.children_by_field_name("name"):
            if ch.type == "dotted_name":
                name, alias = _text(ch), ""
            elif ch.type == "aliased_import":
                name = _text(ch.child_by_field_name("name"))
                alias = _text(ch.child_by_field_name("alias"))
            else:
                continue
            self._emit(
                "struct",
                "import_dependency",
                node,
                payload={"module": module, "name": name, "alias": alias},
                ast_kind="ImportFrom",
            )
        for ch in _named_children(node):
            if ch.type == "wildcard_import":
                self._emit(
                    "struct",
                    "import_dependency",
                    node,
                    payload={"module": module, "name": "*", "alias": ""},
                    ast_kind="ImportFrom",
                )

    # -- definitions -------------------------------------------------------------

    def _visit_decorated(self, node) -> None:
        decorators = []
        for ch in node.named_children:
            if ch.type == "decorator":
                inner = _named_children(ch)
                if inner:
                    decorators.append(inner[0])
        definition = node.child_by_field_name("definition")
        if definition is None:
            return
        if definition.type == "class_definition":
            self._visit_class(definition, decorators)
        elif definition.type == "function_definition":
            self._visit_function(definition, decorators)

    def _class_scope(self, name: str) -> _SymbolScope:
        qualified_name = self._qualified_child_name(name)
        return _SymbolScope(
            uid=compute_uid(qualified_name, f"{name}()->_", self.language),
            qualified_name=qualified_name,
            kind="class",
            is_class=True,
        )

    def _visit_class(self, node, decorators: list) -> None:
        name = _text(node.child_by_field_name("name"))
        self.current_callable_bindings.add(name)
        scope = self._class_scope(name)
        self._emit("struct", "class_def", node, scope=scope, payload={"name": name})
        self._emit(
            "dfg",
            "callable_value",
            node,
            scope=scope,
            payload={"callable_kind": "class", "origin": "definition", "name": name},
            ast_kind="ClassDef",
        )
        if decorators:
            self._emit_decorators(decorators, scope)
        bases: list = []
        keywords: list = []
        superclasses = node.child_by_field_name("superclasses")
        if superclasses is not None:
            for ch in _named_children(superclasses):
                if ch.type == "keyword_argument":
                    keywords.append(ch)
                elif ch.type in ("list_splat", "dictionary_splat"):
                    keywords.append(ch) if ch.type == "dictionary_splat" else bases.append(ch)
                else:
                    bases.append(ch)
        for base in bases:
            base_node = _unwrap(base)
            self._emit(
                "struct",
                "inheritance",
                base_node,
                scope=scope,
                payload={"base": self.render.render(base_node)},
            )
        for keyword in keywords:
            if keyword.type == "dictionary_splat":
                inner = _named_children(keyword)
                value = inner[0] if inner else keyword
                self._emit(
                    "struct",
                    "base_keyword",
                    value,
                    scope=scope,
                    payload={"keyword": "**", "value": self.render.render(value)},
                )
                continue
            kw_name = _text(keyword.child_by_field_name("name"))
            value = _field(keyword, "value")
            if kw_name == "metaclass":
                self._emit(
                    "struct",
                    "metaclass",
                    value,
                    scope=scope,
                    payload={"metaclass": self.render.render(value)},
                )
            else:
                self._emit(
                    "struct",
                    "base_keyword",
                    value,
                    scope=scope,
                    payload={"keyword": kw_name or "**", "value": self.render.render(value)},
                )

        self.scope_stack.append(scope)
        self.callable_bindings_stack.append(set())
        try:
            for decorator in decorators:
                self.visit(decorator)
            for base in bases:
                self.visit(_unwrap(base))
            for keyword in keywords:
                if keyword.type == "dictionary_splat":
                    inner = _named_children(keyword)
                    if inner:
                        self.visit(inner[0])
                else:
                    value = _field(keyword, "value")
                    if value is not None:
                        self.visit(value)
            body = node.child_by_field_name("body")
            if body is not None:
                for stmt in _named_children(body):
                    self._emit_class_attributes(stmt)
                    self.visit(stmt)
        finally:
            self.callable_bindings_stack.pop()
            self.scope_stack.pop()

    def _emit_class_attributes(self, stmt) -> None:
        if stmt.type != "expression_statement":
            return
        for ch in _named_children(stmt):
            if ch.type in ("assignment", "augmented_assignment"):
                self._emit(
                    "struct",
                    "class_attribute",
                    ch,
                    payload=self._assignment_payload(ch),
                )

    def _function_scope(self, name: str, signature: str) -> _SymbolScope:
        qualified_name = self._qualified_child_name(name)
        return _SymbolScope(
            uid=compute_uid(qualified_name, signature, self.language),
            qualified_name=qualified_name,
            kind="function",
            is_function=True,
        )

    def _parse_parameters(self, params_node) -> list[dict]:
        """Normalize parameter child nodes into ast ``arg``-like records."""
        out: list[dict] = []
        if params_node is None:
            return out
        for ch in _named_children(params_node):
            t = ch.type
            if t in ("keyword_separator", "positional_separator"):
                continue
            record = {
                "node": ch,
                "name": "",
                "prefix": "",
                "annotation": None,
                "default": None,
            }
            name_node = None
            if t == "identifier":
                name_node = ch
            elif t == "typed_parameter":
                inner = _named_children(ch)
                head = inner[0] if inner else None
                if head is not None and head.type == "list_splat_pattern":
                    record["prefix"] = "*"
                    head_inner = _named_children(head)
                    name_node = head_inner[0] if head_inner else None
                elif head is not None and head.type == "dictionary_splat_pattern":
                    record["prefix"] = "**"
                    head_inner = _named_children(head)
                    name_node = head_inner[0] if head_inner else None
                else:
                    name_node = head
                type_node = ch.child_by_field_name("type")
                if type_node is not None:
                    inner_type = _named_children(type_node)
                    record["annotation"] = inner_type[0] if inner_type else None
            elif t == "default_parameter":
                name_node = ch.child_by_field_name("name")
                record["default"] = _field(ch, "value")
            elif t == "typed_default_parameter":
                name_node = ch.child_by_field_name("name")
                record["default"] = _field(ch, "value")
                type_node = ch.child_by_field_name("type")
                if type_node is not None:
                    inner_type = _named_children(type_node)
                    record["annotation"] = inner_type[0] if inner_type else None
            elif t == "list_splat_pattern":
                record["prefix"] = "*"
                inner = _named_children(ch)
                name_node = inner[0] if inner else None
            elif t == "dictionary_splat_pattern":
                record["prefix"] = "**"
                inner = _named_children(ch)
                name_node = inner[0] if inner else None
            else:
                continue
            record["name"] = _text(name_node) if name_node is not None else ""
            # ast.arg carries the NAME token position, not the */**/annotation span
            record["node"] = name_node if name_node is not None else ch
            out.append(record)
        return out

    def _signature_for_function(self, name: str, params: list[dict], returns) -> str:
        parts: list[str] = []
        for param in params:
            text = param["prefix"] + param["name"]
            if param["annotation"] is not None:
                text = f"{text}: {self.render.render(param['annotation'])}"
            parts.append(text)
        returns_text = f"->{self.render.render(returns)}" if returns is not None else ""
        return f"{name}({', '.join(parts)}){returns_text}"

    def _visit_function(self, node, decorators: list) -> None:
        async_function = _is_async(node)
        name = _text(node.child_by_field_name("name"))
        self.current_callable_bindings.add(name)
        params = self._parse_parameters(node.child_by_field_name("parameters"))
        returns = None
        return_type = node.child_by_field_name("return_type")
        if return_type is not None:
            inner = _named_children(return_type)
            returns = inner[0] if inner else None
        signature = self._signature_for_function(name, params, returns)
        scope = self._function_scope(name, signature)
        fn_kind = "AsyncFunctionDef" if async_function else "FunctionDef"
        self._emit(
            "struct",
            "async_function_def" if async_function else "function_def",
            node,
            scope=scope,
            payload={"name": name},
            ast_kind=fn_kind,
        )
        self._emit(
            "cfg",
            "callable_body",
            node,
            scope=scope,
            payload={"callable_kind": "async_function" if async_function else "function"},
            ast_kind=fn_kind,
        )
        self._emit(
            "dfg",
            "callable_value",
            node,
            scope=scope,
            payload={
                "callable_kind": "async_function" if async_function else "function",
                "origin": "definition",
                "name": name,
                "decorated": bool(decorators),
            },
            ast_kind=fn_kind,
        )
        if async_function:
            self._emit("cfg", "async_suspend_resume", node, scope=scope, ast_kind=fn_kind)
        if len(self.scope_stack) >= 2 and self.scope_stack[-1].is_class:
            self._emit(
                "struct",
                "method_member",
                node,
                scope=scope,
                payload={"owner": self.scope_stack[-1].qualified_name},
                ast_kind=fn_kind,
            )
        if decorators:
            self._emit_decorators(decorators, scope)
        self._emit_parameter_facts(params, scope)
        if returns is not None:
            self._emit_annotation_facts(returns, scope=scope, payload={"kind": "return"})

        self.scope_stack.append(scope)
        self.callable_bindings_stack.append(set())
        try:
            for decorator in decorators:
                self.visit(decorator)
            for param in params:
                if param["default"] is not None:
                    self.visit(param["default"])
            body = node.child_by_field_name("body")
            if body is not None:
                for stmt in _named_children(body):
                    self.visit(stmt)
        finally:
            self.callable_bindings_stack.pop()
            self.scope_stack.pop()

    def _emit_parameter_facts(self, params: list[dict], scope: _SymbolScope) -> None:
        for param in params:
            payload = {"name": param["name"]}
            evidence = param["name"]
            if param["annotation"] is not None:
                evidence = f"{param['name']}: {self.render.render(param['annotation'])}"
            self._emit_param(
                "struct", "parameter_decl", param["node"], scope, payload, evidence
            )
            self._emit_param("dfg", "parameter_input", param["node"], scope, payload, evidence)
            default = param["default"]
            if default is not None:
                default_payload = {
                    "name": param["name"],
                    "default": self.render.render(default),
                    "default_kind": _ast_kind(default),
                }
                self._emit("struct", "parameter_default", default, scope=scope, payload=default_payload)
                self._emit(
                    "dfg", "parameter_default_value", default, scope=scope, payload=default_payload
                )
                self._maybe_emit_callable_value(
                    default,
                    scope=scope,
                    source="parameter_default",
                    payload={"parameter": param["name"]},
                )
            if param["annotation"] is not None:
                self._emit_annotation_facts(
                    param["annotation"],
                    scope=scope,
                    payload={"kind": "parameter", "name": param["name"]},
                )

    def _emit_param(
        self, axis: AxisName, bit: str, node, scope: _SymbolScope, payload: dict, evidence: str
    ) -> None:
        text = evidence if len(evidence) <= 160 else evidence[:157] + "..."
        self.facts.append(
            AxisFact(
                symbol_uid=scope.uid,
                qualified_name=scope.qualified_name,
                symbol_kind=scope.kind,
                axis=axis,
                bit=bit,
                line=_line(node),
                evidence=text,
                ast_kind="arg",
                payload=dict(payload),
            )
        )

    def _emit_decorators(self, decorators: list, scope: _SymbolScope) -> None:
        for decorator in decorators:
            payload = {"decorator": self.render.render(decorator)}
            self._emit("struct", "decorator_attachment", decorator, scope=scope, payload=payload)
            self._emit(
                "struct",
                "decorator_shape",
                decorator,
                scope=scope,
                payload=self._decorator_shape_payload(decorator),
            )
            self._emit("cfg", "decorator_application", decorator, scope=scope, payload=payload)

    def _decorator_shape_payload(self, decorator) -> dict[str, Any]:
        payload = {"decorator": self.render.render(decorator), **self._expr_payload(decorator)}
        decorator = _unwrap(decorator)
        if decorator.type == "call":
            func = _field(decorator, "function")
            positional, keywords = self._call_arguments(decorator)
            payload.update(
                {
                    "callee": self.render.render(func),
                    "callee_kind": _ast_kind(func),
                    "args": [self._expr_payload(self._splat_inner(arg)) for arg in positional],
                    "keywords": [
                        {
                            "name": kw_name or "**",
                            **self._expr_payload(kw_value),
                        }
                        for kw_name, kw_value in keywords
                    ],
                }
            )
        return payload

    # -- lambdas / calls ---------------------------------------------------------

    def _visit_lambda(self, node) -> None:
        self._emit("cfg", "callable_body", node, payload={"callable_kind": "lambda"})
        self._emit(
            "dfg",
            "callable_value",
            node,
            payload={"callable_kind": "lambda", "origin": "expression"},
        )
        params = node.child_by_field_name("parameters")
        if params is not None:
            for ch in _named_children(params):
                if ch.type in ("default_parameter", "typed_default_parameter"):
                    value = _field(ch, "value")
                    if value is not None:
                        self.visit(value)
        body = _field(node, "body")
        if body is not None:
            self.visit(body)

    def _call_arguments(self, node) -> tuple[list, list[tuple[str | None, Any]]]:
        """Split argument nodes ast-style: positional (incl. ``*``) and keywords."""
        args_node = node.child_by_field_name("arguments")
        positional: list = []
        keywords: list[tuple[str | None, Any]] = []
        if args_node is None:
            return positional, keywords
        if args_node.type == "generator_expression":
            return [args_node], keywords
        for ch in _named_children(args_node):
            if ch.type == "keyword_argument":
                keywords.append((_text(ch.child_by_field_name("name")), _field(ch, "value")))
            elif ch.type == "dictionary_splat":
                inner = _named_children(ch)
                keywords.append((None, inner[0] if inner else ch))
            else:
                positional.append(ch)
        return positional, keywords

    @staticmethod
    def _splat_inner(arg):
        arg = _unwrap(arg)
        if arg.type == "list_splat":
            inner = _named_children(arg)
            return _unwrap(inner[0]) if inner else arg
        return arg

    def _call_name(self, func) -> str:
        func = _unwrap(func)
        if func.type == "identifier":
            return _text(func)
        if func.type == "attribute":
            return _text(func.child_by_field_name("attribute"))
        return self.render.render(func)

    @staticmethod
    def _looks_like_constructor_call(func) -> bool:
        func = _unwrap(func)
        if func.type == "identifier":
            return bool(_text(func)[:1].isupper())
        if func.type == "attribute":
            return bool(_text(func.child_by_field_name("attribute"))[:1].isupper())
        return False

    def _visit_call(self, node) -> None:
        func = _field(node, "function")
        # `[*idx(...)]` parses as call(function=list_splat(idx)) — the splat
        # belongs to the enclosing Starred in ast, not to the call itself.
        starred_call = func is not None and func.type in ("list_splat", "list_splat_pattern")
        if starred_call:
            inner = _named_children(func)
            if not inner:
                self._visit_children(node)
                return
            func = _unwrap(inner[0])
        payload = {"callee": self._call_name(func)}
        self._emit("cfg", "call_site", node, payload=payload)
        if func.type != "attribute":
            self._emit(
                "cfg",
                "value_call",
                func,
                payload={"callee": self.render.render(func), "callee_kind": _ast_kind(func)},
            )
        if func.type == "attribute":
            self._emit("cfg", "method_dispatch", func, payload=payload)
        if self._looks_like_constructor_call(func):
            self._emit("cfg", "constructor_call", node, payload=payload)
            self._emit("dfg", "constructor_value", node, payload=payload)
        self._emit_call_argument_facts(node, func)
        self._emit_container_call_facts(node, func)
        # ast generic_visit order: func, args, keywords
        self.visit(func)
        positional, keywords = self._call_arguments(node)
        for arg in positional:
            self.visit(arg)
        for _, kw_value in keywords:
            if kw_value is not None:
                self.visit(kw_value)

    def _visit_attribute(self, node) -> None:
        # Walk up through paren wrappers: ast sees (a.b)() as Call(func=Attribute).
        top = node
        while top.parent is not None and top.parent.type == "parenthesized_expression":
            top = top.parent
        parent = top.parent
        is_call_func = (
            parent is not None
            and parent.type == "call"
            and parent.child_by_field_name("function") is not None
            and parent.child_by_field_name("function").id == top.id
        )
        if not is_call_func:
            self._emit(
                "dfg",
                "attr_read",
                node,
                payload={"attribute": _text(node.child_by_field_name("attribute"))},
            )
        obj = _field(node, "object")
        if obj is not None:
            self.visit(obj)

    def _visit_subscript(self, node) -> None:
        payload = self._subscript_key_payload(node)
        self._emit("dfg", "subscript_read", node, payload=payload)
        self._emit("dfg", "container_read_key", node, payload=payload)
        self._emit("dfg", "keyed_read", node, payload=payload)
        indexes = _fields(node, "subscript")
        is_tuple_slice = _subscript_indexes_form_tuple(indexes)
        key_node = indexes[0] if len(indexes) == 1 and not is_tuple_slice else None
        if key_node is not None:
            self._emit_literal_key(
                key_node,
                context="subscript_read",
                container=self.render.render(_field(node, "value")),
            )
        value = _field(node, "value")
        if value is not None:
            self.visit(value)
        if is_tuple_slice:
            # ast wraps multi-element slices in a Load Tuple, which emits its
            # own collection facts before the elements are visited.
            self._emit_synthetic_tuple(indexes)
        for ix in indexes:
            self.visit(ix)

    def _emit_synthetic_tuple(self, elements: list) -> None:
        """Collection facts for Load Tuples tree-sitter leaves implicit:
        multi-element subscript slices and bare statement-level tuples."""
        first = elements[0]
        body = ", ".join(self.render.render(ix, _P_TEST) for ix in elements)
        if len(elements) == 1:
            body = f"{body},"
        evidence = f"({body})"
        if len(evidence) > 160:
            evidence = evidence[:157] + "..."
        owner = self.current_scope
        tuple_bits: tuple[tuple[AxisName, str], ...] = (
            ("dfg", "collection_assembly"),
            ("struct", "literal_shape"),
        )
        for axis, bit in tuple_bits:
            self.facts.append(
                AxisFact(
                    symbol_uid=owner.uid,
                    qualified_name=owner.qualified_name,
                    symbol_kind=owner.kind,
                    axis=axis,
                    bit=bit,
                    line=_line(first),
                    evidence=evidence,
                    ast_kind="Tuple",
                    payload={"shape": "tuple"},
                )
            )
        for elt in elements:
            self._maybe_emit_callable_value(_unwrap(elt), source="collection_value")

    def _emit_call_argument_facts(self, node, func) -> None:
        callee = self.render.render(func)
        positional, keywords = self._call_arguments(node)
        for index, arg in enumerate(positional):
            starred = _unwrap(arg).type == "list_splat"
            expr = self._splat_inner(arg)
            payload = {
                "callee": callee,
                "position": index,
                "argument_kind": "starred" if starred else "positional",
                **self._expr_payload(expr),
            }
            self._emit("dfg", "call_argument", expr, payload=payload)
            self._maybe_emit_callable_value(
                expr,
                source="call_argument",
                payload={"callee": callee, "position": index},
            )
        for kw_name, kw_value in keywords:
            expr = _unwrap(kw_value)
            payload = {
                "callee": callee,
                "keyword": kw_name or "**",
                "argument_kind": "kwargs" if kw_name is None else "keyword",
                **self._expr_payload(expr),
            }
            self._emit("dfg", "call_argument", expr, payload=payload)
            self._maybe_emit_callable_value(
                expr,
                source="call_argument",
                payload={"callee": callee, "keyword": kw_name or "**"},
            )

    def _visit_expression_statement(self, node) -> None:
        children = _named_children(node)
        has_comma = any(ch.type == "," for ch in node.children)
        is_assignment = any(
            ch.type in ("assignment", "augmented_assignment") for ch in children
        )
        if children and has_comma and not is_assignment:
            # bare statement-level tuple: ast sees Expr(Tuple(...)) — including
            # the single-element `call(...),` trailing-comma form
            self._emit_synthetic_tuple(children)
        for ch in children:
            self.visit(ch)

    def _visit_type_alias(self, node) -> None:
        """PEP 695 aliases — and the tree-sitter misparse of ``type(x).a = v``.

        The grammar reads any statement starting with ``type(`` followed by
        ``=`` as a type alias, swallowing the ``type(...)`` call. A real alias
        mirrors ast.TypeAlias (only the value expression is walked); the
        misparse is reconstructed as an Assign via a line-padded re-parse of
        the target expression.
        """
        types = [ch for ch in _named_children(node) if ch.type == "type"]
        if len(types) != 2:
            self._visit_children(node)
            return
        left_inner = _named_children(types[0])
        right_inner = _named_children(types[1])
        left = _unwrap(left_inner[0]) if left_inner else None
        value = _unwrap(right_inner[0]) if right_inner else None
        if left is None or left.type in ("identifier", "generic_type"):
            if value is not None:
                self.visit(value)
            return
        target = self._reparse_expression(f"type{_text(types[0])}", node)
        self._emit(
            "dfg",
            "assignment_binding",
            node,
            payload={
                "targets": [self.render.render(target)] if target is not None else [],
                "value": self.render.render(value) if value is not None else "",
            },
            ast_kind="Assign",
        )
        if value is not None:
            self._emit_assignment_value_bits(value)
            self._maybe_emit_callable_value(value, source="assignment_value")
        if target is not None:
            self._emit_assignment_target_bits(target, value)
            self._visit_target(target)
        if value is not None:
            self.visit(value)

    def _reparse_expression(self, expr_text: str, at_node):
        """Parse ``expr_text`` standalone, line-padded to ``at_node``'s row."""
        try:
            pad = "\n" * at_node.start_point[0]
            tree = _default_parser().parse(bytes(f"{pad}({expr_text})", "utf8"))
            stmt = _named_children(tree.root_node)
            if not stmt or stmt[0].type != "expression_statement":
                return None
            inner = _named_children(stmt[0])
            if not inner:
                return None
            self._reparse_keepalive.append(tree)
            return _unwrap(inner[0])
        except Exception:
            return None

    def _visit_delete(self, node) -> None:
        for ch in _named_children(node):
            if ch.type == "expression_list":
                for target in _named_children(ch):
                    self._visit_target(target)
            else:
                self._visit_target(ch)

    def _container_mutation_values(self, method: str, positional, keywords) -> list:
        args = [self._splat_inner(arg) for arg in positional]
        if method in {"add", "append", "extend", "update"}:
            values = args
        elif method == "insert":
            values = args[1:]
        elif method == "setdefault":
            values = args[1:]
        else:
            values = []
        if method == "update":
            values.extend(kw_value for kw_name, kw_value in keywords if kw_name is not None)
        return values

    def _container_mutation_key(self, method: str, positional):
        if method in {"insert", "setdefault"} and positional:
            return self._splat_inner(positional[0])
        return None

    def _emit_container_call_facts(self, node, func) -> None:
        if func.type != "attribute":
            return
        method = _text(func.child_by_field_name("attribute"))
        container_node = _field(func, "object")
        container = self.render.render(container_node)
        positional, keywords = self._call_arguments(node)
        if method in _CONTAINER_MUTATION_METHODS:
            values = self._container_mutation_values(method, positional, keywords)
            key = self._container_mutation_key(method, positional)
            payload: dict[str, Any] = {
                "container": container,
                "method": method,
                "callee": self.render.render(func),
                "arguments": [
                    self._expr_payload(self._splat_inner(arg)) for arg in positional
                ],
                "keywords": [
                    {"name": kw_name or "**", **self._expr_payload(kw_value)}
                    for kw_name, kw_value in keywords
                ],
            }
            if key is not None:
                payload.update(self._key_payload(key))
            if values:
                payload["values"] = [self._expr_payload(value) for value in values]
                payload["value"] = self.render.render(values[0])
                payload["value_kind"] = _ast_kind(values[0])

            self._emit("dfg", "container_write_value", node, payload=payload)
            for value in values:
                self._maybe_emit_callable_value(
                    value,
                    source="container_write_value",
                    payload={"container": container, "method": method},
                )
            if key is not None:
                self._emit_literal_key(key, context="container_method_write", container=container)

        if method in _CONTAINER_READ_METHODS and positional:
            key = self._splat_inner(positional[0])
            payload = {
                "container": container,
                "method": method,
                "callee": self.render.render(func),
                **self._key_payload(key),
            }
            self._emit("dfg", "container_read_key", node, payload=payload)
            self._emit("dfg", "keyed_read", node, payload=payload)
            self._emit_literal_key(key, context="container_method_read", container=container)

    # -- branches / loops ---------------------------------------------------------

    def _emit_branch_condition(self, condition, *, kind: str) -> None:
        condition = _unwrap(condition)
        payload = {
            "kind": kind,
            "condition": self.render.render(condition),
            "condition_kind": _ast_kind(condition),
            "reads": self._read_expression_payloads(condition),
        }
        self._emit("cfg", "branch_condition", condition, payload=payload)
        self._emit("dfg", "branch_influence", condition, payload=payload)

    def _visit_if(self, node) -> None:
        self._emit("cfg", "branch_selector", node, payload={"kind": "if"}, ast_kind="If")
        condition = _field(node, "condition")
        self._emit_branch_condition(condition, kind="if")
        self.visit(condition)
        consequence = node.child_by_field_name("consequence")
        if consequence is not None:
            self._visit_children(consequence)
        for alt in node.children_by_field_name("alternative"):
            if alt.type == "elif_clause":
                self._emit(
                    "cfg", "branch_selector", alt, payload={"kind": "if"}, ast_kind="If"
                )
                alt_condition = _field(alt, "condition")
                self._emit_branch_condition(alt_condition, kind="if")
                self.visit(alt_condition)
                alt_consequence = alt.child_by_field_name("consequence")
                if alt_consequence is not None:
                    self._visit_children(alt_consequence)
            elif alt.type == "else_clause":
                self._visit_children(alt)

    def _visit_ifexp(self, node) -> None:
        self._emit(
            "cfg",
            "branch_selector",
            node,
            payload={"kind": "if_expression"},
            ast_kind="IfExp",
        )
        children = _named_children(node)
        if len(children) != 3:
            self._visit_children(node)
            return
        body, test, orelse = children
        self._emit_branch_condition(test, kind="if_expression")
        # ast field order: test, body, orelse
        self.visit(_unwrap(test))
        self.visit(_unwrap(body))
        self.visit(_unwrap(orelse))

    def _visit_match(self, node) -> None:
        self._emit("cfg", "branch_selector", node, payload={"kind": "match"}, ast_kind="Match")
        subject = _field(node, "subject")
        self._emit_branch_condition(subject, kind="match_subject")
        body = node.child_by_field_name("body")
        cases = (
            [ch for ch in _named_children(body) if ch.type == "case_clause"]
            if body is not None
            else []
        )
        for case in cases:
            guard = case.child_by_field_name("guard")
            if guard is not None:
                guard_children = _named_children(guard)
                if guard_children:
                    self._emit_branch_condition(guard_children[0], kind="match_guard")
        self.visit(subject)
        for case in cases:
            for ch in _named_children(case):
                if ch.type == "case_pattern":
                    self._visit_case_pattern(ch)
                elif ch.type == "if_clause":
                    guard_children = _named_children(ch)
                    if guard_children:
                        self.visit(_unwrap(guard_children[0]))
                elif ch.type == "block":
                    self._visit_children(ch)

    def _visit_case_pattern(self, node) -> None:
        """Mirror ast attr_read emission for dotted names inside match patterns."""
        if node.type == "dotted_name":
            segments = [ch for ch in node.named_children if ch.type == "identifier"]
            if len(segments) > 1:
                text = _text(node)
                parts = text.split(".")
                for depth in range(len(parts) - 1, 0, -1):
                    self._emit(
                        "dfg",
                        "attr_read",
                        node,
                        payload={"attribute": parts[depth]},
                        ast_kind="Attribute",
                    )
            return
        for ch in _named_children(node):
            self._visit_case_pattern(ch)

    def _visit_for(self, node) -> None:
        async_iteration = _is_async(node)
        kind = "AsyncFor" if async_iteration else "For"
        self._emit("cfg", "loop_driver", node, ast_kind=kind)
        if async_iteration:
            self._emit("cfg", "async_suspend_resume", node, ast_kind=kind)
        target = _field(node, "left")
        iterable = _field(node, "right")
        self._emit_iteration_source(target, iterable, async_iteration=async_iteration)
        self._emit_binding_targets(target, "loop_target")
        self._visit_target(target)
        self.visit(iterable)
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit_children(body)
        for alt in node.children_by_field_name("alternative"):
            self._visit_children(alt)

    def _visit_while(self, node) -> None:
        self._emit("cfg", "loop_driver", node, ast_kind="While")
        condition = _field(node, "condition")
        self._emit_branch_condition(condition, kind="while")
        self.visit(condition)
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit_children(body)
        for alt in node.children_by_field_name("alternative"):
            self._visit_children(alt)

    def _visit_comprehension(self, node) -> None:
        shape = _COLLECTION_SHAPES[node.type]
        kind = _ast_kind(node)
        self._emit("cfg", "loop_driver", node, ast_kind=kind)
        self._emit("dfg", "collection_assembly", node, payload={"shape": shape}, ast_kind=kind)
        self._emit("struct", "literal_shape", node, payload={"shape": shape}, ast_kind=kind)
        self._visit_comprehension_children(node)

    def _visit_generator(self, node) -> None:
        self._emit("cfg", "loop_driver", node, ast_kind="GeneratorExp")
        self._visit_comprehension_children(node)

    def _visit_comprehension_children(self, node) -> None:
        body = node.child_by_field_name("body")
        if body is not None:
            self.visit(_unwrap(body))
        for ch in _named_children(node):
            if ch.type == "for_in_clause":
                self._visit_target(_field(ch, "left"))
                right = _field(ch, "right")
                if right is not None:
                    self.visit(right)
            elif ch.type == "if_clause":
                inner = _named_children(ch)
                if inner:
                    self.visit(_unwrap(inner[0]))

    # -- with / try / raise ----------------------------------------------------------

    def _visit_with(self, node) -> None:
        async_with = _is_async(node)
        kind = "AsyncWith" if async_with else "With"
        self._emit("cfg", "context_enter_exit", node, ast_kind=kind)
        if async_with:
            self._emit("cfg", "async_suspend_resume", node, ast_kind=kind)
        items: list[tuple[Any, Any]] = []  # (context_expr, optional_vars)
        for clause in node.named_children:
            if clause.type != "with_clause":
                continue
            for item in _named_children(clause):
                if item.type != "with_item":
                    continue
                value = item.child_by_field_name("value")
                if value is not None and value.type == "as_pattern":
                    inner = _named_children(value)
                    context_expr = _unwrap(inner[0]) if inner else None
                    alias = value.child_by_field_name("alias")
                    target = None
                    if alias is not None:
                        alias_inner = _named_children(alias)
                        target = _unwrap(alias_inner[0]) if alias_inner else None
                    items.append((context_expr, target))
                else:
                    items.append((_unwrap(value) if value is not None else None, None))
        for _context_expr, target in items:
            if target is not None:
                self._emit(
                    "dfg",
                    "context_resource",
                    target,
                    payload={"target": self.render.render(target)},
                )
        for context_expr, target in items:
            if context_expr is not None:
                self.visit(context_expr)
            if target is not None:
                self._visit_target(target)
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit_children(body)

    def _visit_try(self, node) -> None:
        is_try_star = any(
            any(sub.type == "*" for sub in ch.children)
            for ch in node.named_children
            if ch.type == "except_clause"
        )
        if not is_try_star:
            # ast.TryStar has no visitor in the ast twin — handlers only
            self._emit("cfg", "exception_transfer", node, ast_kind="Try")
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit_children(body)
        for ch in node.named_children:
            if ch.type == "except_clause":
                self._visit_except(ch)
            elif ch.type in ("else_clause", "finally_clause"):
                self._visit_children(ch)

    def _visit_except(self, node) -> None:
        caught_expr = None
        bound_name = ""
        value = node.child_by_field_name("value")
        if value is None:
            named = [
                ch for ch in _named_children(node) if ch.type != "block"
            ]
            value = named[0] if named else None
        if value is not None and value.type == "as_pattern":
            inner = _named_children(value)
            caught_expr = _unwrap(inner[0]) if inner else None
            alias = value.child_by_field_name("alias")
            if alias is not None:
                bound_name = _text(alias)
        elif value is not None:
            caught_expr = _unwrap(value)

        self._emit("cfg", "exception_transfer", node, ast_kind="ExceptHandler")
        payload: dict[str, Any] = {
            "caught_type": self.render.render(caught_expr) if caught_expr is not None else "",
            "caught_type_kind": _ast_kind(caught_expr) if caught_expr is not None else "bare",
            "bound_name": bound_name,
        }
        if caught_expr is not None and caught_expr.type == "tuple":
            payload["caught_types"] = [
                self.render.render(elt) for elt in _named_children(caught_expr)
            ]
        self._emit("cfg", "exception_handler_type", node, payload=payload, ast_kind="ExceptHandler")
        if bound_name:
            self._emit(
                "dfg",
                "exception_value",
                node,
                payload={"name": bound_name},
                ast_kind="ExceptHandler",
            )
        if caught_expr is not None:
            self.visit(caught_expr)
        block = node.child_by_field_name("body") or next(
            (ch for ch in _named_children(node) if ch.type == "block"), None
        )
        if block is not None:
            self._visit_children(block)

    def _visit_raise(self, node) -> None:
        raw_cause = node.child_by_field_name("cause")
        cause = _unwrap(raw_cause) if raw_cause is not None else None
        exc = None
        for ch in _named_children(node):
            if raw_cause is not None and ch.id == raw_cause.id:
                continue
            exc = _unwrap(ch)
            break
        self._emit("cfg", "exception_transfer", node, ast_kind="Raise")
        payload: dict[str, Any] = {"raise_kind": "bare"}
        if exc is not None:
            payload = {"raise_kind": "expression", **self._expr_payload(exc)}
            if exc.type == "call":
                payload.update(self._constructed_output_payload(exc, destination="raise"))
        if cause is not None:
            payload["cause"] = self.render.render(cause)
            payload["cause_kind"] = _ast_kind(cause)
        self._emit("cfg", "exception_raise_value", node, payload=payload, ast_kind="Raise")
        if exc is not None:
            self.visit(exc)
        if cause is not None:
            self.visit(cause)

    def _visit_await(self, node) -> None:
        self._emit("cfg", "async_suspend_resume", node, ast_kind="Await")
        self._visit_children(node)

    # -- returns / yields ---------------------------------------------------------

    def _visit_return(self, node) -> None:
        children = _named_children(node)
        value = _unwrap(children[0]) if children else None
        self._emit("cfg", "return_exit", node, ast_kind="Return")
        self._emit(
            "dfg",
            "return_shape_kind",
            node,
            payload=self._return_shape_payload(value),
            ast_kind="Return",
        )
        if value is not None:
            self._emit("dfg", "return_output", value)
            self._maybe_emit_callable_value(value, source="return")
            if self._looks_like_constructed_output(value):
                self._emit(
                    "dfg",
                    "constructed_output",
                    value,
                    payload=self._constructed_output_payload(value, destination="return"),
                )
            if self._contains_attr_read(value):
                self._emit("dfg", "projection", value)
            self.visit(value)

    def _visit_yield(self, node) -> None:
        is_from = any(ch.type == "from" for ch in node.children)
        kind = "YieldFrom" if is_from else "Yield"
        self._emit("cfg", "generator_yield", node, ast_kind=kind)
        children = _named_children(node)
        value = _unwrap(children[0]) if children else None
        if value is not None:
            self._emit("dfg", "yield_output", value)
            self.visit(value)

    # -- assignments ---------------------------------------------------------------

    @staticmethod
    def _assignment_chain(node) -> tuple[list, Any]:
        """Flatten ``a = b = value`` chains into (targets, value)."""
        targets = []
        current = node
        while current is not None and current.type == "assignment":
            left = current.child_by_field_name("left")
            if left is not None:
                targets.append(_unwrap(left))
            nxt = current.child_by_field_name("right")
            if nxt is not None and nxt.type == "assignment":
                current = nxt
                continue
            return targets, (_unwrap(nxt) if nxt is not None else None)
        return targets, None

    def _assignment_payload(self, node) -> dict[str, Any]:
        if node.type == "assignment":
            type_node = node.child_by_field_name("type")
            if type_node is not None:
                inner = _named_children(type_node)
                annotation = inner[0] if inner else None
                value = _field(node, "right")
                return {
                    "target": self.render.render(_field(node, "left")),
                    "annotation": self.render.render(annotation) if annotation is not None else "",
                    "value": self.render.render(value) if value is not None else "",
                }
            targets, value = self._assignment_chain(node)
            return {
                "targets": [self.render.render(t) for t in targets],
                "value": self.render.render(value) if value is not None else "",
            }
        if node.type == "augmented_assignment":
            return {
                "target": self.render.render(_field(node, "left")),
                "value": self.render.render(_field(node, "right")),
            }
        return {}

    def _visit_assignment(self, node) -> None:
        type_node = node.child_by_field_name("type")
        if type_node is not None:
            self._visit_ann_assignment(node, type_node)
            return
        targets, value = self._assignment_chain(node)
        self._emit(
            "dfg",
            "assignment_binding",
            node,
            payload=self._assignment_payload(node),
            ast_kind="Assign",
        )
        if value is not None:
            self._emit_assignment_value_bits(value)
            self._maybe_emit_callable_value(value, source="assignment_value")
        for target in targets:
            self._emit_assignment_target_bits(target, value)
        for target in targets:
            self._visit_target(target)
        if value is not None:
            self.visit(value)

    def _visit_ann_assignment(self, node, type_node) -> None:
        target = _field(node, "left")
        inner = _named_children(type_node)
        annotation = inner[0] if inner else None
        value = _field(node, "right")
        if annotation is not None:
            self._emit_annotation_facts(
                annotation,
                payload={"kind": "assignment", "target": self.render.render(target)},
            )
        self._emit(
            "dfg",
            "assignment_binding",
            node,
            payload=self._assignment_payload(node),
            ast_kind="AnnAssign",
        )
        if value is not None:
            self._emit_assignment_value_bits(value)
            self._maybe_emit_callable_value(value, source="assignment_value")
        self._emit_assignment_target_bits(target, value)
        self._visit_target(target)
        if value is not None:
            self.visit(value)

    def _visit_aug_assignment(self, node) -> None:
        self._emit("dfg", "augmented_mutation", node, ast_kind="AugAssign")
        target = _field(node, "left")
        value = _field(node, "right")
        self._emit_assignment_target_bits(target, value)
        self._visit_target(target)
        if value is not None:
            self.visit(value)

    def _visit_named_expression(self, node) -> None:
        target = node.child_by_field_name("name")
        value = _field(node, "value")
        self._emit(
            "dfg",
            "assignment_binding",
            node,
            payload={"target": _text(target)},
            ast_kind="NamedExpr",
        )
        if value is not None:
            self._emit_assignment_value_bits(value)
            self._maybe_emit_callable_value(value, source="assignment_value")
            self.visit(value)

    def _emit_assignment_value_bits(self, value) -> None:
        if value is None:
            return
        value = _unwrap(value)
        if value.type == "call":
            func = _field(value, "function")
            payload = {"callee": self._call_name(func)}
            self._emit("dfg", "call_result_origin", value, payload=payload)
            if self._looks_like_constructor_call(func):
                self._emit("dfg", "constructor_value", value, payload=payload)
                self._emit(
                    "dfg",
                    "constructed_output",
                    value,
                    payload=self._constructed_output_payload(value, destination="assignment"),
                )
        if value.type in _COLLECTION_SHAPES and value.type != "expression_list":
            self._emit(
                "dfg",
                "collection_assembly",
                value,
                payload={"shape": _COLLECTION_SHAPES[value.type]},
            )
        elif value.type == "expression_list":
            self._emit("dfg", "collection_assembly", value, payload={"shape": "tuple"})

    def _flatten_targets(self, target):
        target = _unwrap(target)
        if target.type in ("pattern_list", "tuple_pattern", "list_pattern", "tuple", "list"):
            for ch in _named_children(target):
                yield from self._flatten_targets(ch)
        else:
            yield target

    def _emit_assignment_target_bits(self, target, value) -> None:
        if target is None:
            return
        for leaf in self._flatten_targets(target):
            if self._target_is_starred(leaf):
                continue  # ast yields the Starred node itself, which matches nothing
            if leaf.type == "attribute":
                attr = _text(leaf.child_by_field_name("attribute"))
                payload = {"attribute": attr, "target": self.render.render(leaf)}
                self._emit("dfg", "attr_write", leaf, payload=payload)
                obj = _field(leaf, "object")
                if obj is not None and obj.type == "identifier" and _text(obj) in {"self", "cls"}:
                    self._emit("struct", "instance_attribute_hint", leaf, payload=payload)
            elif leaf.type == "subscript":
                payload = {
                    "target": self.render.render(leaf),
                    **self._subscript_key_payload(leaf),
                }
                self._emit("dfg", "subscript_write", leaf, payload=payload)
                indexes = _fields(leaf, "subscript")
                key_node = (
                    indexes[0]
                    if len(indexes) == 1 and not _subscript_indexes_form_tuple(indexes)
                    else None
                )
                container_text = self.render.render(_field(leaf, "value"))
                if key_node is not None:
                    self._emit_literal_key(
                        key_node, context="subscript_write", container=container_text
                    )
                if value is not None:
                    write_payload = {
                        **payload,
                        "value": self.render.render(value),
                        "value_kind": _ast_kind(value),
                    }
                    self._emit("dfg", "container_write_value", leaf, payload=write_payload)
                    self._emit(
                        "dfg",
                        "keyed_write",
                        leaf,
                        payload=self._keyed_write_payload(
                            key_text=self._render_key_of_subscript(leaf),
                            key_kind=self._key_kind_of_subscript(leaf),
                            key_literal_node=key_node,
                            value=value,
                            container=container_text,
                        ),
                    )
                    self._maybe_emit_callable_value(
                        value,
                        source="container_write_value",
                        payload={
                            "container": container_text,
                            "key": self._render_key_of_subscript(leaf),
                        },
                    )
            elif leaf.type == "identifier" and value is not None and value.type == "identifier":
                self._emit(
                    "dfg",
                    "aliasing",
                    leaf,
                    payload={"target": _text(leaf), "source": _text(value)},
                )

    @staticmethod
    def _target_is_starred(leaf) -> bool:
        """True for ``*x.y`` / ``*d[k]`` targets — tree-sitter nests the splat
        inside the attribute/subscript, ast wraps the whole thing in Starred."""
        return leaf.type in ("list_splat", "list_splat_pattern") or _is_starred_chain(leaf)

    def _emit_binding_targets(self, target, source_kind: str) -> None:
        if target is None:
            return
        for leaf in self._flatten_targets(target):
            if leaf.type == "identifier":
                self._emit(
                    "dfg",
                    "assignment_binding",
                    leaf,
                    payload={"target": _text(leaf), "source_kind": source_kind},
                )

    def _emit_iteration_source(self, target, iterable, *, async_iteration: bool = False) -> None:
        self._emit(
            "dfg",
            "iteration_source",
            iterable,
            payload={
                "target": self.render.render(target),
                "target_kind": _ast_kind(target),
                "iterable": self.render.render(iterable),
                "iterable_kind": _ast_kind(iterable),
                "async": async_iteration,
            },
        )

    # -- collections ----------------------------------------------------------------

    def _visit_dict(self, node) -> None:
        self._emit("dfg", "collection_assembly", node, payload={"shape": "dict"})
        self._emit("struct", "literal_shape", node, payload={"shape": "dict"})
        entries = _named_children(node)
        for ch in entries:
            if ch.type == "pair":
                key = _unwrap(ch.child_by_field_name("key"))
                value = _unwrap(ch.child_by_field_name("value"))
                self._emit(
                    "dfg",
                    "keyed_write",
                    key,
                    payload=self._keyed_write_payload(
                        key_text=self.render.render(key),
                        key_kind=_ast_kind(key),
                        key_literal_node=key,
                        value=value,
                        container="dict_literal",
                    ),
                )
                self._emit_literal_key(key, context="dict_literal")
                if value is not None:
                    self._maybe_emit_callable_value(
                        value,
                        source="keyed_write",
                        payload={"container": "dict_literal", "key": self.render.render(key)},
                    )
                    self._maybe_emit_callable_value(value, source="collection_value")
            elif ch.type == "dictionary_splat":
                inner = _named_children(ch)
                if inner:
                    self._maybe_emit_callable_value(_unwrap(inner[0]), source="collection_value")
        self._visit_children(node)

    def _visit_list_or_tuple(self, node) -> None:
        shape = _COLLECTION_SHAPES[node.type]
        self._emit("dfg", "collection_assembly", node, payload={"shape": shape})
        self._emit("struct", "literal_shape", node, payload={"shape": shape})
        for elt in _named_children(node):
            self._maybe_emit_callable_value(_unwrap(elt), source="collection_value")
        self._visit_children(node)

    def _visit_set(self, node) -> None:
        self._emit("dfg", "collection_assembly", node, payload={"shape": "set"})
        self._emit("struct", "literal_shape", node, payload={"shape": "set"})
        for elt in _named_children(node):
            self._maybe_emit_callable_value(_unwrap(elt), source="collection_value")
        self._visit_children(node)

    # -- payload builders ---------------------------------------------------------------

    def _maybe_emit_callable_value(
        self,
        node,
        *,
        source: str,
        scope: _SymbolScope | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if node is None:
            return
        node = _unwrap(node)
        callable_payload = self._callable_value_payload(node)
        if callable_payload is None:
            return
        if payload:
            callable_payload.update(payload)
        callable_payload["source"] = source
        self._emit("dfg", "callable_value", node, scope=scope, payload=callable_payload)

    def _callable_value_payload(self, node) -> dict[str, Any] | None:
        if node.type == "lambda":
            return {"callable_kind": "lambda", **self._expr_payload(node)}
        if node.type == "identifier" and self._name_is_callable_binding(_text(node)):
            return {"callable_kind": "known_name", **self._expr_payload(node)}
        return None

    def _expr_payload(self, node) -> dict[str, Any]:
        node = _unwrap(node)
        payload: dict[str, Any] = {
            "expression": self.render.render(node),
            "expression_kind": _ast_kind(node),
        }
        t = node.type
        if t == "identifier":
            payload["name"] = _text(node)
        elif t == "attribute":
            payload["attribute"] = _text(node.child_by_field_name("attribute"))
            payload["receiver"] = self.render.render(_field(node, "object"))
        elif t == "subscript":
            payload["container"] = self.render.render(_field(node, "value"))
            payload["key"] = self._render_key_of_subscript(node)
        elif t in _CONSTANT_TYPES and not _is_fstring(node):
            value = self.render.literal_value(node)
            if value is not _NO_LITERAL:
                payload["literal"] = _json_safe_literal(value)
        elif t == "call":
            func = _field(node, "function")
            payload["callee"] = self.render.render(func)
            payload["callee_kind"] = _ast_kind(func)
        return payload

    def _read_expression_payloads(self, node) -> list[dict[str, Any]]:
        """BFS over an expression, mirroring ``ast.walk`` read collection."""
        from collections import deque

        reads: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        queue = deque([node])
        while queue:
            current = queue.popleft()
            t = current.type
            if t in _IGNORED_CHILD_TYPES:
                continue
            payload: dict[str, Any] | None = None
            if t == "subscript":
                payload = {
                    "read_kind": "subscript",
                    **self._expr_payload(current),
                    **self._subscript_key_payload(current),
                }
            elif t == "attribute":
                payload = {"read_kind": "attribute", **self._expr_payload(current)}
            elif t == "identifier" and not self._is_store_identifier(current):
                payload = {"read_kind": "name", **self._expr_payload(current)}
            if payload is not None:
                key = (str(payload["expression"]), str(payload["expression_kind"]))
                if key not in seen:
                    seen.add(key)
                    reads.append(payload)
            for ch in self._reads_children(current):
                if ch is not None:
                    queue.append(_unwrap(ch))
        return reads

    @staticmethod
    def _reads_children(current) -> list:
        """Children in ``ast.walk`` order: flatten wrappers, skip store slots
        and identifier tokens that are not real ast Name nodes."""
        t = current.type
        if t == "call":
            func = current.child_by_field_name("function")
            args_node = current.child_by_field_name("arguments")
            out = [func] if func is not None else []
            if args_node is not None:
                if args_node.type == "generator_expression":
                    out.append(args_node)
                else:
                    out.extend(_named_children(args_node))
            return out
        if t == "attribute":
            return [current.child_by_field_name("object")]
        if t == "keyword_argument" or t == "named_expression":
            return [current.child_by_field_name("value")]
        if t == "conditional_expression":
            children = _named_children(current)
            # ast IfExp field order: test, body, orelse
            return [children[1], children[0], children[2]] if len(children) == 3 else children
        if t == "dictionary":
            keys: list = []
            values: list = []
            for ch in _named_children(current):
                if ch.type == "pair":
                    key = ch.child_by_field_name("key")
                    value = ch.child_by_field_name("value")
                    if key is not None:
                        keys.append(key)
                    if value is not None:
                        values.append(value)
                elif ch.type == "dictionary_splat":
                    values.extend(_named_children(ch))
                else:
                    values.append(ch)
            return keys + values
        if t == "for_in_clause":
            return [current.child_by_field_name("right")]
        if t == "lambda":
            return [current.child_by_field_name("body")]
        return [ch for ch in current.named_children if ch.type not in _IGNORED_CHILD_TYPES]

    @staticmethod
    def _is_store_identifier(node) -> bool:
        parent = node.parent
        if parent is None:
            return False
        if parent.type == "named_expression":
            name = parent.child_by_field_name("name")
            return name is not None and name.id == node.id
        return False

    def _render_key_of_subscript(self, node) -> str:
        return self.render.render_subscript_index(node, bare=False)

    @staticmethod
    def _key_kind_of_subscript(node) -> str:
        indexes = _fields(node, "subscript")
        if not indexes or _subscript_indexes_form_tuple(indexes):
            return "Tuple"
        return _ast_kind(indexes[0])

    def _subscript_key_payload(self, node) -> dict[str, Any]:
        value = _field(node, "value")
        indexes = _fields(node, "subscript")
        key_node = (
            indexes[0]
            if len(indexes) == 1 and not _subscript_indexes_form_tuple(indexes)
            else None
        )
        payload: dict[str, Any] = {
            "container": self.render.render(value),
            "container_kind": _ast_kind(value),
            "subscript": self.render.render(node),
            "key": self._render_key_of_subscript(node),
            "key_kind": self._key_kind_of_subscript(node),
        }
        if key_node is not None:
            literal = self.render.literal_value(key_node)
            if literal is not _NO_LITERAL:
                payload["key_literal"] = _json_safe_literal(literal)
        return payload

    def _key_payload(self, key) -> dict[str, Any]:
        key = _unwrap(key)
        payload: dict[str, Any] = {
            "key": self.render.render(key),
            "key_kind": _ast_kind(key),
        }
        literal = self.render.literal_value(key)
        if literal is not _NO_LITERAL:
            payload["key_literal"] = _json_safe_literal(literal)
        return payload

    def _keyed_write_payload(
        self,
        *,
        key_text: str,
        key_kind: str,
        key_literal_node,
        value,
        container: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "container": container,
            "key": key_text,
            "key_kind": key_kind,
        }
        if key_literal_node is not None:
            literal = self.render.literal_value(key_literal_node)
            if literal is not _NO_LITERAL:
                payload["key_literal"] = _json_safe_literal(literal)
        if value is not None:
            payload["value"] = self.render.render(value)
            payload["value_kind"] = _ast_kind(value)
        return payload

    def _emit_literal_key(self, key, *, context: str, container: str = "") -> None:
        key = _unwrap(key)
        literal = self.render.literal_value(key)
        if literal is _NO_LITERAL:
            return
        safe = _json_safe_literal(literal)
        payload: dict[str, Any] = {
            "key": self.render.render(key),
            "key_kind": _ast_kind(key),
            "key_literal": safe,
            "literal": safe,
            "context": context,
        }
        if container:
            payload["container"] = container
        self._emit("struct", "literal_key", key, payload=payload)

    def _emit_annotation_facts(
        self,
        annotation,
        *,
        scope: _SymbolScope | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        annotation = _unwrap(annotation)
        annotation_payload = {"annotation": self.render.render(annotation)}
        if payload:
            annotation_payload.update(payload)
        self._emit("struct", "annotation", annotation, scope=scope, payload=annotation_payload)
        for generic in self._iter_generic_shapes(annotation):
            self._emit(
                "struct",
                "generic_shape",
                generic,
                scope=scope,
                payload={**annotation_payload, **self._generic_shape_payload(generic)},
            )

    def _iter_generic_shapes(self, annotation):
        """BFS for subscript/generic_type nodes, mirroring ``ast.walk`` order."""
        from collections import deque

        queue = deque([annotation])
        while queue:
            current = queue.popleft()
            if current.type in ("subscript", "generic_type"):
                yield current
            for ch in current.named_children:
                if ch.type not in _IGNORED_CHILD_TYPES:
                    queue.append(ch)

    def _generic_shape_payload(self, node) -> dict[str, Any]:
        if node.type == "generic_type":
            children = _named_children(node)
            base = children[0] if children else None
            args = (
                [_unwrap(arg) for arg in _named_children(children[1])]
                if len(children) == 2 and children[1].type == "type_parameter"
                else []
            )
            return {
                "generic": self.render.render(node),
                "base": self.render.render(base) if base is not None else "",
                "args": [self._expr_payload(arg) for arg in args],
            }
        indexes = _fields(node, "subscript")
        return {
            "generic": self.render.render(node),
            "base": self.render.render(_field(node, "value")),
            "args": [self._expr_payload(arg) for arg in indexes],
        }

    def _return_shape_payload(self, value) -> dict[str, Any]:
        if value is None:
            return {"shape_kind": "none", "expression": "", "expression_kind": "None"}
        payload = {"shape_kind": self._return_shape_kind(value), **self._expr_payload(value)}
        if value.type in _COLLECTION_SHAPES:
            payload["collection_shape"] = _COLLECTION_SHAPES[value.type]
        return payload

    def _return_shape_kind(self, value) -> str:
        t = value.type
        if t in ("dictionary", "dictionary_comprehension"):
            return "mapping"
        if t in ("list", "tuple", "expression_list", "list_comprehension"):
            return "sequence"
        if t in ("set", "set_comprehension"):
            return "set"
        if self._looks_like_constructed_output(value):
            return "constructed"
        if t == "call":
            return "call_result"
        if t == "lambda":
            return "callable"
        if t == "identifier":
            return "name"
        if t == "attribute":
            return "attribute"
        if t == "subscript":
            return "subscript"
        if t in _CONSTANT_TYPES and not _is_fstring(value):
            return "literal"
        return _ast_kind(value)

    def _looks_like_constructed_output(self, value) -> bool:
        if value is None or value.type != "call":
            return False
        func = _field(value, "function")
        return self._looks_like_constructor_call(func)

    def _constructed_output_payload(self, call, *, destination: str) -> dict[str, Any]:
        func = _field(call, "function")
        positional, keywords = self._call_arguments(call)
        return {
            "destination": destination,
            "callee": self.render.render(func),
            "callee_kind": _ast_kind(func),
            "args": [self._expr_payload(self._splat_inner(arg)) for arg in positional],
            "keywords": [
                {"keyword": kw_name or "**", **self._expr_payload(kw_value)}
                for kw_name, kw_value in keywords
            ],
        }

    def _contains_attr_read(self, node) -> bool:
        if node.type == "attribute":
            return True
        return any(
            self._contains_attr_read(ch) for ch in node.named_children if ch.type not in _IGNORED_CHILD_TYPES
        )
