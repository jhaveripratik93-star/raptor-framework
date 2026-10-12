"""Tests for scripts/discover_collectors.py.

All Kubernetes/Prometheus/HTTP clients are faked (same pattern as
tests/test_collectors.py) -- no live cluster or network needed. Covers:
  - listing Deployments as candidate workloads
  - resolving Services that belong to a workload via label-selector overlap
  - the Prometheus JMX-pattern confirm-or-hint logic (confirmed vs not)
  - the HTTP port-probe logic (first responding port wins)
  - end-to-end discover() producing a DiscoveryResult
  - build_draft_yaml() rendering both the confirmed and UNVERIFIED paths
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from discover_collectors import (  # noqa: E402
    list_workloads, find_services_for_labels, find_redis_service,
    probe_prometheus, probe_http_ports, discover, build_draft_yaml,
    DiscoveryResult, ServiceInfo,
)


# ------------------------------------------------------------------- fakes
class _ObjNS:
    """Minimal attribute bag, mirrors kubernetes client model objects."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _deployment(name, match_labels):
    return _ObjNS(
        metadata=_ObjNS(name=name),
        spec=_ObjNS(selector=_ObjNS(match_labels=match_labels)),
    )


def _service(name, selector, cluster_ip="10.0.0.1", ports=(8080,)):
    # cluster_ip=None mirrors a headless Service in the real K8s API.
    return _ObjNS(
        metadata=_ObjNS(name=name),
        spec=_ObjNS(
            selector=selector, cluster_ip=cluster_ip,
            ports=[_ObjNS(port=p) for p in ports],
        ),
    )


class _FakeAppsApi:
    def __init__(self, deployments):
        self._deployments = deployments

    def list_namespaced_deployment(self, namespace=None):
        return _ObjNS(items=self._deployments)


class _FakeCoreApi:
    def __init__(self, services):
        self._services = services

    def list_namespaced_service(self, namespace=None):
        return _ObjNS(items=self._services)


class _FakeResp:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self._ok = ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("http error")

    def json(self):
        return self._payload


class _FakePromSession:
    """Scripted responses keyed by substring match on the query param."""
    def __init__(self, scalar_by_query_substring=None, series=None):
        self.scalar_by_query_substring = scalar_by_query_substring or {}
        self.series = series or []

    def get(self, url, params=None, timeout=None):
        if url.endswith("/api/v1/series"):
            return _FakeResp({
                "status": "success",
                "data": [{"__name__": n} for n in self.series],
            })
        query = (params or {}).get("query", "")
        for substr, value in self.scalar_by_query_substring.items():
            if substr in query:
                return _FakeResp({
                    "status": "success",
                    "data": {"result": [{"value": [0, str(value)]}]},
                })
        return _FakeResp({"status": "success", "data": {"result": []}})


class _FakeHttpSession:
    """Scripted: a set of (host, port) that respond; everything else raises."""
    def __init__(self, reachable_ports: set[int]):
        self.reachable_ports = reachable_ports

    def get(self, url, timeout=None):
        for port in self.reachable_ports:
            if f":{port}" in url:
                return _FakeResp({})
        raise ConnectionError("refused")


# --------------------------------------------------------------- workloads
def test_list_workloads_reads_name_and_selector():
    api = _FakeAppsApi([
        _deployment("svc-a", {"app": "svc-a"}),
        _deployment("svc-b", {"app": "svc-b", "tier": "backend"}),
    ])
    candidates = list_workloads(api, "ns1")
    assert [c.name for c in candidates] == ["svc-a", "svc-b"]
    assert candidates[1].pod_label_selector == "app=svc-b,tier=backend"


def test_list_workloads_empty_namespace():
    assert list_workloads(_FakeAppsApi([]), "ns1") == []


# ---------------------------------------------------------------- services
def test_find_services_for_labels_matches_subset_selector():
    services = [
        _service("svc-a", {"app": "svc-a"}, ports=(8080, 9090)),
        _service("unrelated", {"app": "other"}),
    ]
    core = _FakeCoreApi(services)
    found = find_services_for_labels(core, "ns1", {"app": "svc-a", "tier": "backend"})
    assert len(found) == 1
    assert found[0].name == "svc-a"
    assert found[0].dns_name == "svc-a.ns1.svc.cluster.local"
    assert found[0].ports == [8080, 9090]


def test_find_services_for_labels_no_match():
    core = _FakeCoreApi([_service("other", {"app": "other"})])
    assert find_services_for_labels(core, "ns1", {"app": "svc-a"}) == []


def test_find_services_skips_services_with_no_selector():
    core = _FakeCoreApi([_service("headless", {})])
    assert find_services_for_labels(core, "ns1", {"app": "x"}) == []


