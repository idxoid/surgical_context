"""Golden-parity harness: ast-based vs tree-sitter axis extractor.

Runs both ``PythonAxisExtractor`` (ast) and ``PythonAxisExtractorTS``
(tree-sitter) over every ``.py`` file under the given roots and diffs the
outputs at two strictness tiers.

Tier A (hard fail) — everything retrieval consumes structurally:
  - per-``qualified_name`` (axis, bit) fact multisets with line numbers
  - ``ast_kind`` bit sets (persisted as ``ast_kind_bits``)
  - keyed-fact literals: (bit, key_kind, key_literal) multisets
  - ``ContainerKindClassifier`` kind sets per profile
  - ``AxisContractCompiler`` contract ids per profile

Tier B (report only) — cosmetic text parity with ``ast.unparse``:
  - full fact-dict equality rate
  - evidence / payload text mismatch counts and samples

Usage:
    .venv/bin/python scripts/axis_ts_parity.py QA/repos/flask [more roots ...]
    .venv/bin/python scripts/axis_ts_parity.py --all   # all python QA repos
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from context_engine.axis.container_kind import ContainerKindClassifier  # noqa: E402
from context_engine.axis.contract_compiler import AxisContractCompiler  # noqa: E402
from context_engine.axis.schema import AxisExtraction, AxisFact  # noqa: E402
from context_engine.parser.adapters.python_axis_extractor import (  # noqa: E402
    PythonAxisExtractor,
)
from context_engine.parser.adapters.python_axis_extractor_ts import (  # noqa: E402
    PythonAxisExtractorTS,
)

PY_QA_REPOS = ["celery", "click", "django", "fastapi", "flask", "pydantic", "sqlalchemy"]

_KEYED_BITS = {
    "keyed_read",
    "keyed_write",
    "container_read_key",
    "container_write_value",
    "literal_key",
    "subscript_read",
    "subscript_write",
}


def _profile_map(facts: list[AxisFact]) -> dict[str, list[AxisFact]]:
    grouped: dict[str, list[AxisFact]] = defaultdict(list)
    for fact in facts:
        grouped[fact.qualified_name].append(fact)
    return grouped


def _bit_multiset(facts: list[AxisFact]) -> Counter:
    return Counter((f.axis, f.bit, f.line) for f in facts)


def _kind_bits(facts: list[AxisFact]) -> set[str]:
    return {f.ast_kind for f in facts}


def _keyed_literals(facts: list[AxisFact]) -> Counter:
    out: Counter = Counter()
    for f in facts:
        if f.bit in _KEYED_BITS:
            payload = f.payload or {}
            out[
                (
                    f.bit,
                    str(payload.get("key_kind", "")),
                    repr(payload.get("key_literal", "<none>")),
                )
            ] += 1
    return out


def _classify(extraction: AxisExtraction) -> dict[str, tuple[set, set]]:
    """qualified_name -> (container kind set, contract id set), probe-less."""
    classifier = ContainerKindClassifier()
    compiler = AxisContractCompiler()
    out: dict[str, tuple[set, set]] = {}
    for profile in extraction.profiles.values():
        kinds = classifier.classify(profile)
        contracts = compiler.compile(profile, kinds)
        out[profile.qualified_name] = (
            {match.kind for match in kinds},
            {getattr(c, "contract_id", getattr(c, "name", repr(c))) for c in contracts},
        )
    return out


class Stats:
    def __init__(self) -> None:
        self.files = 0
        self.skipped_syntax = 0
        self.failed_files = 0
        self.old_time = 0.0
        self.new_time = 0.0
        self.facts_old = 0
        self.facts_new = 0
        self.tier_a_files_bad = 0
        self.tier_a_msgs: list[str] = []
        self.exact_facts = 0
        self.text_only_diffs = 0
        self.evidence_only_diffs = 0
        self.tier_b_samples: list[str] = []
        self.unknown_kinds: Counter = Counter()


def _tier_a_diff(path: str, old: list[AxisFact], new: list[AxisFact], stats: Stats) -> bool:
    ok = True
    old_map = _profile_map(old)
    new_map = _profile_map(new)
    msgs = stats.tier_a_msgs

    def report(msg: str) -> None:
        nonlocal ok
        ok = False
        if len(msgs) < 400:
            msgs.append(f"{path}: {msg}")

    for qn in sorted(set(old_map) | set(new_map)):
        o, n = old_map.get(qn, []), new_map.get(qn, [])
        if not o:
            report(f"[scope+] {qn}: only in TS ({len(n)} facts)")
            continue
        if not n:
            report(f"[scope-] {qn}: only in ast ({len(o)} facts)")
            continue
        bo, bn = _bit_multiset(o), _bit_multiset(n)
        if bo != bn:
            missing = bo - bn
            extra = bn - bo
            for key, count in list(missing.items())[:6]:
                report(f"[bit-] {qn}: ast-only {key} x{count}")
            for key, count in list(extra.items())[:6]:
                report(f"[bit+] {qn}: ts-only {key} x{count}")
        ko, kn = _kind_bits(o), _kind_bits(n)
        if ko != kn:
            report(f"[kind] {qn}: ast_kinds ast-only={sorted(ko - kn)} ts-only={sorted(kn - ko)}")
        lo, ln = _keyed_literals(o), _keyed_literals(n)
        if lo != ln:
            report(
                f"[key] {qn}: literals ast-only={list((lo - ln).items())[:4]} "
                f"ts-only={list((ln - lo).items())[:4]}"
            )
    return ok


def _tier_a_classifier_diff(
    path: str, old_ex: AxisExtraction, new_ex: AxisExtraction, stats: Stats
) -> bool:
    ok = True
    old_cls = _classify(old_ex)
    new_cls = _classify(new_ex)
    for qn in sorted(set(old_cls) | set(new_cls)):
        o_kinds, o_contracts = old_cls.get(qn, (set(), set()))
        n_kinds, n_contracts = new_cls.get(qn, (set(), set()))
        if o_kinds != n_kinds:
            ok = False
            if len(stats.tier_a_msgs) < 400:
                stats.tier_a_msgs.append(
                    f"{path}: [ckind] {qn}: ast={sorted(o_kinds)} ts={sorted(n_kinds)}"
                )
        if o_contracts != n_contracts:
            ok = False
            if len(stats.tier_a_msgs) < 400:
                stats.tier_a_msgs.append(
                    f"{path}: [contract] {qn}: ast-only={sorted(o_contracts - n_contracts)} "
                    f"ts-only={sorted(n_contracts - o_contracts)}"
                )
    return ok


def _tier_b_diff(path: str, old: list[AxisFact], new: list[AxisFact], stats: Stats) -> None:
    """Pair facts by (qn, axis, bit, line, order) and compare rendered text."""
    index: dict[tuple, list[AxisFact]] = defaultdict(list)
    for f in new:
        index[(f.qualified_name, f.axis, f.bit, f.line)].append(f)
    for f in old:
        bucket = index.get((f.qualified_name, f.axis, f.bit, f.line))
        if not bucket:
            continue
        g = bucket.pop(0)
        payload_eq = f.payload == g.payload
        evidence_eq = f.evidence == g.evidence
        if payload_eq and evidence_eq and f.ast_kind == g.ast_kind:
            stats.exact_facts += 1
            continue
        if payload_eq and f.ast_kind == g.ast_kind:
            stats.evidence_only_diffs += 1
            if len(stats.tier_b_samples) < 60:
                stats.tier_b_samples.append(
                    f"{path}:{f.line} {f.bit} [evidence]\n  ast: {f.evidence!r}\n  ts:  {g.evidence!r}"
                )
            continue
        stats.text_only_diffs += 1
        if len(stats.tier_b_samples) < 60:
            diff_keys = sorted(
                k for k in set(f.payload) | set(g.payload) if f.payload.get(k) != g.payload.get(k)
            )[:4]
            detail = "; ".join(
                f"{k}: ast={f.payload.get(k)!r} ts={g.payload.get(k)!r}" for k in diff_keys
            )
            if f.ast_kind != g.ast_kind:
                detail = f"ast_kind ast={f.ast_kind} ts={g.ast_kind}; " + detail
            stats.tier_b_samples.append(f"{path}:{f.line} {f.bit} [payload] {detail}")


def run(roots: list[Path], *, limit: int | None, classify: bool) -> Stats:
    old_extractor = PythonAxisExtractor()
    new_extractor = PythonAxisExtractorTS()
    stats = Stats()

    files: list[Path] = []
    for root in roots:
        files.extend(sorted(root.rglob("*.py")))
    if limit:
        files = files[:limit]

    for file_path in files:
        try:
            source = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = str(file_path)
        t0 = time.perf_counter()
        try:
            old_ex = old_extractor.extract(source, rel)
        except SyntaxError:
            stats.skipped_syntax += 1
            continue
        except RecursionError:
            stats.skipped_syntax += 1
            continue
        t1 = time.perf_counter()
        try:
            new_ex = new_extractor.extract(source, rel)
        except Exception as exc:  # noqa: BLE001 - harness must survive and report
            stats.failed_files += 1
            if len(stats.tier_a_msgs) < 400:
                stats.tier_a_msgs.append(f"{rel}: [crash] TS extractor: {exc!r}")
            continue
        t2 = time.perf_counter()

        stats.files += 1
        stats.old_time += t1 - t0
        stats.new_time += t2 - t1
        stats.facts_old += len(old_ex.facts)
        stats.facts_new += len(new_ex.facts)

        for fact in new_ex.facts:
            # CamelCased tree-sitter fallbacks that are not real ast names
            if fact.ast_kind and fact.ast_kind[0].isupper() and "_" in fact.ast_kind:
                stats.unknown_kinds[fact.ast_kind] += 1

        a_ok = _tier_a_diff(rel, old_ex.facts, new_ex.facts, stats)
        if classify:
            a_ok = _tier_a_classifier_diff(rel, old_ex, new_ex, stats) and a_ok
        if not a_ok:
            stats.tier_a_files_bad += 1
        _tier_b_diff(rel, old_ex.facts, new_ex.facts, stats)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="*", help="directories to scan")
    parser.add_argument("--all", action="store_true", help="scan all python QA repos")
    parser.add_argument("--limit", type=int, default=None, help="max files")
    parser.add_argument("--no-classify", action="store_true", help="skip classifier/contract diff")
    parser.add_argument("--samples", type=int, default=25, help="tier B samples to print")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    roots = [Path(r) for r in args.roots]
    if args.all:
        roots.extend(repo_root / "QA" / "repos" / name for name in PY_QA_REPOS)
    roots = [r for r in roots if r.exists()]
    if not roots:
        print("no roots to scan", file=sys.stderr)
        return 2

    stats = run(roots, limit=args.limit, classify=not args.no_classify)

    total_paired = stats.exact_facts + stats.text_only_diffs + stats.evidence_only_diffs
    print(
        f"\nfiles={stats.files} skipped_syntax={stats.skipped_syntax} crashed={stats.failed_files}"
    )
    print(f"facts: ast={stats.facts_old} ts={stats.facts_new}")
    print(f"time:  ast={stats.old_time:.2f}s ts={stats.new_time:.2f}s")
    print(f"tier A: files_with_mismatch={stats.tier_a_files_bad} messages={len(stats.tier_a_msgs)}")
    if total_paired:
        print(
            f"tier B: exact={stats.exact_facts} ({stats.exact_facts / total_paired:.2%}) evidence_only={stats.evidence_only_diffs} payload_text={stats.text_only_diffs}"
        )
    if stats.unknown_kinds:
        print(f"unknown ast_kinds from TS fallback: {dict(stats.unknown_kinds.most_common(10))}")

    print("\n--- tier A messages (first 80) ---")
    for msg in stats.tier_a_msgs[:80]:
        print(msg)
    print(f"\n--- tier B samples (first {args.samples}) ---")
    for msg in stats.tier_b_samples[: args.samples]:
        print(msg)
    return 1 if stats.tier_a_files_bad or stats.failed_files else 0


if __name__ == "__main__":
    raise SystemExit(main())
