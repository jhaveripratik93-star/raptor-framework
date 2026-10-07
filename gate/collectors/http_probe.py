"""HTTP probe collector — measures 5 user-facing metrics from live requests.

For services that expose no application metrics to Prometheus (and with no
latency/availability data in InfluxDB), we measure what a caller actually
experiences by making N real HTTP requests to the service and observing the
responses. From those N probes we derive:

  - endpoint_latency : mean latency (ms) of successful requests
  - p99_latency      : 99th-percentile latency (ms) of successful requests
  - http_error_rate  : % of responses with a 5xx status
  - availability     : % of probes that got any HTTP response (not a conn error)
  - packet_loss      : % of probes that failed to connect at all

This is active black-box probing rather than passive scraping — honest,
first-hand data gathered the way the service's clients see it.
"""

from __future__ import annotations

import time

import requests

from .base import CollectorError


class HttpProbeCollector:
    name = "http_probe"
    provides = (
        "endpoint_latency", "p99_latency", "http_error_rate",
        "availability", "packet_loss",
    )

    def __init__(self, base_url: str, path: str = "/", probe_count: int = 10,
                 timeout_seconds: float = 5.0,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.path = path if path.startswith("/") else f"/{path}"
        self.probe_count = max(1, probe_count)
        self.timeout = timeout_seconds
        self._session = session or requests.Session()

    @classmethod
    def from_config(cls, cfg: dict) -> "HttpProbeCollector":
        return cls(
            base_url=cfg["base_url"],
            path=cfg.get("path", "/"),
            probe_count=int(cfg.get("probe_count", 10)),
            timeout_seconds=float(cfg.get("timeout_seconds", 5)),
        )

    def _one_probe(self) -> tuple[bool, int | None, float | None]:
        """Return (connected, status_code, latency_ms).

        connected is False only on a transport-level failure (no HTTP response).
        A 5xx still counts as connected — the server answered.
        """
        url = f"{self.base_url}{self.path}"
        start = time.perf_counter()
        try:
            resp = self._session.get(url, timeout=self.timeout)
        except requests.RequestException:
            return (False, None, None)
        latency_ms = (time.perf_counter() - start) * 1000.0
        return (True, resp.status_code, latency_ms)

    def collect(self) -> dict[str, float]:
        connected = 0
        server_errors = 0
        latencies: list[float] = []

        for _ in range(self.probe_count):
            ok, status, latency_ms = self._one_probe()
            if not ok:
                continue  # connection failure -> counts as packet loss
            connected += 1
            if latency_ms is not None:
                latencies.append(latency_ms)
            if status is not None and 500 <= status < 600:
                server_errors += 1

        total = self.probe_count
        packet_loss = 100.0 * (total - connected) / total
        availability = 100.0 * connected / total

        if connected == 0:
            # No HTTP response at all across every probe. Report the failure so
            # the gate fail-closes rather than scoring a fabricated sample.
            raise CollectorError(
                f"http probe to {self.base_url}{self.path} got no response in "
                f"{total} attempts (0% availability)"
            )

        error_rate = 100.0 * server_errors / connected
        endpoint_latency = sum(latencies) / len(latencies)
        p99 = _percentile(latencies, 99.0)

        return {
            "endpoint_latency": round(endpoint_latency, 3),
            "p99_latency": round(p99, 3),
            "http_error_rate": round(error_rate, 3),
            "availability": round(availability, 3),
            "packet_loss": round(packet_loss, 3),
        }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        raise CollectorError("percentile of empty latency list")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac
