"""Live demo loop: collect metrics -> run the Isolation Forest + SLA engine -> show verdict.

Meant to be copied into the robot pod (/opt/raptor-gate) and run there, where it
has in-cluster access to Prometheus / Redis / Kubernetes API / the target HTTP
service via the collectors_incluster_<app>.yaml config.

Each loop iteration prints three clearly separated phases, so a client watching
the terminal can see the pipeline step-by-step:
  1. COLLECTING  - the 9 raw metrics pulled live from each source
  2. SCORING     - the Isolation Forest anomaly score + SLA rule evaluation
  3. VERDICT     - the final PROMOTE / ROLLBACK decision and why

Usage (run from /opt/raptor-gate inside the pod):
    export REDIS_PASSWORD='...'
    python3.11 scripts/live_sample_and_score.py \
        --collectors gate/config/collectors_incluster_rest-api.yaml \
        --baseline data/v1_baseline_live.csv \
        --duration 120 --interval 20
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
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
    parser.add_argument("--collectors", default="gate/config/collectors_incluster_rest-api.yaml")
    parser.add_argument("--baseline", default="data/v1_baseline_live.csv")
    parser.add_argument("--config", default="gate/config/production.yaml",
                         help="SLA threshold config (defaults to the bundled one)")
    parser.add_argument("--component", default=None,
                         help="override the component name (defaults to collectors.yaml target.workload)")
    parser.add_argument("--duration", type=float, default=120.0,
                         help="total time to run, in seconds (default: 120 = 2 minutes)")
    parser.add_argument("--interval", type=float, default=20.0,
                         help="seconds between samples (default: 20)")
    parser.add_argument("--output", default=None,
                         help="CSV file to write results to. Defaults to "
                              "data/live_results_<app>_<timestamp>.csv so each run "
                              "produces a new file instead of overwriting.")
    parser.add_argument("--no-plot", action="store_true",
                         help="Skip rendering the dashboard PNG after the run "
                              "(by default a PNG is written next to the CSV via "
                              "scripts/plot_dashboard.py).")
    args = parser.parse_args(argv)

    # Load the collectors YAML early so we know the app name before building
    # the output path.  The aggregator is constructed from the same cfg later.
    cfg = yaml.safe_load(Path(args.collectors).read_text())
    component = args.component or cfg.get("target", {}).get("workload", "canary")

    # Build the output path once so the filename is fixed for the whole run.
    if args.output:
        output_path = Path(args.output)
    else:
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Sanitise the app name: replace characters that are awkward in filenames.
        safe_app = component.replace("/", "_").replace(":", "_")
        output_path = Path("data") / f"live_results_{safe_app}_{ts_str}.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------- train once, upfront
    _hr("STEP 0: TRAINING THE ISOLATION FOREST ON THE v1 BASELINE")
    baseline_rows = load_baseline(args.baseline)
    print(f"  Loaded {len(baseline_rows)} healthy baseline samples from {args.baseline}")
    model = AnomalyModel.train(baseline_rows)
    sla = SlaEngine.from_yaml(args.config) if Path(args.config).exists() else SlaEngine()
    scorer = HealthScorer(model, sla)
    print(f"  Model trained. SLA thresholds loaded from "
          f"{args.config if Path(args.config).exists() else '(built-in defaults)'}")

    # cfg and component were already resolved above; just build the aggregator.
    aggregator = MetricAggregator.from_config(cfg)
    print(f"  Collectors configured from {args.collectors} (target: {component})")
    print(f"  Results will be written to {output_path}")

    # ------------------------------------------------------------- main loop
    start = time.time()
    sample_num = 0
    verdicts = []
    # Parallel list to `verdicts`: (wall-clock timestamp, raw metric sample
    # dict) captured at COLLECTION time for that same sample -- not at the
    # end of the run -- so the CSV reflects what was actually observed and
    # when, not just the final decision.
    records: list[tuple[str, dict[str, float]]] = []

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
        sample_timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
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

        # Naive comparison: what a dashboard that ONLY checks this one
        # sample's own SLA thresholds would show -- no Isolation Forest, no
        # cross-sample persistent-breach memory (trend.py), no within-window
        # volatility check (volatility.py). Derived straight from the same
        # verdict.violations the real gate already computed -- no extra
        # scoring pass needed, since the SLA engine always runs first inside
        # HealthScorer.score() regardless of which signals fire afterwards.
        naive_decision = "PROMOTE" if not verdict.violations else "ROLLBACK"
        if naive_decision != verdict.decision:
            reason = (", ".join(p.detail for p in verdict.persistent_breaches)
                       if verdict.persistent_breaches else
                       "Isolation Forest anomaly score dropped health below the gate threshold")
            print(f"    >>> DIVERGENCE: naive SLA-only monitoring would say {naive_decision}, "
                  f"the gate says {verdict.decision} <<<")
            print(f"        Why: {reason}")

        verdicts.append(verdict)
        records.append((sample_timestamp, dict(result.sample)))

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

    # Write every verdict AND the raw metric sample that produced it as one
    # row in the output CSV -- so the file is a self-contained audit trail
    # (what was observed, when, and what the gate decided), not just the
    # decision with no evidence behind it.
    csv_fields = (["sample", "timestamp"] + list(METRIC_NAMES) +
                  ["decision", "health_score", "anomaly_score", "is_anomaly",
                   "violations", "violated_metrics", "promote",
                   "naive_decision", "divergence", "forced_rollback",
                   "persistent_breach_metrics"])
    with output_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=csv_fields)
        writer.writeheader()
        n_divergence = 0
        for i, (v, (ts, sample)) in enumerate(zip(verdicts, records), start=1):
            naive_decision = "PROMOTE" if not v.violations else "ROLLBACK"
            divergence = naive_decision != v.decision
            n_divergence += divergence
            row = {
                "sample": i,
                "timestamp": ts,
                "decision": v.decision,
                "health_score": v.health_score,
                "anomaly_score": v.anomaly_score,
                "is_anomaly": v.is_anomaly,
                "violations": len(v.violations),
                "violated_metrics": ";".join(viol.metric for viol in v.violations),
                "promote": v.promote,
                "naive_decision": naive_decision,
                "divergence": divergence,
                "forced_rollback": v.forced_rollback,
                "persistent_breach_metrics": ";".join(
                    p.metric for p in v.persistent_breaches
                ),
            }
            for metric in METRIC_NAMES:
                row[metric] = sample.get(metric, "")
            writer.writerow(row)
    print()
    print(f"  Results saved to: {output_path}")
    print(f"  Naive-vs-gate divergence: {n_divergence} of {len(verdicts)} sample(s)")

    if not args.no_plot:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import plot_dashboard  # scripts/plot_dashboard.py, same directory
            png_path = plot_dashboard.render_dashboard(output_path)
            print(f"  Dashboard image saved to: {png_path}")
        except Exception as exc:  # never let plotting failure affect the gate's exit code
            print(f"  WARNING: could not render dashboard PNG ({exc}). "
                  f"Re-run with: python scripts/plot_dashboard.py --csv {output_path}",
                  file=sys.stderr)

    last = verdicts[-1]
    print()
    print(f"  Final (most recent) decision: {last.decision}")
    return last.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
