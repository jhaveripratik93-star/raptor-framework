"""Tests for the Phase 3 live collectors, using mocked clients only.

No real network / cluster access — each collector's external client is faked so
the parsing, aggregation, and fail-closed behaviour can be verified offline.
"""

from __future__ import annotations

import pytest

from gate.collectors.base import CollectorError
from gate.collectors.prometheus import PrometheusCollector
from gate.collectors.influxdb import InfluxDbCollector
from gate.collectors.redis_latency import RedisCollector
from gate.collectors.kubernetes_restarts import KubernetesCollector
from gate.collectors.network import NetworkProbeCollector
from gate.collectors.aggregator import MetricAggregator


# --------------------------------------------------------------- Prometheus
class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, value):
        self._value = value

    def get(self, url, params=None, timeout=None):
        return _FakeResp({
            "status": "success",
            "data": {"result": [{"value": [123456789, str(self._value)]}]},
        })


def test_prometheus_collector_parses_scalar():
    queries = {"cpu_usage": "q1", "memory_usage": "q2", "http_error_rate": "q3"}
    c = PrometheusCollector("http://x:9090", queries,
                            session=_FakeSession(46.0))
    sample = c.collect()
    assert sample == {"cpu_usage": 46.0, "memory_usage": 46.0,
                      "http_error_rate": 46.0}


def test_prometheus_provides_derives_from_queries():
    # Real-cluster config: only cpu + memory are queryable from Prometheus.
    c = PrometheusCollector("http://x:9090",
                            {"cpu_usage": "q1", "memory_usage": "q2"},
                            session=_FakeSession(50.0))
    assert c.provides == ("cpu_usage", "memory_usage")
    assert c.collect() == {"cpu_usage": 50.0, "memory_usage": 50.0}


def test_prometheus_empty_result_raises():
    class _EmptySession:
        def get(self, url, params=None, timeout=None):
            return _FakeResp({"status": "success", "data": {"result": []}})

    c = PrometheusCollector("http://x:9090", {"cpu_usage": "q"},
                            session=_EmptySession())
    c.provides = ("cpu_usage",)
    with pytest.raises(CollectorError):
        c.collect()


# ----------------------------------------------------------------- InfluxDB
class _FakeInfluxSession:
    def __init__(self, value):
        self._value = value

    def post(self, url, params=None, data=None, headers=None, timeout=None):
        csv = f"#datatype,string\n,result,table,_value\n,_result,0,{self._value}\n"
        return _FakeResp(None) if False else _InfluxResp(csv)


class _InfluxResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_influxdb_collector_parses_csv():
    measurements = {
        "p99_latency": {"measurement": "http_latency", "field": "p99_ms"},
        "availability": {"measurement": "uptime", "field": "availability_pct"},
    }
    c = InfluxDbCollector("http://x:8086", "org", "bucket", "token",
                          measurements, session=_FakeInfluxSession(90.0))
    sample = c.collect()
    assert sample == {"p99_latency": 90.0, "availability": 90.0}


# -------------------------------------------------------------------- Redis
class _FakeRedis:
    def ping(self):
        return True


def test_redis_collector_measures_latency():
    c = RedisCollector("localhost", 6379, None, latency_samples=3,
                       client=_FakeRedis())
    sample = c.collect()
    assert "redis_latency" in sample
    assert sample["redis_latency"] >= 0.0


def test_redis_ping_failure_raises():
    class _BadRedis:
        def ping(self):
            raise ConnectionError("no route")

    c = RedisCollector("localhost", 6379, None, client=_BadRedis())
    with pytest.raises(CollectorError):
        c.collect()


# --------------------------------------------------------------- Kubernetes
class _CS:
    def __init__(self, restart_count):
        self.restart_count = restart_count


class _PodStatus:
    def __init__(self, restarts):
        self.container_statuses = [_CS(r) for r in restarts]


class _Pod:
    def __init__(self, restarts):
        self.status = _PodStatus(restarts)


class _PodList:
    def __init__(self, pods):
        self.items = pods


class _FakeK8sApi:
    def __init__(self, pods):
        self._pods = pods

    def list_namespaced_pod(self, namespace=None, label_selector=None):
        return _PodList(self._pods)


def test_kubernetes_collector_returns_max_restarts():
    api = _FakeK8sApi([_Pod([0, 1]), _Pod([4]), _Pod([2])])
    c = KubernetesCollector("robot-poc", "app=x", api=api)
    sample = c.collect()
    assert sample == {"pod_restarts": 4.0}


def test_kubernetes_no_pods_raises():
    c = KubernetesCollector("robot-poc", "app=x", api=_FakeK8sApi([]))
    with pytest.raises(CollectorError):
        c.collect()


# ------------------------------------------------------------ network probe
def test_network_probe_computes_latency_and_loss():
    calls = {"n": 0}

    def fake_connect(host, port, timeout):
        calls["n"] += 1
        # Fail every 5th attempt to simulate 20% loss.
        if calls["n"] % 5 == 0:
            raise OSError("timeout")

    c = NetworkProbeCollector("h", 80, probe_count=10, connect=fake_connect)
    sample = c.collect()
    assert sample["packet_loss"] == pytest.approx(20.0)
    assert sample["endpoint_latency"] >= 0.0


def test_network_probe_total_loss_raises():
    def always_fail(host, port, timeout):
        raise OSError("down")

    c = NetworkProbeCollector("h", 80, probe_count=3, connect=always_fail)
    with pytest.raises(CollectorError):
        c.collect()


# ------------------------------------------------------------- aggregator
class _StubCollector:
    def __init__(self, name, provides, sample=None, error=None):
        self.name = name
        self.provides = provides
        self._sample = sample or {}
        self._error = error

    def collect(self):
        if self._error:
            raise CollectorError(self._error)
        return self._sample


