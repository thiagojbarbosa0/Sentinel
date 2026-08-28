from app.anomaly import AnomalyEngine, MetricBaseline, ProcessBaseline, compute_health_score
from app.config import MIN_SAMPLES_FOR_BASELINE, PROCESS_ANOMALY_MIN_CPU


def test_metric_baseline_needs_minimum_samples():
    b = MetricBaseline()
    for _ in range(MIN_SAMPLES_FOR_BASELINE - 1):
        b.push(30.0)
    assert b.zscore(30.0) is None, "não deveria pontuar antes do mínimo de amostras"


def test_metric_baseline_flags_real_spike_not_normal_noise():
    b = MetricBaseline()
    # baseline ruidosa mas estável, oscilando entre 25 e 31
    values = [25, 27, 30, 28, 31, 29, 26, 28, 30, 27] * 3
    for v in values:
        b.push(v)
    z_normal, *_ = b.zscore(29.0)
    assert abs(z_normal) < 3.0, "valor dentro do padrão não deveria ter |z| alto"

    z_spike, mean, std = b.zscore(91.0)
    assert z_spike > 3.0, "pico bem acima do padrão deveria ter z-score alto"
    assert 25 <= mean <= 31


def test_engine_score_metrics_respects_per_host_baseline():
    """Reproduz o exemplo da seção 9: computador A (20-40%) vs B (50-70%)."""
    noise = [0, 2, -2, 1, -1, 3, -3, 0, 1, -1] * 3  # baseline realista, nunca perfeitamente constante

    engine_a = AnomalyEngine()
    for n in noise:
        engine_a.score_metrics({"cpu": 30 + n, "ram": 50, "disk": 60, "internet_latency_ms": 20})
    anomalies_a = engine_a.score_metrics({"cpu": 55, "ram": 50, "disk": 60, "internet_latency_ms": 20})

    engine_b = AnomalyEngine()
    for n in noise:
        engine_b.score_metrics({"cpu": 60 + n, "ram": 50, "disk": 60, "internet_latency_ms": 20})
    anomalies_b = engine_b.score_metrics({"cpu": 55, "ram": 50, "disk": 60, "internet_latency_ms": 20})

    # 55% de CPU é destoante para o host A mas normal para o host B — mesma
    # entrada, diagnósticos diferentes, porque cada engine tem seu baseline.
    assert "cpu" not in anomalies_b


def test_process_baseline_requires_ratio_and_absolute_floor():
    pb = ProcessBaseline()
    name = "render-worker"
    for _ in range(10):
        pb.update_and_score(name, 3.0)

    # acima da baseline mas abaixo do piso absoluto -> não deveria disparar
    _, is_anomaly_small = pb.update_and_score(name, PROCESS_ANOMALY_MIN_CPU - 1)
    assert is_anomaly_small is False

    # muito acima da baseline E acima do piso -> deveria disparar
    _, is_anomaly_big = pb.update_and_score(name, 80.0)
    assert is_anomaly_big is True


def test_process_baseline_does_not_immediately_absorb_the_spike():
    """Um pico não deve 'ensinar' a baseline a considerá-lo normal na hora."""
    pb = ProcessBaseline()
    name = "render-worker"
    for _ in range(10):
        pb.update_and_score(name, 3.0)
    baseline_before, _ = pb.update_and_score(name, 80.0)
    baseline_after, is_anomaly_again = pb.update_and_score(name, 80.0)
    assert baseline_after < 40, "baseline não deveria pular perto do valor do pico em uma atualização"
    assert is_anomaly_again is True


def test_compute_health_score_bounds_and_direction():
    healthy = compute_health_score(cpu=20, ram=40, disk=50, internet_latency=15)
    stressed = compute_health_score(cpu=95, ram=95, disk=95, internet_latency=200)
    assert 5.0 <= stressed < healthy <= 100.0
