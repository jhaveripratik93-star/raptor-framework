"""Demo: correlated multi-metric drift that never crosses a single SLA line.

This is the scenario signal 1 (the Isolation Forest) is actually good at,
as distinct from signal 3 (gate/signals/trend.py, needs a metric to actually
CROSS its SLA repeatedly) and signal 4 (gate/signals/volatility.py, needs raw
per-tick data inside one window). Here, NO metric ever breaches its own
threshold -- a naive SLA-only check would PROMOTE every single deployment.

The scenario: a slow, memory-leak-style degradation. memory_usage climbs,
and because of a shared root cause (GC pressure stalling request handling,
contention spilling into the cache client), p99_latency, endpoint_latency,
redis_latency, cpu_usage, http_error_rate, and packet_loss ALL drift upward
together, deployment after deployment -- while availability drifts down.
Every individual value stays under (or over, for availability's >= rule)
its own SLA line throughout. That joint, correlated movement across 6+ of
the 9 metrics at once is exactly the shape a per-metric threshold check
cannot see, and exactly what Isolation Forest training on
data/v1_baseline.csv (30 real, correlated rows -- NOT the thin 10-row live
baseline) is positioned to catch: each new sample lands further and further
from the dense region the healthy baseline actually occupied.

Run from the repo root:
    python scripts/demos/demo_correlated_drift.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gate.dataio import load_baseline
from gate.model import AnomalyModel
from gate.sla import SlaEngine
from gate.scoring import HealthScorer

BASELINE_PATH = "data/v1_baseline.csv"

# Six successive "deployments" of the same component. Each row is a
# complete, independent 9-metric sample -- memory_usage, p99_latency,
# endpoint_latency, redis_latency, cpu_usage, and http_error_rate/
# packet_loss all drift upward together; availability drifts down.
# Compare each column's SLA threshold (gate/metrics.py) against the max
# value it ever reaches below -- not one column ever crosses its line.
TIMELINE = [
    # cpu,  mem,  restarts, err,  p99,  avail, redis, endpoint, loss
    dict(cpu_usage=43,  memory_usage=51, pod_restarts=0, http_error_rate=0.10,
         p99_latency=81,  availability=99.90, redis_latency=3.0, endpoint_latency=86,  packet_loss=0.10),
    dict(cpu_usage=45,  memory_usage=55, pod_restarts=0, http_error_rate=0.15,
         p99_latency=95,  availability=99.85, redis_latency=4.0, endpoint_latency=95,  packet_loss=0.12),
    dict(cpu_usage=48,  memory_usage=60, pod_restarts=0, http_error_rate=0.20,
         p99_latency=110, availability=99.80, redis_latency=5.0, endpoint_latency=105, packet_loss=0.15),
    dict(cpu_usage=52,  memory_usage=65, pod_restarts=0, http_error_rate=0.30,
         p99_latency=130, availability=99.75, redis_latency=6.5, endpoint_latency=120, packet_loss=0.20),
    dict(cpu_usage=58,  memory_usage=70, pod_restarts=1, http_error_rate=0.40,
         p99_latency=155, availability=99.70, redis_latency=8.0, endpoint_latency=145, packet_loss=0.30),
    dict(cpu_usage=65,  memory_usage=73, pod_restarts=1, http_error_rate=0.50,
         p99_latency=180, availability=99.60, redis_latency=9.0, endpoint_latency=170, packet_loss=0.40),
]

SLA_LIMITS = dict(
    cpu_usage="<=80", memory_usage="<=75", pod_restarts="<=3",
    http_error_rate="<=1", p99_latency="<=200", availability=">=99.5",
    redis_latency="<=10", endpoint_latency="<=200", packet_loss="<=1",
)


def main() -> None:
    baseline = load_baseline(BASELINE_PATH)
    print(f"Baseline: {len(baseline)} rows loaded from {BASELINE_PATH} "
          f"(real variance across cpu/mem/latency, NOT a single-point baseline)")

    model = AnomalyModel.train(baseline)
    sla = SlaEngine()
    scorer = HealthScorer(model, sla)  # signal 1 + 2 only -- no trend/volatility overrides

    print()
    print("SLA thresholds (for reference -- watch NONE of these ever get crossed below):")
    for name, limit in SLA_LIMITS.items():
        print(f"  {name:<18s} {limit}")

    header = (f"{'deploy #':>8} | {'mem%':>5} | {'p99ms':>6} | {'redisms':>8} | "
              f"{'endms':>6} | {'avail%':>7} | {'SLA breaches':>13} | "
              f"{'anomaly':>8} | {'health':>7} | naive  | gate")
    print()
    print(header)
    print("-" * len(header))

    naive_all_promote = True
    first_rollback = None
    for i, sample in enumerate(TIMELINE, start=1):
        violations = sla.evaluate(sample)
        naive_decision = "PROMOTE" if not violations else "ROLLBACK"
        if naive_decision != "PROMOTE":
            naive_all_promote = False

        verdict = scorer.score("v2-memleak", sample)
        if verdict.decision == "ROLLBACK" and first_rollback is None:
            first_rollback = i

        print(f"{i:>8} | {sample['memory_usage']:>5} | {sample['p99_latency']:>6} | "
              f"{sample['redis_latency']:>8} | {sample['endpoint_latency']:>6} | "
              f"{sample['availability']:>7} | {len(violations):>13} | "
              f"{verdict.anomaly_score:>8.1f} | {verdict.health_score:>7.1f} | "
              f"{naive_decision:<6} | {verdict.decision}")

    print()
    if naive_all_promote:
        print("Every deployment above has ZERO SLA breaches -- a naive SLA-only "
              "monitor would PROMOTE all 6, every time, with no warning at all.")
    if first_rollback is not None:
        print(f"The real gate (Isolation Forest + SLA) first ROLLBACKs at deployment "
              f"{first_rollback} -- purely because the joint shape of the sample "
              f"(memory, latency, redis, cpu all drifting together) stopped looking "
              f"like anything in the healthy baseline, well before any single metric "
              f"crossed its own threshold.")
    else:
        print("The real gate never rolled back either in this run -- see the anomaly "
              "score column: if it stayed near 100 throughout, the baseline's own "
              "variance was wide enough to absorb this drift. Try steeper per-step "
              "increases in TIMELINE above to push it further from the baseline.")


if __name__ == "__main__":
    main()
