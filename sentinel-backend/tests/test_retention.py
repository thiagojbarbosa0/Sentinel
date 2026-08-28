import time

from app import database as db


def _insert_metric_at(ts: float):
    db.insert_metric({
        "ts": ts, "cpu": 10, "ram": 20, "disk": 30, "disk_read": 0, "disk_write": 0,
        "net_rx": 0, "net_tx": 0, "net_latency_gateway": None, "net_latency_dns": None,
        "net_latency_internet": None, "process_count": 5, "health_score": 90,
    })


def test_purge_removes_metrics_older_than_cutoff():
    now = time.time()
    _insert_metric_at(now - 20 * 86400)  # 20 dias atrás — deveria ser removido
    _insert_metric_at(now - 1 * 86400)   # 1 dia atrás — deveria ficar

    deleted = db.purge_old_data(metrics_cutoff_ts=now - 14 * 86400, events_cutoff_ts=now - 90 * 86400)

    assert deleted["metrics"] == 1
    remaining = db.fetch_all("SELECT * FROM metrics")
    assert len(remaining) == 1


def test_purge_never_removes_open_anomalies_or_pending_recommendations():
    now = time.time()
    old_ts = now - 200 * 86400

    anomaly_id = db.insert_anomaly({
        "ts": old_ts, "metric": "cpu", "value": 90, "baseline_mean": 30, "baseline_std": 5,
        "zscore": 12, "severity": "Alta", "diagnosis": "teste", "evidence": ["e"],
        "confidence": 0.8, "status": "diagnosed",  # em aberto!
    })
    rec_id = db.insert_recommendation({
        "ts": old_ts, "anomaly_id": anomaly_id, "action_type": "flag_for_review",
        "target": "x", "description": "teste", "risk": "NONE",
        "expected_impact": "-", "status": "pending",  # em aberto!
    })

    deleted = db.purge_old_data(metrics_cutoff_ts=now - 14 * 86400, events_cutoff_ts=now - 90 * 86400)

    assert deleted["anomalies"] == 0
    assert deleted["recommendations"] == 0
    assert db.fetch_one("SELECT * FROM anomalies WHERE id=?", (anomaly_id,)) is not None
    assert db.fetch_one("SELECT * FROM recommendations WHERE id=?", (rec_id,)) is not None


def test_purge_removes_resolved_anomalies_past_cutoff():
    now = time.time()
    old_ts = now - 200 * 86400
    anomaly_id = db.insert_anomaly({
        "ts": old_ts, "metric": "cpu", "value": 90, "baseline_mean": 30, "baseline_std": 5,
        "zscore": 12, "severity": "Alta", "diagnosis": "teste", "evidence": ["e"],
        "confidence": 0.8, "status": "resolved",
    })
    deleted = db.purge_old_data(metrics_cutoff_ts=now - 14 * 86400, events_cutoff_ts=now - 90 * 86400)
    assert deleted["anomalies"] == 1
    assert db.fetch_one("SELECT * FROM anomalies WHERE id=?", (anomaly_id,)) is None


def test_storage_stats_reports_counts_and_size():
    _insert_metric_at(time.time())
    stats = db.storage_stats()
    assert stats["row_counts"]["metrics"] == 1
    assert stats["db_size_bytes"] > 0
    assert stats["location"]
    assert stats["backend"] in ("sqlite", "postgresql")
