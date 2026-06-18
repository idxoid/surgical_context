"""Analytical layer for the axis extractor — axis-bit coverage and container-
kind inventory.

This module produces *reports*, not new axis bits. It walks the existing
extractor to inventory the axis bits it can emit and catalogs benchmark question
pack metadata. Axis validation (``QA/axis_benchmark.py``) scores ``file_recall``
only — fixture ``required_roles`` were cascade leftovers and are no longer read.

It does not author roles, propose new axis bits, or decide policy. Output is
data the human can read to decide where the next extractor / container-kind
work should land.

Terminology (see ``docs/axis_terminology.md``):

  fact      = physical AST/graph observation
  axis bit  = normalized fact on CFG/DFG/STRUCT (what L1 emits)
  contract  = provable combination of axis bits on a symbol
  role      = user/benchmark requirement, satisfied by >=1 contract
  bucket    = optimisation grouping (not used in this module)

Two layers only, per the current architectural scope:

  L1 — extractor axis bits (what the analyser can read today)
  L2 — container kinds     (analytical fingerprint sketches for future classifiers)
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

AxisName = Literal["cfg", "dfg", "struct"]
PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# L1: extractor bit inventory — derived from the extractor source itself so it
# stays in lockstep with what the extractor actually emits. No hand list.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractorInventory:
    """Snapshot of every (axis, bit) the extractor can emit."""

    cfg: frozenset[str]
    dfg: frozenset[str]
    struct: frozenset[str]

    @property
    def all_pairs(self) -> set[tuple[AxisName, str]]:
        return (
            {("cfg", b) for b in self.cfg}
            | {("dfg", b) for b in self.dfg}
            | {("struct", b) for b in self.struct}
        )

    def has(self, axis: AxisName, bit: str) -> bool:
        return bit in {"cfg": self.cfg, "dfg": self.dfg, "struct": self.struct}[axis]

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "cfg": sorted(self.cfg),
            "dfg": sorted(self.dfg),
            "struct": sorted(self.struct),
        }


def inventory_extractor_bits(
    extractor_path: Path = PROJECT_ROOT / "context_engine" / "axis" / "python_extractor.py",
) -> ExtractorInventory:
    """Walk the extractor AST to find every `self._emit(axis, bit, ...)` call.

    Conditional bits (e.g. ``"async_function_def" if async else "function_def"``)
    are unfolded so both branches enter the inventory.
    """
    with open(extractor_path) as fp:
        tree = ast.parse(fp.read())

    cfg: set[str] = set()
    dfg: set[str] = set()
    struct: set[str] = set()
    buckets: dict[str, set[str]] = {"cfg": cfg, "dfg": dfg, "struct": struct}

    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_emit"
            and len(node.args) >= 2
        ):
            continue
        axis_arg, bit_arg = node.args[0], node.args[1]
        axis = axis_arg.value if isinstance(axis_arg, ast.Constant) else None
        if axis not in buckets:
            continue
        # Bit may be a literal or a conditional expression
        for bit_value in _string_values_from(bit_arg):
            buckets[axis].add(bit_value)

    return ExtractorInventory(
        cfg=frozenset(cfg),
        dfg=frozenset(dfg),
        struct=frozenset(struct),
    )


def _string_values_from(node: ast.AST) -> list[str]:
    """Best-effort: extract every literal string this expression can resolve to."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _string_values_from(node.body) + _string_values_from(node.orelse)
    return []



# ---------------------------------------------------------------------------
# L2: container-kind catalogue (analytical sketch).
#
# Each container kind is described by the FINGERPRINT a future L2 classifier
# would have to detect. The fingerprint is written in terms of axis bits and
# graph topology hints. We are NOT classifying here; we are listing what L2
# would need to express. A kind that recurs across multiple frameworks is
# healthy; one that ties to a single library is a candidate for library marker
# instead of new kind.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainerKindSpec:
    name: str
    description: str
    distinguishing_bits: tuple[str, ...]
    topology_hints: tuple[str, ...]
    expected_frameworks: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "distinguishing_bits": list(self.distinguishing_bits),
            "topology_hints": list(self.topology_hints),
            "expected_frameworks": list(self.expected_frameworks),
        }


