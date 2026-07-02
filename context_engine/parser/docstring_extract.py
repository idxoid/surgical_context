"""Extract in-code docstrings / JSDoc for symbol metadata."""

from __future__ import annotations

import ast
import re

_SKIP_JS_SIBLING_TYPES = frozenset(
    {
        "decorator",
        "export",
        "default",
        "abstract",
        "async",
        "static",
        "readonly",
        "public",
        "private",
        "protected",
    }
)

_PYTHON_DEF_TYPES = frozenset({"function_definition", "class_definition"})


def _node_source(source_code: str, node) -> str:
    """Slice a node's text byte-safely.

    Tree-sitter offsets are BYTE offsets; slicing the ``str`` with them shifts
    the window as soon as any non-ASCII character (an em-dash in a docstring)
    appears earlier in the file — the mangled literal then fails
    ``ast.literal_eval`` and every later docstring in the file is lost.
    """
    text = getattr(node, "text", None)
    if text is not None:
        return text.decode("utf-8", errors="replace")
    raw = source_code.encode("utf-8")
    return raw[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


_TS_DEF_TYPES = frozenset(
    {
        "function_declaration",
        "method_definition",
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
    }
)


def strip_jsdoc(text: str) -> str:
    """Normalize a ``/** ... */`` block to plain text."""
    stripped = text.strip()
    if not stripped.startswith("/**"):
        return ""
    inner = stripped[3:]
    if inner.endswith("*/"):
        inner = inner[:-2]
    lines: list[str] = []
    for line in inner.splitlines():
        lines.append(re.sub(r"^\s*\*\s?", "", line).rstrip())
    return "\n".join(lines).strip()


def leading_jsdoc(source_code: str, node) -> str:
    """Return the nearest leading ``/** */`` comment above a declaration.

    Skips decorators and export modifiers — the same sibling walk used for
    decorator attachment, but collecting doc comments instead.
    """
    anchor = node
    if anchor.parent is not None and anchor.parent.type == "export_statement":
        anchor = anchor.parent
    parent = anchor.parent
    if parent is None:
        return ""
    children = parent.children
    try:
        anchor_idx = children.index(anchor)
    except ValueError:
        return ""
    for sib in reversed(children[:anchor_idx]):
        if sib.type == "decorator":
            continue
        if sib.type in _SKIP_JS_SIBLING_TYPES:
            continue
        if sib.type == "comment":
            text = _node_source(source_code, sib)
            if text.lstrip().startswith("/**"):
                return strip_jsdoc(text)
            continue
        break
    return ""


def python_docstring(source_code: str, node) -> str:
    """First string literal in a function/class body (PEP 257 docstring)."""
    if node.type not in _PYTHON_DEF_TYPES:
        return ""
    body = node.child_by_field_name("body")
    if body is None:
        return ""
    for child in body.children:
        if child.type != "expression_statement":
            continue
        expr = child.children[0] if child.children else None
        if expr is None or expr.type != "string":
            continue
        literal = _node_source(source_code, expr)
        try:
            value = ast.literal_eval(literal)
        except (SyntaxError, ValueError):
            return ""
        return str(value).strip() if isinstance(value, str) else ""
    return ""


def python_module_docstring(source_code: str, root) -> str:
    """PEP 257 module docstring — the file's first statement, comments aside.

    Unlike defs/classes the module node has no ``body`` field; its statements
    are direct children. Stops at the first non-comment statement: a docstring
    below any real code is not a module docstring.
    """
    for child in root.children:
        if child.type == "comment":
            continue
        if child.type != "expression_statement":
            return ""
        expr = child.children[0] if child.children else None
        if expr is None or expr.type != "string":
            return ""
        literal = _node_source(source_code, expr)
        try:
            value = ast.literal_eval(literal)
        except (SyntaxError, ValueError):
            return ""
        return str(value).strip() if isinstance(value, str) else ""
    return ""


def docstrings_by_start_line(source_code: str, tree, *, language: str) -> dict[int, str]:
    """Map ``start_line`` (1-based) -> docstring text for one source file."""
    if tree is None:
        return {}
    out: dict[int, str] = {}
    if language == "python":
        # Module docstring keys on the module symbol's start line (always 1),
        # not the literal's own line — a shebang/comment above the docstring
        # must not detach it from the module symbol.
        module_doc = python_module_docstring(source_code, tree.root_node)
        if module_doc:
            out[tree.root_node.start_point[0] + 1] = module_doc
    for node in _iter_nodes(tree.root_node):
        if language == "python":
            if node.type not in _PYTHON_DEF_TYPES:
                continue
            text = python_docstring(source_code, node)
        elif language in {"typescript", "javascript"}:
            if node.type not in _TS_DEF_TYPES:
                continue
            text = leading_jsdoc(source_code, node)
        else:
            continue
        if text:
            out[node.start_point[0] + 1] = text
    return out


def attach_docstrings(symbols, source_code: str, *, tree, language: str) -> None:
    """Mutate ``SymbolMetadata`` rows in place with extracted docstrings."""
    by_line = docstrings_by_start_line(source_code, tree, language=language)
    if not by_line:
        return
    for symbol in symbols:
        doc = by_line.get(symbol.start_line, "")
        if doc:
            symbol.docstring = doc


def _iter_nodes(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))
