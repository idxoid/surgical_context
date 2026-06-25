"""Derive CALLS_EXTERNAL / IMPORTS_EXTERNAL link rows from parse facts (C1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from context_engine.indexer.external_boundary import (
    classify_external_root,
    external_pkg_uid,
    external_root_from_qualified_name,
    external_symbol_uid,
)
from context_engine.parser.import_scan import split_python_from_import


@dataclass(frozen=True)
class ExternalCallLink:
    caller_uid: str
    external_root: str
    callee_member: str
    call_site_line: int
    confidence: float
    kind: str = "call"


@dataclass(frozen=True)
class ExternalImportLink:
    file_path: str
    external_root: str


@dataclass(frozen=True)
class ExternalSymbolImportLink:
    """One named import from an external module: ``from M import N [as A]``.

    ``qualified_name`` is ``M.N`` (the upstream identity of the imported symbol,
    used for catalogue lookup). ``local_alias`` is the in-file binding name
    (``N`` if no ``as`` clause; ``A`` if aliased). The pair lets us connect
    *the local name* a file uses to *the upstream qualified name* the catalogue
    speaks about, without name-pattern matching inside the catalogue.
    """

    file_path: str
    qualified_name: str
    module: str
    name: str
    local_alias: str


def _module_root(module: str) -> str:
    module = (module or "").strip()
    if not module or module.startswith("."):
        return ""
    if module.startswith("@"):
        parts = module.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else module
    return re.split(r"[/.]", module, maxsplit=1)[0]


def _module_from_import_line(stripped: str) -> str:
    if stripped.startswith("import "):
        match = re.search(r"\bfrom\s+['\"]([^'\"]+)['\"]", stripped)
        if match:
            return match.group(1).strip()
        side_effect = re.match(r"import\s+['\"]([^'\"]+)['\"]", stripped)
        if side_effect:
            return side_effect.group(1).strip()
        return stripped[7:].split(",")[0].strip().split(" as ")[0].strip()
    if stripped.startswith("from "):
        parts = stripped[5:].split(" import ", 1)
        if len(parts) == 2:
            return parts[0].strip()
    return ""


def _import_roots_from_source(
    source_code: str,
    file_path: str,
    boundary: frozenset[str],
    project_external_roots: frozenset[str] = frozenset(),
) -> list[str]:
    """Scan Python/JS import syntaxes for external package roots."""
    roots: list[str] = []
    seen: set[str] = set()

    def add_module(module: str) -> None:
        root = _module_root(module)
        if (
            not root
            or root in seen
            or classify_external_root(root, boundary, project_external_roots) != "external"
        ):
            return
        seen.add(root)
        roots.append(root)

    for line in source_code.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        add_module(_module_from_import_line(stripped))

    for match in re.finditer(r"\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)", source_code):
        add_module(match.group(1))
    return roots


def _external_call_link_from_row(
    call: dict,
    *,
    boundary: frozenset[str],
    project_external_roots: frozenset[str],
    seen: set[tuple[str, str, int]],
) -> ExternalCallLink | None:
    caller_uid = str(call.get("caller_uid") or "")
    qn = str(call.get("callee_qualified_name") or "")
    if not caller_uid or not qn or call.get("callee_uid"):
        return None
    root = external_root_from_qualified_name(qn)
    if classify_external_root(root, boundary, project_external_roots) != "external":
        return None
    line = int(call.get("call_site_line") or 0)
    key = (caller_uid, root, line)
    if key in seen:
        return None
    seen.add(key)
    member = qn[len(root) + 1 :] if qn.startswith(f"{root}.") and len(qn) > len(root) + 1 else ""
    return ExternalCallLink(
        caller_uid=caller_uid,
        external_root=root,
        callee_member=member,
        call_site_line=line,
        confidence=float(call.get("confidence") or 0.85),
        kind=str(call.get("call_kind") or "call"),
    )


def collect_external_call_links(
    calls: list[dict],
    *,
    boundary: frozenset[str],
    project_external_roots: frozenset[str] = frozenset(),
) -> list[ExternalCallLink]:
    """Turn unresolved static external ``callee_qualified_name`` facts into link rows."""
    out: list[ExternalCallLink] = []
    seen: set[tuple[str, str, int]] = set()
    for call in calls:
        link = _external_call_link_from_row(
            call,
            boundary=boundary,
            project_external_roots=project_external_roots,
            seen=seen,
        )
        if link is not None:
            out.append(link)
    return out


def collect_external_import_links(
    source_code: str,
    file_path: str,
    *,
    boundary: frozenset[str],
    project_external_roots: frozenset[str] = frozenset(),
) -> list[ExternalImportLink]:
    roots = _import_roots_from_source(
        source_code,
        file_path,
        boundary,
        project_external_roots,
    )
    return [ExternalImportLink(file_path=file_path, external_root=root) for root in roots]


# Single ``import M`` / ``import M as A`` clause. ``M.N`` is allowed (e.g.
# ``import urllib.parse``); the alias is what the source actually binds.
_BARE_IMPORT_ITEM = re.compile(r"^([\w\.]+)(?:\s+as\s+(\w+))?$")
# One item inside a ``from M import N, P as Q`` body.
_FROM_IMPORT_ITEM = re.compile(r"^(\w+)(?:\s+as\s+(\w+))?$")


def _strip_trailing_comment(body: str) -> str:
    """Drop everything after ``#``; ``__all__``-style inline strings stay intact."""
    in_single = in_double = False
    out: list[str] = []
    for ch in body:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            break
        out.append(ch)
    return "".join(out).strip()


