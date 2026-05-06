from __future__ import annotations


class BudgetSelector:
    """Marginal-gain and pruning budget policy."""

    def __init__(self, host):
        self.host = host

    def calculate_marginal_gain(
        self,
        *,
        c,
        chosen,
        target,
        intent=None,
        mechanism: str = "",
        query: str = "",
        required_roles: list[str],
        candidate_roles=None,
    ) -> float:
        return float(
            self.host._calculate_marginal_gain_impl(
                c=c,
                chosen=chosen,
                target=target,
                intent=intent,
                mechanism=mechanism,
                query=query,
                required_roles=required_roles,
                candidate_roles=candidate_roles,
            )
        )
