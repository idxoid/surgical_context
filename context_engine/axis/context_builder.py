"""Context builder — RoleCandidate → expanded code bundle for an LLM.

The role-driven retrieval primitive returns ranked seed symbols. An
``/ask``-style consumer needs the *code* around those seeds — not just
the seed name and uid. This module is the bridge: walk every candidate's
structural neighbourhood via the shared ``graph_walk`` core (one batched
grouped walk per expansion step), dedupe + depth-rank the related
symbols per seed, and pull their ``code`` from Lance.

What "neighbourhood" means depends on the contract that satisfied the
role. ``deferred_binding_flow`` (the only mode any current contract
uses) walks ``DECORATED_BY | USES_TYPE | INJECTS | HANDLES | REFERENCES
| HAS_API | INHERITED_API`` first (the structural binding ring) then
``CALLS_*`` for runtime dispatch — exactly what a question about a
registry or dependency-binding pattern needs to surface.

The output is a ``ContextBundle`` per candidate, ready for prompt
assembly. We do not produce the final prompt: prompt shape is the
consumer's choice (chat format, tool-use schema, etc.).
"""

from __future__ import annotations

import heapq
from collections import namedtuple
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from context_engine.axis.graph_walk import steps_for_mode, walk_neighbours_grouped
from context_engine.axis.role_retrieval import RoleCandidate
from context_engine.axis.test_file_filter import is_test_path
from context_engine.observability.metrics import estimate_text_tokens

_RENDER_LADDER: tuple[str, ...] = (
    "impact_surface",
    "signature_only",
    "fold_compact",
    "fold",
    "hybrid_compact",
    "hybrid",
    "full",
)

_CLASS_DEF_PREFIX = "class "

# One expansion hit: a neighbour reached from a seed, tagged with the
# step that found it. Mirrors the fields the bundle builder reads off the
# legacy ``AxisGraphHit``.
_Hit = namedtuple("_Hit", "uid name file_path depth step")

_PayloadRow = dict[str, Any]


@dataclass(frozen=True)
class ContextSymbol:
    """One symbol in the assembled context: the seed (depth 0) or a
    related symbol reached through graph expansion."""

    uid: str
    name: str
    file_path: str
    role: str
    distance_from_seed: int
    expansion_step: str | None
    code: str | None
    qualified_name: str = ""
    kind: str = ""
    direction: str = "callee"
    edge_type: str = ""
    relevance_score: float = 0.0
    utility_score: float = 0.0
    start_line: int = 0
    end_line: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "uid": self.uid,
            "name": self.name,
            "file_path": self.file_path,
            "role": self.role,
            "distance_from_seed": self.distance_from_seed,
            "expansion_step": self.expansion_step,
            "code": self.code,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "direction": self.direction,
            "edge_type": self.edge_type,
            "relevance_score": self.relevance_score,
            "utility_score": self.utility_score,
        }
        if self.start_line > 0:
            payload["start_line"] = self.start_line
        if self.end_line >= self.start_line > 0:
            payload["end_line"] = self.end_line
        return payload


_CALLER_KINDS = frozenset(
    {
        "reverse_calls",
        "impacted_tests",
        "structural_inheritor",
        "forward_affects",
        "trace_callers",
    }
)


def _candidate_direction(candidate: RoleCandidate) -> str:
    kind = candidate.satisfying_kinds[0] if candidate.satisfying_kinds else ""
    return "caller" if kind in _CALLER_KINDS else "callee"


@dataclass(frozen=True)
class ContextRenderBudget:
    """Echelon-2 render and token-packing knobs for ``build_context_for_candidates``."""

    token_budget: int | None = None
    render_mode: str = "full"
    per_transaction_share: float = 0.10
    file_soft_cap_share: float = 0.25
    signature_only_initial: bool = False


@dataclass(frozen=True)
class ContextBundle:
    """Bundle for one seed candidate: the seed plus its expanded
    related symbols, ordered closest-first."""

    role: str
    seed: ContextSymbol
    related: tuple[ContextSymbol, ...] = field(default_factory=tuple)
    utility_score: float = 0.0
    passive: bool = False
    render_mode: str = "full"

    def all_symbols(self) -> list[ContextSymbol]:
        return [self.seed, *self.related]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "seed": self.seed.to_dict(),
            "related": [s.to_dict() for s in self.related],
            "utility_score": self.utility_score,
            "passive": self.passive,
            "render_mode": self.render_mode,
        }


