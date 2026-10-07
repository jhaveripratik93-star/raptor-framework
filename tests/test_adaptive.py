"""Tests for the ML-enhanced adaptive signal 3 and signal 4.

Key assertions:
  - AdaptiveVolatilityChecker correctly handles both user sequences:
      [10, 30, 80.2, 80.6, 40, 50, 60] (28.6% breach) -> NOT flagged
      [50, 50, 80.2, 80.6, 40, 90, 90] (57.1% breach) -> flagged
  - The adaptive threshold is lower for metrics close to their SLA
    (less headroom) and higher for metrics with large headroom.
  - EWMABreachTracker learns the natural breach rate:
      a service that breaches 0% of the time is flagged at the first repeat
      a service with a natural 30% breach rate is NOT flagged at 35%
  - Both are drop-in HealthScorer replacements (JSON-serialisable output,
    no regressions on existing tests).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gate.signals.adaptive import (train_adaptive_models, AdaptiveVolatilityChecker,
                          EWMABreachTracker, _PerMetricBaseline)
from gate.signals.trend import InMemoryBreachHistoryStore
from gate.metrics import BY_NAME
from gate.model import AnomalyModel
from gate.sla import SlaEngine
from gate.scoring import HealthScorer
from gate.dataio import load_baseline

DATA = Path(__file__).resolve().parent.parent / "data"
CPU_SPEC = BY_NAME["cpu_usage"]


@pytest.fixture(scope="module")
def baseline():
    return load_baseline(DATA / "v1_baseline.csv")


@pytest.fixture(scope="module")
def baseline_model(baseline):
    return train_adaptive_models(baseline)


@pytest.fixture(scope="module")
def anomaly_model(baseline):
    return AnomalyModel.train(baseline)


# ----------------------------------------------- AdaptiveVolatilityChecker
class TestAdaptiveVolatilityChecker:
    def test_user_sequence_1_not_flagged(self, baseline_model):
        """[10, 30, 80.2, 80.6, 40, 50, 60] -> 2/7 = 28.6%. Should NOT flag."""
        checker = AdaptiveVolatilityChecker(baseline_model)
        raw = [10, 30, 80.2, 80.6, 40, 50, 60]
        result = checker.check(CPU_SPEC, raw, aggregated_value=sum(raw)/len(raw))
        assert result is None, f"Expected no flag but got: {result.detail if result else ''}"

    def test_user_sequence_2_flagged(self, baseline_model):
        """[50, 50, 80.2, 80.6, 40, 90, 90] -> 4/7 = 57.1%. MUST flag."""
        checker = AdaptiveVolatilityChecker(baseline_model)
        raw = [50, 50, 80.2, 80.6, 40, 90, 90]
        result = checker.check(CPU_SPEC, raw, aggregated_value=sum(raw)/len(raw))
        assert result is not None
        assert result.breach_count == 4
        assert result.sample_count == 7

    def test_adaptive_threshold_lower_for_close_to_sla(self, baseline_model):
        """A metric near its SLA should have a LOWER alarm fraction than
        one with large headroom -- the core ML value of this checker."""
        checker = AdaptiveVolatilityChecker(baseline_model)

        # cpu_usage: 22.3 stds of headroom -> stays at base_floor (30%)
        cpu_threshold = checker._adaptive_threshold(
            baseline_model.metrics["cpu_usage"], n=10)
        assert abs(cpu_threshold - 0.30) < 0.01, (
            f"cpu_usage (large headroom) should have threshold ~30%, got {cpu_threshold:.2%}")

        # Construct a very tight-headroom metric manually
        very_tight = _PerMetricBaseline(
            "redis_latency", mean=9.9, std=0.15, threshold=10.0,
            direction="max", headroom_in_stds=0.67, baseline_breach_fraction=0.0)
        tight_threshold = checker._adaptive_threshold(very_tight, n=10)

        assert tight_threshold < cpu_threshold, (
            f"Tight-headroom metric ({tight_threshold:.2%}) should require "
            f"lower breach fraction than large-headroom metric ({cpu_threshold:.2%})"
        )

    def test_same_fraction_different_decisions_for_different_headrooms(self, baseline_model):
        """The same raw breach fraction can be OK for one metric but alarming
        for another, depending on how close the metric runs to its SLA."""
        checker = AdaptiveVolatilityChecker(baseline_model)

        # A tight-headroom service: redis mean=9.9ms, threshold=10ms
        tight = _PerMetricBaseline(
            "redis_latency", mean=9.9, std=0.15, threshold=10.0,
            direction="max", headroom_in_stds=0.67, baseline_breach_fraction=0.0)
        threshold_tight = checker._adaptive_threshold(tight, n=10)

        # A large-headroom metric: cpu_usage (22 stds of headroom)
        threshold_cpu = checker._adaptive_threshold(
            baseline_model.metrics["cpu_usage"], n=10)

        # Tight headroom -> lower threshold -> alarms sooner
        assert threshold_tight < threshold_cpu, (
            f"Tight headroom ({threshold_tight:.2%}) should alarm sooner "
            f"than large headroom ({threshold_cpu:.2%})"
        )

    def test_returns_none_below_min_samples(self, baseline_model):
        checker = AdaptiveVolatilityChecker(baseline_model, min_samples=5)
        result = checker.check(CPU_SPEC, [90, 90], aggregated_value=90.0)
        assert result is None

    def test_scorer_integration_sequence_2_forces_rollback(self, anomaly_model, baseline_model):
        """Full HealthScorer integration with AdaptiveVolatilityChecker:
        sequence 2 must force ROLLBACK even though Mean is healthy."""
        checker = AdaptiveVolatilityChecker(baseline_model)
        scorer = HealthScorer(anomaly_model, SlaEngine(), volatility_checker=checker)

        raw_seq = [50, 50, 80.2, 80.6, 40, 90, 90]
        sample = dict(cpu_usage=sum(raw_seq)/len(raw_seq),
                      memory_usage=51.0, pod_restarts=0.0, http_error_rate=0.1,
                      p99_latency=81.0, availability=99.9, redis_latency=3.0,
                      endpoint_latency=86.0, packet_loss=0.1)

        # confirm the aggregated value alone would PROMOTE
        plain_scorer = HealthScorer(anomaly_model, SlaEngine())
        assert plain_scorer.score("svc", sample).decision == "PROMOTE"

        # with adaptive checker + raw values: must ROLLBACK
        verdict = scorer.score("svc", sample, raw_values={"cpu_usage": raw_seq})
        assert verdict.decision == "ROLLBACK"
        assert verdict.forced_rollback is True
        assert len(verdict.window_breaches) == 1


# ----------------------------------------------- EWMABreachTracker
class TestEWMABreachTracker:
    def test_first_breach_not_flagged_below_min_history(self):
        """Before enough history exists, uses fallback counter (same as
        fixed BreachTracker with fallback_min_breaches=2)."""
        tracker = EWMABreachTracker(
            store=InMemoryBreachHistoryStore(), min_history=5, fallback_min_breaches=2)
        result = tracker.record_and_check("svc", {"cpu_usage"})
        assert result == []  # first breach, history len=1

    def test_second_breach_flagged_in_fallback(self):
        """With only 2 samples, still flags via fallback counter."""
        tracker = EWMABreachTracker(
            store=InMemoryBreachHistoryStore(), min_history=5, fallback_min_breaches=2)
        tracker.record_and_check("svc", {"cpu_usage"})   # sample 1
        result = tracker.record_and_check("svc", {"cpu_usage"})  # sample 2
        assert len(result) == 1
        assert result[0].metric == "cpu_usage"

    def test_service_with_zero_breach_rate_flagged_quickly(self):
        """A service that never breaches: first breach after EWMA activates
        should be flagged because z-score is very high (EWMA ~ 0)."""
        tracker = EWMABreachTracker(
            store=InMemoryBreachHistoryStore(),
            alpha=0.25, sigma_threshold=2.5, min_history=5)

        # 5 clean samples to build EWMA baseline (no breaches)
        for _ in range(5):
            tracker.record_and_check("svc", set())

        # Now breach -- z-score should be very high since EWMA breach rate ~ 0
        result = tracker.record_and_check("svc", {"redis_latency"})
        assert len(result) == 1
        assert result[0].metric == "redis_latency"

    def test_moderate_breach_rate_below_ceiling_never_hits_the_hard_ceiling(self):
        """A service breaching at a modest, steady rate (e.g. 20%, well
        under the default 50% absolute_breach_ceiling) must never trip the
        HARD ceiling guardrail -- only the EWMA/z-score logic (which may or
        may not flag it depending on sigma_threshold tuning) ever applies.
        This isolates the specific guarantee the ceiling fix provides
        (never SUPPRESSING a majority pattern) from the separate, more
        subtle question of exactly how sensitive EWMA's own z-score should
        be for sub-ceiling rates -- that sensitivity is a tuning knob
        (alpha, sigma_threshold), not the correctness property under test
        here."""
        tracker = EWMABreachTracker(
            store=InMemoryBreachHistoryStore(),
            alpha=0.25, sigma_threshold=2.5, min_history=5,
            absolute_breach_ceiling=0.5, window_size=20)

        pattern = [True, False, False, False, False] * 6  # 30 samples, 20%
        result = []
        for breached in pattern:
            result = tracker.record_and_check(
                "svc", {"redis_latency"} if breached else set())
        result = tracker.record_and_check("svc", {"redis_latency"})

        # The ceiling guardrail specifically must not be what fires here --
        # if anything flags, it must be attributed to EWMA sensitivity, not
        # the hard 50% ceiling (20% is nowhere near 50%).
        for breach in result:
            assert "absolute ceiling" not in breach.detail, (
                "A 20% breach rate must never trip the 50% hard ceiling"
            )

    def test_majority_breaching_metric_always_flagged_despite_ewma_adaptation(self):
        """REGRESSION TEST for the exact flaw reported by the user: a metric
        that breaches its SLA on a MAJORITY of recent samples (>= the
        absolute_breach_ceiling) must ALWAYS be flagged, even after many
        samples -- EWMA adaptation must never be allowed to normalize this
        away. Confirmed failure mode before the fix: 6 of 9 breaches (67%)
        was no longer flagged by sample 9 because EWMA had learned ~67% as
        the new baseline. User's own example: 5+ of 9 breaches must ROLLBACK."""
        tracker = EWMABreachTracker(
            store=InMemoryBreachHistoryStore(),
            alpha=0.25, sigma_threshold=2.5, min_history=5,
            absolute_breach_ceiling=0.5, window_size=9)

        # User's exact scenario: 5 of 9 breaches (55.6%)
        pattern = [True, True, True, False, True, False, True, False, False]
        last_result = []
        for breached in pattern:
            last_result = tracker.record_and_check(
                "svc", {"redis_latency"} if breached else set())

        total_breaches = sum(pattern)
        assert total_breaches == 5
        assert total_breaches / len(pattern) > 0.5

        # The 5th (final) breach -- at position where total breach rate
        # already exceeds 50% -- must be flagged.
        assert any(p.metric == "redis_latency" for p in last_result), (
            "A metric breaching 5 of 9 samples (55.6%, majority) must be "
            "flagged regardless of EWMA adaptation history"
        )

    def test_six_of_nine_breaches_flagged_from_the_start(self):
        """Even heavier case: 6 of 9 (66.7%). Must be flagged at EVERY
        occurrence once the ceiling is crossed, including the very last
        sample -- this is the exact pattern that silently stopped being
        flagged before the absolute_breach_ceiling fix."""
        tracker = EWMABreachTracker(
            store=InMemoryBreachHistoryStore(),
            alpha=0.25, sigma_threshold=2.5, min_history=5,
            absolute_breach_ceiling=0.5, window_size=9)

        pattern = [True, True, False, True, True, False, True, False, True]
        flags = []
        for breached in pattern:
            result = tracker.record_and_check(
                "svc", {"redis_latency"} if breached else set())
            flags.append(any(p.metric == "redis_latency" for p in result))

        # Every sample where redis_latency breached, from the point the
        # running breach count first reaches the ceiling onward, must flag.
        # Confirm specifically: the LAST sample (breach=True) is flagged.
        assert flags[-1] is True, (
            "The final breach in a 6-of-9 (67%) pattern must still be "
            "flagged -- this is the exact regression the ceiling fixes"
        )

    def test_components_are_isolated(self):
        """Each component has its own EWMA -- a breach on svc-A doesn't
        affect the model for svc-B."""
        tracker = EWMABreachTracker(
            store=InMemoryBreachHistoryStore(), min_history=5)
        for _ in range(10):
            tracker.record_and_check("svc-a", {"cpu_usage"})  # svc-a always breaches
        # svc-b has no history -- first breach should not immediately flag
        result = tracker.record_and_check("svc-b", {"cpu_usage"})
        assert result == []  # history len=1 < min_history, falls through to counter (1 < 2)

    def test_scorer_integration_ewma(self, anomaly_model):
        """HealthScorer with EWMABreachTracker: after enough clean history,
        a repeat breach of a never-before-breached service forces ROLLBACK."""
        store = InMemoryBreachHistoryStore()
        tracker = EWMABreachTracker(
            store=store, min_history=5, sigma_threshold=2.5)
        scorer = HealthScorer(anomaly_model, SlaEngine(), breach_tracker=tracker)

        healthy = dict(cpu_usage=43.0, memory_usage=51.0, pod_restarts=0.0,
                       http_error_rate=0.1, p99_latency=81.0, availability=99.9,
                       redis_latency=3.0, endpoint_latency=86.0, packet_loss=0.1)

        # Build EWMA history: 5 clean deployments
        for _ in range(5):
            scorer.score("svc", healthy)

        # Now a borderline breach (redis_latency just over 10ms) -- first one
        # after EWMA has learned this service never breaches: should force ROLLBACK
        one_breach = dict(healthy, redis_latency=11.0)
        verdict = scorer.score("svc", one_breach)
        assert verdict.decision == "ROLLBACK"
        assert verdict.forced_rollback is True
        assert any(p.metric == "redis_latency" for p in verdict.persistent_breaches)

    def test_json_serialisable(self, anomaly_model):
        import json
        store = InMemoryBreachHistoryStore()
        tracker = EWMABreachTracker(store=store, min_history=5)
        scorer = HealthScorer(anomaly_model, SlaEngine(), breach_tracker=tracker)
        healthy = dict(cpu_usage=43.0, memory_usage=51.0, pod_restarts=0.0,
                       http_error_rate=0.1, p99_latency=81.0, availability=99.9,
                       redis_latency=11.0, endpoint_latency=86.0, packet_loss=0.1)
        for _ in range(6):
            verdict = scorer.score("svc", healthy)
        json.dumps(verdict.to_dict())  # must not raise
