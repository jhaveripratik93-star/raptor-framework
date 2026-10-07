# Phase 1 — Verification & Smoke Tests

Run these after the three `helm install` commands. All commands run from the
Windows VDI (Git Bash / MINGW64) against the `robot-poc` namespace.

## 1. Everything is up

```bash
# All pods should reach Running / Completed. Give it a few minutes on first boot.
kubectl get pods -n robot-poc

# Every PVC must be Bound on the network-block storage class.
kubectl get pvc -n robot-poc

# Services you'll connect to.
kubectl get svc -n robot-poc
```

If a pod is stuck in `Pending`, check storage/scheduling:

```bash
kubectl describe pod <pod-name> -n robot-poc | tail -30
```

A PVC stuck in `Pending` almost always means the storage class name is wrong or
the provisioner is unavailable — confirm with `kubectl get sc`.

---

## 2. Redis smoke test

```bash
# Fetch the password the chart stored (skip if you set your own).
kubectl get secret redis -n robot-poc -o jsonpath='{.data.redis-password}' | base64 -d; echo

# Exec into the Redis pod and PING.
kubectl exec -it -n robot-poc redis-master-0 -- \
  redis-cli -a "$(kubectl get secret redis -n robot-poc -o jsonpath='{.data.redis-password}' | base64 -d)" ping
# Expect: PONG

# Latency sample (this is the metric the gate reads):
kubectl exec -it -n robot-poc redis-master-0 -- \
  redis-cli -a "<password>" --latency
```

---

## 3. Prometheus smoke test

```bash
# Port-forward the Prometheus service to your VDI.
kubectl port-forward -n robot-poc svc/kube-prom-kube-prometheus-prometheus 9090:9090
```

Then in a browser on the VDI: <http://localhost:9090>

- Status → Targets: node-exporter and kube-state-metrics should be **UP**.
- Try a query: `up` — should return series. This confirms CPU/memory series
  (the gate's Prometheus-sourced metrics) are being scraped.

Grafana:

```bash
kubectl port-forward -n robot-poc svc/kube-prom-grafana 3000:80
```

<http://localhost:3000> — login `admin` / the password you set at install.

> Service names assume release name `kube-prom`. If yours differs, list them:
> `kubectl get svc -n robot-poc | grep -E 'prometheus|grafana'`

---

## 4. InfluxDB smoke test

```bash
kubectl port-forward -n robot-poc svc/influxdb-influxdb2 8086:80
```

<http://localhost:8086> — login with the admin user/password set at install.
Confirm the `raptor-gate` org and `deployment-metrics` bucket exist (these hold
the p99-latency and availability series the gate reads).

Health check from the CLI:

```bash
curl -s http://localhost:8086/health
# Expect JSON with "status":"pass"
```

---

## 5. Record connection details for Phase 3

The gate (production mode) will need these in-cluster DNS names:

| Component | In-cluster endpoint (from a pod in robot-poc) |
|-----------|-----------------------------------------------|
| Redis | `redis-master.robot-poc.svc.cluster.local:6379` |
| Prometheus | `kube-prom-kube-prometheus-prometheus.robot-poc.svc.cluster.local:9090` |
| InfluxDB | `influxdb-influxdb2.robot-poc.svc.cluster.local:80` |

Confirm the actual names against `kubectl get svc -n robot-poc` — release-name
prefixes can differ from the examples above.

---

## Teardown (if you need to start over)

```bash
helm uninstall redis     -n robot-poc
helm uninstall kube-prom -n robot-poc
helm uninstall influxdb  -n robot-poc

# PVCs are NOT deleted by helm uninstall — remove them explicitly if desired:
kubectl get pvc -n robot-poc
kubectl delete pvc <name> -n robot-poc
```