def _quote_sql_value(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _symbol_payload_columns(table) -> list[str]:
    columns = ["uid", "code", "workspace_id"]
    try:
        schema_names = set(table.schema.names)
        for optional in ("qualified_name", "name", "file_path", "start_line", "end_line"):
            if optional in schema_names:
                columns.append(optional)
    except Exception:
        pass
    return columns


def _symbol_payload_filter_sql(
    workspace_id: str,
    uids: set[str],
    *,
    sym_table_fn,
) -> str:
    from context_engine.database.lance_workspace_tables import workspace_partitioned_enabled

    uid_filter = ", ".join(_quote_sql_value(uid) for uid in sorted(uids))
    if workspace_partitioned_enabled() and callable(sym_table_fn):
        return f"uid IN ({uid_filter})"
    return f"workspace_id = {_quote_sql_value(workspace_id)} AND uid IN ({uid_filter})"


def _resolve_symbols_table(lance, workspace_id: str):
    sym_table_fn = getattr(lance, "symbols_table", None)
    if callable(sym_table_fn):
        return sym_table_fn(workspace_id), sym_table_fn
    return lance._sym_table, sym_table_fn  # noqa: SLF001


def _filter_symbol_payload_arrow(arrow, workspace_id: str, uids: set[str]):
    try:
        import pyarrow as pa
        import pyarrow.compute as pc

        uid_set = pa.array(list(uids), type=arrow["uid"].type)
        mask = pc.and_(
            pc.equal(
                arrow["workspace_id"],
                pa.scalar(workspace_id, type=arrow["workspace_id"].type),
            ),
            pc.is_in(arrow["uid"], value_set=uid_set),
        )
        return arrow.filter(mask)
    except Exception:
        return arrow


def _load_symbol_payload_arrow(
    lance_table, columns: list[str], filter_sql: str, workspace_id: str, uids: set[str]
):
    try:
        return lance_table.to_table(columns=columns, filter=filter_sql)
    except TypeError:
        arrow = lance_table.to_table(columns=columns)
        return _filter_symbol_payload_arrow(arrow, workspace_id, uids)


def _payloads_from_pylist_fallback(
    arrow,
    workspace_id: str,
    uids: set[str],
) -> dict[str, _PayloadRow]:
    out: dict[str, _PayloadRow] = {}
    for row in arrow.to_pylist():
        uid = str(row.get("uid") or "")
        if row.get("workspace_id") == workspace_id and uid in uids:
            out[uid] = {
                "code": row.get("code"),
                "qualified_name": row.get("qualified_name") or "",
                "name": row.get("name") or "",
                "file_path": row.get("file_path") or "",
                "start_line": row.get("start_line") or 0,
                "end_line": row.get("end_line") or 0,
            }
    return out


def _arrow_column_pylist(arrow, column: str, length: int) -> list:
    try:
        return cast("list", arrow[column].to_pylist())
    except Exception:
        return [""] * length


def _payloads_from_arrow_table(
    arrow,
    workspace_id: str,
    uids: set[str],
) -> dict[str, _PayloadRow]:
    try:
        row_uids = arrow["uid"].to_pylist()
        codes = arrow["code"].to_pylist()
        workspace_ids = arrow["workspace_id"].to_pylist()
    except Exception:
        return _payloads_from_pylist_fallback(arrow, workspace_id, uids)

    row_count = len(row_uids)
    qualified_names = _arrow_column_pylist(arrow, "qualified_name", row_count)
    names = _arrow_column_pylist(arrow, "name", row_count)
    file_paths = _arrow_column_pylist(arrow, "file_path", row_count)
    start_lines = _arrow_column_pylist(arrow, "start_line", row_count)
    end_lines = _arrow_column_pylist(arrow, "end_line", row_count)

    out: dict[str, _PayloadRow] = {}
    for (
        uid_raw,
        code,
        row_workspace_id,
        qualified_name,
        name,
        file_path,
        start_line,
        end_line,
    ) in zip(
        row_uids,
        codes,
        workspace_ids,
        qualified_names,
        names,
        file_paths,
        start_lines,
        end_lines,
        strict=False,
    ):
        if row_workspace_id != workspace_id:
            continue
        uid = str(uid_raw or "")
        if uid in uids:
            out[uid] = {
                "code": code,
                "qualified_name": str(qualified_name or ""),
                "name": str(name or ""),
                "file_path": str(file_path or ""),
                "start_line": _int_payload_value(start_line),
                "end_line": _int_payload_value(end_line),
            }
    return out


def _fetch_symbol_payloads(
    lance,
    workspace_id: str,
    uids: set[str],
) -> dict[str, _PayloadRow]:
    """Pull ``code`` + lightweight render metadata for a set of uids.

    Lance does not give us a clean WHERE-by-list across heterogeneous
    columns; one full scan filtered in-process is acceptable for the
    workspaces we currently target (thousands of symbols, not millions).
    """
    if not uids:
        return {}
    table, sym_table_fn = _resolve_symbols_table(lance, workspace_id)
    columns = _symbol_payload_columns(table)
    filter_sql = _symbol_payload_filter_sql(workspace_id, uids, sym_table_fn=sym_table_fn)
    arrow = _load_symbol_payload_arrow(table.to_lance(), columns, filter_sql, workspace_id, uids)
    return _payloads_from_arrow_table(arrow, workspace_id, uids)


def _fetch_codes(
    lance,
    workspace_id: str,
    uids: set[str],
) -> dict[str, str | None]:
    """Backward-compatible code-only view used by older callers/tests."""
    return {
        uid: payload.get("code")
        for uid, payload in _fetch_symbol_payloads(lance, workspace_id, uids).items()
    }


def _read_symbol_code_from_file(
    file_path: str,
    start_line: int,
    end_line: int,
    *,
    workspace_root: Path | None,
) -> str:
    from context_engine.workspace_paths import resolve_graph_file_path

    resolved = resolve_graph_file_path(file_path, workspace_root=workspace_root)
    if not resolved:
        return ""
    try:
        lines = Path(resolved).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    if start_line < 1 or end_line < start_line:
        return ""
    return "\n".join(lines[start_line - 1 : end_line])


def _missing_symbol_uids(uids: set[str], payloads: dict[str, _PayloadRow]) -> list[str]:
    return [uid for uid in uids if not str((payloads.get(uid) or {}).get("code") or "").strip()]


def _int_payload_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _merge_payload_span(row: _PayloadRow | None, span: dict[str, Any]) -> _PayloadRow:
    out: _PayloadRow = dict(row or {})
    name = str(span.get("name") or "")
    file_path = str(span.get("file_path") or "")
    if name and not out.get("name"):
        out["name"] = name
    if file_path and not out.get("file_path"):
        out["file_path"] = file_path
    start_line = _int_payload_value(span.get("start_line"))
    end_line = _int_payload_value(span.get("end_line"))
    if start_line > 0:
        out["start_line"] = start_line
    if end_line >= start_line > 0:
        out["end_line"] = end_line
    return out


def _payload_span(uid: str, row: _PayloadRow | None) -> dict[str, Any] | None:
    if not row:
        return None
    start_line = _int_payload_value(row.get("start_line"))
    end_line = _int_payload_value(row.get("end_line"))
    if start_line <= 0 or end_line < start_line:
        return None
    return {
        "uid": uid,
        "name": str(row.get("name") or ""),
        "file_path": str(row.get("file_path") or ""),
        "start_line": start_line,
        "end_line": end_line,
    }


def _merge_symbol_spans(
    db,
    workspace_id: str,
    uids: set[str],
    payloads: dict[str, _PayloadRow],
) -> dict[str, _PayloadRow]:
    if not uids or db is None:
        return payloads

    get_spans = getattr(db, "get_symbol_spans_by_uids", None)
    if not callable(get_spans):
        return payloads

    spans = get_spans(list(uids), workspace_id=workspace_id)
    if not spans:
        return payloads

    out = dict(payloads)
    for uid, span in spans.items():
        if uid in uids:
            out[uid] = _merge_payload_span(out.get(uid), span)
    return out


def _hydrate_payload_from_span(
    span: dict,
    *,
    workspace_root,
    existing: _PayloadRow | None,
) -> _PayloadRow | None:
    code = _read_symbol_code_from_file(
        str(span.get("file_path") or ""),
        int(span.get("start_line") or 0),
        int(span.get("end_line") or 0),
        workspace_root=workspace_root,
    )
    if not code.strip():
        return None
    row = _merge_payload_span(existing, span)
    row.setdefault("name", str(span.get("name") or ""))
    row.setdefault("file_path", str(span.get("file_path") or ""))
    row["code"] = code
    return row


def _hydrate_missing_symbol_code(
    db,
    workspace_id: str,
    uids: set[str],
    payloads: dict[str, _PayloadRow],
) -> dict[str, _PayloadRow]:
    """Fill empty Lance payloads from on-disk source using Neo4j line spans."""
    if not uids or db is None:
        return payloads

    missing = _missing_symbol_uids(uids, payloads)
    if not missing:
        return payloads

    get_spans = getattr(db, "get_symbol_spans_by_uids", None)
    if not callable(get_spans):
        return payloads

    from context_engine.workspace_paths import registered_workspace_root

    workspace_root = registered_workspace_root(db, workspace_id)
    spans = {
        uid: span for uid in missing if (span := _payload_span(uid, payloads.get(uid))) is not None
    }
    still_missing_span = [uid for uid in missing if uid not in spans]
    if still_missing_span:
        spans.update(get_spans(still_missing_span, workspace_id=workspace_id) or {})
    if not spans:
        return payloads

    out = dict(payloads)
    for uid in missing:
        span = spans.get(uid)
        if not span:
            continue
        hydrated = _hydrate_payload_from_span(
            span,
            workspace_root=workspace_root,
            existing=out.get(uid),
        )
        if hydrated is not None:
            out[uid] = hydrated
    return out


def _is_callable_header(stripped: str) -> bool:
    return stripped.startswith(("def ", "async def ", _CLASS_DEF_PREFIX))


def _code_signature(code: str | None) -> str:
    """Best-effort, parser-free trim of a symbol's code to its signature:
    leading decorators plus the ``def``/``class`` header (through the line
    ending in ``:``, so multi-line parameter lists survive). Non-callable
    symbols (a module-level assignment, say) collapse to their first
    non-empty line. Empty in, empty out."""
    if not code:
        return ""
    lines = code.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n and lines[i].strip().startswith("@"):  # decorators
        out.append(lines[i])
        i += 1
    started = False
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not started:
            if not _is_callable_header(stripped):
                # Not a callable/class — first non-empty line is the "signature".
                if stripped:
                    out.append(line)
                    break
                i += 1
                continue
            started = True
        out.append(line)
        if stripped.endswith(":"):
            break
        i += 1
    return "\n".join(out)


def _code_impact_surface(
    code: str | None,
    *,
    qualified_name: str = "",
    name: str = "",
) -> str:
    """One-line blast-radius stub — cheapest render for impact breadth."""
    sig = _code_signature(code or "")
    first = next((ln.strip() for ln in sig.splitlines() if ln.strip()), "")
    if first:
        return first
    return (qualified_name or name or "").strip()


def _collapse_long_line(line: str, *, limit: int = 140) -> str:
    stripped = line.rstrip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 4].rstrip() + " ..."


def _looks_like_assignment(stripped: str) -> bool:
    if "==" in stripped or "!=" in stripped or "<=" in stripped or ">=" in stripped:
        return False
    return "=" in stripped


def _keep_compact_body_line(stripped: str) -> bool:
    if not stripped or stripped.startswith("#"):
        return False
    prefixes = (
        "if ",
        "elif ",
        "else:",
        "for ",
        "async for ",
        "while ",
        "try:",
        "except ",
        "finally:",
        "with ",
        "async with ",
        "match ",
        "case ",
        "return ",
        "yield ",
        "yield from ",
        "raise ",
        "import ",
        "from ",
        "assert ",
    )
    if stripped.startswith(prefixes):
        return True
    if _looks_like_assignment(stripped):
        return True
    return "(" in stripped and ")" in stripped


def _advance_docstring_state(stripped: str, in_docstring: bool) -> tuple[bool, bool]:
    """Return ``(new_in_docstring, should_skip_line)`` for compact body scans."""
    if not stripped.startswith(('"""', "'''")):
        return in_docstring, False
    if stripped.count('"""') == 1 or stripped.count("'''") == 1:
        in_docstring = not in_docstring
    return in_docstring, True


def _compact_body_lines(
    lines: list[str],
    signature_lines: list[str],
    *,
    max_body_lines: int,
) -> list[str]:
    out: list[str] = []
    kept = 0
    in_docstring = False
    for line in lines[len(signature_lines) :]:
        stripped = line.strip()
        if not stripped:
            continue
        in_docstring, skip = _advance_docstring_state(stripped, in_docstring)
        if skip or in_docstring:
            continue
        if not _keep_compact_body_line(stripped):
            continue
        out.append(_collapse_long_line(line))
        kept += 1
        if kept >= max_body_lines:
            out.append("    ...")
            break
    if kept == 0:
        out.append("    ...")
    return out


def _code_compact(code: str | None, *, max_body_lines: int = 24) -> str:
    """Parser-light body compaction: signature + structural/call-bearing lines."""
    signature = _code_signature(code)
    if not code or not signature:
        return signature
    lines = code.splitlines()
    signature_lines = signature.splitlines()
    if not signature_lines or len(signature_lines) >= len(lines):
        return signature

    out = list(signature_lines)
    out.extend(_compact_body_lines(lines, signature_lines, max_body_lines=max_body_lines))
    return "\n".join(out)


