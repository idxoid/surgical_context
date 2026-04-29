from sidecar.context.weight_tuner import GridSearchTuner


def test_weight_tuner_accepts_recall_alias_for_direct_api_use():
    def eval_func(_weights):
        return {"recall_at_5": 0.75, "pass_rate": 0.5}

    tuner = GridSearchTuner(
        eval_func,
        metric="recall@5",
        alpha_range=(1.0,),
        beta_range=(1.0,),
        gamma_range=(1.0,),
        delta_range=(1.0,),
        epsilon_range=(1.0,),
    )

    tuner.tune()
    best = tuner.best_result()

    assert best is not None
    assert best.metric == 0.75


def test_weight_tuner_breaks_pass_rate_ties_by_precision_then_tokens():
    def eval_func(weights):
        alpha = round(weights.alpha, 1)
        if alpha == 0.1:
            return {"pass_rate": 1.0, "precision_at_5": 0.4, "tokens_surgical": 1000}
        if alpha == 0.2:
            return {"pass_rate": 1.0, "precision_at_5": 0.6, "tokens_surgical": 5000}
        if alpha == 0.3:
            return {"pass_rate": 1.0, "precision_at_5": 0.6, "tokens_surgical": 2000}
        return {"pass_rate": 0.75, "precision_at_5": 0.9, "tokens_surgical": 100}

    tuner = GridSearchTuner(
        eval_func,
        metric="pass_rate",
        alpha_range=(0.1, 0.2, 0.3, 0.4),
        beta_range=(1.0,),
        gamma_range=(1.0,),
        delta_range=(1.0,),
        epsilon_range=(1.0,),
    )

    tuner.tune()
    best = tuner.best_result()

    assert best is not None
    assert best.metric == 1.0
    assert best.weights.alpha == 0.3
    assert best.metrics["precision_at_5"] == 0.6
    assert best.metrics["tokens_surgical"] == 2000

    formatted = tuner.format_results(top_k=2)
    assert "P@5" in formatted
    assert "Tokens" in formatted
