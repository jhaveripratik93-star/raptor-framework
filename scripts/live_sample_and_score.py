"""Live demo loop: collect metrics -> run the Isolation Forest + SLA engine -> show verdict.

Meant to be copied into the robot pod (/opt/raptor-gate) and run there, where it
has in-cluster access to Prometheus / Redis / Kubernetes API / the target HTTP
service via the collectors_incluster.yaml config.

Each loop iteration prints three clearly separated phases, so a client watching
the terminal can see the pipeline step-by-step:
  1. COLLECTING  - the 9 raw metrics pulled live from each source
  2. SCORING     - the Isolation Forest anomaly score + SLA rule evaluation
  3. VERDICT     - the final PROMOTE / ROLLBACK decision and why

Usage (run from /opt/raptor-gate inside the pod):
    export REDIS_PASSWORD='...'
    python3.11 scripts/live_sample_and_score.py \
        --collectors gate/config/collectors_incluster.yaml \
        --baseline data/v1_baseline_live.csv \
        --duration 120 --interval 20
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from gate.collectors import MetricAggregator  # noqa: E402
from gate.dataio import load_baseline  # noqa: E402
from gate.model import AnomalyModel  # noqa: E402
from gate.sla import SlaEngine  # noqa: E402
from gate.scoring import HealthScorer  # noqa: E402
from gate.metrics import METRIC_NAMES  # noqa: E402


def _hr(title: str) -> None:
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live collect -> score -> verdict loop")
    parser.add_argument("--collectors", default="gate/config/collectors_incluster.yaml")
    parser.add_argument("--baseline", default="data/v1_baseline_live.csv")
    parser.add_argument("--config", default="gate/config/production.yaml",
                         help="SLA threshold config (defaults to the bundled one)")
    parser.add_argument("--component", default=None,
                         help="override the component name (defaults to collectors.yaml target.workload)")
    parser.add_argument("--duration", type=float, default=120.0,
                         help="total time to run, in seconds (default: 120 = 2 minutes)")
    parser.add_argument("--interval", type=float, default=20.0,
                         help="seconds between samples (default: 20)")
    args = parser.parse_args(argv)

    # ---------------------------------------------------- train once, upfront
    _hr("STEP 0: TRAINING THE ISOLATION FOREST ON THE v1 BASELINE")
    baseline_rows = load_baseline(args.baseline)
    print(f"  Loaded {len(baseline_rows)} healthy baseline samples from {args.baseline}")
    model = AnomalyModel.train(baseline_rows)
    sla = SlaEngine.from_yaml(args.config) if Path(args.config).exists() else SlaEngine()
    scorer = HealthScorer(model, sla)
    print(f"  Model trained. SLA thresholds loaded from "
          f"{args.config if Path(args.config).exists() else '(built-in defaults)'}")

    cfg = yaml.safe_load(Path(args.collectors).read_text())
    component = args.component or cfg.get("target", {}).get("workload", "canary")
    aggregator = MetricAggregator.from_config(cfg)
    print(f"  Collectors configured from {args.collectors} (target: {component})")

    # ------------------------------------------------------------- main loop
    start = time.time()
    sample_num = 0
    verdicts = []

    while time.time() - start < args.duration:
        sample_num += 1
        ts = time.strftime("%H:%M:%S")

        _hr(f"SAMPLE {sample_num}  @ {ts}  --  PHASE 1: COLLECTING LIVE METRICS")
        try:
            result = aggregator.collect(require_complete=False)
        except Exception as exc:  # defensive: aggregator itself should not raise
            print(f"  Unexpected collection error: {exc}")
            result = None

        if result is None or not result.sample:
            print("  No metrics collected this round.")
            if result and result.errors:
                print(f"  Collector errors: {json.dumps(result.errors, indent=2)}")
            time.sleep(args.interval)
            continue

        for name in METRIC_NAMES:
            val = result.sample.get(name)
            flag = "" if val is not None else "  <-- MISSING"
            print(f"    {name:<20s}: {val if val is not None else '-':>10}{flag}")
        if result.errors:
            print(f"  (partial) collector errors: {json.dumps(result.errors)}")

        if not result.complete:
            print()
            print(f"  Incomplete sample (missing: {result.missing}) -- "
                  f"skipping scoring for this round (fail-closed).")
            time.sleep(args.interval)
            continue

        _hr(f"SAMPLE {sample_num}  --  PHASE 2: ISOLATION FOREST + SLA SCORING")
        verdict = scorer.score(component, result.sample)
        print(f"    Anomaly score (Isolation Forest) : {verdict.anomaly_score} / 100")
        print(f"    Flagged as statistical anomaly    : {verdict.is_anomaly}")
        print(f"    SLA breaches                      : {len(verdict.violations)}")
        for v in verdict.violations:
            print(f"      - {v.detail}")
        print(f"    Health score (anomaly - SLA deductions) : {verdict.health_score} / 100 "
              f"(gate threshold >= {verdict.gate_threshold})")

        _hr(f"SAMPLE {sample_num}  --  PHASE 3: VERDICT")
        print(f"    GATE DECISION: {verdict.decision}  "
              f"({'exit 0, pipeline would PROMOTE' if verdict.promote else 'exit 1, pipeline would ROLLBACK'})")

        verdicts.append(verdict)

        remaining = args.duration - (time.time() - start)
        if remaining > args.interval:
            time.sleep(args.interval)
        elif remaining > 0:
            time.sleep(remaining)

    _hr("RUN SUMMARY")
    if not verdicts:
        print("  No complete samples were scored during this run.")
        return 1

    for i, v in enumerate(verdicts, start=1):
        print(f"  Sample {i}: {v.decision:<9s} health={v.health_score:>5} "
              f"anomaly={v.anomaly_score:>5} violations={len(v.violations)}")

    last = verdicts[-1]
    print()
    print(f"  Final (most recent) decision: {last.decision}")
    return last.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
