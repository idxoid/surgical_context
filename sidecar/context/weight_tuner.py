"""Weight tuning for UnifiedRanker via grid/random search.

Systematically explores the weight space (α, β, γ, δ, ε) to find the best
combination for a given corpus and evaluation set.

Usage:
    tuner = GridSearchTuner(eval_func=benchmark_eval, metric="recall_at_5")
    results = tuner.tune(n_trials=100)
    print(tuner.format_results(top_k=5))
"""

from __future__ import annotations

import itertools
import json
import time
from dataclasses import dataclass, asdict
from typing import Callable, Sequence

from sidecar.context.unified_ranker import RankerWeights


_METRIC_ALIASES = {
    "recall@5": "recall_at_5",
    "precision@5": "precision_at_5",
}


def _canonical_metric_name(metric: str) -> str:
    return _METRIC_ALIASES.get(metric, metric)


def _metric_value(metrics: dict, metric: str) -> float:
    canonical = _canonical_metric_name(metric)
    if canonical in metrics:
        return metrics.get(canonical, 0.0)
    if metric in metrics:
        return metrics.get(metric, 0.0)
    return 0.0


def _metric_float(metrics: dict, key: str, default: float = 0.0) -> float:
    value = metrics.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class TuneResult:
    """Single trial result."""

    weights: RankerWeights
    metric: float
    metrics: dict  # full metrics (pass_rate, recall@5, file_recall, etc.)
    trial: int
    duration_sec: float


class WeightTuner:
    """Base class for weight search strategies."""

    def __init__(
        self,
        eval_func: Callable[[RankerWeights], dict],
        metric: str = "recall@5",
        metric_higher_is_better: bool = True,
    ):
        """
        Args:
            eval_func: function(weights) -> dict with at least metric key
            metric: key in the returned dict to optimize. ``recall@5`` and
                ``precision@5`` aliases are accepted for convenience.
            metric_higher_is_better: whether higher metric values are better
        """
        self.eval_func = eval_func
        self.metric = _canonical_metric_name(metric)
        self.metric_label = metric
        self.metric_higher_is_better = metric_higher_is_better
        self.results: list[TuneResult] = []

    def tune(self) -> list[TuneResult]:
        """Run the tuning search. Subclasses implement."""
        raise NotImplementedError

    def _eval_weights(self, weights: RankerWeights, trial: int) -> TuneResult:
        """Evaluate a single weight combination."""
        t0 = time.perf_counter()
        metrics = self.eval_func(weights)
        duration = time.perf_counter() - t0
        metric_val = _metric_value(metrics, self.metric)
        return TuneResult(
            weights=weights,
            metric=metric_val,
            metrics=metrics,
            trial=trial,
            duration_sec=duration,
        )

    def best_result(self) -> TuneResult | None:
        """Return the best result found."""
        if not self.results:
            return None
        return max(self.results, key=self._result_sort_key)

    def _result_sort_key(self, result: TuneResult) -> tuple[float, ...]:
        """Rank results with metric-aware tie-breakers.

        For ``pass_rate`` tuning we prefer:
        1. higher pass rate
        2. higher precision@5
        3. lower tokens_surgical
        """
        primary = result.metric if self.metric_higher_is_better else -result.metric
        if self.metric == "pass_rate":
            precision = _metric_float(result.metrics, "precision_at_5", 0.0)
            tokens = _metric_float(result.metrics, "tokens_surgical", float("inf"))
            return (primary, precision, -tokens)
        return (primary,)

    def format_results(self, top_k: int = 5) -> str:
        """Format results as readable table."""
        if not self.results:
            return "No results yet."

        sorted_results = sorted(
            self.results,
            key=self._result_sort_key,
            reverse=True,
        )

        include_tiebreak_cols = self.metric == "pass_rate"
        header = (
            f"{'Trial':<6} {'α':<7} {'β':<7} {'γ':<7} {'δ':<7} {'ε':<7} {self.metric_label:<12}"
        )
        if include_tiebreak_cols:
            header += f" {'P@5':<8} {'Tokens':<10}"
        header += f" {'Time(s)':<8}"

        lines = [
            f"\nWeight Tuning Results (metric={self.metric_label}, top-{top_k}):",
            "=" * 120,
            header,
            "-" * 120,
        ]

        for result in sorted_results[:top_k]:
            w = result.weights
            row = (
                f"{result.trial:<6} {w.alpha:<7.3f} {w.beta:<7.3f} {w.gamma:<7.3f} "
                f"{w.delta:<7.3f} {w.epsilon:<7.3f} {result.metric:<12.4f}"
            )
            if include_tiebreak_cols:
                row += (
                    f" {_metric_float(result.metrics, 'precision_at_5', 0.0):<8.4f}"
                    f" {int(_metric_float(result.metrics, 'tokens_surgical', 0.0)):<10}"
                )
            row += f" {result.duration_sec:<8.1f}"
            lines.append(row)

        lines.append("=" * 120)
        best = self.best_result()
        if best:
            lines.append(f"\nBest: {best.weights}")
            lines.append(f"Metric: {best.metric:.4f}")
            lines.append(f"Full metrics: {json.dumps(best.metrics, indent=2)}")

        return "\n".join(lines)