def _class_parent_from_qualified_name(sym: ContextSymbol) -> str | None:
    """Return the parent class qualified name for callable class members.

    This is deliberately conservative. Top-level module functions also have
    dotted qualified names, so we only treat a symbol as a class member when its
    code header is callable-shaped and the qualified name has at least
    ``module.Class.member`` structure. Ambiguous singletons are left unfolded.
    """
    qn = sym.qualified_name.strip()
    if not qn or "." not in qn:
        return None
    raw_signature = _code_signature(sym.code)
    signature = raw_signature.lstrip()
    if signature.startswith(_CLASS_DEF_PREFIX):
        return qn
    if not signature.startswith(("def ", "async def ")):
        return None
    header_line = next(
        (
            line
            for line in raw_signature.splitlines()
            if line.strip() and not line.strip().startswith("@")
        ),
        "",
    )
    if not header_line[:1].isspace():
        return None
    parts = qn.split(".")
    if len(parts) < 3:
        return None
    return ".".join(parts[:-1])


def _class_name_from_qualified_name(qualified_name: str) -> str:
    return qualified_name.rsplit(".", 1)[-1] or qualified_name


def _indent_block(code: str) -> str:
    lines = (code or "").splitlines()
    if not lines:
        return "    ..."
    non_empty = [line for line in lines if line.strip()]
    min_indent = min(
        (len(line) - len(line.lstrip(" ")) for line in non_empty),
        default=0,
    )
    out = []
    for line in lines:
        stripped_indent = line[min_indent:] if len(line) >= min_indent else line
        out.append("    " + stripped_indent if stripped_indent.strip() else "")
    return "\n".join(out)


def _is_class_definition_symbol(sym: ContextSymbol, parent: str) -> bool:
    sig = _code_signature(sym.code).lstrip()
    return sig.startswith(_CLASS_DEF_PREFIX) and sym.qualified_name == parent


def _index_class_member_groups(
    symbols: list[ContextSymbol],
) -> tuple[dict[str, list[ContextSymbol]], dict[str, str]]:
    by_parent: dict[str, list[ContextSymbol]] = {}
    parent_by_uid: dict[str, str] = {}
    for sym in symbols:
        parent = _class_parent_from_qualified_name(sym)
        if parent is None:
            continue
        by_parent.setdefault(parent, []).append(sym)
        parent_by_uid[sym.uid] = parent
    return by_parent, parent_by_uid


def _should_fold_class_group(
    parent: str,
    members: list[ContextSymbol],
    *,
    core_tier_only: bool,
) -> bool:
    has_class_symbol = any(_is_class_definition_symbol(member, parent) for member in members)
    if not (has_class_symbol or len(members) >= 2):
        return False
    if core_tier_only and not any(_file_tier_from_path(m.file_path) == "core" for m in members):
        return False
    return True


def _foldable_class_parents(
    by_parent: dict[str, list[ContextSymbol]],
    *,
    core_tier_only: bool,
) -> set[str]:
    return {
        parent
        for parent, members in by_parent.items()
        if _should_fold_class_group(parent, members, core_tier_only=core_tier_only)
    }


def _class_header_from_members(parent: str, members: list[ContextSymbol]) -> str:
    class_header = f"class {_class_name_from_qualified_name(parent)}:"
    for member in members:
        if _is_class_definition_symbol(member, parent):
            sig = _code_signature(member.code).lstrip()
            class_header = sig.splitlines()[0]
            break
    return class_header


def _folded_member_body(member: ContextSymbol, *, parent: str, compact: bool) -> str | None:
    if _is_class_definition_symbol(member, parent):
        return None
    sig = _code_signature(member.code).lstrip()
    if compact:
        return _indent_block(sig)
    keep_full = member.distance_from_seed == 0 or member.name == "__init__"
    return _indent_block((member.code or "") if keep_full else sig or "")


def _folded_class_code(parent: str, members: list[ContextSymbol], *, compact: bool) -> str:
    class_header = _class_header_from_members(parent, members)
    body_blocks = [
        block
        for member in members
        if (block := _folded_member_body(member, parent=parent, compact=compact)) is not None
    ]
    return "\n".join([class_header, *(body_blocks or ["    ..."])])


def _build_folded_class_symbol(
    parent: str,
    members: list[ContextSymbol],
    *,
    compact: bool,
) -> ContextSymbol:
    first = min(members, key=lambda m: (m.distance_from_seed, m.uid))
    return cast(
        ContextSymbol,
        replace(
            first,
            name=_class_name_from_qualified_name(parent),
            qualified_name=parent,
            distance_from_seed=min(m.distance_from_seed for m in members),
            expansion_step="fold",
            code=_folded_class_code(parent, members, compact=compact),
        ),
    )


def _fold_class_symbols(
    symbols: list[ContextSymbol],
    *,
    compact: bool = False,
    core_tier_only: bool = False,
) -> list[ContextSymbol]:
    """Fold already-selected class members into synthetic class blocks.

    No graph lookup occurs here: the group is built only from symbols that are
    already in the bundle. A group with one ambiguous method is left alone; a
    group with a class symbol or multiple members is safe to render as a class
    skeleton.
    """
    by_parent, parent_by_uid = _index_class_member_groups(symbols)
    foldable = _foldable_class_parents(by_parent, core_tier_only=core_tier_only)

    out: list[ContextSymbol] = []
    emitted: set[str] = set()
    for sym in symbols:
        parent = parent_by_uid.get(sym.uid)
        if parent is None or parent not in foldable:
            out.append(sym)
            continue
        if parent in emitted:
            continue
        emitted.add(parent)
        out.append(_build_folded_class_symbol(parent, by_parent[parent], compact=compact))
    return out


def _apply_fold_render(
    bundles: list[ContextBundle],
    *,
    compact: bool = False,
) -> list[ContextBundle]:
    folded: list[ContextBundle] = []
    for bundle in bundles:
        symbols = _fold_class_symbols(bundle.all_symbols(), compact=compact)
        if not symbols:
            folded.append(bundle)
            continue
        seed = symbols[0]
        related = tuple(symbols[1:])
        folded.append(replace(bundle, seed=seed, related=related))
    return folded


def _render_impact_tiered(
    bundle: ContextBundle,
    *,
    full_render_max_depth: int = 0,
) -> ContextBundle:
    """Impact echelon-2: core-tier fold groups as ``fold_compact``, rest tiered.

    * **Upper tier + fold objects** — production (``core``) class blocks fold
      into a compact skeleton (``fold_compact``).
    * **Anchor symbols** on production (``core``) files — full signature header.
    * **Everything else** — one-line signature stub (``impact_surface``).
    """
    del full_render_max_depth
    symbols = _fold_class_symbols(
        list(bundle.all_symbols()),
        compact=True,
        core_tier_only=True,
    )
    if not symbols:
        return cast(
            ContextBundle,
            replace(bundle, render_mode="impact_tiered"),
        )
    trimmed: list[ContextSymbol] = []
    for sym in symbols:
        if sym.expansion_step == "fold":
            trimmed.append(sym)
        elif sym.distance_from_seed == 0 and _file_tier_from_path(sym.file_path) == "core":
            trimmed.append(replace(sym, code=_code_signature(sym.code)))
        else:
            trimmed.append(
                replace(
                    sym,
                    code=_code_impact_surface(
                        sym.code,
                        qualified_name=sym.qualified_name,
                        name=sym.name,
                    ),
                )
            )
    seed = trimmed[0]
    related = tuple(trimmed[1:])
    return cast(
        ContextBundle,
        replace(bundle, seed=seed, related=related, render_mode="impact_tiered"),
    )


def _trim_symbol_for_mode(
    sym: ContextSymbol,
    render_mode: str,
    *,
    full_render_max_depth: int,
) -> ContextSymbol:
    if render_mode == "impact_surface":
        return cast(
            ContextSymbol,
            replace(
                sym,
                code=_code_impact_surface(
                    sym.code,
                    qualified_name=sym.qualified_name,
                    name=sym.name,
                ),
            ),
        )
    if render_mode == "signature_only":
        return cast(ContextSymbol, replace(sym, code=_code_signature(sym.code)))
    if render_mode == "hybrid_compact":
        if sym.distance_from_seed <= full_render_max_depth:
            return cast(ContextSymbol, replace(sym, code=_code_compact(sym.code)))
        return cast(ContextSymbol, replace(sym, code=_code_signature(sym.code)))
    if render_mode == "hybrid" and sym.distance_from_seed > full_render_max_depth:
        return cast(ContextSymbol, replace(sym, code=_code_signature(sym.code)))
    return sym


def _render_bundle(
    bundle: ContextBundle,
    render_mode: str,
    *,
    full_render_max_depth: int = 0,
) -> ContextBundle:
    if render_mode == "impact_tiered":
        return _render_impact_tiered(bundle, full_render_max_depth=full_render_max_depth)
    if render_mode in ("fold", "fold_compact"):
        rendered = _apply_fold_render(
            [bundle],
            compact=render_mode == "fold_compact",
        )[0]
        if rendered == bundle:
            return bundle
    elif render_mode in (
        "impact_surface",
        "signature_only",
        "hybrid",
        "hybrid_compact",
    ):
        rendered = cast(
            ContextBundle,
            replace(
                bundle,
                seed=_trim_symbol_for_mode(
                    bundle.seed,
                    render_mode,
                    full_render_max_depth=full_render_max_depth,
                ),
                related=tuple(
                    _trim_symbol_for_mode(
                        rel,
                        render_mode,
                        full_render_max_depth=full_render_max_depth,
                    )
                    for rel in bundle.related
                ),
            ),
        )
    else:
        rendered = bundle
    return cast(ContextBundle, replace(rendered, render_mode=render_mode))


