from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sidecar.context.intent_classifier import Intent
    from sidecar.context.types import SubgraphNode


class TargetSelector:
    """Primary symbol selection and duplicate disambiguation."""

    def __init__(self, host):
        self.host = host

    def get_target(
        self,
        symbol_name: str,
        query: str = "",
        intent: Intent | None = None,
        *,
        with_metadata: bool = False,
    ) -> SubgraphNode | tuple[SubgraphNode | None, dict] | None:
        target, metadata = self._select_target_candidate(symbol_name, query=query, intent=intent)
        if with_metadata:
            return target, metadata
        return target

    def _select_target_candidate(
        self,
        symbol_name: str,
        *,
        query: str = "",
        intent: Intent | None = None,
    ) -> tuple[SubgraphNode | None, dict]:
        rows = self.host._load_target_candidates(symbol_name)
        if not rows:
            module_row = self.host._load_module_target_candidate(symbol_name)
            if module_row is not None:
                target = self.host._build_target_node(
                    module_row,
                    provenance=["primary:module-target"],
                )
                return target, {
                    "strategy": "module_fallback",
                    "ambiguous": False,
                    "symbol": symbol_name,
                    "candidates_considered": 1,
                    "selected_uid": target.uid,
                    "selected_file_path": target.file_path,
                    "selected_kind": "module",
                    "selection_reason": "module_or_package",
                    "alternatives": [],
                }
            return None, {
                "strategy": "not_found",
                "ambiguous": False,
                "symbol": symbol_name,
                "candidates_considered": 0,
            }

        if len(rows) == 1:
            target = self.host._build_target_node(rows[0], provenance=["primary:target"])
            return target, {
                "strategy": "unique_match",
                "ambiguous": False,
                "symbol": symbol_name,
                "candidates_considered": 1,
                "selected_uid": target.uid,
                "selected_file_path": target.file_path,
                "selected_kind": getattr(rows[0], "get", lambda *_: "")("kind", ""),
                "alternatives": [],
            }

        scored_rows = []
        for row in rows:
            score, breakdown = self.host._score_target_candidate(row, query=query, intent=intent)
            scored_rows.append((score, row, breakdown))
        scored_rows.sort(
            key=lambda item: (
                item[0],
                item[1].get("outgoing_edges", 0),
                item[1].get("total_edges", 0),
                -item[1].get("token_estimate", 0),
                -len(item[1].get("file_path", "")),
            ),
            reverse=True,
        )

        best_score, best_row, best_breakdown = scored_rows[0]
        target = self.host._build_target_node(
            best_row,
            provenance=[
                "primary:target",
                f"target-selection:{best_breakdown['role']}",
            ],
        )
        alternatives = [
            {
                "uid": row["uid"],
                "file_path": row["file_path"],
                "kind": row.get("kind", ""),
                "qualified_name": row.get("qualified_name", ""),
                "score": round(score, 3),
                "role": breakdown["role"],
                "breakdown": breakdown["components"],
            }
            for score, row, breakdown in scored_rows[:5]
        ]
        metadata = {
            "strategy": "duplicate_resolution",
            "ambiguous": True,
            "symbol": symbol_name,
            "candidates_considered": len(scored_rows),
            "selected_uid": best_row["uid"],
            "selected_file_path": best_row["file_path"],
            "selected_kind": best_row.get("kind", ""),
            "selected_qualified_name": best_row.get("qualified_name", ""),
            "selected_score": round(best_score, 3),
            "selection_reason": best_breakdown["role"],
            "alternatives": alternatives,
        }
        return target, metadata
