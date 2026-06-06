"""Derive CALLS_EXTERNAL / IMPORTS_EXTERNAL link rows from parse facts (C1)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sidecar.indexer.external_boundary import (
    classify_external_root,
    external_pkg_uid,
    external_root_from_qualified_name,
    external_symbol_uid,
)


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
        module = ""
        if stripped.startswith("import "):
            match = re.search(r"\bfrom\s+['\"]([^'\"]+)['\"]", stripped)
            if match:
                module = match.group(1).strip()
            else:
                side_effect = re.match(r"import\s+['\"]([^'\"]+)['\"]", stripped)
                if side_effect:
                    module = side_effect.group(1).strip()
                else:
                    module = stripped[7:].split(",")[0].strip().split(" as ")[0].strip()
        elif stripped.startswith("from "):
            parts = stripped[5:].split(" import ", 1)
            if len(parts) != 2:
                continue
            module = parts[0].strip()
        add_module(module)

    for match in re.finditer(r"\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)", source_code):
        add_module(match.group(1))
    return roots


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
        caller_uid = str(call.get("caller_uid") or "")
        qn = str(call.get("callee_qualified_name") or "")
        if not caller_uid or not qn or call.get("callee_uid"):
            continue
        root = external_root_from_qualified_name(qn)
        if classify_external_root(root, boundary, project_external_roots) != "external":
            continue
        member = qn[len(root) + 1 :] if qn.startswith(f"{root}.") and len(qn) > len(root) + 1 else ""
        line = int(call.get("call_site_line") or 0)
        key = (caller_uid, root, line)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            ExternalCallLink(
                caller_uid=caller_uid,
                external_root=root,
                callee_member=member,
                call_site_line=line,
                confidence=float(call.get("confidence") or 0.85),
                kind=str(call.get("call_kind") or "call"),
            )
        )
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


# Match ``from M import …`` line headers; the body after ``import`` is split
# downstream. ``M`` is what we resolve against the external boundary.
_FROM_IMPORT_HEADER = re.compile(r"^from\s+([\w\.]+)\s+import\s+(.+)$")
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

    # Phase 1: glue together logical import lines so multi-line ``from X import (
    #     a, b, c,
    # )`` is recognised as one statement.
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

    # Phase 2: classify each logical line as ``from M import …`` or ``import X``.
    for line in logical_lines:
        from_match = _FROM_IMPORT_HEADER.match(line)
        if from_match:
            module = from_match.group(1).strip()
            if module.startswith("."):
                continue
            body = from_match.group(2).strip().strip("()").strip()
            for chunk in body.split(","):
                item = chunk.strip()
                if not item or item.startswith("*"):
                    continue
                item_match = _FROM_IMPORT_ITEM.match(item)
                if not item_match:
                    continue
                name, alias = item_match.group(1), item_match.group(2)
                add_link(module, name, alias)
            continue
        if line.startswith("import "):
            body = line[len("import "):].strip().strip("()").strip()
            for chunk in body.split(","):
                item = chunk.strip()
                if not item:
                    continue
                item_match = _BARE_IMPORT_ITEM.match(item)
                if not item_match:
                    continue
                dotted, alias = item_match.group(1), item_match.group(2)
                if "." not in dotted:
                    # Plain ``import pkg`` only nominates a package — the
                    # ``IMPORTS_EXTERNAL`` edge already covers it; nothing
                    # named to model.
                    continue
                module, _, name = dotted.rpartition(".")
                add_link(module, name, alias)
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
                "external_pkg_uid": external_pkg_uid(
                    workspace_id, _module_root(link.module)
                ),
                "external_symbol_uid": external_symbol_uid(
                    workspace_id, link.qualified_name
                ),
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
    return link_boundary(
        external_call_link_rows(call_links, workspace_id),
        external_import_link_rows(import_links, workspace_id),
        workspace_id=workspace_id,
    )