def _bundle_token_count(bundle: ContextBundle) -> int:
    return sum(estimate_text_tokens(sym.code or "") for sym in bundle.all_symbols())


def _render_modes_for_credit(initial_mode: str) -> tuple[str, ...]:
    if initial_mode not in _RENDER_LADDER:
        return (initial_mode, "fold", "signature_only")
    idx = _RENDER_LADDER.index(initial_mode)
    return tuple(reversed(_RENDER_LADDER[: idx + 1]))


def _render_with_transaction_limit(
    bundle: ContextBundle,
    initial_mode: str,
    *,
    per_transaction_limit: int,
    full_render_max_depth: int,
    render_cache: dict[tuple[int, str], tuple[ContextBundle, int]] | None = None,
) -> tuple[ContextBundle, int]:
    # Within one budget pass per_transaction_limit/full_render_max_depth are
    # constant, so (bundle, initial_mode) fully determines the render. The
    # upgrade loop re-asks for the same modes across repeated pushes; memoise.
    cache_key = (id(bundle), initial_mode)
    if render_cache is not None and cache_key in render_cache:
        return render_cache[cache_key]
    last: tuple[ContextBundle, int] | None = None
    result: tuple[ContextBundle, int] | None = None
    for mode in _render_modes_for_credit(initial_mode):
        rendered = _render_bundle(
            bundle,
            mode,
            full_render_max_depth=full_render_max_depth,
        )
        cost = _bundle_token_count(rendered)
        last = (rendered, cost)
        if cost <= per_transaction_limit or mode in ("impact_surface", "signature_only"):
            result = (rendered, cost)
            break
    if result is None:
        assert last is not None
        result = last
    if render_cache is not None:
        render_cache[cache_key] = result
    return result


#: Utility added per extra class-member a bundle folds into a class block.
#: A fold-block stands for several symbols at a cheap folded cost, so a bundle
#: carrying a coherent N-method class ranks above a lone symbol of equal base
#: utility (the design's fold aggregation bonus). It is 0 until ``qualified_name``
#: is indexed (no qn -> no fold grouping -> no bonus), so it stays graceful
#: pre-reindex.
#: Kept deliberately even though it is file_recall-neutral in ablation: the
#: bonus optimizes fold PACKING DENSITY (a coherent class block per token), which
#: file_recall cannot measure — recall-neutral here does not mean useless.
FOLD_AGGREGATION_BONUS = 0.1

_MODE_ROLES = frozenset({"impact_analysis", "trace_dependency"})
_EXAMPLE_SEGMENTS = frozenset(
    {
        "benchmarks",
        "codemods",
        "demo",
        "demos",
        "docs_src",
        "example",
        "examples",
        "sample",
        "samples",
        "tutorial",
        "tutorials",
    }
)
_DOC_SEGMENTS = frozenset({"doc", "docs", "documentation"})
_CORE_TIER_WEIGHT = {
    "core": 1.0,
    "stub": 0.5,
    "doc": 0.35,
    "example": 0.25,
    "test": 0.15,
}
_MODE_TIER_WEIGHT = {
    "core": 1.0,
    "stub": 0.5,
    "doc": 0.6,
    "example": 0.6,
    "test": 1.0,
}
_STRUCTURAL_BRIDGE_STEP_BONUS = {
    "deferred_runtime_dispatch": 0.45,
    "hook_transparency": 1.50,
}


def _fold_aggregation_bonus(bundle: ContextBundle, *, per_member: float) -> float:
    """Queue-utility bonus for a bundle that folds class members into blocks.

    Counts in-context members beyond the first in each foldable (>=2-member)
    class group — the same grouping ``_fold_class_symbols`` renders — so a bundle
    holding a 3-method class block outranks a single-symbol bundle of equal base
    utility. Bundles with no foldable group get 0 (unchanged ordering).
    """
    by_parent: dict[str, int] = {}
    for sym in bundle.all_symbols():
        parent = _class_parent_from_qualified_name(sym)
        if parent is not None:
            by_parent[parent] = by_parent.get(parent, 0) + 1
    extra = sum(count - 1 for count in by_parent.values() if count >= 2)
    return per_member * extra


def _bundle_files(bundle: ContextBundle) -> set[str]:
    return {sym.file_path for sym in bundle.all_symbols() if sym.file_path}


def _bundle_steps(bundle: ContextBundle) -> set[str]:
    return {sym.expansion_step for sym in bundle.all_symbols() if sym.expansion_step}


def _structural_bridge_bonus(bundle: ContextBundle) -> float:
    primary = _primary_file(bundle)
    if not primary:
        return 0.0
    bonus = 0.0
    seen: set[tuple[str, str]] = set()
    impact_mode = _bundle_impact_mode(bundle)
    for sym in bundle.related:
        step = sym.expansion_step
        if not step or not sym.file_path:
            continue
        if sym.file_path == primary:
            continue
        key = (step, sym.file_path)
        if key in seen:
            continue
        seen.add(key)
        step_bonus = _STRUCTURAL_BRIDGE_STEP_BONUS.get(step, 0.08)
        bonus += step_bonus * _tier_weight_for_path(sym.file_path, impact_mode=impact_mode)
    return bonus


def _primary_file(bundle: ContextBundle) -> str:
    return bundle.seed.file_path or ""


def _base_credit_utility(bundle: ContextBundle) -> float:
    return max(0.0, bundle.utility_score) + _fold_aggregation_bonus(
        bundle, per_member=FOLD_AGGREGATION_BONUS
    )


@lru_cache(maxsize=4096)
def _file_tier_from_path(path: str) -> str:
    if not path:
        return "core"
    norm = path.replace("\\", "/").lower()
    if is_test_path(norm):
        return "test"
    parts = [p for p in norm.split("/") if p]
    if any(part in _EXAMPLE_SEGMENTS for part in parts):
        return "example"
    if any(part in _DOC_SEGMENTS for part in parts):
        return "doc"
    if norm.endswith(".pyi"):
        return "stub"
    return "core"


def _tier_weight_for_path(path: str, *, impact_mode: bool) -> float:
    table = _MODE_TIER_WEIGHT if impact_mode else _CORE_TIER_WEIGHT
    return table.get(_file_tier_from_path(path), 1.0)


def _bundle_impact_mode(bundle: ContextBundle) -> bool:
    return bundle.role in _MODE_ROLES


def _bundle_tier_weight(bundle: ContextBundle) -> float:
    files = _bundle_files(bundle)
    if not files:
        return 1.0
    impact_mode = _bundle_impact_mode(bundle)
    weights = [_tier_weight_for_path(path, impact_mode=impact_mode) for path in files]
    primary = _primary_file(bundle)
    if primary:
        weights.append(_tier_weight_for_path(primary, impact_mode=impact_mode))
    return max(weights)


def _initial_credit_render(
    bundle: ContextBundle,
    *,
    transaction_limit: int,
    full_render_max_depth: int,
    initial_mode: str = "signature_only",
    signature_only_initial: bool = False,
) -> tuple[ContextBundle, int]:
    """Cheap coverage render: signatures everywhere, compact fold when cheap.

    A compact fold block is still a cheap coverage artifact, but it carries
    class topology that a plain signature loses. Passive seeds stay
    signature-first; they are the breadth reservoir and can be upgraded later.
    Impact profile uses ``impact_tiered``: core-tier fold groups render as
    ``fold_compact`` blocks; every other symbol keeps a full signature header.
    """
    if initial_mode == "impact_tiered":
        rendered = _render_impact_tiered(
            bundle,
            full_render_max_depth=full_render_max_depth,
        )
        return rendered, _bundle_token_count(rendered)
    if signature_only_initial or bundle.passive:
        rendered = _render_bundle(
            bundle,
            initial_mode,
            full_render_max_depth=full_render_max_depth,
        )
        return rendered, _bundle_token_count(rendered)
    signature = _render_bundle(
        bundle,
        "signature_only",
        full_render_max_depth=full_render_max_depth,
    )
    signature_cost = _bundle_token_count(signature)
    if bundle.passive:
        return signature, signature_cost
    folded = _render_bundle(
        bundle,
        "fold_compact",
        full_render_max_depth=full_render_max_depth,
    )
    if folded.render_mode != "fold_compact":
        return signature, signature_cost
    folded_cost = _bundle_token_count(folded)
    if folded_cost <= transaction_limit:
        return folded, folded_cost
    return signature, signature_cost


def _target_credit_modes(bundle: ContextBundle, render_mode: str) -> tuple[str, ...]:
    del bundle
    if render_mode == "impact_tiered":
        return ("impact_tiered",)
    if render_mode not in _RENDER_LADDER:
        return _RENDER_LADDER
    idx = _RENDER_LADDER.index(render_mode)
    return _RENDER_LADDER[: idx + 1]


def _next_upgrade_render(
    bundle: ContextBundle,
    *,
    current_mode: str,
    current_cost: int,
    render_mode: str,
    transaction_limit: int,
    full_render_max_depth: int,
    render_cache: dict | None = None,
) -> tuple[ContextBundle, int] | None:
    modes = _target_credit_modes(bundle, render_mode)
    try:
        start = modes.index(current_mode) + 1
    except ValueError:
        start = 0
    for mode in modes[start:]:
        rendered, cost = _render_with_transaction_limit(
            bundle,
            mode,
            per_transaction_limit=transaction_limit,
            full_render_max_depth=full_render_max_depth,
            render_cache=render_cache,
        )
        if mode.startswith("fold") and rendered.render_mode != mode:
            continue
        if rendered.render_mode == current_mode or cost < current_cost:
            continue
        return rendered, cost
    return None


