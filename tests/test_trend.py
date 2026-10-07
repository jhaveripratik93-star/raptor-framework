"""Tests for the persistent-breach tracker (gate/signals/trend.py) and its wiring
into HealthScorer (gate/scoring.py).

Covers:
  - BreachTracker in isolation (counting, window, min_breaches threshold)
  - all three BreachHistoryStore backends (memory, file, redis-like fake)
  - HealthScorer forcing ROLLBACK when a tracker is supplied and a metric
    persists, including the headline case this feature exists for: a
    single metric breaching across samples where no individual sample's
    SLA_BREACH_PENALTY alone would have failed it.
  - HealthScorer behaving EXACTLY as before when no tracker is supplied
    (the opt-in / non-breaking guarantee).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gate.signals.trend import (
    BreachTracker,
    InMemoryBreachHistoryStore,
    FileBreachHistoryStore,
    RedisBreachHistoryStore,
)
from gate.model import AnomalyModel
from gate.sla import SlaEngine
from gate.scoring import HealthScorer
from gate.dataio import load_baseline

DATA = Path(__file__).resolve().parent.parent / "data"


# ------------------------------------------------------------- BreachTracker
def test_tracker_flags_metric_breached_in_min_samples():
    tracker = BreachTracker(window_size=5, min_breaches=2)
    assert tracker.record_and_check("svc", {"cpu_usage"}) == []
    result = tracker.record_and_check("svc", {"cpu_usage"})
    assert len(result) == 1
    assert result[0].metric == "cpu_usage"
    assert result[0].breach_count == 2
    assert result[0].window_size == 2


def test_tracker_does_not_flag_single_isolated_breach():
    tracker = BreachTracker(window_size=5, min_breaches=2)
    result = tracker.record_and_check("svc", {"cpu_usage"})
    assert result == []


def test_tracker_window_slides_and_forgets_old_breaches():
    tracker = BreachTracker(window_size=3, min_breaches=2)
    tracker.record_and_check("svc", {"cpu_usage"})       # [cpu]
    tracker.record_and_check("svc", set())                # [cpu], []
    tracker.record_and_check("svc", set())                # [cpu], [], []
    # the single cpu breach is now the ONLY entry containing it, 3 samples
    # ago is still inside a window_size=3 history (it's exactly the oldest
    # kept entry) -- push one more empty sample to evict it entirely.
    result = tracker.record_and_check("svc", set())        # [], [], [] (cpu evicted)
    assert result == []


def test_tracker_different_metrics_tracked_independently():
    tracker = BreachTracker(window_size=5, min_breaches=2)
    tracker.record_and_check("svc", {"cpu_usage"})
    tracker.record_and_check("svc", {"memory_usage"})
    result = tracker.record_and_check("svc", {"memory_usage"})
    assert {p.metric for p in result} == {"memory_usage"}


def test_tracker_components_are_isolated():
    tracker = BreachTracker(window_size=5, min_breaches=2)
    tracker.record_and_check("svc-a", {"cpu_usage"})
    tracker.record_and_check("svc-a", {"cpu_usage"})
    # svc-b has never breached cpu_usage -- must not see svc-a's history.
    result_b = tracker.record_and_check("svc-b", {"cpu_usage"})
    assert result_b == []


def test_tracker_rejects_invalid_params():
    with pytest.raises(ValueError):
        BreachTracker(min_breaches=0)
    with pytest.raises(ValueError):
        BreachTracker(window_size=1, min_breaches=2)


# --------------------------------------------------------- history stores
def test_in_memory_store_round_trip():
    store = InMemoryBreachHistoryStore()
    store.save("svc", [["cpu_usage"], []])
    assert store.load("svc") == [["cpu_usage"], []]
    assert store.load("other") == []


def test_file_store_round_trip(tmp_path):
    store = FileBreachHistoryStore(tmp_path / "breach_history")
    assert store.load("svc") == []
    store.save("svc", [["memory_usage"]])
    # fresh store instance pointed at the same directory -> must see the data.
    store2 = FileBreachHistoryStore(tmp_path / "breach_history")
    assert store2.load("svc") == [["memory_usage"]]


def test_file_store_survives_corrupt_file(tmp_path):
    store = FileBreachHistoryStore(tmp_path / "breach_history")
    store.dir.mkdir(parents=True, exist_ok=True)
    store._component_path("svc").write_text("{not valid json")
    assert store.load("svc") == []  # degrades gracefully, does not raise


def test_file_store_scales_independently_of_other_components(tmp_path):
    """Regression test for the O(n) load/save bug this class used to have:
    scoring component #N must not get slower as more OTHER components are
    recorded alongside it (each component gets its own file now)."""
    import time
    store = FileBreachHistoryStore(tmp_path / "breach_history")
    tracker = BreachTracker(store=store, window_size=5, min_breaches=2)

    # Seed many unrelated components.
    for i in range(500):
        tracker.record_and_check(f"service-{i}", {"cpu_usage"})

    # Scoring ONE component repeatedly must stay fast regardless of how
    # many other components' files already exist alongside it.
    start = time.perf_counter()
    for _ in range(20):
        tracker.record_and_check("service-0", {"cpu_usage"})
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"expected O(1) per-call cost, took {elapsed:.3f}s for 20 calls"


def test_file_store_sanitises_unusual_component_names(tmp_path):
    """Component names can contain characters unsafe for filenames (slashes,
    colons, etc. -- real workload names sometimes do) and must not collide
    with each other after sanitisation."""
    store = FileBreachHistoryStore(tmp_path / "breach_history")
    store.save("robot-poc/eric-cenx-rest-api", [["cpu_usage"]])
    store.save("robot-poc/eric-cenx-analytics", [["memory_usage"]])
    assert store.load("robot-poc/eric-cenx-rest-api") == [["cpu_usage"]]
    assert store.load("robot-poc/eric-cenx-analytics") == [["memory_usage"]]


class _FakeRedis:
    """Minimal in-memory stand-in for a redis.Redis client."""
    def __init__(self):
        self._data: dict[str, str] = {}

    def get(self, key):
        return self._data.get(key)

    def setex(self, key, ttl, value):
        self._data[key] = value


def test_redis_store_round_trip():
    client = _FakeRedis()
    store = RedisBreachHistoryStore(client)
    assert store.load("svc") == []
    store.save("svc", [["redis_latency"]])
    assert store.load("svc") == [["redis_latency"]]


def test_redis_store_degrades_gracefully_with_no_client():
    store = RedisBreachHistoryStore(None)
    assert store.load("svc") == []
    store.save("svc", [["x"]])  # must not raise


def test_redis_store_degrades_gracefully_on_backend_error():
    class _Boom:
        def get(self, key):
            raise ConnectionError("down")

        def setex(self, key, ttl, value):
            raise ConnectionError("down")

    store = RedisBreachHistoryStore(_Boom())
    assert store.load("svc") == []
    store.save("svc", [["x"]])  # must not raise


# ------------------------------------------------- HealthScorer integration
@pytest.fixture(scope="module")
def model():
    baseline = load_baseline(DATA / "v1_baseline.csv")
    return AnomalyModel.train(baseline)


def _borderline_sample(**overrides):
    """A sample matching data/v1_baseline.csv's own row 0 (which this model
    scores at a perfect anomaly_health=100.0) with exactly one SLA breach
    (redis_latency just over its 10ms limit). At SLA_BREACH_PENALTY=30 this
    gives health_score = 100 - 30 = 70, which is >= GATE_THRESHOLD (70) --
    i.e. this sample PROMOTEs on its own merits, by design, so any ROLLBACK
    seen in these tests must be coming from the breach tracker, not from
    the ordinary health-score arithmetic."""
    sample = {
        "cpu_usage": 43.0, "memory_usage": 51.0, "pod_restarts": 0.0,
        "http_error_rate": 0.1, "p99_latency": 81.0, "availability": 99.9,
        "redis_latency": 11.0,  # just over the 10ms SLA -> 1 breach
        "endpoint_latency": 86.0, "packet_loss": 0.1,
    }
    sample.update(overrides)
    return sample


def test_single_sample_with_one_breach_still_promotes_without_tracker(model):
    """Baseline behaviour, unchanged: one mild breach alone still PROMOTEs."""
    scorer = HealthScorer(model, SlaEngine())  # no tracker — old behaviour
    verdict = scorer.score("svcX", _borderline_sample())
    assert verdict.decision == "PROMOTE"
    assert verdict.forced_rollback is False
    assert verdict.persistent_breaches == []


def test_repeated_breach_of_same_metric_forces_rollback(model):
    """THE headline scenario: the same metric (redis_latency) breaches on
    2 of the last 5 samples. Each sample individually would PROMOTE on its
    own health score -- but the pattern across samples must force ROLLBACK
    on the second occurrence."""
    tracker = BreachTracker(window_size=5, min_breaches=2)
    scorer = HealthScorer(model, SlaEngine(), breach_tracker=tracker)

    v1 = scorer.score("svcX", _borderline_sample())
    assert v1.decision == "PROMOTE"
    assert v1.forced_rollback is False

    v2 = scorer.score("svcX", _borderline_sample())
    assert v2.health_score >= 70  # the health score itself still looks fine
    assert v2.decision == "ROLLBACK"  # but the gate overrides it
    assert v2.forced_rollback is True
    assert any(p.metric == "redis_latency" for p in v2.persistent_breaches)


def test_single_isolated_breach_does_not_force_rollback_with_tracker(model):
    """One breach, then a clean sample -- must NOT trip the override."""
    tracker = BreachTracker(window_size=5, min_breaches=2)
    scorer = HealthScorer(model, SlaEngine(), breach_tracker=tracker)

    scorer.score("svcY", _borderline_sample())
    clean = _borderline_sample(redis_latency=3.0)  # no breach this time
    v2 = scorer.score("svcY", clean)
    assert v2.decision == "PROMOTE"
    assert v2.forced_rollback is False


def test_already_rollback_sample_is_not_double_flagged_as_forced(model):
    """If the sample would ROLLBACK on its own merits anyway, the tracker
    may still flag it as persistent, but forced_rollback should reflect
    whether the OVERRIDE changed the outcome, not just that breaches exist."""
    tracker = BreachTracker(window_size=5, min_breaches=1)  # trips on 1st breach
    scorer = HealthScorer(model, SlaEngine(), breach_tracker=tracker)

    # A sample with enough breaches to already ROLLBACK on health score.
    bad = _borderline_sample(
        memory_usage=82, pod_restarts=4, http_error_rate=2.8,
        p99_latency=380, availability=97.5, endpoint_latency=410,
        packet_loss=1.8,
    )
    v = scorer.score("svcZ", bad)
    assert v.decision == "ROLLBACK"
    assert v.forced_rollback is False  # already ROLLBACK on its own merits


def test_verdict_is_still_json_serialisable_with_persistent_breaches(model):
    import json as _json
    tracker = BreachTracker(window_size=5, min_breaches=1)
    scorer = HealthScorer(model, SlaEngine(), breach_tracker=tracker)
    v = scorer.score("svcW", _borderline_sample())
    _json.dumps(v.to_dict())  # must not raise
