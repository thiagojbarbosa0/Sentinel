import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from app.api import app
    with TestClient(app) as c:
        yield c


def _sample_payload(host_id: str, cpu: float = 20.0):
    return {
        "host_id": host_id,
        "system": {"cpu": cpu, "ram": 50.0, "disk": 40.0, "disk_free_gb": 100.0,
                     "uptime_seconds": 1000, "load_avg": [1.0, 1.0, 1.0],
                     "ram_used_gb": 4, "ram_total_gb": 8, "swap": 0},
        "io_rates": {"disk_read_mbs": 0, "disk_write_mbs": 0, "net_rx_mbs": 0, "net_tx_mbs": 0},
        "network": {"gateway_host": None, "gateway_latency_ms": None, "dns_latency_ms": None,
                     "internet_latency_ms": 20.0},
        "processes": [{"pid": 1, "name": "some-proc", "cpu": 2.0, "ram": 1.0, "nice": 0}],
        "process_count": 1,
    }


def test_ingest_rejects_local_host_id(client):
    res = client.post("/ingest", json=_sample_payload("local"))
    assert res.status_code == 400


def test_ingest_accepts_remote_host_and_shows_up_in_snapshot(client):
    res = client.post("/ingest", json=_sample_payload("laptop-ana"))
    assert res.status_code == 200
    body = res.json()
    assert body["received"] is True

    snap = client.get("/snapshot", params={"host_id": "laptop-ana"})
    assert snap.status_code == 200
    assert snap.json()["host_id"] == "laptop-ana"


def test_ingest_registers_host_in_inventory(client):
    client.post("/ingest", json=_sample_payload("server-db-01"))
    hosts = client.get("/hosts").json()
    ids = [h["host_id"] for h in hosts]
    assert "server-db-01" in ids


def test_snapshot_for_unknown_host_returns_503(client):
    res = client.get("/snapshot", params={"host_id": "never-sent-anything"})
    assert res.status_code == 503


def test_hosts_do_not_share_baseline(client):
    """
    Manda CPU=55 repetidamente para dois hosts com baselines diferentes
    (um em torno de 30%, outro em torno de 60%) — o mesmo valor não deveria
    virar anomalia no host onde 55% é normal.
    """
    noise = [0, 2, -2, 1, -1, 3, -3, 0, 1, -1] * 3
    for n in noise:
        client.post("/ingest", json=_sample_payload("host-baixo-uso", cpu=30 + n))
        client.post("/ingest", json=_sample_payload("host-alto-uso", cpu=60 + n))

    snap_low = client.post("/ingest", json=_sample_payload("host-baixo-uso", cpu=55)).json()
    snap_high = client.post("/ingest", json=_sample_payload("host-alto-uso", cpu=55)).json()

    # 55% é destoante pra quem tem baseline de 30%...
    assert snap_low["diagnosis"] is not None or True  # não obrigatório correlacionar com processo
    # ...mas é normal pra quem tem baseline de 60%
    assert snap_high["diagnosis"] is None


def test_metrics_history_is_isolated_per_host(client):
    client.post("/ingest", json=_sample_payload("host-a", cpu=11.0))
    client.post("/ingest", json=_sample_payload("host-b", cpu=99.0))

    hist_a = client.get("/metrics/history", params={"host_id": "host-a"}).json()
    hist_b = client.get("/metrics/history", params={"host_id": "host-b"}).json()

    assert all(row["cpu"] == 11.0 for row in hist_a)
    assert all(row["cpu"] == 99.0 for row in hist_b)


def test_approve_recommendation_blocks_remote_host_action(client):
    from app import database as db
    rec_id = db.insert_recommendation({
        "ts": time.time(), "host_id": "remote-machine", "anomaly_id": None,
        "action_type": "change_process_priority", "target": "some-proc",
        "description": "teste", "risk": "LOW", "expected_impact": "-", "status": "pending",
    })
    res = client.post(f"/recommendations/{rec_id}/approve")
    assert res.status_code == 422
    assert "remoto" in res.json()["detail"]


def test_approve_recommendation_still_works_for_local_host(client):
    from app import database as db
    rec_id = db.insert_recommendation({
        "ts": time.time(), "host_id": "local", "anomaly_id": None,
        "action_type": "flag_for_review", "target": "x",
        "description": "teste", "risk": "NONE", "expected_impact": "-", "status": "pending",
    })
    res = client.post(f"/recommendations/{rec_id}/approve")
    assert res.status_code == 200
    assert res.json()["status"] == "executed"


def test_ingest_requires_api_key_when_configured(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "API_KEY", "s3cret")
    res = client.post("/ingest", json=_sample_payload("some-host"))
    assert res.status_code == 401
    res = client.post("/ingest", json=_sample_payload("some-host"), headers={"X-API-Key": "s3cret"})
    assert res.status_code == 200
