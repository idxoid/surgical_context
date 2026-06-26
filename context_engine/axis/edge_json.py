"""Decode persisted adjacency edge maps."""

from __future__ import annotations

import json


def decode_edge_uid_map(raw: object) -> dict[str, set[str]]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        payload = raw
    else:
        try:
            payload = json.loads(str(raw))
        except (TypeError, json.JSONDecodeError):
            return {}
    if not isinstance(payload, dict):
        return {}
    decoded: dict[str, set[str]] = {}
    for edge_type, uids in payload.items():
        if not isinstance(edge_type, str):
            continue
        if not isinstance(uids, list):
            continue
        decoded[edge_type] = {str(uid) for uid in uids if uid}
    return decoded


__all__ = ["decode_edge_uid_map"]
