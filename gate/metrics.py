"""Canonical definitions for the 9 metrics the deployment gate monitors.

This is the single source of truth. The SLA engine, the model feature order,
the CSV loaders, and the tests all derive from METRICS so they can never drift
apart.

Each metric has:
  - name        : canonical key used everywhere (CSV columns, feature vectors)
  - source      : where it comes from in production mode (document section 5)
  - unit        : human-readable unit for reporting
  - threshold   : the SLA limit
  - direction   : how to compare the value against the threshold
                  "max" -> value must be <= threshold (lower is better)
                  "min" -> value must be >= threshold (higher is better)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricSpec:
    name: str
    source: str
    unit: str
    threshold: float
    direction: str  # "max" or "min"

    def is_breach(self, value: float) -> bool:
        """Return True if `value` violates this metric's SLA threshold."""
        if self.direction == "max":
            return value > self.threshold
        if self.direction == "min":
            return value < self.threshold
        raise ValueError(f"unknown direction {self.direction!r} for {self.name}")

    def describe_breach(self, value: float) -> str:
        op = "<=" if self.direction == "max" else ">="
        return (
            f"{self.name}={value}{self.unit} violates SLA "
            f"(must be {op} {self.threshold}{self.unit})"
        )


# Order matters: this defines the feature-vector order fed to the model.
# Thresholds and sources are taken directly from the framework document,
# section 5 "Metrics Monitored & SLA Thresholds".
METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("cpu_usage",        "Prometheus",     "%",  80.0, "max"),
    MetricSpec("memory_usage",     "Prometheus",     "%",  75.0, "max"),
    MetricSpec("pod_restarts",     "Kubernetes API", "",    3.0, "max"),
    MetricSpec("http_error_rate",  "Prometheus",     "%",   1.0, "max"),
    MetricSpec("p99_latency",      "InfluxDB",       "ms", 200.0, "max"),
    MetricSpec("availability",     "InfluxDB",       "%",  99.5, "min"),
    MetricSpec("redis_latency",    "Redis INFO",     "ms", 10.0, "max"),
    MetricSpec("endpoint_latency", "Network probe",  "ms", 200.0, "max"),
    MetricSpec("packet_loss",      "Network probe",  "%",   1.0, "max"),
)

METRIC_NAMES: tuple[str, ...] = tuple(m.name for m in METRICS)

BY_NAME: dict[str, MetricSpec] = {m.name: m for m in METRICS}


def feature_vector(sample: dict[str, float]) -> list[float]:
    """Extract metric values from a sample dict in canonical METRICS order.

    Raises KeyError if the sample is missing any monitored metric, so we fail
    loudly rather than silently scoring a partial vector.
    """
    return [float(sample[name]) for name in METRIC_NAMES]