def test_aggregator_merges_all_metrics():
    collectors = [
        _StubCollector("prometheus", ("cpu_usage", "memory_usage", "http_error_rate"),
                       {"cpu_usage": 46, "memory_usage": 53, "http_error_rate": 0.2}),
        _StubCollector("influxdb", ("p99_latency", "availability"),
                       {"p99_latency": 90, "availability": 99.8}),
        _StubCollector("redis", ("redis_latency",), {"redis_latency": 3}),
        _StubCollector("kubernetes", ("pod_restarts",), {"pod_restarts": 0}),
        _StubCollector("network_probe", ("endpoint_latency", "packet_loss"),
                       {"endpoint_latency": 88, "packet_loss": 0.15}),
    ]
    result = MetricAggregator(collectors).collect(require_complete=True)
    assert result.complete
    assert set(result.sample) == {
        "cpu_usage", "memory_usage", "http_error_rate", "p99_latency",
        "availability", "redis_latency", "endpoint_latency", "packet_loss",
        "pod_restarts",
    }


def test_aggregator_records_partial_failure():
    collectors = [
        _StubCollector("prometheus", ("cpu_usage", "memory_usage", "http_error_rate"),
                       {"cpu_usage": 46, "memory_usage": 53, "http_error_rate": 0.2}),
        _StubCollector("influxdb", ("p99_latency", "availability"),
                       error="influx down"),
        _StubCollector("redis", ("redis_latency",), {"redis_latency": 3}),
        _StubCollector("kubernetes", ("pod_restarts",), {"pod_restarts": 0}),
        _StubCollector("network_probe", ("endpoint_latency", "packet_loss"),
                       {"endpoint_latency": 88, "packet_loss": 0.15}),
    ]
    agg = MetricAggregator(collectors)
    # Non-strict: returns a partial result recording the failure.
    result = agg.collect(require_complete=False)
    assert not result.complete
    assert result.missing == ["p99_latency", "availability"]
    assert "influxdb" in result.errors
    # Strict: raises because the sample is incomplete.
    with pytest.raises(CollectorError):
        agg.collect(require_complete=True)


# --------------------------------------------------------------- HTTP prober
from gate.collectors.http_probe import HttpProbeCollector  # noqa: E402


class _ProbeResp:
    def __init__(self, status_code):
        self.status_code = status_code


class _ProbeSession:
    """Yields a scripted sequence of responses / connection errors."""
    def __init__(self, script):
        # script: list of either int status codes, or the string "fail".
        self._script = list(script)
        self._i = 0

    def get(self, url, timeout=None):
        item = self._script[self._i % len(self._script)]
        self._i += 1
        if item == "fail":
            import requests
            raise requests.ConnectionError("refused")
        return _ProbeResp(item)


def test_http_probe_all_healthy():
    c = HttpProbeCollector("http://svc:8080", path="/", probe_count=10,
                           session=_ProbeSession([200]))
    s = c.collect()
    assert s["availability"] == 100.0
    assert s["packet_loss"] == 0.0
    assert s["http_error_rate"] == 0.0
    assert s["endpoint_latency"] >= 0.0
    assert s["p99_latency"] >= 0.0


def test_http_probe_error_rate_and_loss():
    # 10 probes: 2 connection failures, 2 x 500, rest 200.
    script = ["fail", 500, 200, 200, 200, "fail", 500, 200, 200, 200]
    c = HttpProbeCollector("http://svc:8080", probe_count=10,
                           session=_ProbeSession(script))
    s = c.collect()
    # 8 connected of 10 -> 20% loss, 80% availability.
    assert s["packet_loss"] == pytest.approx(20.0)
    assert s["availability"] == pytest.approx(80.0)
    # 2 of the 8 connected were 5xx -> 25% error rate.
    assert s["http_error_rate"] == pytest.approx(25.0)


def test_http_probe_total_failure_raises():
    c = HttpProbeCollector("http://svc:8080", probe_count=3,
                           session=_ProbeSession(["fail"]))
    with pytest.raises(CollectorError):
        c.collect()


def test_http_probe_from_config():
    cfg = {"base_url": "http://svc:8080", "path": "/health",
           "probe_count": 5, "timeout_seconds": 3}
    c = HttpProbeCollector.from_config(cfg)
    assert c.base_url == "http://svc:8080"
    assert c.path == "/health"
    assert c.probe_count == 5


# --------------------------------------- aggregator built from real-ish config
def test_aggregator_from_config_builds_real_collector_set():
    from gate.collectors.aggregator import MetricAggregator
    from gate.collectors.prometheus import PrometheusCollector
    from gate.collectors.kubernetes_restarts import KubernetesCollector
    from gate.collectors.redis_latency import RedisCollector
    from gate.collectors.http_probe import HttpProbeCollector

    cfg = {
        "window": "5m",
        "prometheus": {"base_url": "http://x:9090",
                       "queries": {"cpu_usage": "q", "memory_usage": "q"}},
        "kubernetes": {"namespace": "robot-poc",
                       "pod_label_selector": "app=x"},
        "redis": {"host": "localhost", "port": 6379},
        "http_probe": {"base_url": "http://svc:8080", "path": "/"},
    }
    agg = MetricAggregator.from_config(cfg)
    kinds = {type(c) for c in agg.collectors}
    assert kinds == {PrometheusCollector, KubernetesCollector,
                     RedisCollector, HttpProbeCollector}
    # Together the collectors must cover all 9 canonical metrics.
    covered = set()
    for c in agg.collectors:
        covered.update(c.provides)
    from gate.metrics import METRIC_NAMES
    assert covered == set(METRIC_NAMES)
