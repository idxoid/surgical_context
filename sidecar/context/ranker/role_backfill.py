from __future__ import annotations


class RoleBackfill:
    """Role-driven recovery and merge policy."""

    def __init__(self, host):
        self.host = host

    def merge_role_backfill(self, pool, backfill):
        return self.host._merge_role_backfill_impl(pool, backfill)
