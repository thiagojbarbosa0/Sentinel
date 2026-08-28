import random

from app.ml_anomaly import MultivariateAnomalyDetector


def _normal_sample(rng: random.Random) -> dict:
    return {
        "cpu": 28 + rng.uniform(-4, 4),
        "ram": 55 + rng.uniform(-3, 3),
        "disk": 60 + rng.uniform(-1, 1),
        "internet_latency_ms": 20 + rng.uniform(-3, 3),
    }


def test_returns_none_before_minimum_samples():
    det = MultivariateAnomalyDetector(min_samples=40)
    rng = random.Random(1)
    for _ in range(30):
        result = det.update_and_score(_normal_sample(rng))
    assert result is None
    assert det.is_trained is False


def test_trains_after_minimum_samples_and_stays_quiet_on_normal_data():
    det = MultivariateAnomalyDetector(min_samples=40, retrain_every=1000)
    rng = random.Random(2)
    flags = []
    for _ in range(120):
        result = det.update_and_score(_normal_sample(rng))
        flags.append(result is not None)
    assert det.is_trained is True
    # ruído puramente gaussiano não deveria disparar muitos falsos positivos
    assert sum(flags) <= 8, f"falsos positivos demais em dado normal: {sum(flags)}"


def test_flags_a_correlated_multivariate_shift_not_extreme_in_any_single_metric():
    det = MultivariateAnomalyDetector(min_samples=40, retrain_every=1000, contamination=0.05)
    rng = random.Random(3)
    for _ in range(150):
        det.update_and_score(_normal_sample(rng))

    # Cada métrica sobe moderadamente (nenhuma seria um z-score extremo
    # isoladamente), mas a COMBINAÇÃO das quatro é rara.
    shifted = {"cpu": 46, "ram": 68, "disk": 63, "internet_latency_ms": 34}
    flags = [det.update_and_score(shifted) is not None for _ in range(5)]
    assert any(flags), "deveria sinalizar ao menos uma vez um desvio correlacionado sustentado"


def test_feature_missing_returns_none_gracefully():
    det = MultivariateAnomalyDetector(min_samples=5)
    for _ in range(10):
        det.update_and_score({"cpu": 20, "ram": 40, "disk": 50, "internet_latency_ms": 10})
    assert det.update_and_score({"cpu": 20, "ram": 40}) is None  # faltam features


def test_retrain_happens_after_configured_interval():
    det = MultivariateAnomalyDetector(min_samples=10, retrain_every=5)
    rng = random.Random(4)
    for _ in range(10):
        det.update_and_score(_normal_sample(rng))
    assert det.is_trained is True
    model_before = det._model
    for _ in range(5):
        det.update_and_score(_normal_sample(rng))
    assert det._model is not model_before, "deveria ter re-treinado após retrain_every novas amostras"