def _glue_logical_import_lines(source_code: str) -> list[str]:
    logical_lines: list[str] = []
    buf: list[str] = []
    paren_depth = 0
    pending_continuation = False
    for raw in source_code.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            if not buf:
                continue
        buf.append(_strip_trailing_comment(stripped))
        paren_depth += stripped.count("(") - stripped.count(")")
        pending_continuation = stripped.endswith("\\")
        if pending_continuation:
            buf[-1] = buf[-1].rstrip("\\").rstrip()
        if paren_depth <= 0 and not pending_continuation:
            line = " ".join(part for part in buf if part).strip()
            if line:
                logical_lines.append(line.strip("()").strip())
            buf = []
            paren_depth = 0
    return logical_lines


def _parse_from_import_line(line: str, add_link) -> bool:
    from_parts = split_python_from_import(line)
    if not from_parts:
        return False
    module, body = from_parts
    if module.startswith("."):
        return True
    body = body.strip("()").strip()
    for chunk in body.split(","):
        item = chunk.strip()
        if not item or item.startswith("*"):
            continue
        item_match = _FROM_IMPORT_ITEM.match(item)
        if not item_match:
            continue
        name, alias = item_match.group(1), item_match.group(2)
        add_link(module, name, alias)
    return True


def _parse_bare_import_line(line: str, add_link) -> None:
    if not line.startswith("import "):
        return
    body = line[len("import ") :].strip().strip("()").strip()
    for chunk in body.split(","):
        item = chunk.strip()
        if not item:
            continue
        item_match = _BARE_IMPORT_ITEM.match(item)
        if not item_match:
            continue
        dotted, alias = item_match.group(1), item_match.group(2)
        if "." not in dotted:
            continue
        module, _, name = dotted.rpartition(".")
        add_link(module, name, alias)


def _append_external_symbol_import_link(
    links: list[ExternalSymbolImportLink],
    seen: set[tuple[str, str, str]],
    *,
    file_path: str,
    boundary: frozenset[str],
    project_external_roots: frozenset[str],
    module: str,
    name: str,
    alias: str | None,
) -> None:
    module = (module or "").strip()
    name = (name or "").strip()
    if not module or not name:
        return
    root = _module_root(module)
    if classify_external_root(root, boundary, project_external_roots) != "external":
        return
    qualified_name = f"{module}.{name}" if name != module else module
    key = (file_path, qualified_name, alias or name)
    if key in seen:
        return
    seen.add(key)
    links.append(
        ExternalSymbolImportLink(
            file_path=file_path,
            qualified_name=qualified_name,
            module=module,
            name=name,
            local_alias=alias or name,
        )
    )


