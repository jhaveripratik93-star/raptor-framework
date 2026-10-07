"""Live end-to-end proof: ingest the user's two example sequences through the
real FastAPI app's /ingest + /score endpoints (via TestClient, no server
process needed) and show the window-volatility signal firing correctly.

Run from the repo root:
    python scripts/demos/demo_volatility_live.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi.testclient import TestClient
from gate.dataio import load_baseline
from gate.model import AnomalyModel
from gate.serving.app import create_app

model = AnomalyModel.train(load_baseline("data/v1_baseline.csv"))
app = create_app(model=model)  # no Redis -> memory-only store, that's fine
client = TestClient(app)

OTHER = dict(
    memory_usage=51.0, pod_restarts=0.0, http_error_rate=0.1,
    p99_latency=81.0, availability=99.9, redis_latency=3.0,
    endpoint_latency=86.0, packet_loss=0.1,
)


def run_sequence(label, component, seq):
    print(f"\n=== {label} ===")
    print(f"Raw cpu_usage ticks (10s apart): {seq}")
    for i, v in enumerate(seq):
        client.post("/ingest", json={
            "component": component,
            "sample": {"cpu_usage": float(v), **OTHER},
        })
    resp = client.post("/score", json={"component": component})
    body = resp.json()
    print(f"Aggregated cpu_usage (Mean, via /features): "
          f"{client.get(f'/features/{component}').json()['features']['cpu_usage']:.3f}")
    print(f"decision={body['decision']}  health_score={body['health_score']}  "
          f"forced_rollback={body['forced_rollback']}")
    if body["window_breaches"]:
        for w in body["window_breaches"]:
            print(f"  -> WINDOW BREACH: {w['detail']}")
    else:
        print("  -> no window breaches flagged")


run_sequence("Sequence 1", "svc-seq1", [10, 30, 80.2, 80.6, 40, 50, 60])
run_sequence("Sequence 2", "svc-seq2", [50, 50, 80.2, 80.6, 40, 90, 90])
