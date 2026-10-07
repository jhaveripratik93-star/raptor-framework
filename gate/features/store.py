"""Feature store — windowed aggregation with Redis caching.

Mirrors Raptor's feature store role:
  - ingest raw metric samples over time (per component)
  - compute the declared windowed aggregate per feature (e.g. 5-min mean/p99)
  - cache the resulting feature vector in Redis with a TTL, and serve it

Raw samples are held in a bounded in-memory ring buffer per (component, metric),
pruned to the feature's window. The computed vector is cached in Redis so
repeated scoring calls within the window are cheap — the same caching benefit
the document attributes to Raptor.

If Redis is unavailable, the store degrades gracefully to memory-only (still
correct, just no cross-process cache).
"""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque

from ..metrics import METRIC_NAMES
from .registry import FeatureSpec, registered_features
# Importing definitions registers the 9 features as a side effect.
from . import definitions  # noqa: F401


class FeatureStore:
    def __init__(self, redis_client=None, cache_ttl_seconds: int = 60,
                 max_points: int = 10_000, now=time.time):
        self._features: dict[str, FeatureSpec] = registered_features()
        self._redis = redis_client
        self._ttl = cache_ttl_seconds
        self._now = now
        # (component, metric) -> deque[(timestamp, value)]
        self._buffers: dict[tuple[str, str], deque] = defaultdict(
            lambda: deque(maxlen=max_points)
        )

    # --------------------------------------------------------------- ingest
    def ingest(self, component: str, sample: dict[str, float],
               timestamp: float | None = None) -> None:
        """Add a raw metric sample for a component."""
        ts = timestamp if timestamp is not None else self._now()
        for metric in METRIC_NAMES:
            if metric in sample:
                self._buffers[(component, metric)].append(
                    (ts, float(sample[metric]))
                )

    # ------------------------------------------------------------ aggregate
    def _windowed_values(self, component: str, metric: str,
                         window_seconds: float) -> list[float]:
        buf = self._buffers.get((component, metric))
        if not buf:
            return []
        cutoff = self._now() - window_seconds
        return [v for (ts, v) in buf if ts >= cutoff]

    def compute_feature(self, component: str, metric: str) -> float:
        spec = self._features[metric]
        values = self._windowed_values(component, metric, spec.over_seconds)
        return spec.aggregate(values)

    def feature_vector(self, component: str) -> dict[str, float]:
        """Compute the full aggregated feature vector for a component.

        Uses the Redis cache when a fresh entry exists; otherwise computes from
        the buffers and refreshes the cache.
        """
        cached = self._cache_get(component)
        if cached is not None:
            return cached

        vector = {m: self.compute_feature(component, m) for m in METRIC_NAMES}
        self._cache_set(component, vector)
        return vector

    def windowed_values(self, component: str, metric: str) -> list[float]:
        """Return the raw (un-aggregated) values still inside this metric's
        window for `component` -- e.g. every 10-second cpu_usage tick inside
        the current 5-minute window, before Mean/Max/P99 collapses them into
        one number.

        This is what gate/signals/volatility.py's WindowVolatilityChecker needs:
        Mean aggregation can hide a window where a metric spent a large
        fraction of its ticks over the SLA line (e.g. 4 of 7 ticks breaching
        80% CPU, averaging out to a comfortable-looking 68%). The single
        aggregated value in feature_vector() above cannot answer "how much of
        this window was actually bad" -- only the raw ticks can.
        """
        spec = self._features[metric]
        return self._windowed_values(component, metric, spec.over_seconds)

    # ---------------------------------------------------------- redis cache
    def _cache_key(self, component: str) -> str:
        return f"raptorgate:features:{component}"

    def _cache_get(self, component: str) -> dict[str, float] | None:
        if self._redis is None:
            return None
        try:
            raw = self._redis.get(self._cache_key(component))
        except Exception:
            return None  # cache is best-effort; never fail scoring on it
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return {k: float(v) for k, v in data.items()}
        except (ValueError, TypeError):
            return None

    def _cache_set(self, component: str, vector: dict[str, float]) -> None:
        if self._redis is None:
            return
        try:
            self._redis.setex(
                self._cache_key(component), self._ttl, json.dumps(vector)
            )
        except Exception:
            pass  # best-effort

    # ------------------------------------------------------------- helpers
    @property
    def features(self) -> dict[str, FeatureSpec]:
        return dict(self._features)