CONTAINER_KINDS: dict[str, ContainerKindSpec] = {
    "web_route_register": ContainerKindSpec(
        name="web_route_register",
        description="Container that maps URL patterns to handler callables.",
        distinguishing_bits=(
            "struct.decorator_shape",
            "dfg.keyed_write",
            "struct.literal_key",
        ),
        topology_hints=(
            "outgoing HAS_API fan to many handler-callables",
            "USES_TYPE / external import root in {starlette.routing, werkzeug.routing, fastapi.routing}",
            "keyed_write keys include HTTP method literals or URL patterns",
        ),
        expected_frameworks=(
            "Flask",
            "FastAPI",
            "Django (URLconf)",
            "Starlette",
            "Express",
            "NestJS",
        ),
    ),
    "task_register": ContainerKindSpec(
        name="task_register",
        description="Container that registers callables as deferred / queued tasks.",
        distinguishing_bits=(
            "struct.decorator_shape",
            "dfg.callable_value",
            "dfg.keyed_write",
        ),
        topology_hints=(
            "import_dependency to messaging packages (kombu/amqp/billiard/redis transport)",
            "INSTANTIATES of queue-like objects",
            "decorator_shape carries task-options payload (literal keys: name, queue, retries)",
        ),
        expected_frameworks=("Celery", "RQ", "Dramatiq", "Huey"),
    ),
    "signal_register": ContainerKindSpec(
        name="signal_register",
        description="Bidirectional callable storage: receivers attached and later iterated.",
        distinguishing_bits=(
            "dfg.callable_value",
            "dfg.container_write_value",
            "dfg.iteration_source",
        ),
        topology_hints=(
            "class with `connect`/`disconnect`/`send` shape (axis fingerprint, not name)",
            "no web/task/model fingerprint present",
            "callable storage iterated under a fan-out call site",
        ),
        expected_frameworks=("Django signals", "blinker", "Vue ref", "RTK listener middleware"),
    ),
    "data_model": ContainerKindSpec(
        name="data_model",
        description="Class whose body declares field-typed descriptors / annotations.",
        distinguishing_bits=(
            "struct.class_def",
            "struct.class_attribute",
            "struct.annotation",
            "dfg.constructed_output",
        ),
        topology_hints=(
            "multiple class_attribute + annotation pairs in class body",
            "methods returning constructed value of the same class",
            "validators / serializers referencing the same class",
        ),
        expected_frameworks=(
            "Pydantic Model",
            "Django Model",
            "SQLAlchemy declarative_base",
            "msgspec",
            "attrs",
            "dataclass",
        ),
    ),
    "di_container": ContainerKindSpec(
        name="di_container",
        description="Object resolving provider references to argument slots.",
        distinguishing_bits=(
            "dfg.parameter_default_value",
            "dfg.callable_value",
            "dfg.call_argument",
        ),
        topology_hints=(
            "parameter default holds marker carrying a callable provider",
            "call site resolves provider into the argument slot",
            "no route/task fingerprint",
        ),
        expected_frameworks=(
            "FastAPI Depends",
            "Click context",
            "NestJS provider",
            "pytest fixture",
        ),
    ),
    "middleware_chain": ContainerKindSpec(
        name="middleware_chain",
        description="Ordered list of callables appended at registration and invoked in sequence.",
        distinguishing_bits=(
            "dfg.callable_value",
            "dfg.container_write_value",
            "dfg.iteration_source",
        ),
        topology_hints=(
            "single container appended into via a builder method",
            "iteration_source over the container drives sequential invocation",
            "each callable receives the next callable as argument (wrapper continuation)",
        ),
        expected_frameworks=(
            "ASGI/WSGI middleware stack",
            "Express app.use",
            "Django MIDDLEWARE",
            "NestJS interceptors",
        ),
    ),
    "config_carrier": ContainerKindSpec(
        name="config_carrier",
        description="Object carrying typed configuration values consumed by other code.",
        distinguishing_bits=(
            "struct.class_def",
            "struct.annotation",
            "dfg.attr_read",
            "dfg.keyed_read",
        ),
        topology_hints=(
            "class body holds annotated literal defaults",
            "field reads observed in branch conditions elsewhere",
        ),
        expected_frameworks=("Pydantic Settings", "Django settings", "Celery conf", "ConfigDict"),
    ),
    "error_dispatch": ContainerKindSpec(
        name="error_dispatch",
        description="Container mapping exception types to handler callables.",
        distinguishing_bits=(
            "cfg.exception_handler_type",
            "dfg.callable_value",
            "dfg.keyed_write",
        ),
        topology_hints=(
            "literal key in container is an exception class",
            "value read at runtime and invoked from raise path",
        ),
        expected_frameworks=(
            "FastAPI exception_handlers",
            "Flask errorhandler",
            "Django middleware",
        ),
    ),
    "proxy_object": ContainerKindSpec(
        name="proxy_object",
        description="Object whose attribute reads/writes resolve to a scoped target.",
        distinguishing_bits=(
            "dfg.context_resource",
            "dfg.attr_read",
        ),
        topology_hints=(
            "lazy-proxy pattern (LocalProxy, ContextVar wrapper)",
            "graph layer: is_proxy_binding marker already exists",
        ),
        expected_frameworks=("Flask current_app/request", "FastAPI Depends-scoped"),
    ),
    "metadata_carrier": ContainerKindSpec(
        name="metadata_carrier",
        description="Object holding declarative metadata read at runtime.",
        distinguishing_bits=(
            "dfg.keyed_write",
            "dfg.keyed_read",
            "struct.literal_key",
        ),
        topology_hints=(
            "write and read share literal key identity",
            "key set is small, fixed at declaration time",
        ),
        expected_frameworks=(
            "Pydantic field info",
            "SQLAlchemy mapper info",
            "NestJS metadata reflection",
            "dataclass __init_subclass__",
        ),
    ),
    # registry_kind is an *abstract* parameter for contracts that take any
    # registry-shaped container. The concrete kind is one of the above (web /
    # task / signal / middleware / error_dispatch / metadata_carrier).
    "registry_kind": ContainerKindSpec(
        name="registry_kind",
        description="Abstract parameter — any concrete registry-shaped container kind.",
        distinguishing_bits=(),
        topology_hints=(
            "any of web_route_register / task_register / signal_register / middleware_chain / "
            "error_dispatch / metadata_carrier",
        ),
        expected_frameworks=(),
    ),
}


