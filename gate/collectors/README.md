# Phase 3 — Live Collectors (Production Mode)

Turns the gate from CSV-driven (local mode) into one that scores **live** canary
metrics from the Phase-1 infra in `robot-poc`. The collected sample has the exact
same shape as the CSV sample, so the SLA engine, model, and scoring are unchanged.

## Collectors

| Collector | Metrics | Source |
|-----------|---------|--------|
| `PrometheusCollector` | cpu_usage, memory_usage, http_error_rate | Prometheus `/api/v1/query` (PromQL) |
| `InfluxDbCollector` | p99_latency, availability | InfluxDB 2.x `/api/v2/query` (Flux) |
| `RedisCollector` | redis_latency | Redis `PING` timing (avg of N samples) |
| `KubernetesCollector` | pod_restarts | K8s API — max container restart count |
| `NetworkProbeCollector` | endpoint_latency, packet_loss | TCP connect probe (N attempts) |

`MetricAggregator` runs all five and merges them into one metric-sample dict.
A single failing source is recorded (not fatal) unless `require_complete=True`.

## Fail-closed guarantee

If metric collection fails in production mode, the gate returns **ROLLBACK**
(exit 1) — never PROMOTE. A broken collector must never let a bad deploy through.

## Reaching the cluster from your Windows VDI

The collectors talk to `localhost` ports by default. Open a port-forward per
service (each in its own terminal), or edit `config/collectors.yaml` to use the
in-cluster `*.svc.cluster.local` names if the gate runs as a pod.

```bash
kubectl port-forward -n robot-poc svc/kube-prom-kube-prometheus-prometheus 9090:9090
kubectl port-forward -n robot-poc svc/influxdb-influxdb2 8086:80
kubectl port-forward -n robot-poc svc/redis-master 6379:6379
```

Kubernetes pod restarts use your kubeconfig directly (no port-forward needed).

## Secrets (never committed)

```bash
export INFLUX_TOKEN='<the influx admin token you set at install>'
export REDIS_PASSWORD='<the redis password you set at install>'
```

On Windows PowerShell:

```powershell
$env:INFLUX_TOKEN='<token>'
$env:REDIS_PASSWORD='<password>'
```

## Run production mode

```bash
python -m gate.cli --mode production \
  --baseline data/v1_baseline.csv \
  --collectors gate/config/collectors.yaml
```

The baseline still trains from the v1 CSV; only the canary sample is collected
live. Exit code: 0 = PROMOTE, 1 = ROLLBACK.

## Adapting to your real workload

`config/collectors.yaml` is templated for a canary workload named
`raptor-gate-canary`. Update these to match the service you actually canary:

- `target.workload`, `target.pod_label_selector`
- The `pod=~"..."` regex inside each PromQL query
- `influxdb.measurements` — the measurement/field names in your bucket
- `network_probe.target_host` / `target_port`

## Tests

All collectors are covered by `tests/test_collectors.py` with mocked clients
(no live infra), and the production CLI path by `tests/test_cli_production.py`.

```bash
python -m pytest
```