#: Selection-independent per-bundle inputs to the coverage/upgrade gains.
#: Computed once per budget pass so the lazy-greedy heap re-evaluations reuse the
#: expensive structural rollups instead of recomputing them on every pop.
_BundleStatic = namedtuple(
    "_BundleStatic",
    "files steps tier_weight impact_mode base_utility structural_bridge",
)


def _dedupe_bundles_by_seed_uid(bundles: list[ContextBundle]) -> list[ContextBundle]:
    """One packer row per seed symbol — axis flattening repeats the same uid.

    Impact pools often carry the same symbol on seeds, structural walks, and
    impact_analysis (185 rows, 172 uids). Marginal packing runs on unique
    symbols; the highest-utility row wins when roles disagree.
    """
    best: dict[str, ContextBundle] = {}
    order: list[str] = []
    for bundle in bundles:
        if bundle.seed is None or not bundle.seed.uid:
            continue
        uid = bundle.seed.uid
        if uid not in best:
            order.append(uid)
            best[uid] = bundle
        elif bundle.utility_score > best[uid].utility_score:
            best[uid] = bundle
    return [best[uid] for uid in order]


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _mad(values: list[float]) -> float:
    if not values:
        return 0.0
    center = _median(values)
    return _median([abs(v - center) for v in values])


def _noise_level_from_tail(
    tail: list[float],
    *,
    k: float = 1.0,
) -> float:
    """``median(tail) + k * 1.4826 * MAD(tail)`` with ``noise >= 1``."""
    if not tail:
        return 1.0
    med = _median(tail)
    mad = _mad(tail)
    return max(1.0, med + k * 1.4826 * mad)


def _leader_pool_metrics(
    static: list[_BundleStatic],
    *,
    k: float = 1.0,
) -> tuple[float, int]:
    """Tail noise threshold and leader breadth for the ladder cap.

    Floor and signal live on the SAME axis: ``marginality = 100 * (u/peak)``
    (closeness to peak). The noise floor is the robust location + spread of
    the weak half's marginality::

        noise_level = median(marginality_tail) + k * 1.4826 * MAD(tail)

    Leaders are symbols with ``marginality > noise_level`` — above what the
    noise typically reaches. A weaker tail LOWERS the floor (signal stands out
    against weak noise); a tail hugging the peak raises it (nothing clearly
    leads). The pre-2026-07 form measured the floor on the complementary
    distance-from-peak axis, which inverted that response.  Then::

        %leader = 100 / COUNT(marginality > noise_level)

    Weak symbols below the floor are excluded from the divisor; they still enter
    via marginal packer on leftover budget.
    """
    n = len(static)
    if n <= 1:
        return 1.0, 1
    utilities = [max(1e-9, st.base_utility) for st in static]
    peak = max(utilities)
    sorted_u = sorted(utilities)
    median_u = _median(sorted_u)
    tail_u = [u for u in sorted_u if u <= median_u]
    if not tail_u:
        tail_u = sorted_u
    tail = [100.0 * (u / peak) for u in tail_u]
    noise_level = _noise_level_from_tail(tail, k=k)
    leader_count = sum(1 for u in utilities if (100.0 * u / peak) > noise_level)
    return noise_level, max(1, leader_count)


def _leader_transaction_limit(
    token_budget: int,
    *,
    leader_count: int,
) -> int:
    """Per-symbol ladder-step cap: ``budget * (100 / leader_count) / 100``."""
    if token_budget <= 0:
        return 1
    count = max(1, leader_count)
    if count <= 1:
        return max(1, int(token_budget))
    leader_share = (100.0 / count) / 100.0
    return max(1, int(token_budget * leader_share))


_UPGRADE_MODE_BONUS: dict[str, float] = {
    "impact_tiered": 0.12,
    "impact_surface": 0.08,
    "fold_compact": 0.18,
    "fold": 0.22,
    "hybrid_compact": 0.25,
    "hybrid": 0.28,
    "full": 0.38,
}


@dataclass
class _TokenCreditCoverageState:
    file_soft_cap: int
    covered_files: set[str] = field(default_factory=set)
    covered_roles: set[str] = field(default_factory=set)
    covered_steps: set[str] = field(default_factory=set)
    file_counts: dict[str, int] = field(default_factory=dict)
    file_tokens: dict[str, int] = field(default_factory=dict)
    # First-wins uid set. The prompt path (``axis_bundles_to_prompt_context``)
    # dedupes symbols by uid keeping the FIRST occurrence in bundle order, so
    # a uid repeated across bundles prints exactly once. Purchases price only
    # uids not yet present (exact during phase 1 — entries are append-only and
    # renders don't change there). Upgrades change renders and may DROP uids
    # from a bundle (fold regrouping), which shifts which occurrence prints —
    # so the upgrade phase never trusts membership bookkeeping and re-derives
    # printed size from the selected entries directly
    # (``_first_wins_printed_tokens``).
    printed_uids: set[str] = field(default_factory=set)

    def marginal_purchase_cost(self, rendered: ContextBundle) -> int:
        seen: set[str] = set()
        total = 0
        for sym in rendered.all_symbols():
            uid = sym.uid
            if uid in seen or uid in self.printed_uids:
                continue
            seen.add(uid)
            total += estimate_text_tokens(sym.code or "")
        return total

    def charge_purchase(self, rendered: ContextBundle) -> None:
        for sym in rendered.all_symbols():
            self.printed_uids.add(sym.uid)

    def file_saturation_penalty(self, bundle: ContextBundle) -> float:
        primary = _primary_file(bundle)
        if not primary:
            return 0.0
        return min(0.5, self.file_tokens.get(primary, 0) / self.file_soft_cap)

    def record_selection(self, bundle: ContextBundle, cost: int) -> None:
        files = _bundle_files(bundle)
        self.covered_files.update(files)
        self.covered_roles.add(bundle.role)
        self.covered_steps.update(_bundle_steps(bundle))
        for path in files:
            self.file_counts[path] = self.file_counts.get(path, 0) + 1
        primary = _primary_file(bundle)
        if primary:
            self.file_tokens[primary] = self.file_tokens.get(primary, 0) + cost


def _bundle_static_rows(bundles: list[ContextBundle]) -> list[_BundleStatic]:
    return [
        _BundleStatic(
            files=_bundle_files(b),
            steps=_bundle_steps(b),
            tier_weight=_bundle_tier_weight(b),
            impact_mode=_bundle_impact_mode(b),
            base_utility=_base_credit_utility(b),
            structural_bridge=_structural_bridge_bonus(b),
        )
        for b in bundles
    ]


def _credit_coverage_gain(
    idx: int,
    bundle: ContextBundle,
    *,
    static: list[_BundleStatic],
    state: _TokenCreditCoverageState,
) -> float:
    st = static[idx]
    files = st.files
    new_files = files - state.covered_files
    new_steps = st.steps - state.covered_steps
    gain: float = st.base_utility * st.tier_weight
    if new_files:
        new_file_weight = max(
            _tier_weight_for_path(path, impact_mode=st.impact_mode) for path in new_files
        )
        gain += (0.35 + 0.08 * max(0, len(new_files) - 1)) * new_file_weight
    if bundle.role not in state.covered_roles:
        gain += 0.20
    gain += 0.10 * len(new_steps)
    gain += st.structural_bridge * st.tier_weight
    gain += 0.05 if not bundle.passive else -0.03
    gain -= 0.15 * sum(state.file_counts.get(path, 0) for path in files)
    gain -= state.file_saturation_penalty(bundle)
    return max(0.001, gain)


def _credit_optimistic_value(st: _BundleStatic, bundle: ContextBundle, cost: int) -> float:
    optimistic_gain = st.base_utility + 0.35 + 0.20
    optimistic_gain *= st.tier_weight
    optimistic_gain += 0.10 * len(st.steps)
    optimistic_gain += st.structural_bridge * st.tier_weight
    if not bundle.passive:
        optimistic_gain += 0.05
    return float(optimistic_gain / max(1, cost))


def _build_initial_credit_heap(
    bundles: list[ContextBundle],
    static: list[_BundleStatic],
    *,
    transaction_limit: int,
    full_render_max_depth: int,
    initial_mode: str,
    signature_only_initial: bool,
) -> list[tuple[float, int, int, ContextBundle]]:
    initial: list[tuple[float, int, int, ContextBundle]] = []
    for idx, bundle in enumerate(bundles):
        rendered, cost = _initial_credit_render(
            bundle,
            transaction_limit=transaction_limit,
            full_render_max_depth=full_render_max_depth,
            initial_mode=initial_mode,
            signature_only_initial=signature_only_initial,
        )
        value = _credit_optimistic_value(static[idx], bundle, cost)
        heapq.heappush(initial, (-value, idx, cost, rendered))
    return initial


