"""Linear-time import and lightweight call-pattern scanners (ReDoS-safe)."""

from __future__ import annotations

from collections.abc import Iterator


def read_quoted_literal(source_code: str, start: int) -> tuple[str, int] | None:
    if start >= len(source_code) or source_code[start] not in "'\"":
        return None
    quote = source_code[start]
    out: list[str] = []
    i = start + 1
    while i < len(source_code):
        ch = source_code[i]
        if ch == "\\" and i + 1 < len(source_code):
            out.append(source_code[i : i + 2])
            i += 2
            continue
        if ch == quote:
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    return None


def find_closing_brace(source_code: str, open_index: int) -> int | None:
    if open_index >= len(source_code) or source_code[open_index] != "{":
        return None
    depth = 0
    in_single = in_double = False
    i = open_index
    while i < len(source_code):
        ch = source_code[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if in_single or in_double:
            if ch == "\\" and i + 1 < len(source_code):
                i += 2
                continue
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _is_js_identifier_char(ch: str, *, first: bool = False) -> bool:
    if not ch:
        return False
    if ch.isalpha() or ch in "_$":
        return True
    return not first and ch.isdigit()


def is_js_word_at(source_code: str, index: int, word: str) -> bool:
    if not source_code.startswith(word, index):
        return False
    before_ok = index == 0 or not _is_js_identifier_char(source_code[index - 1])
    after_index = index + len(word)
    after_ok = after_index >= len(source_code) or not _is_js_identifier_char(
        source_code[after_index], first=True
    )
    return before_ok and after_ok


def iter_es_module_imports(source_code: str) -> Iterator[tuple[str, str]]:
    i = 0
    source_len = len(source_code)
    while i < source_len:
        idx = source_code.find("import", i)
        if idx < 0:
            break
        if not is_js_word_at(source_code, idx, "import"):
            i = idx + 6
            continue
        cursor = idx + 6
        while cursor < source_len and source_code[cursor] in " \t\n\r":
            cursor += 1
        spec_start = cursor
        from_idx = source_code.find(" from ", cursor)
        if from_idx < 0 or ";" in source_code[spec_start:from_idx]:
            i = idx + 6
            continue
        spec = source_code[spec_start:from_idx].strip()
        path_cursor = from_idx + 6
        while path_cursor < source_len and source_code[path_cursor] in " \t\n\r":
            path_cursor += 1
        parsed = read_quoted_literal(source_code, path_cursor)
        if parsed is None or not spec:
            i = idx + 6
            continue
        source_path, path_cursor = parsed
        yield spec, source_path
        i = path_cursor


def iter_const_destructure_requires(source_code: str) -> Iterator[tuple[str, str]]:
    i = 0
    source_len = len(source_code)
    while i < source_len:
        idx = source_code.find("const", i)
        if idx < 0:
            break
        if not is_js_word_at(source_code, idx, "const"):
            i = idx + 5
            continue
        cursor = idx + 5
        while cursor < source_len and source_code[cursor] in " \t\n\r":
            cursor += 1
        if cursor >= source_len or source_code[cursor] != "{":
            i = idx + 5
            continue
        close_brace = find_closing_brace(source_code, cursor)
        if close_brace is None:
            break
        inner = source_code[cursor + 1 : close_brace]
        cursor = close_brace + 1
        while cursor < source_len and source_code[cursor] in " \t\n\r":
            cursor += 1
        if cursor >= source_len or source_code[cursor] != "=":
            i = idx + 5
            continue
        cursor += 1
        while cursor < source_len and source_code[cursor] in " \t\n\r":
            cursor += 1
        if not source_code.startswith("require(", cursor):
            i = idx + 5
            continue
        cursor += len("require(")
        while cursor < source_len and source_code[cursor] in " \t\n\r":
            cursor += 1
        parsed = read_quoted_literal(source_code, cursor)
        if parsed is None:
            i = idx + 5
            continue
        source_path, cursor = parsed
        yield inner, source_path
        i = cursor


def iter_simple_commonjs_requires(source_code: str) -> Iterator[tuple[str, str]]:
    i = 0
    source_len = len(source_code)
    while i < source_len:
        next_match: tuple[int, str] | None = None
        for keyword in ("const", "let", "var"):
            idx = source_code.find(keyword, i)
            if idx < 0:
                continue
            if not is_js_word_at(source_code, idx, keyword):
                continue
            if next_match is None or idx < next_match[0]:
                next_match = (idx, keyword)
        if next_match is None:
            break
        idx, keyword = next_match
        cursor = idx + len(keyword)
        while cursor < source_len and source_code[cursor] in " \t\n\r":
            cursor += 1
        name_start = cursor
        if name_start >= source_len or not _is_js_identifier_char(
            source_code[name_start], first=True
        ):
            i = idx + len(keyword)
            continue
        cursor = name_start + 1
        while cursor < source_len and _is_js_identifier_char(source_code[cursor]):
            cursor += 1
        alias = source_code[name_start:cursor]
        while cursor < source_len and source_code[cursor] in " \t\n\r":
            cursor += 1
        if cursor >= source_len or source_code[cursor] != "=":
            i = idx + len(keyword)
            continue
        cursor += 1
        while cursor < source_len and source_code[cursor] in " \t\n\r":
            cursor += 1
        if not source_code.startswith("require(", cursor):
            i = idx + len(keyword)
            continue
        cursor += len("require(")
        while cursor < source_len and source_code[cursor] in " \t\n\r":
            cursor += 1
        parsed = read_quoted_literal(source_code, cursor)
        if parsed is None or not alias:
            i = idx + len(keyword)
            continue
        source_path, cursor = parsed
        yield alias, source_path
        i = cursor


def _is_python_import_module(name: str) -> bool:
    if not name:
        return False
    index = 0
    while index < len(name) and name[index] == ".":
        index += 1
    rest = name[index:]
    if not rest:
        return index > 0
    return all(part.isidentifier() for part in rest.split("."))


def split_python_from_import(line: str) -> tuple[str, str] | None:
    """Parse ``from MODULE import BODY`` without backtracking regex."""
    if not line.startswith("from "):
        return None
    remainder = line[5:]
    sep = " import "
    split_at = remainder.find(sep)
    if split_at < 0:
        return None
    module = remainder[:split_at].strip()
    body = remainder[split_at + len(sep) :].strip()
    if not module or not body or not _is_python_import_module(module):
        return None
    return module, body


def split_python_import_clause(line: str) -> str | None:
    """Parse ``import ITEMS`` body without backtracking regex."""
    if not line.startswith("import "):
        return None
    body = line[7:].strip()
    return body if body else None


def _skip_typescript_generic(source: str, start: int) -> int | None:
    if start >= len(source) or source[start] != "<":
        return None
    i = start + 1
    while i < len(source):
        ch = source[i]
        if ch in "\n;{}()":
            return None
        if ch == ">":
            return i + 1
        i += 1
    return None


def iter_typescript_body_call_fallback_names(body: str) -> Iterator[tuple[str, int]]:
    """Yield ``(callee_name, name_start)`` for ``name<...>?(...)`` call shapes."""
    i = 0
    body_len = len(body)
    while i < body_len:
        ch = body[i]
        if not (ch.isalpha() or ch in "_$") or (
            i > 0 and (body[i - 1].isalnum() or body[i - 1] in "_$")
        ):
            i += 1
            continue
        name_start = i
        i += 1
        while i < body_len and (body[i].isalnum() or body[i] in "_$"):
            i += 1
        name = body[name_start:i]
        cursor = i
        while cursor < body_len and body[cursor] in " \t":
            cursor += 1
        if cursor < body_len and body[cursor] == "<":
            generic_end = _skip_typescript_generic(body, cursor)
            if generic_end is None:
                continue
            cursor = generic_end
            while cursor < body_len and body[cursor] in " \t":
                cursor += 1
        if cursor < body_len and body[cursor] == "(":
            yield name, name_start
