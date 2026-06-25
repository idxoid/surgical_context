"""Linear-time import and lightweight call-pattern scanners (ReDoS-safe)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from context_engine.parser.adapters.ts_package_aliases import resolve_package_subpath
from context_engine.parser.uid import current_project_root, module_name_from_path

_REQUIRE_CALL = "require("


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


def _toggle_js_string_quote(ch: str, in_single: bool, in_double: bool) -> tuple[bool, bool] | None:
    if ch == "'" and not in_double:
        return (not in_single, in_double)
    if ch == '"' and not in_single:
        return (in_single, not in_double)
    return None


def _advance_inside_js_string(source_code: str, index: int) -> int:
    if source_code[index] == "\\" and index + 1 < len(source_code):
        return index + 2
    return index + 1


def _skip_js_whitespace(source_code: str, cursor: int) -> int:
    while cursor < len(source_code) and source_code[cursor] in " \t\n\r":
        cursor += 1
    return cursor


def find_closing_brace(source_code: str, open_index: int) -> int | None:
    if open_index >= len(source_code) or source_code[open_index] != "{":
        return None
    depth = 0
    in_single = in_double = False
    i = open_index
    while i < len(source_code):
        ch = source_code[i]
        toggled = _toggle_js_string_quote(ch, in_single, in_double)
        if toggled is not None:
            in_single, in_double = toggled
            i += 1
            continue
        if in_single or in_double:
            i = _advance_inside_js_string(source_code, i)
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


def _try_parse_es_import(source_code: str, idx: int) -> tuple[str, str, int] | None:
    if not is_js_word_at(source_code, idx, "import"):
        return None
    cursor = _skip_js_whitespace(source_code, idx + 6)
    spec_start = cursor
    from_idx = source_code.find(" from ", cursor)
    if from_idx < 0 or ";" in source_code[spec_start:from_idx]:
        return None
    spec = source_code[spec_start:from_idx].strip()
    path_cursor = _skip_js_whitespace(source_code, from_idx + 6)
    parsed = read_quoted_literal(source_code, path_cursor)
    if parsed is None or not spec:
        return None
    source_path, path_cursor = parsed
    return spec, source_path, path_cursor


def iter_es_module_imports(source_code: str) -> Iterator[tuple[str, str]]:
    i = 0
    source_len = len(source_code)
    while i < source_len:
        idx = source_code.find("import", i)
        if idx < 0:
            break
        parsed = _try_parse_es_import(source_code, idx)
        if parsed is None:
            i = idx + 6
            continue
        spec, source_path, i = parsed
        yield spec, source_path


@dataclass(frozen=True)
class _ConstDestructureScan:
    next_i: int
    value: tuple[str, str] | None = None
    stop: bool = False


def _scan_const_destructure_require(source_code: str, idx: int) -> _ConstDestructureScan:
    if not is_js_word_at(source_code, idx, "const"):
        return _ConstDestructureScan(idx + 5)
    cursor = _skip_js_whitespace(source_code, idx + 5)
    if cursor >= len(source_code) or source_code[cursor] != "{":
        return _ConstDestructureScan(idx + 5)
    close_brace = find_closing_brace(source_code, cursor)
    if close_brace is None:
        return _ConstDestructureScan(idx, stop=True)
    inner = source_code[cursor + 1 : close_brace]
    cursor = _skip_js_whitespace(source_code, close_brace + 1)
    if cursor >= len(source_code) or source_code[cursor] != "=":
        return _ConstDestructureScan(idx + 5)
    cursor = _skip_js_whitespace(source_code, cursor + 1)
    parsed = _parse_require_literal(source_code, cursor)
    if parsed is None:
        return _ConstDestructureScan(idx + 5)
    source_path, cursor = parsed
    return _ConstDestructureScan(cursor, value=(inner, source_path))


def iter_const_destructure_requires(source_code: str) -> Iterator[tuple[str, str]]:
    i = 0
    source_len = len(source_code)
    while i < source_len:
        idx = source_code.find("const", i)
        if idx < 0:
            break
        scanned = _scan_const_destructure_require(source_code, idx)
        if scanned.stop:
            break
        if scanned.value is not None:
            yield scanned.value
        i = scanned.next_i


_DECL_KEYWORDS = ("const", "let", "var")


def _find_earliest_decl_keyword(source_code: str, start: int) -> tuple[int, str] | None:
    next_match: tuple[int, str] | None = None
    for keyword in _DECL_KEYWORDS:
        idx = source_code.find(keyword, start)
        if idx < 0:
            continue
        if not is_js_word_at(source_code, idx, keyword):
            continue
        if next_match is None or idx < next_match[0]:
            next_match = (idx, keyword)
    return next_match


def _read_js_identifier(source_code: str, cursor: int) -> tuple[str, int] | None:
    if cursor >= len(source_code) or not _is_js_identifier_char(source_code[cursor], first=True):
        return None
    name_start = cursor
    cursor += 1
    while cursor < len(source_code) and _is_js_identifier_char(source_code[cursor]):
        cursor += 1
    return source_code[name_start:cursor], cursor


def _parse_require_literal(source_code: str, cursor: int) -> tuple[str, int] | None:
    cursor = _skip_js_whitespace(source_code, cursor)
    if not source_code.startswith(_REQUIRE_CALL, cursor):
        return None
    cursor = _skip_js_whitespace(source_code, cursor + len(_REQUIRE_CALL))
    return read_quoted_literal(source_code, cursor)


def _try_parse_simple_commonjs_require(
    source_code: str,
    idx: int,
    keyword: str,
) -> tuple[str, str, int] | None:
    cursor = _skip_js_whitespace(source_code, idx + len(keyword))
    ident = _read_js_identifier(source_code, cursor)
    if ident is None:
        return None
    alias, cursor = ident
    cursor = _skip_js_whitespace(source_code, cursor)
    if cursor >= len(source_code) or source_code[cursor] != "=":
        return None
    parsed = _parse_require_literal(source_code, cursor + 1)
    if parsed is None or not alias:
        return None
    source_path, cursor = parsed
    return alias, source_path, cursor


def iter_simple_commonjs_requires(source_code: str) -> Iterator[tuple[str, str]]:
    i = 0
    source_len = len(source_code)
    while i < source_len:
        next_match = _find_earliest_decl_keyword(source_code, i)
        if next_match is None:
            break
        idx, keyword = next_match
        parsed = _try_parse_simple_commonjs_require(source_code, idx, keyword)
        if parsed is None:
            i = idx + len(keyword)
            continue
        alias, source_path, i = parsed
        yield alias, source_path


def parse_named_import_bindings(spec: str, source: str, out: dict[str, str]) -> None:
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


def _register_es_module_import(
    spec: str,
    import_source: str,
    normalize_source: Callable[[str], str],
    bindings: dict[str, str],
    module_aliases: set[str],
) -> None:
    spec = spec.strip()
    source = normalize_source(import_source.strip())
    if not spec or not source:
        return
    if spec.startswith("{") and spec.endswith("}"):
        parse_named_import_bindings(spec[1:-1], source, bindings)
        return
    if spec.startswith("* as "):
        alias = spec[len("* as ") :].strip()
        if alias:
            bindings[alias] = source
            module_aliases.add(alias)
        return
    if "," in spec:
        default_alias, rest = spec.split(",", 1)
        default_alias = default_alias.strip()
        if default_alias:
            bindings[default_alias] = source
        rest = rest.strip()
        if rest.startswith("{") and rest.endswith("}"):
            parse_named_import_bindings(rest[1:-1], source, bindings)
        return
    bindings[spec] = source


def build_js_module_import_bindings(
    source_code: str,
    normalize_source: Callable[[str], str],
) -> tuple[dict[str, str], set[str]]:
    """Shared ES/CJS import binding table for JavaScript and TypeScript adapters."""
    bindings: dict[str, str] = {}
    module_aliases: set[str] = set()
    for spec, import_source in iter_es_module_imports(source_code):
        _register_es_module_import(
            spec,
            import_source,
            normalize_source,
            bindings,
            module_aliases,
        )
    for body, import_source in iter_const_destructure_requires(source_code):
        source = normalize_source(import_source.strip())
        parse_named_import_bindings(body, source, bindings)
    for alias, import_source in iter_simple_commonjs_requires(source_code):
        source = normalize_source(import_source.strip())
        if alias and source:
            bindings[alias] = source
    return bindings, module_aliases


def collect_js_ts_import_bindings(
    source_code: str,
    file_path: str,
    normalize_import_source: Callable[[str, str], str],
) -> tuple[dict[str, str], set[str]]:
    return build_js_module_import_bindings(
        source_code,
        lambda import_source: normalize_import_source(file_path, import_source),
    )


def module_name_for_js_resolved_path(resolved: Path) -> str | None:
    for candidate in (
        resolved.with_suffix(".js"),
        resolved.with_suffix(".jsx"),
        resolved / "index.js",
    ):
        if candidate.exists():
            return module_name_from_path(str(candidate))
    return None


def resolve_import_module_name(
    file_path: str,
    source: str,
    *,
    module_for_resolved: Callable[[Path], str | None],
) -> str:
    if not source:
        return ""
    if not source.startswith("."):
        project_root = current_project_root()
        if project_root:
            aliased = resolve_package_subpath(project_root, source)
            if aliased:
                mod = module_for_resolved(Path(aliased))
                if mod:
                    return mod
        return source.replace("/", ".")
    base = Path(file_path).parent
    project_root = current_project_root()
    if project_root:
        base = (Path(project_root) / base).resolve()
    else:
        base = base.resolve()
    resolved = (base / source).resolve()
    mod = module_for_resolved(resolved)
    if mod:
        return mod
    return source.lstrip("./").replace("/", ".")


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


def _skip_ts_whitespace(body: str, cursor: int) -> int:
    while cursor < len(body) and body[cursor] in " \t":
        cursor += 1
    return cursor


def _is_typescript_identifier_start(body: str, index: int) -> bool:
    ch = body[index]
    if not (ch.isalpha() or ch in "_$"):
        return False
    return index == 0 or not (body[index - 1].isalnum() or body[index - 1] in "_$")


def _read_typescript_identifier(body: str, start: int) -> tuple[str, int, int]:
    end = start + 1
    body_len = len(body)
    while end < body_len and (body[end].isalnum() or body[end] in "_$"):
        end += 1
    return body[start:end], start, end


def _cursor_after_optional_generic(body: str, cursor: int) -> int | None:
    cursor = _skip_ts_whitespace(body, cursor)
    if cursor >= len(body) or body[cursor] != "<":
        return cursor
    generic_end = _skip_typescript_generic(body, cursor)
    if generic_end is None:
        return None
    return _skip_ts_whitespace(body, generic_end)


def iter_typescript_body_call_fallback_names(body: str) -> Iterator[tuple[str, int]]:
    """Yield ``(callee_name, name_start)`` for ``name<...>?(...)`` call shapes."""
    i = 0
    body_len = len(body)
    while i < body_len:
        if not _is_typescript_identifier_start(body, i):
            i += 1
            continue
        name, name_start, i = _read_typescript_identifier(body, i)
        cursor = _cursor_after_optional_generic(body, i)
        if cursor is None:
            continue
        if cursor < body_len and body[cursor] == "(":
            yield name, name_start
