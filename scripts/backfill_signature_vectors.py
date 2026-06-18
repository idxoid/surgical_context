"""Backfill the ``signature_vector`` facet onto an existing axis symbol index.

The dual-facet retrieval (see ``role_retrieval._scan_distances``) embeds each
symbol twice: the full body (existing ``vector``) and the signature header
(``signature_vector``). A large body dilutes the body vector, so a
signature/API-shaped query loses the symbol; the signature facet restores it.

Body vectors and all axis payloads are PRESERVED — only the new column is
computed (signature embeds, cached), so this is far cheaper than a structural
reindex. Embedding runs in plain Python (NOT inside a Lance native UDF, which
segfaults alongside the torch encoder), then the augmented rows are written to
a temp table and swapped in.

Usage::

    PYTHONPATH=. .venv/bin/python scripts/backfill_signature_vectors.py
"""

from __future__ import annotations

import sys

import lancedb

from sidecar.database.lancedb_client import (
    AXIS_SYMBOLS_SCHEMA,
    DB_PATH,
    LanceDBClient,
    symbol_signature_text,
)
from sidecar.index_profile import AXIS_PYTHON_V1_PROFILE

TABLE = "symbols_axis_python_v1"
TMP = "symbols_axis_python_v1__sigfacet_tmp"
ADD_CHUNK = 2000


def _augment_workspace(client, src_table, ws: str) -> list[dict]:
    ws_q = ws.replace("'", "''")
    arrow = src_table.to_lance().to_table(filter=f"workspace_id = '{ws_q}'")
    rows = arrow.to_pylist()
    sigs = [symbol_signature_text(str(r.get("code") or "")) for r in rows]
    vecs = client._embed(sigs)  # noqa: SLF001 — internal cached embedder
    for r, v in zip(rows, vecs, strict=False):
        r["signature_vector"] = v
    return rows


def _add_in_chunks(table, rows: list[dict]) -> None:
    for i in range(0, len(rows), ADD_CHUNK):
        table.add(rows[i : i + ADD_CHUNK])


def main() -> int:
    client = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)
    db = lancedb.connect(DB_PATH)
    if TABLE not in db.table_names():
        print(f"table {TABLE} not found")
        return 1
    src = db.open_table(TABLE)
    if "signature_vector" in [f.name for f in src.schema]:
        print("signature_vector already present — nothing to backfill")
        return 0

    workspaces = sorted(
        set(src.to_lance().to_table(columns=["workspace_id"]).column(0).to_pylist())
    )
    src_total = src.count_rows()
    print(f"source rows={src_total} across {len(workspaces)} workspaces")

    # 1. Build the augmented temp table (embedding happens here, in plain
    #    Python — never inside a Lance UDF callback).
    if TMP in db.table_names():
        db.drop_table(TMP)
    tmp = db.create_table(TMP, schema=AXIS_SYMBOLS_SCHEMA)
    done = 0
    for ws in workspaces:
        rows = _augment_workspace(client, src, ws)
        _add_in_chunks(tmp, rows)
        done += len(rows)
        print(f"  [{done}/{src_total}] {ws} (+{len(rows)})", flush=True)

    tmp_total = tmp.count_rows()
    if tmp_total != src_total:
        print(f"ABORT: temp count {tmp_total} != source {src_total}; leaving original intact")
        return 2

    # 2. Swap: drop original, recreate with the facet schema, copy temp back
    #    (pure IO, no embedding), drop temp. If interrupted after the drop,
    #    re-running copies from the intact temp.
    print("temp verified; swapping into place")
    db.drop_table(TABLE)
    dst = db.create_table(TABLE, schema=AXIS_SYMBOLS_SCHEMA)
    copied = 0
    for ws in workspaces:
        ws_q = ws.replace("'", "''")
        rows = tmp.to_lance().to_table(filter=f"workspace_id = '{ws_q}'").to_pylist()
        _add_in_chunks(dst, rows)
        copied += len(rows)
        print(f"  copied {copied}/{tmp_total}", flush=True)

    if dst.count_rows() != src_total:
        print(f"WARNING: final count {dst.count_rows()} != {src_total}; temp KEPT for recovery")
        return 2
    db.drop_table(TMP)
    ok = "signature_vector" in [f.name for f in db.open_table(TABLE).schema]
    print("done; signature_vector present:", ok)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