def _named_imports_from_source(
    source_code: str,
    file_path: str,
    boundary: frozenset[str],
    project_external_roots: frozenset[str] = frozenset(),
) -> list[ExternalSymbolImportLink]:
    """Extract ``from M import N [as A]`` and ``import M.N [as A]`` items.

    Multi-line / parenthesised import bodies are normalized: the scanner joins
    continuation lines and strips the enclosing parentheses, then splits the
    body on commas. Each surviving ``N [as A]`` token becomes one link.
    """
    links: list[ExternalSymbolImportLink] = []
    seen: set[tuple[str, str, str]] = set()

    def add_link(module: str, name: str, alias: str | None) -> None:
        _append_external_symbol_import_link(
            links,
            seen,
            file_path=file_path,
            boundary=boundary,
            project_external_roots=project_external_roots,
            module=module,
            name=name,
            alias=alias,
        )

    for line in _glue_logical_import_lines(source_code):
        if _parse_from_import_line(line, add_link):
            continue
        _parse_bare_import_line(line, add_link)
    return links


def collect_external_symbol_import_links(
    source_code: str,
    file_path: str,
    *,
    boundary: frozenset[str],
    project_external_roots: frozenset[str] = frozenset(),
) -> list[ExternalSymbolImportLink]:
    return _named_imports_from_source(
        source_code,
        file_path,
        boundary,
        project_external_roots,
    )


def external_call_link_rows(
    links: list[ExternalCallLink],
    workspace_id: str,
) -> list[dict]:
    return [
        {
            "caller_uid": link.caller_uid,
            "external_root": link.external_root,
            "external_uid": external_pkg_uid(workspace_id, link.external_root),
            "callee_member": link.callee_member,
            "call_site_line": link.call_site_line,
            "confidence": link.confidence,
            "kind": link.kind,
        }
        for link in links
    ]


def external_import_link_rows(
    links: list[ExternalImportLink],
    workspace_id: str,
) -> list[dict]:
    return [
        {
            "file_path": link.file_path,
            "external_root": link.external_root,
            "external_uid": external_pkg_uid(workspace_id, link.external_root),
        }
        for link in links
    ]


def external_symbol_import_rows(
    links: list[ExternalSymbolImportLink],
    workspace_id: str,
) -> list[dict]:
    rows: list[dict] = []
    for link in links:
        rows.append(
            {
                "file_path": link.file_path,
                "qualified_name": link.qualified_name,
                "module": link.module,
                "name": link.name,
                "local_alias": link.local_alias,
                "external_root": _module_root(link.module),
                "external_pkg_uid": external_pkg_uid(workspace_id, _module_root(link.module)),
                "external_symbol_uid": external_symbol_uid(workspace_id, link.qualified_name),
            }
        )
    return rows


def apply_external_boundary_for_file(
    db,
    *,
    file_path: str,
    source_code: str,
    calls: list[dict],
    boundary: frozenset[str],
    workspace_id: str,
    project_external_roots: frozenset[str] = frozenset(),
) -> tuple[int, int]:
    """Refresh ``IMPORTS_EXTERNAL`` / ``CALLS_EXTERNAL`` for one indexed file."""
    delete_imports = getattr(db, "delete_external_imports_for_file", None)
    link_boundary = getattr(db, "link_external_boundary", None)
    if not callable(link_boundary):
        return 0, 0
    if callable(delete_imports):
        delete_imports(file_path, workspace_id=workspace_id)
    call_links = collect_external_call_links(
        calls,
        boundary=boundary,
        project_external_roots=project_external_roots,
    )
    import_links = collect_external_import_links(
        source_code,
        file_path,
        boundary=boundary,
        project_external_roots=project_external_roots,
    )
    symbol_import_links = collect_external_symbol_import_links(
        source_code,
        file_path,
        boundary=boundary,
        project_external_roots=project_external_roots,
    )
    return cast(
        tuple[int, int],
        link_boundary(
            external_call_link_rows(call_links, workspace_id),
            external_import_link_rows(import_links, workspace_id),
            workspace_id=workspace_id,
            symbol_import_links=external_symbol_import_rows(symbol_import_links, workspace_id),
        ),
    )
