"""Capture a real v1 baseline from the live service via the collectors.

The gate's Isolation Forest is only as good as its baseline. The bundled
data/v1_baseline.csv is synthetic; this script builds a real one by sampling the
live production (v1) service through the same collectors production mode uses,
writing one CSV row per sample.

Run it while v1 is healthy and serving normal traffic — that is what "normal"
should be learned from.

Usage:
    python scripts/capture_baseline.py \
        --collectors gate/config/collectors.yaml \
        --samples 30 --interval 10 --out data/v1_baseline_live.csv

Prerequisites (same as production mode):
    - port-forwards or in-cluster access to Prometheus / InfluxDB / Redis
    - INFLUX_TOKEN and REDIS_PASSWORD exported
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

# Make the repo importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gate.metrics import METRIC_NAMES          # noqa: E402
from gate.collectors import MetricAggregator, CollectorError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture a live v1 baseline")
    parser.add_argument("--collectors", default="gate/config/collectors.yaml")
    parser.add_argument("--samples", type=int, default=30,
                        help="number of samples to collect")
    parser.add_argument("--interval", type=float, default=10.0,
                        help="seconds between samples")
    parser.add_argument("--out", default="data/v1_baseline_live.csv")
    parser.add_argument("--allow-partial", action="store_true",
                        help="keep rows even if some collectors failed "
                             "(missing metrics are skipped for that row)")
    args = parser.parse_args(argv)

    import yaml
    cfg = yaml.safe_load(Path(args.collectors).read_text())
    aggregator = MetricAggregator.from_config(cfg)

    rows: list[dict[str, float]] = []
    print(f"Capturing {args.samples} samples every {args.interval}s "
          f"into {args.out} …")

    for i in range(1, args.samples + 1):
        try:
            result = aggregator.collect(require_complete=not args.allow_partial)
        except CollectorError as exc:
            print(f"  [{i}/{args.samples}] collection failed: {exc}",
                  file=sys.stderr)
            if not args.allow_partial:
                print("Aborting. Fix connectivity/secrets, or use "
                      "--allow-partial.", file=sys.stderr)
                return 1
            continue

        row = result.sample
        rows.append(row)
        missing = [m for m in METRIC_NAMES if m not in row]
        got = len(METRIC_NAMES) - len(missing)
        print(f"  [{i}/{args.samples}] {got}/{len(METRIC_NAMES)} metrics; "
              f"cpu={row.get('cpu_usage', '?')} mem={row.get('memory_usage', '?')} "
              f"p99={row.get('p99_latency', '?')}")
        if missing:
            print(f"        missing: {missing}", file=sys.stderr)
            if result.errors:
                for name, msg in result.errors.items():
                    print(f"        collector {name}: {msg}", file=sys.stderr)

        if i < args.samples:
            time.sleep(args.interval)

    if not rows:
        print("No samples captured; nothing written.", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(METRIC_NAMES))
        writer.writeheader()
        for row in rows:
            # Only write complete rows; partial rows are dropped to keep the
            # training matrix rectangular.
            if all(m in row for m in METRIC_NAMES):
                writer.writerow({m: row[m] for m in METRIC_NAMES})

    written = sum(1 for r in rows if all(m in r for m in METRIC_NAMES))
    print(f"Wrote {written} complete baseline rows to {out}")
    if written < 10:
        print("WARNING: fewer than 10 rows — the Isolation Forest baseline may "
              "be weak. Capture more samples for a robust baseline.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
