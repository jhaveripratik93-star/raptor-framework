"""Discover live cluster targets and draft a collectors.yaml for review.

WHAT THIS DOES
  Connects to the Kubernetes API (read-only: list Deployments/Services/Pods)
  and, if reachable, to Prometheus, to build a DRAFT collectors.yaml for a
  chosen workload -- instead of hand-typing `target.workload`,
  `pod_label_selector`, `redis.host`, and `http_probe.base_url` the way
  every config under gate/config/ currently is.

WHAT THIS DELIBERATELY DOES NOT DO
  It does not guess which Prometheus metric name means "cpu_usage" or
  "memory_usage" for an arbitrary service. Those PromQL queries are a
  judgment call about what the service actually exposes (cAdvisor? a JMX
  exporter? its own /metrics? nothing?) -- getting this wrong means the
  gate silently scores the WRONG metric with no error anywhere, which is
  worse than today's manual-but-explicit config. So this script:
    - tries the ONE pattern already proven twice in this repo (the JMX
      exporter shape used by eric-cenx-rest-api / eric-cenx-analytics) and
      only writes it into the draft if a live query against Prometheus
      actually returns a value for this workload's job -- confirmed, not
      assumed;
    - otherwise lists candidate series names (filtered to ones whose name
      contains "cpu" or "mem") as a comment, and leaves the query itself as
      an explicit placeholder for a human to fill in, exactly like the
      existing "UNVERIFIED" warnings in collectors_incluster_analytics.yaml.
  Everything else that IS safely mechanical (resolving a Deployment's pod
  selector, finding Services and their ClusterIP/ports, trying each
  http_probe port for any real HTTP response) is filled in automatically,
  because none of that requires guessing the service's own semantics -- it
  is reading structured objects the cluster already has.

USAGE
    python scripts/discover_collectors.py --namespace robot-poc
        # lists Deployments in the namespace, asks you to pick one (or pass
        # --workload to skip the prompt)

    python scripts/discover_collectors.py --namespace robot-poc \
        --workload eric-cenx-rest-api \
        --prometheus http://localhost:9090 \
        --out gate/config/collectors_discovered_eric-cenx-rest-api.yaml

    # In-cluster (e.g. run from inside the `robot` pod):
    python scripts/discover_collectors.py --namespace robot-poc \
        --workload eric-cenx-rest-api --in-cluster \
        --prometheus http://kube-prom-kube-prometheus-prometheus.robot-poc.svc.cluster.local:9090

The output is always written to a NEW file (never overwrites
gate/config/collectors.yaml) and is explicitly marked DRAFT -- review it,
confirm anything flagged UNVERIFIED, then rename/merge it where you want.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ----------------------------------------------------------------- data model
@dataclass
class ServiceInfo:
    name: str
    cluster_ip: str | None
    ports: list[int] = field(default_factory=list)
    dns_name: str = ""  # "<name>.<namespace>.svc.cluster.local"


@dataclass
class WorkloadCandidate:
    name: str
    pod_label_selector: str  # "k=v,k2=v2" form, ready for the K8s API


@dataclass
class DiscoveryResult:
    namespace: str
    workload: str
    pod_label_selector: str
    services: list[ServiceInfo]
    redis_service: ServiceInfo | None
    prometheus_job_confirmed: bool
    prometheus_cpu_query: str | None
    prometheus_memory_query: str | None
    prometheus_candidate_series: list[str]
    http_probe_service: ServiceInfo | None
    http_probe_port: int | None
    http_probe_confirmed: bool


# --------------------------------------------------------- Kubernetes: list
def list_workloads(apps_api, namespace: str) -> list[WorkloadCandidate]:
    """List Deployments in `namespace` as candidate workloads.

    Only reads `metadata.name` and `spec.selector.match_labels` -- both
    structural fields every Deployment has, nothing inferred.
    """
    try:
        deployments = apps_api.list_namespaced_deployment(namespace=namespace)
    except Exception as exc:
        raise RuntimeError(f"could not list deployments in {namespace!r}: {exc}") from exc

    candidates: list[WorkloadCandidate] = []
    for dep in getattr(deployments, "items", []) or []:
        name = dep.metadata.name
        match_labels = (
            getattr(getattr(dep.spec, "selector", None), "match_labels", None) or {}
        )
        selector = ",".join(f"{k}={v}" for k, v in sorted(match_labels.items()))
        candidates.append(WorkloadCandidate(name=name, pod_label_selector=selector))
    return candidates


def find_services_for_labels(core_api, namespace: str,
                              match_labels: dict[str, str]) -> list[ServiceInfo]:
    """Find Services in `namespace` whose selector overlaps `match_labels`.

    A Service "belongs" to a workload if its own selector is a subset of
    (or equal to) the workload's pod labels -- the same relationship
    Kubernetes itself uses to route traffic to those pods.
    """
    try:
        services = core_api.list_namespaced_service(namespace=namespace)
    except Exception as exc:
        raise RuntimeError(f"could not list services in {namespace!r}: {exc}") from exc

    results: list[ServiceInfo] = []
    for svc in getattr(services, "items", []) or []:
        selector = getattr(svc.spec, "selector", None) or {}
        if not selector:
            continue
        if not all(match_labels.get(k) == v for k, v in selector.items()):
            continue
        name = svc.metadata.name
        cluster_ip = getattr(svc.spec, "cluster_ip", None)
        ports = [p.port for p in (getattr(svc.spec, "ports", None) or [])]
        results.append(ServiceInfo(
            name=name, cluster_ip=cluster_ip, ports=ports,
            dns_name=f"{name}.{namespace}.svc.cluster.local",
        ))
    return results


_REDIS_PROTOCOL_PORT = 6379


def find_redis_service(core_api, namespace: str) -> ServiceInfo | None:
    """Find the Redis Service in the namespace that actually speaks the
    Redis wire protocol.

    NOT a name-substring match on its own -- a real cluster can have
    several Services with "redis" in the name that are NOT the thing to
    point `redis.host` at: e.g. a Prometheus exporter sidecar Service
    (name contains "redis", but its port is the EXPORTER's HTTP port, not
    6379) or a headless Service used for StatefulSet pod DNS rather than a
    stable VIP. Picking either of those by name alone would draft a config
    that fails outright, or silently targets the wrong endpoint.

    Instead: filter to Services that both (a) mention "redis" in the name
    (so we don't accidentally match an unrelated Service that happens to
    expose port 6379) and (b) actually list port 6379 among their ports --
    a real, checkable fact, not a guess. Among the matches, prefer one with
    a real ClusterIP over a headless Service (`cluster_ip is None`), since
    that's the stable VIP every bundled config in this repo already
    connects to (e.g. `redis-master.<namespace>.svc.cluster.local`) --
    headless Services exist for StatefulSet pod-DNS, not for clients.
    """
    try:
        services = core_api.list_namespaced_service(namespace=namespace)
    except Exception as exc:
        raise RuntimeError(f"could not list services in {namespace!r}: {exc}") from exc

    candidates: list[ServiceInfo] = []
    for svc in getattr(services, "items", []) or []:
        name = svc.metadata.name
        if "redis" not in name.lower():
            continue
        ports = [p.port for p in (getattr(svc.spec, "ports", None) or [])]
        if _REDIS_PROTOCOL_PORT not in ports:
            continue  # e.g. a redis_exporter sidecar Service -- wrong thing
        candidates.append(ServiceInfo(
            name=name, cluster_ip=getattr(svc.spec, "cluster_ip", None),
            ports=ports, dns_name=f"{name}.{namespace}.svc.cluster.local",
        ))

    if not candidates:
        return None
    # Prefer a Service with a real ClusterIP (a stable VIP clients should
    # connect to) over a headless one (cluster_ip is None or "None").
    with_vip = [c for c in candidates if c.cluster_ip and c.cluster_ip != "None"]
    return with_vip[0] if with_vip else candidates[0]


# -------------------------------------------------------------- Prometheus
# The ONE pattern already proven twice in this repo (eric-cenx-rest-api,
# eric-cenx-analytics): a JMX exporter exposing JVM process CPU load and
# heap memory usage. This is tried and VERIFIED LIVE before being written
# into the draft -- never assumed just because the job exists.
_JMX_CPU_QUERY = '100 * avg(java_lang_operatingsystem_processcpuload{{job="{job}"}})'
_JMX_MEMORY_QUERY = (
    '100 * sum(java_lang_memory_heapmemoryusage_used{{job="{job}"}}) / '
    'clamp_min(sum(java_lang_memory_heapmemoryusage_max{{job="{job}"}}), 1)'
)


def _prom_query_scalar(session, base_url: str, promql: str,
                        timeout: float) -> float | None:
    """Run one PromQL instant query; return the scalar value or None.

    None covers every "this didn't work" case (unreachable, bad query,
    empty result) -- the caller treats that as "not confirmed", not an
    error, since Prometheus being briefly unreachable during discovery is
    not fatal to producing a draft config.
    """
    try:
        resp = session.get(f"{base_url.rstrip('/')}/api/v1/query",
                            params={"query": promql}, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return None
    if payload.get("status") != "success":
        return None
    result = payload.get("data", {}).get("result", [])
    if not result:
        return None
    try:
        return float(result[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _prom_series_names(session, base_url: str, job: str,
                        timeout: float) -> list[str]:
    """List distinct metric (__name__) values scraped under `job`.

    Used only to produce candidate-name HINTS in the draft's comments when
    the proven JMX pattern doesn't match -- never to auto-select a query.
    """
    try:
        resp = session.get(
            f"{base_url.rstrip('/')}/api/v1/series",
            params={"match[]": f'{{job="{job}"}}'}, timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return []
    if payload.get("status") != "success":
        return []
    names = sorted({
        series.get("__name__", "") for series in payload.get("data", [])
        if series.get("__name__")
    })
    return names


def probe_prometheus(session, base_url: str, job: str, timeout: float = 10.0) -> tuple[
    bool, str | None, str | None, list[str]
]:
    """Try the proven JMX pattern for `job`; fall back to listing hints.

    Returns (confirmed, cpu_query_or_none, memory_query_or_none,
    candidate_series_names). `confirmed` is True only if BOTH the CPU and
    memory queries returned a real value against this live Prometheus.
    """
    cpu_q = _JMX_CPU_QUERY.format(job=job)
    mem_q = _JMX_MEMORY_QUERY.format(job=job)
    cpu_val = _prom_query_scalar(session, base_url, cpu_q, timeout)
    mem_val = _prom_query_scalar(session, base_url, mem_q, timeout)

    if cpu_val is not None and mem_val is not None:
        return True, cpu_q, mem_q, []

    names = _prom_series_names(session, base_url, job, timeout)
    hints = [n for n in names if "cpu" in n.lower() or "mem" in n.lower()]
    return False, None, None, hints


# ----------------------------------------------------------------- http probe
def probe_http_ports(session, dns_name: str, ports: list[int],
                      path: str = "/", timeout: float = 5.0) -> int | None:
    """Try each port with a real HTTP GET; return the first that answers.

    Any HTTP response counts (even 404/500) -- only a transport-level
    failure (refused, timeout, DNS) disqualifies a port, matching how
    HttpProbeCollector itself treats "connected" vs "packet loss".
    """
    for port in ports:
        url = f"http://{dns_name}:{port}{path}"
        try:
            session.get(url, timeout=timeout)
            return port
        except Exception:
            continue
    return None


# --------------------------------------------------------------------- draft
def build_draft_yaml(result: DiscoveryResult) -> str:
    """Render a hand-formatted collectors.yaml draft, matching the style of
    the hand-written configs already in gate/config/ -- including the same
    "⚠ UNVERIFIED" banner pattern used in collectors_incluster_analytics.yaml
    for anything this script could not confirm live."""
    lines: list[str] = []
    w = lines.append

    w(f"# DRAFT collectors.yaml for workload '{result.workload}' "
      f"-- generated by scripts/discover_collectors.py")
    w("# REVIEW BEFORE USE. Anything marked UNVERIFIED below was NOT")
    w("# confirmed live and must be checked by hand before trusting this file.")
    w("#")
    w(f"# Namespace: {result.namespace}")
    w("")
    w("target:")
    w(f"  namespace: {result.namespace}")
    w(f"  workload: {result.workload}")
    w(f'  pod_label_selector: "{result.pod_label_selector}"')
    w("")
    w('window: "5m"')
    w("")

    w("prometheus:")
    w('  base_url: "<fill in: localhost:9090 via port-forward, or the '
      'in-cluster Prometheus Service DNS>"')
    w("  timeout_seconds: 10")
    if result.prometheus_job_confirmed:
        w("  queries:")
        w("    # Confirmed live against this Prometheus -- both queries")
        w("    # returned a real value for this workload's job.")
        w(f"    cpu_usage: >-")
        w(f"      {result.prometheus_cpu_query}")
        w(f"    memory_usage: >-")
        w(f"      {result.prometheus_memory_query}")
    else:
        w("  # ⚠ UNVERIFIED: no PromQL query could be confirmed for this")
        w(f"  #   workload's job (tried job=\"{result.workload}\" with the JMX")
        w("  #   exporter pattern used by other services in this repo -- it")
        w("  #   did not return a value, so this service likely exposes")
        w("  #   metrics differently, or isn't scraped under that job name).")
        if result.prometheus_candidate_series:
            w("  #   Candidate series found under this job that mention cpu/mem:")
            for name in result.prometheus_candidate_series[:15]:
                w(f"  #     {name}")
        else:
            w("  #   No series at all were found under this job name -- confirm")
            w("  #   the actual job label via Prometheus's Status > Targets page:")
            w(f'  #     curl "<prometheus>/api/v1/query?query=up"')
        w("  queries:")
        w('    cpu_usage: "<TODO: fill in a real PromQL query>"')
        w('    memory_usage: "<TODO: fill in a real PromQL query>"')
    w("")

    w("kubernetes:")
    w(f"  namespace: {result.namespace}")
    w(f'  pod_label_selector: "{result.pod_label_selector}"')
    w("  in_cluster: false  # set true if the gate runs as a pod in-cluster")
    w("")

    if result.redis_service is not None:
        svc = result.redis_service
        port = svc.ports[0] if svc.ports else 6379
        w("redis:")
        w(f'  host: "{svc.dns_name}"  # in-cluster DNS; use "localhost" over a port-forward')
        w(f"  port: {port}")
        w('  password_env: "REDIS_PASSWORD"')
        w("  timeout_seconds: 5")
        w("  latency_samples: 5")
    else:
        w("# ⚠ UNVERIFIED: no Service with 'redis' in its name was found in")
        w(f"# this namespace. If Redis lives elsewhere, add a 'redis:' section")
        w("# by hand (see gate/config/collectors.yaml for the shape).")
    w("")

    if result.http_probe_service is not None:
        svc = result.http_probe_service
        w("http_probe:")
        if result.http_probe_confirmed and result.http_probe_port:
            w(f'  base_url: "http://{svc.dns_name}:{result.http_probe_port}"  '
              f'# confirmed: got a real HTTP response on this port')
        else:
            ports_list = ", ".join(str(p) for p in svc.ports) or "none found"
            w(f"  # ⚠ UNVERIFIED: none of this Service's ports ({ports_list})")
            w("  #   returned an HTTP response during discovery. Confirm by hand:")
            w(f"  #     kubectl exec -n {result.namespace} <some-pod> -- \\")
            w(f"  #       curl -s -o /dev/null -w 'HTTP_CODE=%{{http_code}}\\n' "
              f"--max-time 5 http://{svc.dns_name}:<port>/")
            first_port = svc.ports[0] if svc.ports else 80
            w(f'  base_url: "http://{svc.dns_name}:{first_port}"  # UNCONFIRMED, pick the right port above')
        w('  path: "/"')
        w("  probe_count: 10")
        w("  timeout_seconds: 5")
    else:
        w("# ⚠ UNVERIFIED: no Service matching this workload's pod labels was")
        w("# found, so no http_probe section could be drafted. Add one by hand")
        w("# once you know the Service/port that fronts this workload.")
    w("")

    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------- main
def discover(apps_api, core_api, prom_session, http_session,
             namespace: str, workload: str, prometheus_base_url: str | None,
             timeout: float = 10.0) -> DiscoveryResult:
    """Run the full discovery flow for one workload. Pure function of its
    injected clients, so this is fully testable with fakes (no live
    cluster or network needed) -- see tests/test_discover_collectors.py."""
    candidates = {c.name: c for c in list_workloads(apps_api, namespace)}
    if workload not in candidates:
        available = ", ".join(sorted(candidates)) or "(none found)"
        raise ValueError(
            f"workload {workload!r} not found in namespace {namespace!r}. "
            f"Available: {available}"
        )
    target = candidates[workload]
    match_labels = dict(
        pair.split("=", 1) for pair in target.pod_label_selector.split(",") if pair
    )

    services = find_services_for_labels(core_api, namespace, match_labels)
    redis_service = find_redis_service(core_api, namespace)

    prom_confirmed = False
    cpu_q = mem_q = None
    series_hints: list[str] = []
    if prometheus_base_url and prom_session is not None:
        prom_confirmed, cpu_q, mem_q, series_hints = probe_prometheus(
            prom_session, prometheus_base_url, workload, timeout
        )

    http_service = services[0] if services else None
    http_port = None
    http_confirmed = False
    if http_service is not None and http_session is not None:
        http_port = probe_http_ports(
            http_session, http_service.dns_name, http_service.ports,
            timeout=min(timeout, 5.0),
        )
        http_confirmed = http_port is not None

    return DiscoveryResult(
        namespace=namespace,
        workload=workload,
        pod_label_selector=target.pod_label_selector,
        services=services,
        redis_service=redis_service,
        prometheus_job_confirmed=prom_confirmed,
        prometheus_cpu_query=cpu_q,
        prometheus_memory_query=mem_q,
        prometheus_candidate_series=series_hints,
        http_probe_service=http_service,
        http_probe_port=http_port,
        http_probe_confirmed=http_confirmed,
    )


def _load_k8s_apis(in_cluster: bool):
    try:
        from kubernetes import client, config
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("kubernetes package not installed") from exc
    if in_cluster:
        config.load_incluster_config()
    else:
        config.load_kube_config()
    return client.AppsV1Api(), client.CoreV1Api()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Discover cluster targets and draft a collectors.yaml"
    )
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--workload", default=None,
                        help="Deployment name to target. If omitted, lists "
                             "candidates found in the namespace and exits.")
    parser.add_argument("--prometheus", default=None,
                        help="Prometheus base URL (e.g. http://localhost:9090). "
                             "Omit to skip Prometheus confirmation entirely.")
    parser.add_argument("--in-cluster", action="store_true",
                        help="Use the pod's own service account instead of "
                             "~/.kube/config")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--out", default=None,
                        help="Where to write the draft. Required once "
                             "--workload is given.")
    args = parser.parse_args(argv)

    apps_api, core_api = _load_k8s_apis(args.in_cluster)

    if not args.workload:
        candidates = list_workloads(apps_api, args.namespace)
        if not candidates:
            print(f"No Deployments found in namespace {args.namespace!r}.",
                  file=sys.stderr)
            return 1
        print(f"Deployments found in {args.namespace!r}:")
        for c in candidates:
            print(f"  - {c.name}  (selector: {c.pod_label_selector!r})")
        print("\nRe-run with --workload <name> --out <path> to draft a config.")
        return 0

    if not args.out:
        parser.error("--out is required when --workload is given")

    prom_session = http_session = None
    if args.prometheus:
        import requests
        prom_session = requests.Session()
    import requests
    http_session = requests.Session()

    print(f"Discovering targets for workload={args.workload!r} "
          f"in namespace={args.namespace!r} ...")
    result = discover(
        apps_api, core_api, prom_session, http_session,
        namespace=args.namespace, workload=args.workload,
        prometheus_base_url=args.prometheus, timeout=args.timeout,
    )

    draft = build_draft_yaml(result)
    out_path = Path(args.out)
    if out_path.exists():
        print(f"ERROR: {out_path} already exists. Choose a different --out "
              f"path; this script never overwrites an existing file.",
              file=sys.stderr)
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Explicit UTF-8: the draft uses "⚠" for UNVERIFIED warnings, which the
    # default platform encoding (cp1252 on Windows) cannot write.
    out_path.write_text(draft, encoding="utf-8")

    print(f"\nWrote draft config to {out_path}")
    print(f"  Prometheus queries confirmed live: {result.prometheus_job_confirmed}")
    print(f"  Redis service found: "
          f"{result.redis_service.name if result.redis_service else 'no'}")
    print(f"  HTTP probe port confirmed: "
          f"{result.http_probe_port if result.http_probe_confirmed else 'no'}")
    print("\nThis is a DRAFT. Review every '⚠ UNVERIFIED' section before use.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