def _select_bundles_under_credit_budget(
    initial: list[tuple[float, int, int, ContextBundle]],
    bundles: list[ContextBundle],
    static: list[_BundleStatic],
    state: _TokenCreditCoverageState,
    token_budget: int,
) -> tuple[list[dict[str, object]], int]:
    selected: list[dict[str, object]] = []
    selected_indices: set[int] = set()
    skipped_indices: set[int] = set()
    used = 0

    while initial:
        _neg_value, idx, cost, rendered = heapq.heappop(initial)
        if idx in selected_indices or idx in skipped_indices:
            continue
        source = bundles[idx]
        # ``cost`` is the gross render size (kept on the entry for ladder
        # monotonicity); the budget pays only the marginal price — tokens of
        # uids the prompt's first-wins dedupe will actually print.
        marginal_cost = state.marginal_purchase_cost(rendered)
        current_value = _credit_coverage_gain(idx, source, static=static, state=state) / max(
            1, marginal_cost
        )
        best_competing = -initial[0][0] if initial else -1.0
        if current_value + 1e-12 < best_competing:
            heapq.heappush(initial, (-current_value, idx, cost, rendered))
            continue
        if selected and used + marginal_cost > token_budget:
            skipped_indices.add(idx)
            continue
        selected.append(
            {
                "index": idx,
                "source": source,
                "rendered": rendered,
                "cost": cost,
            }
        )
        selected_indices.add(idx)
        used += marginal_cost
        state.record_selection(source, marginal_cost)
        state.charge_purchase(rendered)
        if used >= token_budget:
            break
    return selected, used


def _first_wins_printed_tokens(selected: list[dict[str, object]]) -> int:
    """Exact deduped-prompt size of the selected renders (first-wins by uid).

    This is the ground truth ``used`` must track: renders change during the
    upgrade phase and fold regrouping can drop uids from a bundle, silently
    promoting a later bundle's occurrence into the prompt — so printed size is
    re-derived from the entries, never inferred from ownership bookkeeping.
    Cost is a ``len()`` sweep over selected symbols — negligible.
    """
    seen: set[str] = set()
    total = 0
    for entry in selected:
        rendered = entry.get("rendered")
        if not isinstance(rendered, ContextBundle):
            continue
        for sym in rendered.all_symbols():
            if sym.uid in seen:
                continue
            seen.add(sym.uid)
            total += estimate_text_tokens(sym.code or "")
    return total


def _upgrade_exact_delta(
    selected: list[dict[str, object]],
    entry_index: int,
    upgraded: ContextBundle,
    *,
    printed_before: int | None = None,
) -> int:
    """Printed-size delta of swapping ``entry_index``'s render for ``upgraded``.

    ``printed_before`` lets callers that maintain the ``used == printed``
    invariant skip the pre-swap sweep (the upgrade loop calls this per
    enqueue/pop, so the saved sweep is the dominant accounting cost).
    """
    entry = selected[entry_index]
    previous = entry.get("rendered")
    if printed_before is None:
        printed_before = _first_wins_printed_tokens(selected)
    entry["rendered"] = upgraded
    after = _first_wins_printed_tokens(selected)
    entry["rendered"] = previous
    return after - printed_before


def _credit_upgrade_entry_parts(
    entry: dict[str, object],
) -> tuple[ContextBundle, ContextBundle, int, int] | None:
    entry_source = entry.get("source")
    current = entry.get("rendered")
    current_cost = entry.get("cost")
    bundle_index = entry.get("index")
    if not isinstance(current, ContextBundle) or not isinstance(current_cost, int):
        return None
    if not isinstance(entry_source, ContextBundle):
        return None
    if not isinstance(bundle_index, int):
        return None
    return entry_source, current, current_cost, bundle_index


def _credit_upgrade_gain(
    st: _BundleStatic,
    bundle: ContextBundle,
    rendered: ContextBundle,
    state: _TokenCreditCoverageState,
) -> float:
    mode_bonus = _UPGRADE_MODE_BONUS.get(rendered.render_mode, 0.10)
    gain: float = 0.35 * st.base_utility * st.tier_weight
    gain += mode_bonus
    gain += 0.05 if not bundle.passive else 0.03
    gain -= state.file_saturation_penalty(bundle)
    return max(0.001, gain)


def _enqueue_credit_upgrade(
    entry_index: int,
    selected: list[dict[str, object]],
    static: list[_BundleStatic],
    state: _TokenCreditCoverageState,
    upgrade_heap: list[tuple[float, int, int, ContextBundle, int]],
    *,
    render_mode: str,
    transaction_limit: int,
    full_render_max_depth: int,
    render_cache: dict[tuple[int, str], tuple[ContextBundle, int]],
    printed_tokens: int,
) -> None:
    parts = _credit_upgrade_entry_parts(selected[entry_index])
    if parts is None:
        return
    entry_source, current, current_cost, bundle_index = parts
    candidate = _next_upgrade_render(
        entry_source,
        current_mode=current.render_mode,
        current_cost=current_cost,
        render_mode=render_mode,
        transaction_limit=transaction_limit,
        full_render_max_depth=full_render_max_depth,
        render_cache=render_cache,
    )
    if candidate is None:
        return
    upgraded, upgraded_cost = candidate
    if upgraded_cost < current_cost:
        return
    # Priority divides by the exact printed delta: re-rendering uids whose
    # first-wins occurrence lives in another entry costs nothing in the
    # prompt, so such upgrades rank high.
    exact_delta = _upgrade_exact_delta(
        selected, entry_index, upgraded, printed_before=printed_tokens
    )
    priority = _credit_upgrade_gain(static[bundle_index], entry_source, upgraded, state) / max(
        1, exact_delta
    )
    heapq.heappush(
        upgrade_heap,
        (-priority, entry_index, current_cost, upgraded, upgraded_cost),
    )


def _apply_credit_upgrades(
    selected: list[dict[str, object]],
    static: list[_BundleStatic],
    state: _TokenCreditCoverageState,
    *,
    render_mode: str,
    transaction_limit: int,
    full_render_max_depth: int,
    render_cache: dict[tuple[int, str], tuple[ContextBundle, int]],
    token_budget: int,
    used: int,
    entry_filter: set[int] | None = None,
) -> int:
    upgrade_heap: list[tuple[float, int, int, ContextBundle, int]] = []
    for entry_index in range(len(selected)):
        if entry_filter is not None and entry_index not in entry_filter:
            continue
        _enqueue_credit_upgrade(
            entry_index,
            selected,
            static,
            state,
            upgrade_heap,
            render_mode=render_mode,
            transaction_limit=transaction_limit,
            full_render_max_depth=full_render_max_depth,
            render_cache=render_cache,
            printed_tokens=used,
        )

    while upgrade_heap and used < token_budget:
        _neg_priority, entry_index, expected_cost, upgraded, upgraded_cost = heapq.heappop(
            upgrade_heap
        )
        entry = selected[entry_index]
        current_cost = entry["cost"]
        if current_cost != expected_cost or not isinstance(current_cost, int):
            continue
        if upgraded_cost < current_cost:
            continue
        # Recompute the exact delta at pop time — earlier upgrades changed
        # other entries' renders (and hence first-wins winners) since this
        # entry was enqueued.
        exact_delta = _upgrade_exact_delta(selected, entry_index, upgraded, printed_before=used)
        if used + exact_delta > token_budget:
            continue
        entry["rendered"] = upgraded
        entry["cost"] = upgraded_cost
        used += exact_delta
        entry_source = entry["source"]
        if isinstance(entry_source, ContextBundle):
            primary = _primary_file(entry_source)
            if primary:
                state.file_tokens[primary] = state.file_tokens.get(primary, 0) + exact_delta
        _enqueue_credit_upgrade(
            entry_index,
            selected,
            static,
            state,
            upgrade_heap,
            render_mode=render_mode,
            transaction_limit=transaction_limit,
            full_render_max_depth=full_render_max_depth,
            render_cache=render_cache,
            printed_tokens=used,
        )
    return used


def _selected_credit_rendered_bundles(selected: list[dict[str, object]]) -> list[ContextBundle]:
    return [
        rendered_bundle
        for entry in selected
        if isinstance((rendered_bundle := entry.get("rendered")), ContextBundle)
    ]


