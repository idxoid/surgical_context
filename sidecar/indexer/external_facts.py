"""Derive CALLS_EXTERNAL / IMPORTS_EXTERNAL link rows from parse facts (C1)."""

from __future__ import annotations

from dataclasses import dataclass

from sidecar.indexer.external_boundary import (
    classify_external_root,
    external_pkg_uid,
    external_root_from_qualified_name,
)


@dataclass(frozen=True)
class ExternalCallLink:
    caller_uid: str
    external_root: str
    callee_member: str
    call_site_line: int
    confidence: float


@dataclass(frozen=True)
class ExternalImportLink:
    file_path: str
    external_root: str


def _import_roots_from_source(source_code: str, file_path: str, boundary: frozenset[str]) -> list[str]:
    """Scan ``import`` / ``from … import`` lines for published external roots."""
    roots: list[str] = []
    seen: set[str] = set()
    for line in source_code.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        module = ""
        if stripped.startswith("import "):
            module = stripped[7:].split(",")[0].strip().split(" as ")[0].strip()
        elif stripped.startswith("from "):
            parts = stripped[5:].split(" import ", 1)
            if len(parts) != 2:
                continue
            module = parts[0].strip()
            if module == "." or module.startswith("."):
                continue
        if not module:
            continue
        root = external_root_from_qualified_name(module)
        if classify_external_root(root, boundary) != "external" or root in seen:
            continue
        seen.add(root)
        roots.append(root)
    return roots


def collect_external_call_links(
    calls: list[dict],
    *,
    boundary: frozenset[str],
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
        if classify_external_root(root, boundary) != "external":
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
            )
        )
    return out


def collect_external_import_links(
    source_code: str,
    file_path: str,
    *,
    boundary: frozenset[str],
) -> list[ExternalImportLink]:
    roots = _import_roots_from_source(source_code, file_path, boundary)
    return [ExternalImportLink(file_path=file_path, external_root=root) for root in roots]


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


def apply_external_boundary_for_file(
    db,
    *,
    file_path: str,
    source_code: str,
    calls: list[dict],
    boundary: frozenset[str],
    workspace_id: str,
) -> tuple[int, int]:
    """Refresh ``IMPORTS_EXTERNAL`` / ``CALLS_EXTERNAL`` for one indexed file."""
    delete_imports = getattr(db, "delete_external_imports_for_file", None)
    link_boundary = getattr(db, "link_external_boundary", None)
    if not callable(link_boundary):
        return 0, 0
    if callable(delete_imports):
        delete_imports(file_path, workspace_id=workspace_id)
    call_links = collect_external_call_links(calls, boundary=boundary)
    import_links = collect_external_import_links(source_code, file_path, boundary=boundary)
    return link_boundary(
        external_call_link_rows(call_links, workspace_id),
        external_import_link_rows(import_links, workspace_id),
        workspace_id=workspace_id,
    )
