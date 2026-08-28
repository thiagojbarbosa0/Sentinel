import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from app.api import app
    with TestClient(app) as c:
        yield c


def test_health_endpoint(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_snapshot_returns_503_before_first_collection(client):
    # scheduler está desabilitado nos testes (SENTINEL_DISABLE_SCHEDULER=1),
    # então latest_snapshot nunca é preenchido — a API deve avisar, não quebrar.
    res = client.get("/snapshot")
    assert res.status_code == 503


def test_autonomy_get_set_roundtrip(client):
    res = client.post("/autonomy", json={"level": 4})
    assert res.status_code == 200
    assert res.json()["autonomy_level"] == 4

    res = client.get("/autonomy")
    assert res.json()["autonomy_level"] == 4


def test_autonomy_rejects_invalid_level(client):
    res = client.post("/autonomy", json={"level": 9})
    assert res.status_code == 400


def test_autonomy_level_persists_in_settings_table(client):
    client.post("/autonomy", json={"level": 1})
    from app import database as db
    assert db.get_setting("autonomy_level") == "1"


def test_recommendation_approve_and_dismiss_flow(client):
    from app import database as db
    rec_id = db.insert_recommendation({
        "ts": time.time(), "anomaly_id": None, "action_type": "run_network_diagnostic",
        "target": "internet", "description": "Diagnóstico de teste", "risk": "NONE",
        "expected_impact": "Informativo", "status": "pending",
    })

    res = client.get("/recommendations?status=pending")
    assert len(res.json()) == 1

    res = client.post(f"/recommendations/{rec_id}/approve")
    assert res.status_code == 200
    assert res.json()["status"] == "executed"

    res = client.get("/recommendations?status=pending")
    assert len(res.json()) == 0

    actions = client.get("/actions").json()
    assert any(a["recommendation_id"] == rec_id for a in actions)


def test_dismiss_marks_recommendation_and_anomaly(client):
    from app import database as db
    anomaly_id = db.insert_anomaly({
        "ts": time.time(), "metric": "cpu", "value": 90, "baseline_mean": 30,
        "baseline_std": 5, "zscore": 12, "severity": "Alta", "diagnosis": "teste",
        "evidence": ["evidencia"], "confidence": 0.8, "status": "diagnosed",
    })
    rec_id = db.insert_recommendation({
        "ts": time.time(), "anomaly_id": anomaly_id, "action_type": "flag_for_review",
        "target": "x", "description": "teste", "risk": "NONE",
        "expected_impact": "-", "status": "pending",
    })
    res = client.post(f"/recommendations/{rec_id}/dismiss")
    assert res.status_code == 200

    anomaly = db.fetch_one("SELECT status FROM anomalies WHERE id=?", (anomaly_id,))
    assert anomaly["status"] == "dismissed"


def test_approve_nonexistent_recommendation_returns_404(client):
    res = client.post("/recommendations/99999/approve")
    assert res.status_code == 404


def test_approve_twice_returns_409(client):
    from app import database as db
    rec_id = db.insert_recommendation({
        "ts": time.time(), "anomaly_id": None, "action_type": "flag_for_review",
        "target": "x", "description": "teste", "risk": "NONE",
        "expected_impact": "-", "status": "pending",
    })
    client.post(f"/recommendations/{rec_id}/approve")
    res = client.post(f"/recommendations/{rec_id}/approve")
    assert res.status_code == 409


def test_api_key_protects_mutating_routes(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "API_KEY", "s3cret")

    res = client.post("/autonomy", json={"level": 2})
    assert res.status_code == 401

    res = client.post("/autonomy", json={"level": 2}, headers={"X-API-Key": "s3cret"})
    assert res.status_code == 200


def test_api_key_does_not_affect_read_routes(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "API_KEY", "s3cret")
    res = client.get("/events")
    assert res.status_code == 200
