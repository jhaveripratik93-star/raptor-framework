"""Live metric collectors for production mode.

Each collector queries one data source (Prometheus, InfluxDB, Redis, the
Kubernetes API, or a network probe) and returns a partial metric sample:
a dict mapping canonical metric names (from gate.metrics) to float values.

The aggregator merges all partial samples into the single metric-sample dict
that the rest of the gate already consumes — the exact same shape produced by
gate.dataio in local/CSV mode. This is the seam that lets the gate run against
CSV or live infra without any change to the SLA engine, model, or scoring.
"""

from .base import Collector, CollectorError
from .aggregator import MetricAggregator

__all__ = ["Collector", "CollectorError", "MetricAggregator"]
