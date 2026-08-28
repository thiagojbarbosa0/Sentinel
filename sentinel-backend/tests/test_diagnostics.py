from app.diagnostics import diagnose, recommend


def test_diagnose_cpu_spike_correlates_with_process():
    metric_anomalies = {"cpu": {"value": 92.0, "baseline_mean": 28.0, "baseline_std": 4.0,
                                  "zscore": 16.0, "severity": "Alta"}}
    scored_processes = [
        {"pid": 111, "name": "render-worker", "cpu": 63.0, "ram": 15.0, "baseline_cpu": 3.0, "is_anomaly": True},
        {"pid": 222, "name": "Xorg", "cpu": 4.0, "ram": 11.0, "baseline_cpu": 4.0, "is_anomaly": False},
    ]
    diag = diagnose(metric_anomalies, scored_processes, network={}, disk_free_gb=100)
    assert diag is not None
    assert diag["type"] == "process_cpu_spike"
    assert diag["target"] == "render-worker"
    assert 0.0 < diag["confidence"] <= 0.97
    assert any("render-worker" in e for e in diag["evidence"])


def test_diagnose_picks_the_biggest_anomalous_process_when_several_spike():
    metric_anomalies = {"cpu": {"value": 92.0, "baseline_mean": 28.0, "baseline_std": 4.0,
                                  "zscore": 16.0, "severity": "Alta"}}
    scored_processes = [
        {"pid": 1, "name": "proc-a", "cpu": 40.0, "ram": 5, "baseline_cpu": 2.0, "is_anomaly": True},
        {"pid": 2, "name": "proc-b", "cpu": 70.0, "ram": 5, "baseline_cpu": 2.0, "is_anomaly": True},
    ]
    diag = diagnose(metric_anomalies, scored_processes, network={}, disk_free_gb=100)
    assert diag["target"] == "proc-b"


def test_diagnose_returns_none_without_correlatable_cause():
    # CPU anômalo mas nenhum processo isolado explica — sem regra 1 aplicável,
    # e como RAM não está entre as métricas anômalas, a regra 2 também não bate.
    metric_anomalies = {"cpu": {"value": 92.0, "baseline_mean": 28.0, "baseline_std": 4.0,
                                  "zscore": 16.0, "severity": "Alta"}}
    diag = diagnose(metric_anomalies, scored_processes=[], network={}, disk_free_gb=100)
    assert diag is None


def test_diagnose_correlated_deviation_from_ml_signal_alone():
    """Nenhuma métrica cruzou o z-score, mas o ML sinaliza a combinação delas."""
    ml_signal = {
        "is_anomaly": True, "anomaly_score": 0.6,
        "features": {"cpu": 46.0, "ram": 68.0, "disk": 63.0, "internet_latency_ms": 34.0},
    }
    diag = diagnose(metric_anomalies={}, scored_processes=[], network={}, disk_free_gb=100, ml_signal=ml_signal)
    assert diag is not None
    assert diag["type"] == "correlated_deviation"
    assert 0.4 <= diag["confidence"] <= 0.75


def test_diagnose_ml_signal_is_ignored_when_a_metric_rule_already_explains_it():
    """Regra 1-4 tem prioridade — ML só entra quando mais nada explica."""
    metric_anomalies = {"cpu": {"value": 92.0, "baseline_mean": 28.0, "baseline_std": 4.0,
                                  "zscore": 16.0, "severity": "Alta"}}
    scored_processes = [
        {"pid": 111, "name": "render-worker", "cpu": 63.0, "ram": 15.0, "baseline_cpu": 3.0, "is_anomaly": True},
    ]
    ml_signal = {"is_anomaly": True, "anomaly_score": 0.6, "features": {}}
    diag = diagnose(metric_anomalies, scored_processes, network={}, disk_free_gb=100, ml_signal=ml_signal)
    assert diag["type"] == "process_cpu_spike"
    # e a confiança deveria ser reforçada pela concordância do ML
    assert any("Isolation Forest" in e for e in diag["evidence"])


def test_diagnose_disk_low_space_overrides_regardless_of_metric_anomalies():
    diag = diagnose({}, [], network={}, disk_free_gb=1.2)
    assert diag["type"] == "disk_low_space"
    assert diag["severity"] == "Alta"


def test_diagnose_external_network_latency_when_local_hops_are_healthy():
    metric_anomalies = {"internet_latency_ms": {"value": 180.0, "baseline_mean": 20.0,
                                                   "baseline_std": 5.0, "zscore": 32.0, "severity": "Alta"}}
    network = {"gateway_latency_ms": 2.0, "dns_latency_ms": 4.0}
    diag = diagnose(metric_anomalies, [], network, disk_free_gb=100)
    assert diag["type"] == "external_network_latency"


def test_diagnose_no_external_network_diagnosis_when_gateway_is_also_slow():
    """Se o gateway local também está degradado, a causa não é 'externa'."""
    metric_anomalies = {"internet_latency_ms": {"value": 180.0, "baseline_mean": 20.0,
                                                   "baseline_std": 5.0, "zscore": 32.0, "severity": "Alta"}}
    network = {"gateway_latency_ms": 120.0, "dns_latency_ms": 4.0}
    diag = diagnose(metric_anomalies, [], network, disk_free_gb=100)
    assert diag is None


def test_recommend_maps_every_known_diagnosis_type():
    expected_actions = {
        "process_cpu_spike": "change_process_priority",
        "memory_growth": "flag_for_review",
        "disk_low_space": "cleanup_temp_files",
        "external_network_latency": "run_network_diagnostic",
        "correlated_deviation": "flag_for_review",
    }
    for diag_type, action_type in expected_actions.items():
        rec = recommend({"type": diag_type, "target": "x", "cause": "x"})
        assert rec["action_type"] == action_type
        assert rec["status"] == "pending"


def test_recommend_returns_none_for_unknown_diagnosis_type():
    assert recommend({"type": "something_undefined", "target": "x", "cause": "x"}) is None