class GridSearchTuner(WeightTuner):
    """Grid search over discrete weight values."""

    def __init__(
        self,
        eval_func: Callable[[RankerWeights], dict],
        alpha_range: Sequence[float] = (0.6, 0.8, 1.0, 1.2),
        beta_range: Sequence[float] = (0.4, 0.6, 0.8, 1.0),
        gamma_range: Sequence[float] = (0.2, 0.4, 0.6),
        delta_range: Sequence[float] = (0.3, 0.5, 0.7),
        epsilon_range: Sequence[float] = (0.3, 0.5, 0.7),
        metric: str = "recall@5",
    ):
        super().__init__(eval_func, metric)
        self.alpha_range = alpha_range
        self.beta_range = beta_range
        self.gamma_range = gamma_range
        self.delta_range = delta_range
        self.epsilon_range = epsilon_range

    def tune(self) -> list[TuneResult]:
        """Exhaustive grid search."""
        trial = 0
        for alpha, beta, gamma, delta, epsilon in itertools.product(
            self.alpha_range,
            self.beta_range,
            self.gamma_range,
            self.delta_range,
            self.epsilon_range,
        ):
            weights = RankerWeights(
                alpha=alpha, beta=beta, gamma=gamma, delta=delta, epsilon=epsilon
            )
            result = self._eval_weights(weights, trial)
            self.results.append(result)
            trial += 1
            print(
                f"[{trial}] α={alpha:.2f} β={beta:.2f} γ={gamma:.2f} "
                f"δ={delta:.2f} ε={epsilon:.2f} → {self.metric}={result.metric:.4f}"
            )

        return self.results


class RandomSearchTuner(WeightTuner):
    """Random search over weight space."""

    def __init__(
        self,
        eval_func: Callable[[RankerWeights], dict],
        metric: str = "recall@5",
        alpha_range: tuple[float, float] = (0.5, 1.5),
        beta_range: tuple[float, float] = (0.3, 1.2),
        gamma_range: tuple[float, float] = (0.1, 0.8),
        delta_range: tuple[float, float] = (0.2, 1.0),
        epsilon_range: tuple[float, float] = (0.2, 1.0),
    ):
        super().__init__(eval_func, metric)
        self.alpha_range = alpha_range
        self.beta_range = beta_range
        self.gamma_range = gamma_range
        self.delta_range = delta_range
        self.epsilon_range = epsilon_range

    def tune(self, n_trials: int = 100) -> list[TuneResult]:
        """Random search over n_trials."""
        import random

        for trial in range(n_trials):
            alpha = random.uniform(*self.alpha_range)
            beta = random.uniform(*self.beta_range)
            gamma = random.uniform(*self.gamma_range)
            delta = random.uniform(*self.delta_range)
            epsilon = random.uniform(*self.epsilon_range)

            weights = RankerWeights(
                alpha=alpha, beta=beta, gamma=gamma, delta=delta, epsilon=epsilon
            )
            result = self._eval_weights(weights, trial)
            self.results.append(result)
            print(
                f"[{trial + 1}/{n_trials}] α={alpha:.2f} β={beta:.2f} γ={gamma:.2f} "
                f"δ={delta:.2f} ε={epsilon:.2f} → {self.metric}={result.metric:.4f}"
            )

        return self.results


