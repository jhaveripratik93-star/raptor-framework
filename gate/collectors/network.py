"""Network probe collector — endpoint_latency, packet_loss.

Probes a downstream endpoint by opening TCP connections and timing them. From N
attempts it derives:
  - endpoint_latency: average successful connect time in ms
  - packet_loss:      percentage of attempts that failed to connect

A TCP-connect probe is used rather than ICMP ping because ICMP usually requires
elevated privileges and is often blocked in Kubernetes networks, whereas a
connect to the service port reflects what the canary's callers actually see.
"""

from __future__ import annotations

import socket
import time

from .base import CollectorError


class NetworkProbeCollector:
    name = "network_probe"
    provides = ("endpoint_latency", "packet_loss")

    def __init__(self, host: str, port: int, probe_count: int = 10,
                 timeout_seconds: float = 2.0, connect=None):
        self.host = host
        self.port = port
        self.probe_count = max(1, probe_count)
        self.timeout = timeout_seconds
        # `connect` is an injectable connect fn(host, port, timeout) -> None
        # that raises on failure; defaults to a real TCP connect. Enables tests.
        self._connect = connect or self._tcp_connect

    @classmethod
    def from_config(cls, cfg: dict) -> "NetworkProbeCollector":
        return cls(
            host=cfg["target_host"],
            port=int(cfg["target_port"]),
            probe_count=int(cfg.get("probe_count", 10)),
            timeout_seconds=float(cfg.get("timeout_seconds", 2)),
        )

    @staticmethod
    def _tcp_connect(host: str, port: int, timeout: float) -> None:
        with socket.create_connection((host, port), timeout=timeout):
            pass

    def collect(self) -> dict[str, float]:
        successes = 0
        latencies_ms: list[float] = []
        for _ in range(self.probe_count):
            start = time.perf_counter()
            try:
                self._connect(self.host, self.port, self.timeout)
            except OSError:
                continue  # counts as a lost packet
            latencies_ms.append((time.perf_counter() - start) * 1000.0)
            successes += 1

        loss_pct = 100.0 * (self.probe_count - successes) / self.probe_count

        if successes == 0:
            # Total loss: report the loss but there is no latency to average.
            # Use the timeout as the latency figure so the SLA correctly breaches.
            raise CollectorError(
                f"network probe to {self.host}:{self.port} lost all "
                f"{self.probe_count} attempts (100% packet loss)"
            )

        avg_latency = sum(latencies_ms) / len(latencies_ms)
        return {
            "endpoint_latency": round(avg_latency, 3),
            "packet_loss": round(loss_pct, 3),
        }
