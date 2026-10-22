"""Demo: where does the single number raw = -0.111061 actually come from?

gate/model.py calls sklearn's IsolationForest.decision_function() as a
black box. This script opens that box: it reads the REAL fitted trees
inside the model trained on data/v1_baseline_live.csv, walks the one
sample down all 200 of them, and reconstructs decision_function() from
first principles -- then compares the reconstructed number against
sklearn's own decision_function() call to prove they match exactly.

No formula here is invented for this script -- it is the published
Isolation Forest scoring formula (Liu, Ting, Zhou 2008), and the
intermediate numbers (per-tree path lengths) are read directly off the
actual fitted sklearn trees for the actual sample, not simulated.

Run from the repo root:
    python scripts/demos/demo_raw_score_internals.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np

from gate.dataio import load_baseline
from gate.metrics import feature_vector
from gate.model import AnomalyModel

BASELINE_PATH = "data/v1_baseline_live.csv"

# Same sample as the single-sample walkthrough demo -- sample 1 of
# data/scenario_positive_promote.csv
SAMPLE = dict(
    cpu_usage=0.6561085972850679,
    memory_usage=20.59912929100114,
    pod_restarts=0.0,
    http_error_rate=0.0,
    p99_latency=54.754,
    availability=100.0,
    redis_latency=1.01,
    endpoint_latency=9.917,
    packet_loss=0.0,
)


def average_path_length(n_samples_leaf: np.ndarray) -> np.ndarray:
    """c(n): expected path length to isolate a point in a tree built from
    n_samples_leaf points, for an UNFINISHED partition (a leaf that still
    holds more than one training point when the tree stopped growing).
    This is the standard closed-form correction from the Isolation Forest
    paper -- the same formula sklearn uses internally, reproduced here
    (not imported from sklearn's private API) so the full calculation is
    visible end to end.
        c(n) = 0                                     if n <= 1
        c(n) = 1                                      if n == 2
        c(n) = 2*(ln(n-1) + euler_gamma) - 2*(n-1)/n   otherwise
    """
    n = np.asarray(n_samples_leaf, dtype=np.float64)
    out = np.zeros_like(n)
    mask1 = n <= 1
    mask2 = n == 2
    rest = ~(mask1 | mask2)
    out[mask2] = 1.0
    out[rest] = 2.0 * (np.log(n[rest] - 1.0) + np.euler_gamma) - 2.0 * (n[rest] - 1.0) / n[rest]
    return out


def main() -> None:
    baseline = load_baseline(BASELINE_PATH)
    model = AnomalyModel.train(baseline)
    sk_model = model._model  # the real, trained sklearn.ensemble.IsolationForest

    X_sample = np.array([feature_vector(SAMPLE)], dtype=float)

    print(f"n_estimators (number of random trees)     = {sk_model.n_estimators}")
    print(f"max_samples actually used per tree         = {sk_model._max_samples}  "
          f"(= min(256, {len(baseline)} baseline rows) = {len(baseline)})")
    print(f"offset_ (learned at training time)         = {sk_model.offset_}  "
          f"(fixed at -0.5 whenever contamination='auto')")

    # ------------------------------------------------- STEP 1: per-tree path length
    print()
    print("=" * 78)
    print("  STEP 1: walk the sample down EVERY one of the 200 real fitted trees")
    print("=" * 78)
    print("  For each tree: how many internal-node splits did it take to route")
    print("  this sample to a leaf, PLUS a correction if that leaf still held")
    print("  more than 1 training point when the tree stopped growing (i.e. the")
    print("  tree never fully isolated it -- common with only 10 training rows).")
    print()

    depths = np.zeros(1)
    per_tree_h = []
    for tree, feat_idx in zip(sk_model.estimators_, sk_model.estimators_features_):
        X_sub = X_sample[:, feat_idx]
        # number of internal-node splits traversed to reach the leaf
        node_indicator = tree.decision_path(X_sub)
        splits = np.ravel(node_indicator.sum(axis=1))
        # how many training points still share that leaf
        leaf_id = tree.apply(X_sub)
        n_in_leaf = tree.tree_.n_node_samples[leaf_id]
        # h(x) for this one tree = splits + c(n_in_leaf) - 1
        h = splits + average_path_length(n_in_leaf) - 1.0
        depths += h
        per_tree_h.append(float(h[0]))

    print(f"  Shortest path length seen across the 200 trees : {min(per_tree_h):.4f}")
    print(f"  Longest path length seen across the 200 trees  : {max(per_tree_h):.4f}")
    print(f"  First 10 trees' h(x) values, as a sample: "
          f"{[round(v, 3) for v in per_tree_h[:10]]}")

    avg_h = depths[0] / sk_model.n_estimators
    print()
    print(f"  E(h(x)) = average path length across all {sk_model.n_estimators} trees")
    print(f"          = sum(all 200 h(x) values) / 200")
    print(f"          = {depths[0]:.6f} / {sk_model.n_estimators}")
    print(f"          = {avg_h:.6f}")

    # ------------------------------------------------- STEP 2: normalize by c(n)
    print()
    print("=" * 78)
    print("  STEP 2: normalize E(h(x)) against c(max_samples) -- the EXPECTED")
    print("  path length for a point in a RANDOM, unstructured tree of this size")
    print("=" * 78)
    c_n = average_path_length(np.array([sk_model._max_samples]))[0]
    print(f"  c({sk_model._max_samples}) = 2*(ln({sk_model._max_samples}-1) + euler_gamma) "
          f"- 2*({sk_model._max_samples}-1)/{sk_model._max_samples}")
    print(f"       = 2*(ln({sk_model._max_samples - 1}) + 0.5772156649) "
          f"- 2*{sk_model._max_samples - 1}/{sk_model._max_samples}")
    print(f"       = {c_n:.6f}")
    print("  (this is a universal constant for a 10-sample tree -- it does NOT")
    print("  depend on this sample at all, only on how many baseline rows exist)")

    # ------------------------------------------------- STEP 3: the paper's s(x,n)
    print()
    print("=" * 78)
    print("  STEP 3: the Isolation Forest paper's anomaly score s(x,n)")
    print("=" * 78)
    s = 2.0 ** (-avg_h / c_n)
    print(f"  s(x,n) = 2 ^ ( -E(h(x)) / c(n) )")
    print(f"         = 2 ^ ( -{avg_h:.6f} / {c_n:.6f} )")
    print(f"         = 2 ^ ( -{avg_h / c_n:.6f} )")
    print(f"         = {s:.6f}")
    print("  (s close to 1 = anomalous/isolated quickly; s close to 0 = normal,")
    print("   took many splits to isolate, like most of the baseline's own rows)")

    # ------------------------------------------------- STEP 4: sklearn's sign flip
    print()
    print("=" * 78)
    print("  STEP 4: sklearn's score_samples() flips the sign")
    print("=" * 78)
    score_samples = -s
    print(f"  score_samples = -s(x,n) = -{s:.6f} = {score_samples:.6f}")
    print("  (sklearn's convention: LOWER score_samples = MORE anomalous,")
    print("   so it negates the paper's s(x,n) where higher = more anomalous)")

    # ------------------------------------------------- STEP 5: subtract offset_
    print()
    print("=" * 78)
    print("  STEP 5: decision_function = score_samples - offset_")
    print("=" * 78)
    decision_function = score_samples - sk_model.offset_
    print(f"  decision_function = {score_samples:.6f} - ({sk_model.offset_})")
    print(f"                    = {decision_function:.6f}")

    # ------------------------------------------------- STEP 6: verify vs sklearn
    print()
    print("=" * 78)
    print("  STEP 6: verify against sklearn's OWN decision_function() call")
    print("=" * 78)
    real_score_samples = sk_model.score_samples(X_sample)[0]
    real_decision = sk_model.decision_function(X_sample)[0]
    gate_raw = model.raw_score(SAMPLE)
    print(f"  Reconstructed score_samples   = {score_samples:.6f}")
    print(f"  sklearn's score_samples()     = {real_score_samples:.6f}   "
          f"{'MATCH' if abs(score_samples - real_score_samples) < 1e-9 else 'MISMATCH'}")
    print(f"  Reconstructed decision_function = {decision_function:.6f}")
    print(f"  sklearn's decision_function() = {real_decision:.6f}   "
          f"{'MATCH' if abs(decision_function - real_decision) < 1e-9 else 'MISMATCH'}")
    print(f"  gate/model.py's raw_score()   = {gate_raw:.6f}   "
          f"{'MATCH' if abs(gate_raw - real_decision) < 1e-9 else 'MISMATCH'}")


if __name__ == "__main__":
    main()