class CoarseFineTuner(WeightTuner):
    """Two-phase tuning: coarse grid, then fine grid around best."""

    def __init__(
        self,
        eval_func: Callable[[RankerWeights], dict],
        metric: str = "recall@5",
    ):
        super().__init__(eval_func, metric)

    def tune(self) -> list[TuneResult]:
        """Coarse grid, then fine grid."""
        print("\n=== COARSE GRID SEARCH ===")
        coarse_tuner = GridSearchTuner(
            self.eval_func,
            alpha_range=(0.5, 1.0, 1.5),
            beta_range=(0.4, 0.8, 1.2),
            gamma_range=(0.2, 0.6),
            delta_range=(0.3, 0.7),
            epsilon_range=(0.3, 0.7),
            metric=self.metric,
        )
        coarse_tuner.tune()
        self.results.extend(coarse_tuner.results)

        best_coarse = coarse_tuner.best_result()
        if not best_coarse:
            return self.results

        print(f"\nBest coarse: {best_coarse.weights}")

        # Fine grid around best
        print("\n=== FINE GRID SEARCH ===")
        step = 0.1
        fine_tuner = GridSearchTuner(
            self.eval_func,
            alpha_range=(
                max(0.1, best_coarse.weights.alpha - step),
                best_coarse.weights.alpha,
                min(2.0, best_coarse.weights.alpha + step),
            ),
            beta_range=(
                max(0.1, best_coarse.weights.beta - step),
                best_coarse.weights.beta,
                min(2.0, best_coarse.weights.beta + step),
            ),
            gamma_range=(
                max(0.1, best_coarse.weights.gamma - step),
                best_coarse.weights.gamma,
                min(2.0, best_coarse.weights.gamma + step),
            ),
            delta_range=(
                max(0.1, best_coarse.weights.delta - step),
                best_coarse.weights.delta,
                min(2.0, best_coarse.weights.delta + step),
            ),
            epsilon_range=(
                max(0.1, best_coarse.weights.epsilon - step),
                best_coarse.weights.epsilon,
                min(2.0, best_coarse.weights.epsilon + step),
            ),
            metric=self.metric,
        )
        fine_tuner.tune()
        self.results.extend(fine_tuner.results)

        return self.results


# ---------------------------------------------------------------------------
# CLI: run a tuning sweep against QA/qa_benchmark.
# ---------------------------------------------------------------------------


_METRIC_MAP = {
    **_METRIC_ALIASES,
    "file_recall": "file_recall",
    "pass_rate": "pass_rate",
    "reduction_ratio": "reduction_ratio",
}


