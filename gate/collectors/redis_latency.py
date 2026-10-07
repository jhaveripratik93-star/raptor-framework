"""Redis collector — redis_latency.

Measures Redis round-trip latency by timing a series of PINGs and averaging
them (in milliseconds). This is the metric the gate scores against the < 10ms
SLA. Uses the `redis` client; the connection is authenticated with a password
read from the environment.
"""

from __future__ import annotations

import os
import time

from .base import CollectorError


class RedisCollector:
    name = "redis"
    provides = ("redis_latency",)

    def __init__(self, host: str, port: int, password: str | None,
                 latency_samples: int = 5, timeout_seconds: float = 5.0,
                 client=None):
        self.host = host
        self.port = port
        self.password = password
        self.latency_samples = max(1, latency_samples)
        self.timeout = timeout_seconds
        self._client = client  # inject for tests

    @classmethod
    def from_config(cls, cfg: dict) -> "RedisCollector":
        password_env = cfg.get("password_env", "REDIS_PASSWORD")
        password = os.environ.get(password_env) or None
        return cls(
            host=cfg["host"],
            port=int(cfg["port"]),
            password=password,
            latency_samples=int(cfg.get("latency_samples", 5)),
            timeout_seconds=float(cfg.get("timeout_seconds", 5)),
        )

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import redis
        except ImportError as exc:  # pragma: no cover
            raise CollectorError("redis package not installed") from exc
        self._client = redis.Redis(
            host=self.host, port=self.port, password=self.password,
            socket_timeout=self.timeout, socket_connect_timeout=self.timeout,
        )
        return self._client

    def collect(self) -> dict[str, float]:
        client = self._get_client()
        latencies_ms: list[float] = []
        try:
            for _ in range(self.latency_samples):
                start = time.perf_counter()
                if not client.ping():
                    raise CollectorError("redis PING returned falsy")
                latencies_ms.append((time.perf_counter() - start) * 1000.0)
        except CollectorError:
            raise
        except Exception as exc:  # redis.exceptions.* and socket errors
            raise CollectorError(f"redis latency probe failed: {exc}") from exc

        avg_ms = sum(latencies_ms) / len(latencies_ms)
        return {"redis_latency": round(avg_ms, 3)}
