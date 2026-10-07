"""Prometheus collector — cpu_usage, memory_usage, http_error_rate.

Queries the Prometheus HTTP API (`/api/v1/query`) with the PromQL expressions
from collectors.yaml. Each query is an instant query returning a single scalar
(the PromQL itself already averages over the 5-minute window).
"""

from __future__ import annotations

import requests

from .base import CollectorError


class PrometheusCollector:
    name = "prometheus"

    def __init__(self, base_url: str, queries: dict[str, str],
                 timeout_seconds: float = 10.0,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.queries = queries
        self.timeout = timeout_seconds
        self._session = session or requests.Session()
        # Provide exactly the metrics that have a configured query, so the
        # collector adapts to whatever Prometheus can actually answer (e.g.
        # cpu+memory only, when the service exposes no HTTP metrics).
        self.provides = tuple(queries.keys())

    @classmethod
    def from_config(cls, cfg: dict) -> "PrometheusCollector":
        return cls(
            base_url=cfg["base_url"],
            queries=cfg["queries"],
            timeout_seconds=float(cfg.get("timeout_seconds", 10)),
        )

    def _query_scalar(self, promql: str) -> float:
        url = f"{self.base_url}/api/v1/query"
        try:
            resp = self._session.get(
                url, params={"query": promql}, timeout=self.timeout
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise CollectorError(f"prometheus query failed: {exc}") from exc

        if payload.get("status") != "success":
            raise CollectorError(
                f"prometheus returned status={payload.get('status')}: "
                f"{payload.get('error')}"
            )

        result = payload.get("data", {}).get("result", [])
        if not result:
            raise CollectorError(
                f"prometheus query returned no series: {promql!r}"
            )
        # Instant vector: take the first series' value [timestamp, "value"].
        try:
            value = float(result[0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise CollectorError(
                f"could not parse prometheus value from {result[0]!r}"
            ) from exc
        return value

    def collect(self) -> dict[str, float]:
        sample: dict[str, float] = {}
        for metric in self.provides:
            promql = self.queries.get(metric)
            if not promql:
                raise CollectorError(f"no PromQL configured for {metric!r}")
            sample[metric] = self._query_scalar(promql)
        return sample