def _apply_token_credit_budget(
    bundles: list[ContextBundle],
    *,
    token_budget: int,
    render_mode: str,
    full_render_max_depth: int,
    per_transaction_share: float = 0.10,
    file_soft_cap_share: float = 0.25,
    signature_only_initial: bool = False,
) -> list[ContextBundle]:
    """Token Credit System v2 prototype: coverage-first marginal transactions.

    Phase 1 dedupes to unique seed symbols, then buys cheap coverage ordered
    by marginal utility per token. Both phases price transactions by the
    MARGINAL printed cost (the uid ledger on ``_TokenCreditCoverageState``):
    the prompt path dedupes symbols first-wins by uid, so a uid already bought
    by an earlier entry re-renders for free and ``used`` matches the deduped
    prompt size instead of over-billing shared neighbours. Each ladder step is
    capped by ``_leader_transaction_limit`` from a robust tail noise estimate::

        noise_level = median(marginality_tail) + k * 1.4826 * MAD(tail)
        %leader = 100 / COUNT(marginality > noise_level)

    ``marginality = 100 * (u/u_peak)`` on each deduped symbol.  Phase 2 uses
    the same cap, then — if budget remains once every capped upgrade has been
    resolved — reruns the sweep with the leftover budget as the per-step
    limit (cap relaxation: the cap manages contention, and contention is over).
    """
    if token_budget <= 0:
        return bundles

    del per_transaction_share  # profile knob; leader % comes from tail noise

    bundles = _dedupe_bundles_by_seed_uid(bundles)
    state = _TokenCreditCoverageState(
        file_soft_cap=max(1, int(token_budget * file_soft_cap_share)),
    )
    initial_mode = render_mode if render_mode in _RENDER_LADDER else "signature_only"
    render_cache: dict[tuple[int, str], tuple[ContextBundle, int]] = {}
    static = _bundle_static_rows(bundles)
    noise_level, leader_count = _leader_pool_metrics(static)
    transaction_limit = _leader_transaction_limit(
        token_budget,
        leader_count=leader_count,
    )
    peak_utility = max((st.base_utility for st in static), default=0.0)
    leader_indices = {
        idx
        for idx, st in enumerate(static)
        if peak_utility > 0 and (100.0 * st.base_utility / peak_utility) > noise_level
    }

    initial = _build_initial_credit_heap(
        bundles,
        static,
        transaction_limit=transaction_limit,
        full_render_max_depth=full_render_max_depth,
        initial_mode=initial_mode,
        signature_only_initial=signature_only_initial,
    )
    selected, used = _select_bundles_under_credit_budget(
        initial,
        bundles,
        static,
        state,
        token_budget,
    )
    used = _apply_credit_upgrades(
        selected,
        static,
        state,
        render_mode=render_mode,
        transaction_limit=transaction_limit,
        full_render_max_depth=full_render_max_depth,
        render_cache=render_cache,
        token_budget=token_budget,
        used=used,
    )
    if used < token_budget:
        # Cap relaxation on leftover budget: every capped upgrade has landed
        # or been rejected, so contention — the reason the leader cap exists —
        # is over. Re-run the upgrade sweep with the whole remaining budget as
        # the per-step limit so large high-gain bodies the cap kept at
        # signature level can finally buy their render. Leaders only: rendering
        # rich variants is the expensive part of this pass, and symbols below
        # the noise floor are noise by the pool's own measure. Fresh render
        # cache: the memo key is (bundle, mode) and assumes a constant limit.
        relaxed_entries = {
            entry_index
            for entry_index, entry in enumerate(selected)
            if isinstance((bundle_index := entry.get("index")), int)
            and bundle_index in leader_indices
        }
        if relaxed_entries:
            used = _apply_credit_upgrades(
                selected,
                static,
                state,
                render_mode=render_mode,
                transaction_limit=token_budget,
                full_render_max_depth=full_render_max_depth,
                render_cache={},
                token_budget=token_budget,
                used=used,
                entry_filter=relaxed_entries,
            )
    return _selected_credit_rendered_bundles(selected)


def _apply_render_and_budget(
    bundles: list[ContextBundle],
    *,
    token_budget: int | None,
    render_mode: str,
    full_render_max_depth: int = 0,
    per_transaction_share: float = 0.10,
    file_soft_cap_share: float = 0.25,
    signature_only_initial: bool = False,
) -> list[ContextBundle]:
    """Echelon 2: render-trim then token-pack the assembled bundles.

    With ``token_budget`` set the Token Credit System packs the full pool in
    marginal-utility order, buying the minimal render context per bundle along
    its own render ladder — so ``render_mode`` is the ceiling, not a pre-trim,
    and the packer does its own rendering.

    With no budget we just apply ``render_mode`` to every bundle:
      * ``"full"`` — never trims (whole pool, full code).
      * ``"impact_tiered"`` — core fold groups compact; anchors full signature,
        tail one-line stubs.
      * ``"impact_surface"`` — one-line stub per symbol (impact breadth).
      * ``"signature_only"`` — every symbol collapses to its signature.
      * ``"hybrid"`` — only neighbours past ``full_render_max_depth`` collapse
        (default 0 → the seed stays full, every expanded neighbour collapses to
        a signature). Keeping even depth-1 deps full barely economizes (they
        dominate the volume), so the default is seed-only.
      * ``"fold"`` — group each bundle's class members into a folded block.
    """

    if token_budget is not None:
        return _apply_token_credit_budget(
            bundles,
            token_budget=token_budget,
            render_mode=render_mode,
            full_render_max_depth=full_render_max_depth,
            per_transaction_share=per_transaction_share,
            file_soft_cap_share=file_soft_cap_share,
            signature_only_initial=signature_only_initial,
        )

    if render_mode in (
        "impact_tiered",
        "impact_surface",
        "fold",
        "fold_compact",
        "signature_only",
        "hybrid",
        "hybrid_compact",
    ):
        bundles = [
            _render_bundle(b, render_mode, full_render_max_depth=full_render_max_depth)
            for b in bundles
        ]
    return bundles


#: Hook/event archetype edges. Hop 1 crosses the EVENT channel from a topic to
#: its sub/pub sites; hop 2 crosses the HOOK wrapper from those sites to the
#: registration / dispatch API they go through (``listens_for``/``dispatch``/…).
#: ``DECORATED_BY``/``HANDLES`` stay in hop 2 as the legacy decorator path that
#: reached the same API before the dedicated HOOK edge existed — kept as a
#: recall-safe fallback until a benchmark confirms HOOK_* parity lets it drop.
#: All hops are named syntactic edges — no god-type ``USES_TYPE`` fan — so the
#: chain stays precise.
_HOOK_DECL_EDGES: tuple[str, ...] = ("EVENT_SUB", "EVENT_PUB")
_HOOK_REGISTER_API_EDGES: tuple[str, ...] = (
    "HOOK_CONFIG",
    "HOOK_EXEC",
    "DECORATED_BY",
    "HANDLES",
)


def _hook_transparency_hits(
    db, workspace_id: str, seed_uids: list[str], *, limit: int
) -> dict[str, list[_Hit]]:
    """Open hook-DECLARATION seeds through their registration lifecycle.

    An event topic (e.g. ``MapperEvents.before_insert``) is a documented
    near-stub — reaching it renders a docstring, not the mechanism. The wiring
    lives one archetype hop away. Walk the lifecycle two hops: the topic's
    incoming EVENT_SUB/EVENT_PUB sites (who subscribes / publishes — crossing the
    channel), then those sites' HOOK_CONFIG/HOOK_EXEC surface (the registration /
    dispatch API they go through, e.g. ``event.listens_for``), and attribute that
    API surface back to the topic as a distance-2 ``hook_transparency`` hit.

    Seeds with no incoming EVENT edge yield no sites, so this is inert for
    non-topic seeds. The two short hops over named edges keep it precise — it is
    the literal channel->wrapper archetype chain, not a blind binding widening.
    """
    sites_by_seed = walk_neighbours_grouped(
        db,
        workspace_id,
        seed_uids,
        edges=_HOOK_DECL_EDGES,
        direction="undirected",
        max_hops=1,
        limit_per_seed=limit * 4,
    )
    site_to_seeds: dict[str, list[str]] = {}
    for seed, sites in sites_by_seed.items():
        for s in sites:
            site_to_seeds.setdefault(s.uid, []).append(seed)
    if not site_to_seeds:
        return {}
    api_by_site = walk_neighbours_grouped(
        db,
        workspace_id,
        sorted(site_to_seeds),
        edges=_HOOK_REGISTER_API_EDGES,
        direction="undirected",
        max_hops=1,
        limit_per_seed=limit * 4,
    )
    out: dict[str, list[_Hit]] = {}
    for site_uid, apis in api_by_site.items():
        for seed in site_to_seeds.get(site_uid, ()):
            bucket = out.setdefault(seed, [])
            for a in apis:
                bucket.append(_Hit(a.uid, a.name, a.file_path, 2, "hook_transparency"))
    return out


def _candidate_utility_score(
    candidate: RoleCandidate,
    utility_score_fn: Callable[[RoleCandidate], float] | None,
) -> float:
    if utility_score_fn is None:
        return candidate.utility_score if candidate.utility_score is not None else candidate.score
    return utility_score_fn(candidate)


def _merge_grouped_walk_hits(
    hits_per_seed: dict[str, list[_Hit]],
    grouped: dict[str, list],
    step_name: str,
) -> None:
    for su, neighbours in grouped.items():
        bucket = hits_per_seed.get(su)
        if bucket is None:
            continue
        for nb in neighbours:
            bucket.append(_Hit(nb.uid, nb.name, nb.file_path, nb.depth, step_name))


def _append_hook_transparency_hits(
    hits_per_seed: dict[str, list[_Hit]],
    db,
    workspace_id: str,
    seed_uids: list[str],
    *,
    max_per_seed: int,
) -> None:
    for su, extra in _hook_transparency_hits(
        db, workspace_id, seed_uids, limit=max_per_seed
    ).items():
        bucket = hits_per_seed.get(su)
        if bucket is not None:
            bucket.extend(extra)


