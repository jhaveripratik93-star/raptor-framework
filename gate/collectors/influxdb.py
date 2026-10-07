"""InfluxDB collector — p99_latency, availability.

Queries InfluxDB 2.x via the Flux HTTP API (`/api/v2/query`). Each metric maps
to a measurement/field in collectors.yaml. p99_latency uses a 99th-percentile
aggregate over the window; availability uses the window mean.
"""

from __future__ import annotations

import csv
import io
import os

import requests

from .base import CollectorError


class InfluxDbCollector:
    name = "influxdb"
    provides = ("p99_latency", "availability")

    def __init__(self, base_url: str, org: str, bucket: str, token: str,
                 measurements: dict[str, dict[str, str]], window: str = "5m",
                 timeout_seconds: float = 10.0,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.org = org
        self.bucket = bucket
        self.token = token
        self.measurements = measurements
        self.window = window
        self.timeout = timeout_seconds
        self._session = session or requests.Session()

    @classmethod
    def from_config(cls, cfg: dict, window: str = "5m") -> "InfluxDbCollector":
        token_env = cfg.get("token_env", "INFLUX_TOKEN")
        token = os.environ.get(token_env, "")
        if not token:
            raise CollectorError(
                f"InfluxDB token not set; export {token_env}=<token>"
            )
        return cls(
            base_url=cfg["base_url"],
            org=cfg["org"],
            bucket=cfg["bucket"],
            token=token,
            measurements=cfg["measurements"],
            window=window,
            timeout_seconds=float(cfg.get("timeout_seconds", 10)),
        )

    def _flux(self, measurement: str, field: str, aggregate: str) -> str:
        # aggregate is a Flux fn call fragment, e.g. 'quantile(q: 0.99)' or 'mean()'
        return (
            f'from(bucket: "{self.bucket}")\n'
            f"  |> range(start: -{self.window})\n"
            f'  |> filter(fn: (r) => r._measurement == "{measurement}")\n'
            f'  |> filter(fn: (r) => r._field == "{field}")\n'
            f"  |> {aggregate}\n"
            f"  |> keep(columns: [\"_value\"])"
        )

    def _run_flux(self, flux: str) -> float:
        url = f"{self.base_url}/api/v2/query"
        headers = {
            "Authorization": f"Token {self.token}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        }
        try:
            resp = self._session.post(
                url, params={"org": self.org}, data=flux.encode("utf-8"),
                headers=headers, timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise CollectorError(f"influxdb query failed: {exc}") from exc

        return self._parse_first_value(resp.text)

    @staticmethod
    def _parse_first_value(annotated_csv: str) -> float:
        """Extract the first _value from InfluxDB's annotated CSV response."""
        reader = csv.reader(io.StringIO(annotated_csv))
        header: list[str] | None = None
        for row in reader:
            if not row or row[0].startswith("#"):
                continue
            if header is None:
                header = row
                continue
            if "_value" not in header:
                raise CollectorError("influxdb response missing _value column")
            idx = header.index("_value")
            if idx < len(row) and row[idx] != "":
                try:
                    return float(row[idx])
                except ValueError as exc:
                    raise CollectorError(
                        f"non-numeric influxdb _value: {row[idx]!r}"
                    ) from exc
        raise CollectorError("influxdb query returned no data rows")

    def collect(self) -> dict[str, float]:
        p99_cfg = self.measurements["p99_latency"]
        avail_cfg = self.measurements["availability"]
        return {
            "p99_latency": self._run_flux(
                self._flux(p99_cfg["measurement"], p99_cfg["field"],
                           "quantile(q: 0.99)")
            ),
            "availability": self._run_flux(
                self._flux(avail_cfg["measurement"], avail_cfg["field"],
                           "mean()")
            ),
        }
