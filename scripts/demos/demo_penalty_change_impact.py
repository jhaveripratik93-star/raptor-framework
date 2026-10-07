"""Demo: find and show a concrete case where changing SLA_BREACH_PENALTY from
20 to 30 actually flips the PROMOTE/ROLLBACK decision, using the real
HealthScorer math (not simulated).

Run from the repo root:
    python scripts/demos/demo_penalty_change_impact.py
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

base_other = dict(
    cpu_usage=0.75, memory_usage=48.0, pod_restarts=0.0, http_error_rate=0.0,
    p99_latency=8.0, availability=100.0, endpoint_latency=6.0, packet_loss=0.0,
)

# Search for a redis_latency value that (a) does NOT breach its own 10ms SLA,
# (b) nudges the sample enough that the Isolation Forest reports an
# anomaly_score below 100 (since this baseline's redis_latency values range
# ~4-9.4ms, going toward the edge of that range should move the score).
print(f"{'redis_latency':>13} | {'anomaly_score':>13} | {'SLA breach?':>11}")
print("-" * 45)
candidate = None
for rl in [5.0, 7.0, 8.5, 9.0, 9.4, 9.49, 9.499]:
    sample = dict(redis_latency=rl, **base_other)
    anomaly = model.anomaly_health(sample)
    breach = sla.check_metric("redis_latency", rl)
    print(f"{rl:>13} | {anomaly:>13.1f} | {str(breach):>11}")
    if anomaly < 100.0 and not breach and candidate is None:
        candidate = (rl, sample, anomaly)

print()
if candidate is None:
    print("No candidate found with anomaly_score < 100 and no SLA breach on "
          "redis_latency within the tried range; this baseline may be too "
          "tight/saturated to demonstrate a flip this way.")
else:
    rl, sample, anomaly = candidate
    print(f"Using redis_latency={rl} -> anomaly_score={anomaly}")
    print("Now adding exactly ONE SLA breach via cpu_usage=81 (no change to "
          "anomaly_score, since cpu_usage is saturated in this baseline -- "
          "see demo_cpu_usage_isolation_forest.py):")
    sample["cpu_usage"] = 81.0
    anomaly2 = model.anomaly_health(sample)
    violations = sla.evaluate(sample)
    n = len(violations)
    print(f"  anomaly_score (unchanged) = {anomaly2}")
    print(f"  SLA breaches = {n} ({[v.metric for v in violations]})")
    print()
    for penalty in (20, 30):
        scorer = HealthScorer(model, sla, breach_penalty=penalty)
        verdict = scorer.score("demo", sample)
        print(f"  SLA_BREACH_PENALTY={penalty}: health = {anomaly2} - "
              f"({penalty} x {n}) = {verdict.health_score}  -> {verdict.decision}")
