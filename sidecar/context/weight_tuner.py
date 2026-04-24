"""Weight tuning for UnifiedRanker via grid/random search.

Systematically explores the weight space (α, β, γ, δ, ε) to find the best
combination for a given corpus and evaluation set.

Usage:
    tuner = GridSearchTuner(eval_func=benchmark_eval, metric='recall@5')
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
            metric: key in the returned dict to optimize
            metric_higher_is_better: whether higher metric values are better
        """
        self.eval_func = eval_func
        self.metric = metric
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
        metric_val = metrics.get(self.metric, 0.0)
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
        if self.metric_higher_is_better:
            return max(self.results, key=lambda r: r.metric)
        return min(self.results, key=lambda r: r.metric)

    def format_results(self, top_k: int = 5) -> str:
        """Format results as readable table."""
        if not self.results:
            return "No results yet."

        sorted_results = sorted(
            self.results,
            key=lambda r: r.metric,
            reverse=self.metric_higher_is_better,
        )

        lines = [
            f"\nWeight Tuning Results (metric={self.metric}, top-{top_k}):",
            "=" * 120,
            f"{'Trial':<6} {'α':<7} {'β':<7} {'γ':<7} {'δ':<7} {'ε':<7} {self.metric:<12} {'Time(s)':<8}",
            "-" * 120,
        ]

        for result in sorted_results[:top_k]:
            w = result.weights
            lines.append(
                f"{result.trial:<6} {w.alpha:<7.3f} {w.beta:<7.3f} {w.gamma:<7.3f} "
                f"{w.delta:<7.3f} {w.epsilon:<7.3f} {result.metric:<12.4f} {result.duration_sec:<8.1f}"
            )

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
