"""Generate an anchor pack for measuring B_proximity.

Multiplies the python question pack: each question is repeated once per
file-like ``expected_file``, with that file set as the ask ``anchor`` (the
"open file"). Then:

    python -m QA.make_anchor_pack
    python -m QA.axis_benchmark --pack /tmp/anchor_pack.yaml --intent-budget \\
        --out /tmp/prox_on
    python -m QA.axis_benchmark --pack /tmp/anchor_pack.yaml --intent-budget \\
        --no-proximity --out /tmp/prox_off

The on/off diff on the SAME pack isolates B_proximity's boost on *neighbour*
files (the anchor's own trivial self-hit is identical in both arms). This is
the only way to get empirics on B_proximity — the base pack has no anchors.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from QA.axis_benchmark import _load_pack


def main() -> None:
    src = Path("QA/fixtures/questions_python.yaml")
    questions = _load_pack(src)
    out: list[dict] = []
    for q in questions:
        expected = [str(e) for e in (q.get("expected_files") or [])]
        # Only file-like entries (with an extension) work as an "open file"
        # anchor — directory-style expected entries are a region, not a file.
        anchors = [e for e in expected if "." in e.rsplit("/", 1)[-1]]
        for i, anchor in enumerate(anchors):
            nq = dict(q)
            nq["id"] = f"{q.get('id')}#a{i}"
            nq["anchor"] = anchor
            out.append(nq)
    dst = Path("/tmp/anchor_pack.yaml")
    dst.write_text(
        yaml.safe_dump({"questions": out}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(f"{len(out)} anchored questions from {len(questions)} base -> {dst}")


if __name__ == "__main__":
    main()