def test_find_redis_service_matches_by_name():
    core = _FakeCoreApi([
        _service("redis-master", {"app": "redis"}, ports=(6379,)),
        _service("svc-a", {"app": "svc-a"}),
    ])
    found = find_redis_service(core, "ns1")
    assert found is not None
    assert found.name == "redis-master"
    assert found.dns_name == "redis-master.ns1.svc.cluster.local"


def test_find_redis_service_none_when_absent():
    core = _FakeCoreApi([_service("svc-a", {"app": "svc-a"})])
    assert find_redis_service(core, "ns1") is None


def test_find_redis_service_prefers_clusterip_over_headless_and_skips_exporter():
    """Regression test for a real cluster shape: redis-headless (StatefulSet
    pod DNS, no ClusterIP), redis-master (the real VIP), and redis-metrics
    (a Prometheus exporter sidecar on port 9121, NOT the Redis protocol
    port) all exist side by side. Must land on redis-master, not any of the
    other two -- picking redis-metrics would point redis.host at an
    exporter's HTTP port; picking redis-headless works but isn't the
    client-facing VIP every bundled config in this repo already uses."""
    core = _FakeCoreApi([
        _service("redis-headless", {"app": "redis"}, cluster_ip=None, ports=(6379,)),
        _service("redis-master", {"app": "redis"}, cluster_ip="10.110.158.175", ports=(6379,)),
        _service("redis-metrics", {"app": "redis"}, cluster_ip="10.97.68.75", ports=(9121,)),
    ])
    found = find_redis_service(core, "robot-poc")
    assert found is not None
    assert found.name == "redis-master"


def test_find_redis_service_falls_back_to_headless_if_no_vip_exists():
    core = _FakeCoreApi([
        _service("redis-headless", {"app": "redis"}, cluster_ip=None, ports=(6379,)),
    ])
    found = find_redis_service(core, "ns1")
    assert found is not None
    assert found.name == "redis-headless"


def test_find_redis_service_ignores_name_match_without_protocol_port():
    """A Service named 'redis-metrics' alone (no VIP-bearing sibling) must
    NOT be returned just because its name contains 'redis' -- it doesn't
    expose port 6379, so it can't actually be the thing redis.host points
    at."""
    core = _FakeCoreApi([
        _service("redis-metrics", {"app": "redis"}, ports=(9121,)),
    ])
    assert find_redis_service(core, "ns1") is None


# ------------------------------------------------------------- prometheus
def test_probe_prometheus_confirmed_when_both_queries_return_values():
    session = _FakePromSession(scalar_by_query_substring={
        "processcpuload": 0.42, "heapmemoryusage": 55.0,
    })
    confirmed, cpu_q, mem_q, hints = probe_prometheus(session, "http://x:9090", "my-job")
    assert confirmed is True
    assert "my-job" in cpu_q
    assert "my-job" in mem_q
    assert hints == []


def test_probe_prometheus_not_confirmed_falls_back_to_hints():
    session = _FakePromSession(series=[
        "jvm_cpu_percent", "jvm_memory_used_bytes", "http_requests_total",
    ])
    confirmed, cpu_q, mem_q, hints = probe_prometheus(session, "http://x:9090", "my-job")
    assert confirmed is False
    assert cpu_q is None and mem_q is None
    assert "jvm_cpu_percent" in hints
    assert "jvm_memory_used_bytes" in hints
    assert "http_requests_total" not in hints


def test_probe_prometheus_partial_match_is_not_confirmed():
    # Only the CPU query resolves -- must not be treated as confirmed.
    session = _FakePromSession(scalar_by_query_substring={"processcpuload": 1.0})
    confirmed, cpu_q, mem_q, hints = probe_prometheus(session, "http://x:9090", "my-job")
    assert confirmed is False
    assert cpu_q is None and mem_q is None


# ------------------------------------------------------------------ http
def test_probe_http_ports_returns_first_reachable():
    session = _FakeHttpSession(reachable_ports={9013})
    port = probe_http_ports(session, "svc.ns1.svc.cluster.local", [8080, 1235, 9013])
    assert port == 9013


def test_probe_http_ports_none_reachable():
    session = _FakeHttpSession(reachable_ports=set())
    port = probe_http_ports(session, "svc.ns1.svc.cluster.local", [8080])
    assert port is None