# ---------------------------------------------------------------------------
# Question-pack catalog (metadata only — axis scores file_recall, not roles)
# ---------------------------------------------------------------------------


@dataclass
class QuestionRecord:
    """Per-question metadata from benchmark packs."""

    question_id: str
    repo: str
    seed: str | None
    intent: str | None
    mechanism: str | None
    pack: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "question_id": self.question_id,
            "repo": self.repo,
            "seed": self.seed,
            "intent": self.intent,
            "mechanism": self.mechanism,
            "pack": self.pack,
        }


def load_pack(path: Path) -> list[dict]:
    """Load a single YAML pack and return its questions."""
    with open(path) as fp:
        data = yaml.safe_load(fp) or {}
    return list(data.get("questions") or [])


def load_packs(paths: list[Path]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for p in paths:
        for q in load_pack(p):
            qid = q.get("id")
            if qid in seen:
                continue
            seen.add(qid)
            out.append({**q, "_pack": p.name})
    return out


def catalog_question(q: dict) -> QuestionRecord:
    return QuestionRecord(
        question_id=str(q.get("id")),
        repo=str(q.get("repo") or ""),
        seed=q.get("symbol"),
        intent=q.get("intent"),
        mechanism=q.get("mechanism"),
        pack=q.get("_pack"),
    )


def coverage_report(
    questions: list[dict],
    inventory: ExtractorInventory,
) -> dict[str, object]:
    records = [catalog_question(q) for q in questions]
    repo_freq: Counter[str] = Counter()
    mechanism_freq: Counter[str] = Counter()
    for r in records:
        if r.repo:
            repo_freq[r.repo] += 1
        if r.mechanism:
            mechanism_freq[r.mechanism] += 1

    return {
        "summary": {
            "total_questions": len(records),
            "repos": len(repo_freq),
            "distinct_mechanisms": len(mechanism_freq),
        },
        "questions_by_repo": repo_freq.most_common(),
        "questions_by_mechanism": mechanism_freq.most_common(40),
        "extractor_inventory": inventory.to_dict(),
        "container_kinds": {k: v.to_dict() for k, v in CONTAINER_KINDS.items()},
        "per_question": [r.to_dict() for r in records],
    }


# ---------------------------------------------------------------------------
# Markdown report writer
# ---------------------------------------------------------------------------


def render_markdown(report: dict, output: Path) -> None:
    s = report["summary"]
    lines: list[str] = []
    lines.append("# Axis fact inventory report\n")
    lines.append(
        "Generated by `QA.axis_analysis`. L1 extractor bit inventory + L2 container-kind "
        "catalogue + question-pack metadata. Axis benchmark validation uses "
        "`file_recall` on `expected_files` only — fixture roles were removed.\n\n"
    )
    lines.append("## Summary\n\n")
    lines.append(f"- total questions: **{s['total_questions']}**\n")
    lines.append(f"- repos: **{s['repos']}**\n")
    lines.append(f"- distinct mechanisms (annotation): **{s['distinct_mechanisms']}**\n\n")

    lines.append("## Questions by repo\n\n")
    lines.append("| repo | questions |\n|---|---|\n")
    for repo, count in report["questions_by_repo"]:
        lines.append(f"| `{repo}` | {count} |\n")
    lines.append("\n")

    if report["questions_by_mechanism"]:
        lines.append("## Top mechanisms (YAML annotation)\n\n")
        lines.append("| mechanism | questions |\n|---|---|\n")
        for mech, count in report["questions_by_mechanism"]:
            lines.append(f"| `{mech}` | {count} |\n")
        lines.append("\n")

    lines.append("## Container kind catalogue (L2 sketch)\n\n")
    lines.append("| kind | frameworks |\n|---|---|\n")
    for kind, spec in sorted(report["container_kinds"].items()):
        fw = ", ".join(spec.get("expected_frameworks") or []) or "—"
        lines.append(f"| `{kind}` | {fw} |\n")
    lines.append("\n")

    lines.append("## Extractor inventory snapshot\n\n")
    inv = report["extractor_inventory"]
    for axis in ("cfg", "dfg", "struct"):
        lines.append(f"### {axis.upper()} ({len(inv[axis])} bits)\n\n")
        lines.append(", ".join(f"`{b}`" for b in inv[axis]))
        lines.append("\n\n")

    output.write_text("".join(lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_pack_paths() -> list[Path]:
    base = PROJECT_ROOT / "QA" / "fixtures"
    return [
        base / "questions_python.yaml",
        base / "questions_non_python.yaml",
        base / "new_questions_python.yaml",
    ]


def run(
    *,
    pack_paths: list[Path] | None = None,
    out_dir: Path = Path("/tmp/axis_analysis"),
) -> dict[str, object]:
    pack_paths = pack_paths or _default_pack_paths()
    out_dir.mkdir(parents=True, exist_ok=True)
    inventory = inventory_extractor_bits()
    questions = load_packs(pack_paths)
    report = coverage_report(questions, inventory)
    (out_dir / "axis_coverage.json").write_text(json.dumps(report, indent=2, sort_keys=False))
    render_markdown(report, out_dir / "axis_coverage.md")
    return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/tmp/axis_analysis", type=Path)
    parser.add_argument("--pack", action="append", type=Path)
    args = parser.parse_args()
    rep = run(pack_paths=args.pack, out_dir=args.out)
    s = rep["summary"]
    print(
        f"total={s['total_questions']} repos={s['repos']} "
        f"mechanisms={s['distinct_mechanisms']}"
    )
    print(f"Reports: {args.out}/axis_coverage.json  {args.out}/axis_coverage.md")
