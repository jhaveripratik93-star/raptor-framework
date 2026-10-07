"""Demo: does the gate notice a metric that is steadily climbing release over
release, even while every individual reading stays under its SLA threshold?

Simulates 6 successive "deployments" where memory_usage climbs
40 -> 50 -> 60 -> 65 -> 70 -> 74 -> 76 (SLA limit is 75%), scoring each one
independently through the real HealthScorer, exactly as production mode
would -- one aggregator.collect() call per deployment, no memory of the
previous call.

Run from the repo root:
    python scripts/demos/demo_trend_blindspot.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gate.dataio import load_baseline
from gate.model import AnomalyModel
from gate.sla import SlaEngine
from gate.scoring import HealthScorer

baseline = load_baseline("data/v1_baseline_live.csv")
model = AnomalyModel.train(baseline)
sla = SlaEngine()
scorer = HealthScorer(model, sla)

base_other = dict(
    cpu_usage=0.7, pod_restarts=0.0, http_error_rate=0.0,
    p99_latency=8.0, availability=100.0, redis_latency=5.5,
    endpoint_latency=6.0, packet_loss=0.0,
)

# Each entry = one separate deployment's memory_usage reading, in order.
timeline = [40, 50, 60, 65, 70, 74, 76]

print("Simulating 7 SEPARATE deployments over time (memory_usage climbing).")
print("Each one is scored completely independently -- the gate has no idea")
print("what any previous deployment's reading was.\n")

header = f"{'deploy #':>8} | {'memory_usage':>12} | {'anomaly_score':>13} | {'SLA breach':>10} | {'health_score':>12} | decision"
print(header)
print("-" * len(header))

for i, mem in enumerate(timeline, start=1):
    sample = dict(memory_usage=float(mem), **base_other)
    anomaly = model.anomaly_health(sample)
    breach = sla.check_metric("memory_usage", mem)
    verdict = scorer.score(f"deploy-{i}", sample)
    print(f"{i:>8} | {mem:>12} | {anomaly:>13.1f} | {str(breach):>10} | "
          f"{verdict.health_score:>12} | {verdict.decision}")

print()
print("Notice: deployments 1-6 all PROMOTE, even though memory climbed from")
print("40% to 74% across them -- a textbook slow-leak pattern. The gate only")
print("reacts at deployment 7, the instant the value crosses 75%. It had no")
print("mechanism to flag the trend itself during deployments 1-6, because")
print("each score() call only ever sees ONE isolated snapshot.")
