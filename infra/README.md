# Raptor Deployment Gate — Phase 1 Infrastructure

Installs the three data-source backends the deployment gate reads from, into the
`robot-poc` namespace on your remote Kubernetes cluster, using the `network-block`
(Ceph RBD) storage class.

| Component | Chart | Gate role |
|-----------|-------|-----------|
| Redis | `bitnami/redis` | Redis-latency metric source + Raptor feature cache |
| Prometheus (+ Grafana) | `prometheus-community/kube-prometheus-stack` | CPU, memory, HTTP error rate |
| InfluxDB 2.x | `influxdata/influxdb2` | p99 latency, availability |

All commands run from your **Windows VDI** (Git Bash / MINGW64). No local
virtualization is used — everything lands on the remote cluster.

## Prerequisites (already confirmed)

- `kubectl` context points at the target cluster
- `helm` v3.19 installed
- RBAC allows Deployments, StatefulSets, PVCs, CRDs, ClusterRoles in `robot-poc`
- StorageClass `network-block` available

## 0. Namespace + Helm repos

```bash
kubectl create namespace robot-poc --dry-run=client -o yaml | kubectl apply -f -

helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add influxdata https://helm.influxdata.com/
helm repo update
```

## 1. Redis

```bash
helm install redis bitnami/redis \
  -n robot-poc \
  -f infra/helm/redis-values.yaml \
  --set auth.password='CHANGE_ME_redis'
```

## 2. Prometheus stack (Prometheus + Grafana)

```bash
helm install kube-prom prometheus-community/kube-prometheus-stack \
  -n robot-poc \
  -f infra/helm/prometheus-values.yaml \
  --set grafana.adminPassword='CHANGE_ME_grafana'
```

## 3. InfluxDB 2.x

```bash
helm install influxdb influxdata/influxdb2 \
  -n robot-poc \
  -f infra/helm/influxdb-values.yaml \
  --set adminUser.password='CHANGE_ME_influx_pw' \
  --set adminUser.token='CHANGE_ME_influx_token'
```

## 4. Verify

```bash
kubectl get pods    -n robot-poc
kubectl get pvc     -n robot-poc      # all should be Bound on network-block
kubectl get svc     -n robot-poc
```

See `VERIFY.md` for connectivity smoke tests and how to reach each UI.

## Notes

- Passwords/tokens are passed at install with `--set` and never committed. The
  values files keep those fields blank on purpose.
- If a chart version pulls a newer app version than you want, pin it with
  `--version <chart-version>` on the `helm install` line.
- To change any setting later: edit the values file and run `helm upgrade`
  with the same release name and `-f` flag.

## Shared-cluster adjustments (learned during install)

This cluster already runs a full monitoring stack (a prometheus-operator in the
`monitoring` namespace, plus many existing ServiceMonitors). Getting a second
Prometheus to coexist required these settings in `prometheus-values.yaml`:

- **node-exporter disabled** (`prometheus-node-exporter.enabled: false`). The
  existing cluster node-exporter already owns hostPort 9100 on every node, so a
  second DaemonSet could not schedule (`FailedScheduling`: no free ports +
  NodeAffinity rejection). The gate needs pod/container metrics, not node-level
  metrics, so this is not a loss.
- **Control-plane scrape jobs disabled** (kubeApiServer, kubeScheduler,
  kubeControllerManager, kubeProxy, kubeEtcd, coreDns). They overlap the existing
  monitoring and the gate does not use them. `kubelet` (cAdvisor) and
  `kube-state-metrics` are kept — they provide the canary's pod CPU/memory/restart
  series.
- **Discovery scoped to `robot-poc`** via `serviceMonitorNamespaceSelector` /
  `podMonitorNamespaceSelector`, so this Prometheus does not adopt the whole
  cluster's ServiceMonitors.
- **Prometheus startupProbe relaxed** (`failureThreshold: 60`, `periodSeconds:
  10`). The TSDB WAL replay took ~15s; the default startup probe was killing the
  container mid-replay and causing a crashloop. This gives it up to 10 minutes.

### If Prometheus crashloops with `OOMKilled` after a long WAL replay

Symptoms: `kubectl get pods -n robot-poc` shows the prometheus pod
crashlooping with `Startup probe failed: ... statuscode: 503` for several
minutes, then `Liveness/Readiness probe failed: connection reset by peer`,
then `BackOff restarting failed container`.

`kubectl describe pod prometheus-kube-prom-kube-prometheus-prometheus-0 -n
robot-poc | grep -A5 "Last State"` will show `Reason: OOMKilled`, and the
`kubectl logs ... --previous` tail will end right after `msg="write block
started"` (compaction kicking off immediately after WAL replay finishes) --
the kernel kills the process there, not during replay itself.

Root cause: the PVC had filled to ~18GiB on the 20Gi volume (visible in the
logs as `msg="TSDB retention updated" ... size=18GiB`). Compacting
immediately after a 4+ minute WAL replay on a near-full volume needs more
headroom than the original `limits.memory: 2Gi`. This is fixed in
`prometheus-values.yaml` by raising the memory limit to `4Gi` and adding
`retention: 7d` / `retentionSize: 15GB` so the volume doesn't refill to the
same near-capacity state. `helm upgrade` with the updated values file (same
release name) is sufficient -- no uninstall/reinstall needed for this one,
since the release was never left in a failed/pending-install state.

If it still OOMs at 4Gi, go up again (6-8Gi) and/or grow the PVC past 20Gi
instead of just shortening retention -- retention is a stopgap, not a fix for
genuinely needing more history.

### If `helm upgrade` fails with "has no deployed releases"

The previous install left the release in a `failed`/`pending-install` state
(caused by the crashloop above). You cannot `upgrade` from that state — you must
uninstall and reinstall:

```bash
helm list -n robot-poc --all          # confirm the failed/pending state
helm uninstall kube-prom -n robot-poc  # PVCs are NOT deleted
# optional clean start (no real data yet), removes WAL replay entirely:
#   kubectl get pvc -n robot-poc | grep prometheus
#   kubectl delete pvc <prometheus-pvc-name> -n robot-poc
helm install kube-prom prometheus-community/kube-prometheus-stack \
  -n robot-poc -f infra/helm/prometheus-values.yaml \
  --set grafana.adminPassword='<your-password>'
```

## Phase 1 status: COMPLETE

Verified `Running` in `robot-poc`:

| Pod | Status |
|-----|--------|
| `prometheus-kube-prom-kube-prometheus-prometheus-0` | 2/2 Running, 0 restarts |
| `kube-prom-kube-state-metrics-*` | 1/1 Running |
| `kube-prom-kube-prometheus-operator-*` | 1/1 Running |
| `kube-prom-grafana-*` | 3/3 Running |
| `influxdb-influxdb2-0` | 1/1 Running |
| `redis-master-0` | 2/2 Running |
