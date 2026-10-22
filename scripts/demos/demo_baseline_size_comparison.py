"""Demo: does a bigger, more varied baseline fix the "is_anomaly=True on
everything" noise seen with the thin 10-row data/v1_baseline_live.csv?

Trains two Isolation Forests -- one on the original 10-row baseline, one on
a freshly captured 30-row baseline from the same live service -- then scores
the SAME set of plainly-normal samples against both, side by side.

Run from the repo root:
    python scripts/demos/demo_baseline_size_comparison.py \
        --thin data/v1_baseline_live.csv \
        --wide data/v1_baseline_live_30row.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gate.dataio import load_baseline
from gate.model import AnomalyModel
from gate.sla import SlaEngine

# Plainly normal, varied samples -- same shape as what a healthy service
# actually produces (not hand-picked to flatter either baseline).
TEST_SAMPLES = [
    dict(cpu_usage=0.60, memory_usage=22.0, pod_restarts=0, http_error_rate=0.0,
         p99_latency=20.0, availability=100.0, redis_latency=0.4, endpoint_latency=5.0, packet_loss=0.0),
    dict(cpu_usage=0.65, memory_usage=28.0, pod_restarts=0, http_error_rate=0.0,
         p99_latency=35.0, availability=100.0, redis_latency=0.35, endpoint_latency=7.0, packet_loss=0.0),
    dict(cpu_usage=0.70, memory_usage=31.0, pod_restarts=0, http_error_rate=0.0,
         p99_latency=10.0, availability=100.0, redis_latency=0.30, endpoint_latency=4.0, packet_loss=0.0),
    dict(cpu_usage=0.58, memory_usage=25.0, pod_restarts=0, http_error_rate=0.0,
         p99_latency=45.0, availability=100.0, redis_latency=0.50, endpoint_latency=8.5, packet_loss=0.0),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare thin vs wide baseline anomaly noise")
    parser.add_argument("--thin", default="data/v1_baseline_live.csv")
    parser.add_argument("--wide", default="data/v1_baseline_live_30row.csv")
    args = parser.parse_args(argv)

    thin_rows = load_baseline(args.thin)
    wide_rows = load_baseline(args.wide)
    print(f"Thin baseline: {len(thin_rows)} rows from {args.thin}")
    print(f"Wide baseline: {len(wide_rows)} rows from {args.wide}")

    thin_model = AnomalyModel.train(thin_rows)
    wide_model = AnomalyModel.train(wide_rows)
    sla = SlaEngine()

    header = (f"{'sample':>6} | {'SLA breach':>10} | "
              f"{'thin: anomaly / is_anomaly':>27} | "
              f"{'wide: anomaly / is_anomaly':>27}")
    print()
    print(header)
    print("-" * len(header))

    thin_flags = 0
    wide_flags = 0
    for i, sample in enumerate(TEST_SAMPLES, start=1):
        breach = bool(sla.evaluate(sample))
        thin_anom = thin_model.anomaly_health(sample)
        thin_is = thin_model.is_anomaly(sample)
        wide_anom = wide_model.anomaly_health(sample)
        wide_is = wide_model.is_anomaly(sample)
        thin_flags += thin_is
        wide_flags += wide_is
        print(f"{i:>6} | {str(breach):>10} | "
              f"{thin_anom:>18.1f} / {str(thin_is):>5} | "
              f"{wide_anom:>18.1f} / {str(wide_is):>5}")

    print()
    print(f"is_anomaly flagged on {thin_flags}/{len(TEST_SAMPLES)} plainly-normal "
          f"samples using the THIN {len(thin_rows)}-row baseline.")
    print(f"is_anomaly flagged on {wide_flags}/{len(TEST_SAMPLES)} plainly-normal "
          f"samples using the WIDE {len(wide_rows)}-row baseline.")
    print()
    print("None of these samples breach any SLA -- any is_anomaly=True here is "
          "the model being statistically starved by too few baseline rows, "
          "not a real finding. Fewer false flags on the wide baseline is the "
          "fix working as intended.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
