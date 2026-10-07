"""Aggregates all live collectors into one metric-sample dict.

The result is the same shape gate.dataio produces in local/CSV mode, so the
SLA engine, model, and scoring layer are identical across modes.

Each collector is run independently. A failing collector is recorded (its
metrics are missing from the sample) rather than aborting the whole run — the
caller decides whether a partial sample is acceptable via `require_complete`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..metrics import METRIC_NAMES
from .base import Collector, CollectorError


@dataclass
class CollectionResult:
    sample: dict[str, float] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)  # collector name -> msg

    @property
    def complete(self) -> bool:
        return all(m in self.sample for m in METRIC_NAMES)

    @property
    def missing(self) -> list[str]:
        return [m for m in METRIC_NAMES if m not in self.sample]


class MetricAggregator:
    def __init__(self, collectors: list[Collector]):
        self.collectors = collectors

    def collect(self, require_complete: bool = True) -> CollectionResult:
        result = CollectionResult()
        for collector in self.collectors:
            try:
                partial = collector.collect()
            except CollectorError as exc:
                result.errors[collector.name] = str(exc)
                continue
            except Exception as exc:  # defensive: unexpected client errors
                result.errors[collector.name] = f"unexpected: {exc}"
                continue
            # Only accept metrics the collector declared it provides.
            for metric in collector.provides:
                if metric in partial:
                    result.sample[metric] = float(partial[metric])

        if require_complete and not result.complete:
            detail = ", ".join(
                f"{name}: {msg}" for name, msg in result.errors.items()
            ) or "no collector errors reported"
            raise CollectorError(
                f"incomplete metric sample; missing {result.missing}. "
                f"Collector errors: {detail}"
            )
        return result

    # ------------------------------------------------------------- factory
    @classmethod
    def from_config(cls, cfg: dict) -> "MetricAggregator":
        """Build the aggregator from collectors.yaml.

        Collectors are added only for the sections present in the config, so a
        deployment can use whatever sources it actually has. The real-cluster
        setup uses:
          prometheus  -> cpu_usage, memory_usage (cAdvisor)
          kubernetes  -> pod_restarts
          redis       -> redis_latency
          http_probe  -> endpoint_latency, p99_latency, http_error_rate,
                         availability, packet_loss (active black-box probing)
        Legacy sections (influxdb, network_probe) are still honoured if present.
        """
        from .prometheus import PrometheusCollector
        from .redis_latency import RedisCollector
        from .kubernetes_restarts import KubernetesCollector
        from .http_probe import HttpProbeCollector

        window = cfg.get("window", "5m")
        collectors: list[Collector] = []

        if "prometheus" in cfg:
            collectors.append(PrometheusCollector.from_config(cfg["prometheus"]))
        if "kubernetes" in cfg:
            collectors.append(KubernetesCollector.from_config(cfg["kubernetes"]))
        if "redis" in cfg:
            collectors.append(RedisCollector.from_config(cfg["redis"]))
        if "http_probe" in cfg:
            collectors.append(HttpProbeCollector.from_config(cfg["http_probe"]))

        # Legacy / optional sources (only if still configured).
        if "influxdb" in cfg:
            from .influxdb import InfluxDbCollector
            collectors.append(
                InfluxDbCollector.from_config(cfg["influxdb"], window=window)
            )
        if "network_probe" in cfg:
            from .network import NetworkProbeCollector
            collectors.append(
                NetworkProbeCollector.from_config(cfg["network_probe"])
            )

        return cls(collectors)
