"""The 9 gate features, declared Raptor-style.

Each declaration binds a canonical metric to its source and a 5-minute
aggregation window — exactly the "5-minute aggregated feature per metric per
service" the framework document describes. The aggregation function is chosen
to match each metric's meaning:

  - latencies use P99 (tail latency is what the SLA cares about)
  - error rate / cpu / memory / redis / packet loss use Mean over the window
  - availability uses Mean
  - pod restarts use Max (the worst offender in the window)

Importing this module registers all nine features.
"""

from __future__ import annotations

from .registry import feature, aggregation, AggregationFunction as F

WINDOW = "5m"


@feature(source="prometheus", keys="component")
@aggregation(function=F.Mean, over=WINDOW)
def cpu_usage(samples):
    return samples


@feature(source="prometheus", keys="component")
@aggregation(function=F.Mean, over=WINDOW)
def memory_usage(samples):
    return samples


@feature(source="kubernetes", keys="component")
@aggregation(function=F.Max, over=WINDOW)
def pod_restarts(samples):
    return samples


@feature(source="prometheus", keys="component")
@aggregation(function=F.Mean, over=WINDOW)
def http_error_rate(samples):
    return samples


@feature(source="influxdb", keys="component")
@aggregation(function=F.P99, over=WINDOW)
def p99_latency(samples):
    return samples


@feature(source="influxdb", keys="component")
@aggregation(function=F.Mean, over=WINDOW)
def availability(samples):
    return samples


@feature(source="redis", keys="component")
@aggregation(function=F.Mean, over=WINDOW)
def redis_latency(samples):
    return samples


@feature(source="network_probe", keys="component")
@aggregation(function=F.P99, over=WINDOW)
def endpoint_latency(samples):
    return samples


@feature(source="network_probe", keys="component")
@aggregation(function=F.Mean, over=WINDOW)
def packet_loss(samples):
    return samples