def _make_benchmark_eval(
    *,
    questions_path: str | None,
    repo: str | None,
    core12_only: bool,
    project_path: str | None,
    docs_path: str | None,
    workspace_id: str | None,
    repos_root: str | None,
    no_index: bool,
    skip_affects: bool,
    skip_docs: bool,
    metric_key: str,
) -> Callable[[RankerWeights], dict]:
    """Build an eval_func that the tuner can call per weight combination.

    The benchmark is re-run for every trial with ``no_index=True`` after the
    first call so the cost is dominated by retrieval, not indexing.
    """
    import sys
    from pathlib import Path as _Path

    _repo_root = str(_Path(__file__).resolve().parents[2])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

    from QA.qa_benchmark import run_benchmark

    state = {"indexed": no_index}  # once True, subsequent calls skip indexing

    def eval_func(weights: RankerWeights) -> dict:
        metrics = run_benchmark(
            questions_path=questions_path,
            no_index=state["indexed"],
            repo=repo,
            core12_only=core12_only,
            project_path=project_path,
            docs_path=docs_path,
            workspace_id=workspace_id,
            repos_root=repos_root,
            skip_affects=skip_affects,
            skip_docs=skip_docs,
            ranker_weights=weights,
        )
        state["indexed"] = True  # reuse the indexed DB for subsequent trials

        summary = metrics.get("summary", {})
        return {
            metric_key: summary.get(metric_key, 0.0),
            "pass_rate": summary.get("pass_rate", 0.0),
            "recall_at_5": summary.get("recall_at_5", 0.0),
            "precision_at_5": summary.get("precision_at_5", 0.0),
            "file_recall": summary.get("file_recall", 0.0),
            "reduction_ratio": summary.get("reduction_ratio", 0.0),
            "tokens_surgical": summary.get("tokens_surgical", 0),
            "assembly_ms_avg": summary.get("assembly_ms_avg", 0.0),
        }

    return eval_func


def _save_results(tuner: WeightTuner, output_path: str) -> None:
    rows = [
        {
            **asdict(r.weights),
            "metric": r.metric,
            "metrics": r.metrics,
            "trial": r.trial,
            "duration_sec": r.duration_sec,
        }
        for r in tuner.results
    ]
    with open(output_path, "w") as f:
        json.dump(
            {
                "best": asdict(tuner.best_result().weights) if tuner.best_result() else None,
                "results": rows,
            },
            f,
            indent=2,
        )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Tune UnifiedRanker weights via QA benchmark")
    parser.add_argument(
        "--strategy", choices=("grid", "random", "coarse_fine"), default="coarse_fine"
    )
    parser.add_argument("--metric", default="recall@5", choices=sorted(_METRIC_MAP.keys()))
    parser.add_argument("--n-trials", type=int, default=30, help="Trials for random search")
    parser.add_argument("--questions", default=None)
    parser.add_argument("--repo", default=None)
    parser.add_argument("--core12", action="store_true")
    parser.add_argument("--project-path", default=None)
    parser.add_argument("--docs-path", default=None)
    parser.add_argument("--workspace-id", default=None)
    parser.add_argument("--repos-root", default=None)
    parser.add_argument("--no-index", action="store_true", help="Skip initial indexing")
    parser.add_argument("--skip-affects", action="store_true")
    parser.add_argument("--skip-docs", action="store_true", help="Skip docs indexing")
    parser.add_argument("--output", default=None, help="Write full trial log to this JSON path")
    parser.add_argument("--top-k", type=int, default=10)

    args = parser.parse_args()
    metric_key = _METRIC_MAP[args.metric]

    eval_func = _make_benchmark_eval(
        questions_path=args.questions,
        repo=args.repo,
        core12_only=args.core12,
        project_path=args.project_path,
        docs_path=args.docs_path,
        workspace_id=args.workspace_id,
        repos_root=args.repos_root,
        no_index=args.no_index,
        skip_affects=args.skip_affects,
        skip_docs=args.skip_docs,
        metric_key=metric_key,
    )

    if args.strategy == "grid":
        tuner = GridSearchTuner(eval_func, metric=metric_key)
        tuner.tune()
    elif args.strategy == "random":
        tuner = RandomSearchTuner(eval_func, metric=metric_key)
        tuner.tune(n_trials=args.n_trials)
    else:
        tuner = CoarseFineTuner(eval_func, metric=metric_key)
        tuner.tune()

    print(tuner.format_results(top_k=args.top_k))
    if args.output:
        _save_results(tuner, args.output)
        print(f"\nTrial log written to: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
