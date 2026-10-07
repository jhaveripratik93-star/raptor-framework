"""Tests for the Phase 4 feature layer: aggregation + feature store."""

from __future__ import annotations

import pytest

from gate.features.registry import (
    AggregationFunction, parse_window, _percentile, registered_features,
)
from gate.features.store import FeatureStore
from gate.metrics import METRIC_NAMES


def test_parse_window():
    assert parse_window("5m") == 300
    assert parse_window("30s") == 30
    assert parse_window("1h") == 3600
    with pytest.raises(ValueError):
        parse_window("5x")


def test_percentile():
    assert _percentile([10], 99) == 10
    assert _percentile([0, 100], 50) == pytest.approx(50)
    assert _percentile([1, 2, 3, 4, 5], 99) == pytest.approx(4.96, abs=0.01)


def test_all_nine_features_registered():
    feats = registered_features()
    assert set(feats) == set(METRIC_NAMES)
    # p99 metrics use P99, pod_restarts uses Max.
    assert feats["p99_latency"].function is AggregationFunction.P99
    assert feats["endpoint_latency"].function is AggregationFunction.P99
    assert feats["pod_restarts"].function is AggregationFunction.Max
    assert feats["cpu_usage"].function is AggregationFunction.Mean
    # All windows are 5 minutes.
    assert all(f.over_seconds == 300 for f in feats.values())


def _full_sample(**overrides):
    base = {m: 1.0 for m in METRIC_NAMES}
    base.update(overrides)
    return base


def test_feature_store_windowed_mean_and_max():
    t = [1000.0]

    def fake_now():
        return t[0]

    store = FeatureStore(redis_client=None, now=fake_now)
    # Ingest three samples within the 5-min window.
    store.ingest("v2", _full_sample(cpu_usage=40, pod_restarts=0), timestamp=1000)
    store.ingest("v2", _full_sample(cpu_usage=50, pod_restarts=2), timestamp=1010)
    store.ingest("v2", _full_sample(cpu_usage=60, pod_restarts=1), timestamp=1020)
    t[0] = 1030.0

    vec = store.feature_vector("v2")
    assert vec["cpu_usage"] == pytest.approx(50.0)      # mean(40,50,60)
    assert vec["pod_restarts"] == pytest.approx(2.0)    # max(0,2,1)


def test_feature_store_prunes_outside_window():
    t = [0.0]

    def fake_now():
        return t[0]

    store = FeatureStore(redis_client=None, now=fake_now)
    store.ingest("v2", _full_sample(cpu_usage=100), timestamp=0)      # old
    store.ingest("v2", _full_sample(cpu_usage=40), timestamp=400)     # in window
    store.ingest("v2", _full_sample(cpu_usage=44), timestamp=410)
    t[0] = 420.0  # window is 300s -> cutoff 120s; the t=0 sample is excluded

    vec = store.feature_vector("v2")
    assert vec["cpu_usage"] == pytest.approx(42.0)      # mean(40,44), not 100


class _FakeRedis:
    """Minimal in-memory stand-in for redis get/setex."""
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


def test_feature_store_redis_cache_roundtrip():
    fake = _FakeRedis()
    store = FeatureStore(redis_client=fake)
    store.ingest("v2", _full_sample(cpu_usage=42))
    first = store.feature_vector("v2")     # computes + caches
    assert fake.store  # something was cached

    # Mutate buffers; a cached read should return the cached (old) vector.
    store.ingest("v2", _full_sample(cpu_usage=99))
    second = store.feature_vector("v2")
    assert second == first  # served from cache, not recomputed