# --------------------------------------------------------------- discover()
def test_discover_end_to_end_all_confirmed():
    apps = _FakeAppsApi([_deployment("eric-cenx-rest-api", {"app": "eric-cenx-rest-api"})])
    core = _FakeCoreApi([
        _service("eric-cenx-rest-api", {"app": "eric-cenx-rest-api"}, ports=(8080,)),
        _service("redis-master", {"app": "redis"}, ports=(6379,)),
    ])
    prom = _FakePromSession(scalar_by_query_substring={
        "processcpuload": 0.1, "heapmemoryusage": 40.0,
    })
    http = _FakeHttpSession(reachable_ports={8080})

    result = discover(
        apps, core, prom, http,
        namespace="robot-poc", workload="eric-cenx-rest-api",
        prometheus_base_url="http://localhost:9090",
    )

    assert result.pod_label_selector == "app=eric-cenx-rest-api"
    assert result.redis_service.name == "redis-master"
    assert result.prometheus_job_confirmed is True
    assert result.http_probe_confirmed is True
    assert result.http_probe_port == 8080


def test_discover_unknown_workload_raises():
    apps = _FakeAppsApi([_deployment("svc-a", {"app": "svc-a"})])
    core = _FakeCoreApi([])
    with pytest.raises(ValueError, match="not found"):
        discover(apps, core, None, None, namespace="ns1", workload="svc-b",
                 prometheus_base_url=None)


def test_discover_without_prometheus_arg_skips_confirmation():
    apps = _FakeAppsApi([_deployment("svc-a", {"app": "svc-a"})])
    core = _FakeCoreApi([_service("svc-a", {"app": "svc-a"})])
    result = discover(apps, core, None, None, namespace="ns1", workload="svc-a",
                       prometheus_base_url=None)
    assert result.prometheus_job_confirmed is False
    assert result.prometheus_candidate_series == []


# ----------------------------------------------------------- draft render
def test_build_draft_yaml_confirmed_path_has_no_warnings():
    result = DiscoveryResult(
        namespace="robot-poc", workload="svc-a",
        pod_label_selector="app=svc-a",
        services=[ServiceInfo("svc-a", "10.0.0.1", [8080], "svc-a.robot-poc.svc.cluster.local")],
        redis_service=ServiceInfo("redis-master", "10.0.0.2", [6379],
                                   "redis-master.robot-poc.svc.cluster.local"),
        prometheus_job_confirmed=True,
        prometheus_cpu_query='100 * avg(java_lang_operatingsystem_processcpuload{job="svc-a"})',
        prometheus_memory_query='100 * sum(...)',
        prometheus_candidate_series=[],
        http_probe_service=ServiceInfo("svc-a", "10.0.0.1", [8080], "svc-a.robot-poc.svc.cluster.local"),
        http_probe_port=8080,
        http_probe_confirmed=True,
    )
    draft = build_draft_yaml(result)
    # The fixed top-of-file banner always mentions "UNVERIFIED" generically
    # (it's instructional, not a per-section flag); what matters is that no
    # per-section "⚠ UNVERIFIED:" marker appears when everything was confirmed.
    assert "⚠ UNVERIFIED" not in draft
    assert "workload: svc-a" in draft
    assert 'host: "redis-master.robot-poc.svc.cluster.local"' in draft
    assert 'base_url: "http://svc-a.robot-poc.svc.cluster.local:8080"' in draft


def test_build_draft_yaml_unconfirmed_path_flags_warnings():
    result = DiscoveryResult(
        namespace="robot-poc", workload="svc-b",
        pod_label_selector="app=svc-b",
        services=[ServiceInfo("svc-b", "10.0.0.3", [1235, 9013], "svc-b.robot-poc.svc.cluster.local")],
        redis_service=None,
        prometheus_job_confirmed=False,
        prometheus_cpu_query=None,
        prometheus_memory_query=None,
        prometheus_candidate_series=["weird_metric_name"],
        http_probe_service=ServiceInfo("svc-b", "10.0.0.3", [1235, 9013], "svc-b.robot-poc.svc.cluster.local"),
        http_probe_port=None,
        http_probe_confirmed=False,
    )
    draft = build_draft_yaml(result)
    assert draft.count("UNVERIFIED") >= 3  # prometheus, redis, http_probe
    assert "weird_metric_name" in draft
    assert "TODO" in draft


def test_build_draft_yaml_never_claims_confirmation_it_lacks():
    """Regression guard: the confirmed-looking lines must only appear when
    the corresponding *_confirmed flag is actually True."""
    result = DiscoveryResult(
        namespace="ns1", workload="svc-c", pod_label_selector="app=svc-c",
        services=[], redis_service=None,
        prometheus_job_confirmed=False, prometheus_cpu_query=None,
        prometheus_memory_query=None, prometheus_candidate_series=[],
        http_probe_service=None, http_probe_port=None, http_probe_confirmed=False,
    )
    draft = build_draft_yaml(result)
    assert "confirmed live" not in draft.lower() or "⚠" in draft
    assert "no Service with 'redis'" in draft or "redis" in draft.lower()
