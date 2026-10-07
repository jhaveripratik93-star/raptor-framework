"""Automated tests for the deployment gate.

Mirrors the test table in the framework document (section 8):

    test_healthy_component_passes      test_availability_threshold
    test_degraded_component_fails      test_redis_latency_threshold
    test_anomaly_score_present         test_packet_loss_threshold
    test_sla_violations_listed         test_latency_threshold
    test_cpu_sla_threshold             test_http_error_rate_threshold
    test_memory_sla_threshold          test_pod_restart_threshold
"""

from __future__ import annotations

from gate import GATE_THRESHOLD
from gate.sla import SlaEngine


# --------------------------------------------------------------- gate outcomes
def test_healthy_component_passes(scorer, healthy_sample):
    """Healthy metrics -> health score above the gate threshold and PROMOTE."""
    verdict = scorer.score("v2", healthy_sample)
    assert verdict.decision == "PROMOTE"
    assert verdict.health_score >= GATE_THRESHOLD
    assert verdict.exit_code == 0


def test_degraded_component_fails(scorer, degraded_sample):
    """Degraded metrics -> health score below the gate threshold and ROLLBACK."""
    verdict = scorer.score("v2-degraded", degraded_sample)
    assert verdict.decision == "ROLLBACK"
    assert verdict.health_score < GATE_THRESHOLD
    assert verdict.exit_code == 1


def test_anomaly_score_present(scorer, healthy_sample, degraded_sample):
    """Every verdict includes an Isolation Forest anomaly score in [0, 100]."""
    for sample in (healthy_sample, degraded_sample):
        verdict = scorer.score("v2", sample)
        assert isinstance(verdict.anomaly_score, float)
        assert 0.0 <= verdict.anomaly_score <= 100.0


def test_sla_violations_listed(scorer, degraded_sample):
    """All breached SLA thresholds are enumerated in the verdict output.

    The document's degraded scenario has 8 breaches (CPU at 78 stays under 80).
    """
    verdict = scorer.score("v2-degraded", degraded_sample)
    breached = {v.metric for v in verdict.violations}
    assert breached == {
        "memory_usage", "pod_restarts", "http_error_rate", "p99_latency",
        "availability", "redis_latency", "endpoint_latency", "packet_loss",
    }
    assert len(verdict.violations) == 8
    # CPU at 78 is under the 80 threshold -> must NOT be flagged.
    assert "cpu_usage" not in breached


# --------------------------------------------------- per-metric SLA thresholds
# Each test starts from a healthy sample and pushes exactly one metric over.
def _sla():
    return SlaEngine()


def test_cpu_sla_threshold(healthy_baseline_point):
    s = healthy_baseline_point
    assert not _sla().check_metric("cpu_usage", 79.0)
    assert _sla().check_metric("cpu_usage", 81.0)
    s["cpu_usage"] = 85.0
    assert any(v.metric == "cpu_usage" for v in _sla().evaluate(s))


def test_memory_sla_threshold(healthy_baseline_point):
    s = healthy_baseline_point
    assert not _sla().check_metric("memory_usage", 74.0)
    assert _sla().check_metric("memory_usage", 76.0)
    s["memory_usage"] = 82.0
    assert any(v.metric == "memory_usage" for v in _sla().evaluate(s))


def test_pod_restart_threshold(healthy_baseline_point):
    s = healthy_baseline_point
    assert not _sla().check_metric("pod_restarts", 3.0)   # 3 is the limit, ok
    assert _sla().check_metric("pod_restarts", 4.0)
    s["pod_restarts"] = 4.0
    assert any(v.metric == "pod_restarts" for v in _sla().evaluate(s))


def test_http_error_rate_threshold(healthy_baseline_point):
    s = healthy_baseline_point
    assert not _sla().check_metric("http_error_rate", 0.9)
    assert _sla().check_metric("http_error_rate", 1.1)
    s["http_error_rate"] = 2.8
    assert any(v.metric == "http_error_rate" for v in _sla().evaluate(s))


def test_latency_threshold(healthy_baseline_point):
    s = healthy_baseline_point
    assert not _sla().check_metric("p99_latency", 199.0)
    assert _sla().check_metric("p99_latency", 201.0)
    s["p99_latency"] = 380.0
    assert any(v.metric == "p99_latency" for v in _sla().evaluate(s))


def test_availability_threshold(healthy_baseline_point):
    # Availability is a "min" metric: below 99.5 is the breach.
    s = healthy_baseline_point
    assert not _sla().check_metric("availability", 99.6)
    assert _sla().check_metric("availability", 99.4)
    s["availability"] = 97.5
    assert any(v.metric == "availability" for v in _sla().evaluate(s))


def test_redis_latency_threshold(healthy_baseline_point):
    s = healthy_baseline_point
    assert not _sla().check_metric("redis_latency", 9.0)
    assert _sla().check_metric("redis_latency", 11.0)
    s["redis_latency"] = 18.0
    assert any(v.metric == "redis_latency" for v in _sla().evaluate(s))


def test_packet_loss_threshold(healthy_baseline_point):
    s = healthy_baseline_point
    assert not _sla().check_metric("packet_loss", 0.9)
    assert _sla().check_metric("packet_loss", 1.1)
    s["packet_loss"] = 1.8
    assert any(v.metric == "packet_loss" for v in _sla().evaluate(s))


# ------------------------------------------------------------ extra safety net
def test_verdict_is_json_serialisable(scorer, degraded_sample):
    import json
    verdict = scorer.score("v2-degraded", degraded_sample)
    json.dumps(verdict.to_dict())  # must not raise


def test_endpoint_latency_threshold(healthy_baseline_point):
    s = healthy_baseline_point
    assert _sla().check_metric("endpoint_latency", 410.0)
    s["endpoint_latency"] = 410.0
    assert any(v.metric == "endpoint_latency" for v in _sla().evaluate(s))
