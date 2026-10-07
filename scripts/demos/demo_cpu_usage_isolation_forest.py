"""Demo: show how the real Isolation Forest + SLA engine score cpu_usage,
holding the other 8 metrics fixed at normal values, using the ACTUAL
data/v1_baseline_live.csv as the training baseline. No shortcuts -- this
imports and calls the exact same gate.model / gate.sla / gate.scoring code
the production gate uses.

Run from the repo root:
    python scripts/demos/demo_cpu_usage_isolation_forest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gate.dataio import load_baseline
from gate.model import AnomalyModel
from gate.sla import SlaEngine
from gate.scoring import HealthScorer


def main() -> None:
    baseline = load_baseline("data/v1_baseline_live.csv")
    print(f"Baseline rows loaded: {len(baseline)}")

    model = AnomalyModel.train(baseline)
    sla = SlaEngine()
    scorer = HealthScorer(model, sla)

    print()
    print("Internal calibration computed from the baseline rows:")
    print(f"  baseline_mean (avg decision_function of healthy samples): {model._baseline_mean:.4f}")
    print(f"  baseline_min  (least-normal healthy sample)             : {model._baseline_min:.4f}")
    print(f"  tolerance     ((mean - min) * 4.0)                      : {model._tolerance:.4f}")

    # Hold the other 8 metrics fixed at clearly normal values, so only
    # cpu_usage varies across the test points below.
    base_other = dict(
        memory_usage=48.0, pod_restarts=0.0, http_error_rate=0.0,
        p99_latency=8.0, availability=100.0, redis_latency=5.5,
        endpoint_latency=6.0, packet_loss=0.0,
    )

    header = (
        f"{'cpu_usage':>10} | {'raw_fn':>9} | {'deficit':>8} | "
        f"{'anomaly_score':>13} | {'is_anomaly':>10} | {'SLA breach':>10} | "
        f"{'health_score':>12} | decision"
    )
    print()
    print(header)
    print("-" * len(header))

    for cpu in [0.7, 10, 30, 50, 70, 79, 80, 80.1, 81, 90, 95, 100]:
        sample = dict(cpu_usage=float(cpu), **base_other)
        raw = model.raw_score(sample)
        deficit = model._baseline_mean - raw
        anomaly = model.anomaly_health(sample)
        is_anom = model.is_anomaly(sample)
        breach = sla.check_metric("cpu_usage", cpu)
        verdict = scorer.score("demo", sample)
        print(
            f"{cpu:>10} | {raw:>9.4f} | {deficit:>8.4f} | "
            f"{anomaly:>13.1f} | {str(is_anom):>10} | {str(breach):>10} | "
            f"{verdict.health_score:>12} | {verdict.decision}"
        )


if __name__ == "__main__":
    main()
