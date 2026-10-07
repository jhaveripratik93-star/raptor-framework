"""Demo: show, row by row, how baseline_mean / baseline_min / tolerance are
actually derived from data/v1_baseline_live.csv by the real gate.model code.

Run from the repo root:
    python scripts/demos/demo_baseline_calibration.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np
from sklearn.ensemble import IsolationForest

from gate.dataio import load_baseline
from gate.metrics import METRIC_NAMES, feature_vector
from gate.model import AnomalyModel


def main() -> None:
    baseline = load_baseline("data/v1_baseline_live.csv")
    X = np.array([feature_vector(s) for s in baseline], dtype=float)

    print(f"Loaded {len(baseline)} baseline rows, {X.shape[1]} features each.")
    print(f"Feature order: {METRIC_NAMES}")
    print()

    # Fit exactly as AnomalyModel.train() does.
    model = IsolationForest(n_estimators=200, contamination="auto", random_state=42)
    model.fit(X)

    scores = model.decision_function(X)
    print("decision_function() score for EACH baseline row (higher = more normal):")
    for i, (row, score) in enumerate(zip(baseline, scores), start=1):
        print(f"  row {i:2d}: cpu_usage={row['cpu_usage']:.4f}  ->  raw score = {score:.4f}")

    print()
    print(f"baseline_mean = average of the {len(scores)} scores above = {scores.mean():.4f}")
    print(f"baseline_min  = the single lowest (least-normal) score     = {scores.min():.4f}")
    spread = scores.mean() - scores.min()
    print(f"spread = baseline_mean - baseline_min = {spread:.4f}")
    print(f"tolerance = spread * 4.0 = {spread * 4.0:.4f}")

    # Cross-check against the real AnomalyModel.train() path.
    real_model = AnomalyModel.train(baseline)
    print()
    print("Cross-check against AnomalyModel.train() (should match exactly):")
    print(f"  model._baseline_mean = {real_model._baseline_mean:.4f}")
    print(f"  model._baseline_min  = {real_model._baseline_min:.4f}")
    print(f"  model._tolerance     = {real_model._tolerance:.4f}")


if __name__ == "__main__":
    main()
