"""Isolation Forest anomaly model for the deployment gate.

Trained on the v1 production baseline (the known-good state of the service),
the model scores a canary's feature vector and reports how anomalous it looks
relative to that baseline — catching cross-metric degradation that individual
SLA thresholds miss.

The raw sklearn decision_function is unbounded and hard to reason about, so we
convert it into a 0-100 "anomaly health" component:
  100 -> looks exactly like the healthy baseline
    0 -> strongly anomalous
This component is combined with the SLA deductions in scoring.py.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from sklearn.ensemble import IsolationForest

from .metrics import METRIC_NAMES, feature_vector


class AnomalyModel:
    """Wraps an Isolation Forest trained on the v1 baseline."""

    def __init__(self, model: IsolationForest, baseline_min: float,
                 baseline_mean: float, tolerance: float,
                 feature_names: tuple[str, ...] = METRIC_NAMES):
        self._model = model
        # Calibration derived from the baseline at train time.
        #   baseline_mean : mean decision_function score of healthy v1 samples
        #   baseline_min  : the LEAST-normal healthy sample (edge of known-good)
        #   tolerance     : score width that maps to a full 100-point drop
        # A canary at least as normal as baseline_mean scores 100. As it becomes
        # more anomalous than the healthy edge, the score falls to 0 over
        # `tolerance` worth of decision_function units.
        self._baseline_min = baseline_min
        self._baseline_mean = baseline_mean
        self._tolerance = tolerance
        self.feature_names = feature_names

    # ------------------------------------------------------------------ train
    @classmethod
    def train(cls, baseline: list[dict[str, float]],
              random_state: int = 42) -> "AnomalyModel":
        """Fit the Isolation Forest on v1 baseline samples.

        `baseline` is a list of metric-sample dicts (the healthy v1 rows).
        """
        if not baseline:
            raise ValueError("baseline is empty; cannot train the model")

        X = np.array([feature_vector(s) for s in baseline], dtype=float)

        model = IsolationForest(
            n_estimators=200,
            contamination="auto",
            random_state=random_state,
        )
        model.fit(X)

        # Calibrate the 0-100 mapping against the baseline's own scores.
        # decision_function: higher = more normal.
        baseline_scores = model.decision_function(X)
        baseline_mean = float(baseline_scores.mean())
        baseline_min = float(baseline_scores.min())

        # Tolerance = how far below the healthy edge a canary can drift before
        # its anomaly-health hits 0. We use the spread from mean down to the
        # least-normal healthy sample as the natural unit of "normal variation",
        # then allow a few of those before flooring. This keeps genuinely
        # healthy canaries (slightly outside the tight cluster) scoring high,
        # while clearly anomalous ones fall off fast.
        spread = max(baseline_mean - baseline_min, 1e-6)
        tolerance = spread * 4.0
        return cls(model, baseline_min, baseline_mean, tolerance)

    # ------------------------------------------------------------------ score
    def anomaly_health(self, sample: dict[str, float]) -> float:
        """Return a 0-100 score: 100 = normal, 0 = strongly anomalous."""
        raw = float(self._model.decision_function(
            np.array([feature_vector(sample)], dtype=float)
        )[0])

        # Distance below the healthy baseline mean, in decision_function units.
        # Samples at least as normal as the baseline mean get the full 100.
        deficit = self._baseline_mean - raw
        if deficit <= 0:
            return 100.0
        # Linearly map [0, tolerance] deficit -> [100, 0].
        health = 100.0 * (1.0 - deficit / self._tolerance)
        return max(0.0, min(100.0, health))

    def raw_score(self, sample: dict[str, float]) -> float:
        """Expose the raw sklearn decision_function value (higher = normal)."""
        return float(self._model.decision_function(
            np.array([feature_vector(sample)], dtype=float)
        )[0])

    def is_anomaly(self, sample: dict[str, float]) -> bool:
        """sklearn predict: -1 = anomaly, 1 = inlier."""
        return int(self._model.predict(
            np.array([feature_vector(sample)], dtype=float)
        )[0]) == -1

    # ---------------------------------------------------------- persistence
    def save(self, path: str | Path) -> None:
        import joblib
        joblib.dump(
            {
                "model": self._model,
                "baseline_min": self._baseline_min,
                "baseline_mean": self._baseline_mean,
                "tolerance": self._tolerance,
                "feature_names": self.feature_names,
            },
            path,
        )

    @classmethod
    def load(cls, path: str | Path) -> "AnomalyModel":
        import joblib
        blob = joblib.load(path)
        return cls(
            blob["model"],
            blob["baseline_min"],
            blob["baseline_mean"],
            blob["tolerance"],
            tuple(blob["feature_names"]),
        )