def _collect_hits_per_seed(
    db,
    workspace_id: str,
    seed_uids: list[str],
    *,
    traversal_mode: str | None,
    max_per_seed: int,
    hook_transparency: bool,
) -> dict[str, list[_Hit]]:
    """Batch graph walks for every seed; optionally attach hook-transparency hits.

    One grouped walk per expansion step over all seed uids (not one round-trip
    per candidate). ``walk_neighbours_grouped`` still returns per-seed buckets.
    """
    hits_per_seed: dict[str, list[_Hit]] = {u: [] for u in seed_uids}
    steps = steps_for_mode(traversal_mode) if traversal_mode is not None else ()
    for step_name, edges, direction, max_hops in steps:
        grouped = walk_neighbours_grouped(
            db,
            workspace_id,
            seed_uids,
            edges=edges,
            direction=direction,
            max_hops=max_hops,
            limit_per_seed=max_per_seed * 4,
        )
        _merge_grouped_walk_hits(hits_per_seed, grouped, step_name)

    if hook_transparency:
        _append_hook_transparency_hits(
            hits_per_seed,
            db,
            workspace_id,
            seed_uids,
            max_per_seed=max_per_seed,
        )
    return hits_per_seed


def _nearest_expansion_hits(
    hits: list[_Hit],
    *,
    include_tests: bool,
    max_per_seed: int,
) -> list[_Hit]:
    """Dedupe expansion hits by uid (shallowest wins), optionally fence tests."""
    nearest_by_uid: dict[str, _Hit] = {}
    for h in hits:
        if not include_tests and is_test_path(h.file_path or ""):
            continue
        existing = nearest_by_uid.get(h.uid)
        if existing is None or h.depth < existing.depth:
            nearest_by_uid[h.uid] = h
    return sorted(
        nearest_by_uid.values(),
        key=lambda h: (h.depth, (h.name or "").lower(), h.uid),
    )[:max_per_seed]


def _plan_candidate_expansions(
    candidates: list[RoleCandidate],
    hits_per_seed: dict[str, list[_Hit]],
    *,
    include_tests: bool,
    max_per_seed: int,
) -> tuple[list[tuple[RoleCandidate, list[_Hit]]], set[str]]:
    expansion_per_candidate: list[tuple[RoleCandidate, list[_Hit]]] = []
    uids_to_fetch: set[str] = set()
    for cand in candidates:
        uids_to_fetch.add(cand.uid)
        ordered = _nearest_expansion_hits(
            hits_per_seed.get(cand.uid, []),
            include_tests=include_tests,
            max_per_seed=max_per_seed,
        )
        expansion_per_candidate.append((cand, ordered))
        for h in ordered:
            uids_to_fetch.add(h.uid)
    return expansion_per_candidate, uids_to_fetch


def _resolve_context_payloads(
    lance,
    db,
    workspace_id: str,
    uids_to_fetch: set[str],
    *,
    overlay: Any | None,
    user_id: str,
) -> dict[str, _PayloadRow]:
    payload_by_uid = _fetch_symbol_payloads(lance, workspace_id, uids_to_fetch)
    payload_by_uid = _merge_symbol_spans(db, workspace_id, uids_to_fetch, payload_by_uid)
    if overlay is not None:
        from context_engine.axis.overlay_context import merge_saved_overlay_payloads

        payload_by_uid = merge_saved_overlay_payloads(
            payload_by_uid,
            overlay=overlay,
            workspace_id=workspace_id,
            user_id=user_id,
        )
    return _hydrate_missing_symbol_code(
        db,
        workspace_id,
        uids_to_fetch,
        payload_by_uid,
    )


def _context_symbol_from_hit(
    hit: _Hit,
    payload_by_uid: dict[str, _PayloadRow],
) -> ContextSymbol:
    payload = payload_by_uid.get(hit.uid, {})
    return ContextSymbol(
        uid=hit.uid,
        name=hit.name,
        file_path=hit.file_path,
        role=hit.step or "related",
        distance_from_seed=hit.depth,
        expansion_step=hit.step,
        code=payload.get("code"),
        qualified_name=str(payload.get("qualified_name") or ""),
        start_line=_int_payload_value(payload.get("start_line")),
        end_line=_int_payload_value(payload.get("end_line")),
    )


def _context_bundle_for_candidate(
    cand: RoleCandidate,
    hits: list[_Hit],
    payload_by_uid: dict[str, _PayloadRow],
    *,
    utility_score_fn: Callable[[RoleCandidate], float] | None,
) -> ContextBundle:
    seed_payload = payload_by_uid.get(cand.uid, {})
    seed = ContextSymbol(
        uid=cand.uid,
        name=cand.name,
        file_path=cand.file_path,
        role=cand.role,
        distance_from_seed=cand.depth or 0,
        expansion_step=None,
        code=seed_payload.get("code"),
        qualified_name=cand.qualified_name or str(seed_payload.get("qualified_name") or ""),
        kind=cand.satisfying_kinds[0] if cand.satisfying_kinds else "",
        direction=_candidate_direction(cand),
        edge_type=cand.edge_type,
        relevance_score=cand.score,
        utility_score=cand.utility_score if cand.utility_score is not None else cand.score,
        start_line=_int_payload_value(seed_payload.get("start_line")),
        end_line=_int_payload_value(seed_payload.get("end_line")),
    )
    related = tuple(_context_symbol_from_hit(h, payload_by_uid) for h in hits)
    return ContextBundle(
        role=cand.role,
        seed=seed,
        related=related,
        utility_score=_candidate_utility_score(cand, utility_score_fn),
    )


def _build_context_bundles(
    expansion_per_candidate: list[tuple[RoleCandidate, list[_Hit]]],
    payload_by_uid: dict[str, dict[str, str | None]],
    *,
    utility_score_fn: Callable[[RoleCandidate], float] | None,
) -> list[ContextBundle]:
    return [
        _context_bundle_for_candidate(
            cand,
            hits,
            payload_by_uid,
            utility_score_fn=utility_score_fn,
        )
        for cand, hits in expansion_per_candidate
    ]


def _apply_overlay_to_context_bundles(
    bundles: list[ContextBundle],
    *,
    overlay: Any,
    workspace_id: str,
    user_id: str,
) -> list[ContextBundle]:
    from context_engine.axis.overlay_context import apply_dirty_overlay_to_bundles

    return apply_dirty_overlay_to_bundles(
        bundles,
        overlay=overlay,
        workspace_id=workspace_id,
        user_id=user_id,
    )


def build_context_for_candidates(
    candidates: Iterable[RoleCandidate],
    *,
    workspace_id: str,
    db,
    lance,
    max_per_seed: int = 6,
    traversal_mode: str | None = "deferred_binding_flow",
    include_tests: bool = False,
    hook_transparency: bool = False,
    render_budget: ContextRenderBudget | None = None,
    utility_score_fn: Callable[[RoleCandidate], float] | None = None,
    overlay: Any | None = None,
    user_id: str = "anonymous",
) -> list[ContextBundle]:
    """Expand each candidate into a ``ContextBundle`` of related code.

    Every candidate gets a graph WALK (the expensive part); the Token Credit
    budget downstream packs the full pool, so there is no active/passive split
    to bound the walk here.

    ``max_per_seed`` caps how many related symbols come back per seed
    (depth-then-name ordering). ``traversal_mode`` picks the expansion
    pattern from ``AxisQueryPlan``; defaults to deferred-binding
    because every current contract uses it. ``None`` keeps explicitly
    supplied impact/trace candidates as a flat, directionally-labelled set
    without expanding them into siblings through a second graph walk.

    ``include_tests`` mirrors the retrieval-pass flag — by default,
    expansion hits that land in conventional test surfaces are
    dropped. Impact-style consumers can flip the flag to keep them.

    ``render_budget`` / ``token_budget`` are the echelon-2 budget knobs
    (default off = whole pool, full code = benchmark behaviour):
    ``signature_only`` trims each symbol to its signature, and a non-None
    ``token_budget`` hands the pool to the Token Credit packer, which buys the
    minimal render per bundle by marginal utility per token.
    """
    candidates = list(candidates)
    if not candidates:
        return []

    budget = render_budget or ContextRenderBudget()
    all_uids = [c.uid for c in candidates]
    hits_per_seed = _collect_hits_per_seed(
        db,
        workspace_id,
        all_uids,
        traversal_mode=traversal_mode,
        max_per_seed=max_per_seed,
        hook_transparency=hook_transparency,
    )
    expansion_per_candidate, uids_to_fetch = _plan_candidate_expansions(
        candidates,
        hits_per_seed,
        include_tests=include_tests,
        max_per_seed=max_per_seed,
    )
    payload_by_uid = _resolve_context_payloads(
        lance,
        db,
        workspace_id,
        uids_to_fetch,
        overlay=overlay,
        user_id=user_id,
    )
    bundles = _build_context_bundles(
        expansion_per_candidate,
        payload_by_uid,
        utility_score_fn=utility_score_fn,
    )
    if overlay is not None:
        bundles = _apply_overlay_to_context_bundles(
            bundles,
            overlay=overlay,
            workspace_id=workspace_id,
            user_id=user_id,
        )
    return _apply_render_and_budget(
        bundles,
        token_budget=budget.token_budget,
        render_mode=budget.render_mode,
        per_transaction_share=budget.per_transaction_share,
        file_soft_cap_share=budget.file_soft_cap_share,
        signature_only_initial=budget.signature_only_initial,
    )


__all__ = [
    "ContextBundle",
    "ContextRenderBudget",
    "ContextSymbol",
    "build_context_for_candidates",
]
