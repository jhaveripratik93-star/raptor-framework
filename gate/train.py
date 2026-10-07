"""Train the gate's Isolation Forest from the v1 baseline and save the artifact.

This is the "train on v1 production baseline" step from the framework document.
The serving app loads the saved artifact on startup instead of retraining, so
scoring is fast and the served model is a fixed, reproducible version.

Usage:
    python -m gate.train --baseline data/v1_baseline.csv --out model/model.joblib
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .dataio import load_baseline
from .model import AnomalyModel


def build(baseline_path: str, out_path: str) -> str:
    baseline = load_baseline(baseline_path)
    model = AnomalyModel.train(baseline)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    model.save(out)
    return str(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train and save the gate model")
    parser.add_argument("--baseline", required=True,
                        help="CSV of v1 baseline (healthy) samples")
    parser.add_argument("--out", default="model/model.joblib",
                        help="Path to write the trained model artifact")
    args = parser.parse_args(argv)

    path = build(args.baseline, args.out)
    print(f"Trained model saved to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
