"""Demo: exactly how baseline_mean and baseline_min are calculated.

Trains the real IsolationForest on data/v1_baseline_live.csv (10 rows),
then prints the raw decision_function() score for EVERY individual baseline
row -- so baseline_mean (their average) and baseline_min (the single
lowest one) are visibly just plain arithmetic over 10 real numbers, not a
black box.

This does not re-implement anything: it trains the exact same
sklearn.ensemble.IsolationForest with the exact same constructor arguments
gate/model.py uses, then reads scores back from decision_function() the
same way AnomalyModel.train() does internally.

Run from the repo root:
    python scripts/demos/demo_baseline_calibration_detail.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np
from sklearn.ensemble import IsolationForest

from gate.dataio import load_baseline
from gate.metrics import METRICS, feature_vector
from gate.model import AnomalyModel

BASELINE_PATH = "data/v1_baseline_live.csv"


def main() -> None:
    baseline = load_baseline(BASELINE_PATH)
    print(f"Loaded {len(baseline)} rows from {BASELINE_PATH}\n")

    # -------------------------------------------------- build X, same as model.py
    X = np.array([feature_vector(row) for row in baseline], dtype=float)
    print("Feature matrix X -- one row per baseline sample, columns in the")
    print("canonical METRICS order (gate/metrics.py):")
    print("  " + ", ".join(spec.name for spec in METRICS))
    print()

    # ------------------------------------------- fit, exactly like model.py does
    model = IsolationForest(n_estimators=200, contamination="auto", random_state=42)
    model.fit(X)

    # ---------------------------------- score every baseline row against itself
    scores = model.decision_function(X)

    print(f"{'row #':>6} | {'cpu_usage':>10} | {'memory_usage':>12} | {'p99_latency':>11} | {'decision_function score':>24}")
    print("-" * 78)
    for i, (row, score) in enumerate(zip(baseline, scores), start=1):
        marker = ""
        print(f"{i:>6} | {row['cpu_usage']:>10.4f} | {row['memory_usage']:>12.4f} | "
              f"{row['p99_latency']:>11.3f} | {score:>24.6f}{marker}")

    print()
    print("baseline_mean = sum(all 10 scores) / 10")
    total = sum(scores)
    print(f"              = ({' + '.join(f'{s:.6f}' for s in scores)})")
    print(f"              = {total:.6f} / 10")
    print(f"              = {total / len(scores):.6f}")

    print()
    min_idx = int(np.argmin(scores))
    print(f"baseline_min  = the SINGLE LOWEST score among all 10")
    print(f"              = min({', '.join(f'{s:.6f}' for s in scores)})")
    print(f"              = {scores[min_idx]:.6f}   (row #{min_idx + 1} -- "
          f"cpu_usage={baseline[min_idx]['cpu_usage']:.4f}, "
          f"this is the baseline's OWN least-normal-looking healthy row)")

    print()
    print("Cross-check against the real AnomalyModel.train() (gate/model.py):")
    real_model = AnomalyModel.train(baseline)
    print(f"  model._baseline_mean = {real_model._baseline_mean:.6f}   "
          f"{'MATCH' if abs(real_model._baseline_mean - total/len(scores)) < 1e-9 else 'MISMATCH'}")
    print(f"  model._baseline_min  = {real_model._baseline_min:.6f}   "
          f"{'MATCH' if abs(real_model._baseline_min - scores[min_idx]) < 1e-9 else 'MISMATCH'}")

    print()
    print("WHY row #{} has the lowest score, in plain terms:".format(min_idx + 1))
    print("  Isolation Forest isolates a point by repeatedly picking a random")
    print("  feature and a random split value, until that point sits alone in")
    print("  its own partition. A point that is a bit OFF from where the other")
    print("  9 rows cluster (even slightly, on any one or more of the 9 columns)")
    print("  gets isolated in FEWER random splits on average across the 200")
    print("  trees -- and fewer splits-to-isolate = a lower decision_function")
    print("  score. It is not 'wrong' or unhealthy -- it is simply the baseline's")
    print("  own most unusual-looking member, which is exactly why it defines")
    print("  the 'edge of normal' that `tolerance` is built from.")


if __name__ == "__main__":
    main()
