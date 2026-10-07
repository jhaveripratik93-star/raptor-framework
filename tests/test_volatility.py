"""Tests for the within-window breach-fraction checker (gate/signals/volatility.py)
and its wiring into HealthScorer (gate/scoring.py).

Covers:
  - WindowVolatilityChecker in isolation (fraction math, min_samples floor)
  - HealthScorer forcing ROLLBACK when raw_values reveal a majority-bad
    window the aggregated value alone hides
  - The exact two real-world sequences used to validate this feature:
      [10, 30, 80.2, 80.6, 40, 50, 60]       -> 2/7 breaches (29%), should
                                                 NOT trip the default 30%
                                                 threshold on its own
      [50, 50, 80.2, 80.6, 40, 90, 90]        -> 4/7 breaches (57%), MUST
                                                 trip it and force ROLLBACK
                                                 even though Mean=68.7 looks
                                                 healthy
  - HealthScorer behaving EXACTLY as before when no checker/raw_values are
    supplied (the opt-in / non-breaking guarantee).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gate.signals.volatility import WindowVolatilityChecker
from gate.metrics import BY_NAME
from gate.model import AnomalyModel
from gate.sla import SlaEngine
from gate.scoring import HealthScorer
from gate.dataio import load_baseline

DATA = Path(__file__).resolve().parent.parent / "data"

CPU_SPEC = BY_NAME["cpu_usage"]  # threshold=80.0, direction="max"


# --------------------------------------------------- WindowVolatilityChecker
def test_check_returns_none_below_min_fraction():
    checker = WindowVolatilityChecker(min_fraction=0.3, min_samples=3)
    # [10, 30, 80.2, 80.6, 40, 50, 60] -> 2/7 = 28.6%, just under 30%.
    raw = [10, 30, 80.2, 80.6, 40, 50, 60]
    result = checker.check(CPU_SPEC, raw, aggregated_value=50.1)
    assert result is None


def test_check_flags_majority_bad_window():
    checker = WindowVolatilityChecker(min_fraction=0.3, min_samples=3)
    # [50, 50, 80.2, 80.6, 40, 90, 90] -> 4/7 = 57.1%, well over 30%.
    raw = [50, 50, 80.2, 80.6, 40, 90, 90]
    result = checker.check(CPU_SPEC, raw, aggregated_value=68.686)
    assert result is not None
    assert result.metric == "cpu_usage"
    assert result.breach_count == 4
    assert result.sample_count == 7
    assert result.breach_fraction == pytest.approx(4 / 7, abs=1e-3)
    assert "68.686" in result.detail or "68.69" in result.detail


def test_check_returns_none_below_min_samples():
    checker = WindowVolatilityChecker(min_fraction=0.3, min_samples=5)
    raw = [90, 90]  # 100% breach, but only 2 samples -- not enough to judge
    result = checker.check(CPU_SPEC, raw, aggregated_value=90.0)
    assert result is None


def test_check_rejects_invalid_params():
    with pytest.raises(ValueError):
        WindowVolatilityChecker(min_fraction=0.0)
    with pytest.raises(ValueError):
        WindowVolatilityChecker(min_fraction=1.5)
    with pytest.raises(ValueError):
        WindowVolatilityChecker(min_samples=0)


def test_check_all_only_examines_metrics_with_raw_values():
    from gate.metrics import METRICS
    checker = WindowVolatilityChecker(min_fraction=0.3, min_samples=3)
    raw_by_metric = {"cpu_usage": [50, 50, 80.2, 80.6, 40, 90, 90]}
    aggregated = {"cpu_usage": 68.686}
    results = checker.check_all(METRICS, raw_by_metric, aggregated)
    assert len(results) == 1
    assert results[0].metric == "cpu_usage"


# ------------------------------------------------- HealthScorer integration
@pytest.fixture(scope="module")
def model():
    baseline = load_baseline(DATA / "v1_baseline.csv")
    return AnomalyModel.train(baseline)


def _healthy_other_metrics():
    """Row 0 of data/v1_baseline.csv minus cpu_usage -- scores a clean 100
    anomaly health against this baseline when cpu_usage is also healthy."""
    return dict(
        memory_usage=51.0, pod_restarts=0.0, http_error_rate=0.1,
        p99_latency=81.0, availability=99.9, redis_latency=3.0,
        endpoint_latency=86.0, packet_loss=0.1,
    )


def test_user_sequence_one_does_not_force_rollback(model):
    """[10, 30, 80.2, 80.6, 40, 50, 60] -- Mean=50.1 (healthy), 2/7=28.6%
    breach fraction, just under the default 30% threshold. Must NOT force
    a rollback via the volatility signal (it may still legitimately
    PROMOTE or ROLLBACK based on the aggregated value's own merits, but the
    window-volatility override specifically must not fire)."""
    checker = WindowVolatilityChecker(min_fraction=0.3, min_samples=3)
    scorer = HealthScorer(model, SlaEngine(), volatility_checker=checker)

    raw_seq = [10, 30, 80.2, 80.6, 40, 50, 60]
    mean_cpu = sum(raw_seq) / len(raw_seq)
    sample = dict(cpu_usage=mean_cpu, **_healthy_other_metrics())

    verdict = scorer.score("svc", sample, raw_values={"cpu_usage": raw_seq})
    assert verdict.window_breaches == []
    assert verdict.decision == "PROMOTE"
    assert verdict.forced_rollback is False


def test_user_sequence_two_forces_rollback_despite_healthy_mean(model):
    """[50, 50, 80.2, 80.6, 40, 90, 90] -- Mean=68.7 (comfortably under the
    80% SLA, scores a clean PROMOTE on the aggregated value alone), but
    4/7=57.1% of raw ticks breached. The window-volatility signal MUST
    force ROLLBACK here -- this is the exact scenario the feature exists
    for."""
    checker = WindowVolatilityChecker(min_fraction=0.3, min_samples=3)
    scorer = HealthScorer(model, SlaEngine(), volatility_checker=checker)

    raw_seq = [50, 50, 80.2, 80.6, 40, 90, 90]
    mean_cpu = sum(raw_seq) / len(raw_seq)
    sample = dict(cpu_usage=mean_cpu, **_healthy_other_metrics())

    # Confirm the premise: the aggregated value alone would PROMOTE.
    baseline_scorer = HealthScorer(model, SlaEngine())  # no volatility checker
    baseline_verdict = baseline_scorer.score("svc", sample)
    assert baseline_verdict.decision == "PROMOTE"
    assert baseline_verdict.health_score >= 70

    # Now with the checker + raw values supplied: must force ROLLBACK.
    verdict = scorer.score("svc", sample, raw_values={"cpu_usage": raw_seq})
    assert verdict.decision == "ROLLBACK"
    assert verdict.forced_rollback is True
    assert len(verdict.window_breaches) == 1
    assert verdict.window_breaches[0].metric == "cpu_usage"
    assert verdict.window_breaches[0].breach_count == 4
    assert verdict.window_breaches[0].sample_count == 7


def test_no_raw_values_means_no_volatility_check(model):
    """Omitting raw_values entirely must skip signal 4 -- old behaviour,
    e.g. local/CSV mode where only one aggregated value per row exists."""
    checker = WindowVolatilityChecker(min_fraction=0.3, min_samples=3)
    scorer = HealthScorer(model, SlaEngine(), volatility_checker=checker)
    sample = dict(cpu_usage=68.686, **_healthy_other_metrics())

    verdict = scorer.score("svc", sample)  # no raw_values kwarg at all
    assert verdict.window_breaches == []
    assert verdict.decision == "PROMOTE"


def test_no_checker_means_no_volatility_check_even_with_raw_values(model):
    """Supplying raw_values without a volatility_checker must also skip
    signal 4 -- both must be present for it to activate."""
    scorer = HealthScorer(model, SlaEngine())  # no volatility_checker
    sample = dict(cpu_usage=68.686, **_healthy_other_metrics())
    raw_seq = [50, 50, 80.2, 80.6, 40, 90, 90]

    verdict = scorer.score("svc", sample, raw_values={"cpu_usage": raw_seq})
    assert verdict.window_breaches == []
    assert verdict.decision == "PROMOTE"


def test_verdict_json_serialisable_with_window_breaches(model):
    import json
    checker = WindowVolatilityChecker(min_fraction=0.3, min_samples=3)
    scorer = HealthScorer(model, SlaEngine(), volatility_checker=checker)
    sample = dict(cpu_usage=68.686, **_healthy_other_metrics())
    raw_seq = [50, 50, 80.2, 80.6, 40, 90, 90]
    verdict = scorer.score("svc", sample, raw_values={"cpu_usage": raw_seq})
    json.dumps(verdict.to_dict())  # must not raise
