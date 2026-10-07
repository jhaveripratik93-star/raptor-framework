"""Tests for the Phase 4 FastAPI serving app, using the TestClient.

The trained model is injected directly so no artifact file or infra is needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gate.dataio import load_baseline
from gate.model import AnomalyModel
from gate.serving.app import create_app

DATA = Path(__file__).resolve().parent.parent / "data"

HEALTHY = {
    "cpu_usage": 46, "memory_usage": 53, "pod_restarts": 0,
    "http_error_rate": 0.2, "p99_latency": 90, "availability": 99.8,
    "redis_latency": 3, "endpoint_latency": 88, "packet_loss": 0.15,
}
DEGRADED = {
    "cpu_usage": 78, "memory_usage": 82, "pod_restarts": 4,
    "http_error_rate": 2.8, "p99_latency": 380, "availability": 97.5,
    "redis_latency": 18, "endpoint_latency": 410, "packet_loss": 1.8,
}


@pytest.fixture(scope="module")
def client():
    model = AnomalyModel.train(load_baseline(DATA / "v1_baseline.csv"))
    app = create_app(model=model)
    return TestClient(app)


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["model_loaded"] is True
    assert body["status"] == "ok"
    assert len(body["features"]) == 9


def test_score_healthy_promotes(client):
    r = client.post("/score", json={"component": "v2", "sample": HEALTHY})
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "PROMOTE"
    assert body["health_score"] >= 70


def test_score_degraded_rolls_back(client):
    r = client.post("/score", json={"component": "v2-degraded", "sample": DEGRADED})
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "ROLLBACK"
    assert len(body["violations"]) == 8


def test_score_missing_metrics_422(client):
    r = client.post("/score", json={"component": "v2", "sample": {"cpu_usage": 5}})
    assert r.status_code == 422


def test_ingest_then_score_from_store(client):
    # Ingest a healthy sample, then score without an inline sample.
    r = client.post("/ingest", json={"component": "svcA", "sample": HEALTHY})
    assert r.status_code == 200
    r = client.post("/score", json={"component": "svcA"})
    assert r.status_code == 200
    assert r.json()["decision"] == "PROMOTE"


def test_features_endpoint(client):
    client.post("/ingest", json={"component": "svcB", "sample": HEALTHY})
    r = client.get("/features/svcB")
    assert r.status_code == 200
    body = r.json()
    assert body["component"] == "svcB"
    assert set(body["features"]) == set(HEALTHY)


def test_score_without_model_returns_503():
    # No model injected and no artifact -> scorer unavailable.
    import os
    os.environ["GATE_MODEL_PATH"] = "does/not/exist.joblib"
    app = create_app()  # model load fails -> degraded
    client = TestClient(app)
    r = client.post("/score", json={"component": "v2", "sample": HEALTHY})
    assert r.status_code == 503
    os.environ.pop("GATE_MODEL_PATH", None)
