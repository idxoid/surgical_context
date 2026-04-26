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
