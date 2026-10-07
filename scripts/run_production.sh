#!/usr/bin/env bash
#
# Raptor Deployment Gate — end-to-end production runner.
#
# Run this from a Linux shell or Windows Git Bash with `kubectl` already pointed
# at the target cluster. It will:
#   1. Preflight: check kubectl / helm / python and cluster connectivity
#   2. Verify (or optionally install) the Phase-1 Helm infra in robot-poc
#   3. Set up the Python venv and dependencies
#   4. Port-forward Prometheus / InfluxDB / Redis to localhost
#   5. Optionally capture a REAL v1 baseline from the live service
#   6. Run the gate in production mode and print the PROMOTE/ROLLBACK verdict
#
# The script is idempotent and cleans up its port-forwards on exit. It never
# performs destructive actions without asking.
#
# Usage:
#   ./scripts/run_production.sh                 # verify infra, run gate
#   ./scripts/run_production.sh --install       # also install missing infra
#   ./scripts/run_production.sh --capture-baseline 30 10   # samples, interval(s)
#   ./scripts/run_production.sh --kubeconfig ~/.kube/haber020.yaml   # explicit config
#
# Kubeconfig: if your file in ~/.kube is not named 'config' (e.g. haber020.yaml),
# either pass --kubeconfig, export KUBECONFIG, or the script auto-detects a lone
# *.yaml in ~/.kube.
#
set -euo pipefail

# ------------------------------------------------------------------ settings
NS="robot-poc"
PROM_SVC="kube-prom-kube-prometheus-prometheus"
REDIS_SVC="redis-master"
TARGET_SVC="eric-cenx-rest-api"      # the workload being canaried (HTTP prober)
PROM_PORT=9090
REDIS_PORT=6379
TARGET_PORT=8080

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DO_INSTALL=0
CAPTURE_SAMPLES=0
CAPTURE_INTERVAL=10
KUBECONFIG_ARG=""

# ------------------------------------------------------------------ helpers
info()  { printf '\033[0;36m[i]\033[0m %s\n' "$*"; }
ok()    { printf '\033[0;32m[✓]\033[0m %s\n' "$*"; }
warn()  { printf '\033[0;33m[!]\033[0m %s\n' "$*"; }
die()   { printf '\033[0;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

PF_PIDS=()
cleanup() {
  for pid in "${PF_PIDS[@]:-}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

# Pick the venv python (Windows Git Bash vs Linux layout).
venv_python() {
  if [[ -x ".venv/Scripts/python.exe" ]]; then echo ".venv/Scripts/python.exe";
  elif [[ -x ".venv/bin/python" ]]; then echo ".venv/bin/python";
  else echo ""; fi
}

# ------------------------------------------------------------------ args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --install) DO_INSTALL=1; shift ;;
    --kubeconfig) KUBECONFIG_ARG="${2:?--kubeconfig needs a path}"; shift 2 ;;
    --capture-baseline)
      CAPTURE_SAMPLES="${2:-30}"; CAPTURE_INTERVAL="${3:-10}"; shift 3 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

# ================================================================ 1. preflight
info "Preflight checks…"
command -v kubectl >/dev/null || die "kubectl not found on PATH"
command -v helm    >/dev/null || die "helm not found on PATH"
command -v python  >/dev/null || command -v python3 >/dev/null || die "python not found"
PY_SYS="$(command -v python || command -v python3)"

# Resolve the kubeconfig. Precedence:
#   1. --kubeconfig <path>
#   2. an already-exported $KUBECONFIG
#   3. the default ~/.kube/config
#   4. a single *.yaml in ~/.kube/ (your file is named e.g. haber020.yaml, not
#      'config', so kubectl won't pick it up automatically)
resolve_kubeconfig() {
  if [[ -n "$KUBECONFIG_ARG" ]]; then
    [[ -f "$KUBECONFIG_ARG" ]] || die "kubeconfig not found: $KUBECONFIG_ARG"
    export KUBECONFIG="$KUBECONFIG_ARG"; return
  fi
  if [[ -n "${KUBECONFIG:-}" && -f "${KUBECONFIG}" ]]; then
    return  # honour an already-set KUBECONFIG
  fi
  local kdir="$HOME/.kube"
  if [[ -f "$kdir/config" ]]; then
    export KUBECONFIG="$kdir/config"; return
  fi
  # Fall back to a lone yaml file in ~/.kube (common on locked-down VDIs).
  local candidates=("$kdir"/*.yaml "$kdir"/*.yml)
  local found=()
  for f in "${candidates[@]}"; do [[ -f "$f" ]] && found+=("$f"); done
  if [[ ${#found[@]} -eq 1 ]]; then
    export KUBECONFIG="${found[0]}"
    info "Using kubeconfig: $KUBECONFIG"
  elif [[ ${#found[@]} -gt 1 ]]; then
    die "Multiple kubeconfig files in $kdir. Pick one with --kubeconfig <path>: ${found[*]}"
  fi
  # else: leave KUBECONFIG unset and let kubectl try its own defaults.
}
resolve_kubeconfig

kubectl cluster-info >/dev/null 2>&1 || die "kubectl cannot reach the cluster. Set --kubeconfig <path> or fix your context (kubectl config current-context)."
kubectl get ns "$NS" >/dev/null 2>&1 || die "namespace $NS not found"
ok "kubectl, helm, python present; cluster reachable; namespace $NS exists"

# ================================================================ 2. infra
info "Checking Phase-1 infra in $NS…"
have_release() { helm status "$1" -n "$NS" >/dev/null 2>&1; }

MISSING=()
have_release redis     || MISSING+=("redis")
have_release kube-prom || MISSING+=("kube-prom")
have_release influxdb  || MISSING+=("influxdb")

if [[ ${#MISSING[@]} -eq 0 ]]; then
  ok "All Helm releases present (redis, kube-prom, influxdb)"
elif [[ $DO_INSTALL -eq 1 ]]; then
  warn "Installing missing releases: ${MISSING[*]}"
  helm repo add bitnami https://charts.bitnami.com/bitnami >/dev/null 2>&1 || true
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo add influxdata https://helm.influxdata.com/ >/dev/null 2>&1 || true
  helm repo update >/dev/null

  read -rsp "Set Redis password: " REDIS_PW; echo
  read -rsp "Set Grafana admin password: " GRAFANA_PW; echo
  read -rsp "Set InfluxDB password: " INFLUX_PW; echo
  read -rsp "Set InfluxDB token: " INFLUX_TOK; echo

  for m in "${MISSING[@]}"; do
    case "$m" in
      redis) helm install redis bitnami/redis -n "$NS" \
               -f infra/helm/redis-values.yaml --set auth.password="$REDIS_PW" ;;
      kube-prom) helm install kube-prom prometheus-community/kube-prometheus-stack -n "$NS" \
               -f infra/helm/prometheus-values.yaml --set grafana.adminPassword="$GRAFANA_PW" ;;
      influxdb) helm install influxdb influxdata/influxdb2 -n "$NS" \
               -f infra/helm/influxdb-values.yaml \
               --set adminUser.password="$INFLUX_PW" --set adminUser.token="$INFLUX_TOK" ;;
    esac
  done
  info "Waiting for pods to become ready…"
  kubectl wait --for=condition=ready pod -l app.kubernetes.io/instance=redis -n "$NS" --timeout=180s || true
else
  die "Missing releases: ${MISSING[*]}. Re-run with --install to create them, or install manually (see infra/README.md)."
fi

# ================================================================ 3. venv
info "Setting up Python environment…"
PY="$(venv_python)"
if [[ -z "$PY" ]]; then
  "$PY_SYS" -m venv .venv
  PY="$(venv_python)"
fi
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r requirements.txt
ok "venv ready: $PY"

# ================================================================ 4. secrets
# Only REDIS_PASSWORD is needed now — the current collectors.yaml uses
# Prometheus + Kubernetes + Redis + the HTTP prober (no InfluxDB token).
info "Checking collector secrets…"
if [[ -z "${REDIS_PASSWORD:-}" ]]; then
  # Try to read it from the chart secret automatically.
  RP="$(kubectl get secret redis -n "$NS" -o jsonpath='{.data.redis-password}' 2>/dev/null | base64 -d 2>/dev/null || true)"
  if [[ -n "$RP" ]]; then
    export REDIS_PASSWORD="$RP"; ok "REDIS_PASSWORD read from the redis secret"
  else
    read -rsp "Export REDIS_PASSWORD: " REDIS_PASSWORD; echo
    export REDIS_PASSWORD
  fi
fi

# ================================================================ 5. port-forward
info "Opening port-forwards to $NS services…"
port_forward() {
  local svc="$1" local_port="$2" remote_port="$3"
  kubectl port-forward -n "$NS" "svc/$svc" "$local_port:$remote_port" >/dev/null 2>&1 &
  PF_PIDS+=($!)
}
port_forward "$PROM_SVC"   "$PROM_PORT"   9090
port_forward "$REDIS_SVC"  "$REDIS_PORT"  6379
port_forward "$TARGET_SVC" "$TARGET_PORT" 8080
sleep 5
ok "Port-forwards active (prometheus:$PROM_PORT redis:$REDIS_PORT ${TARGET_SVC}:$TARGET_PORT)"

# ================================================================ 6. baseline
BASELINE="data/v1_baseline.csv"
if [[ "$CAPTURE_SAMPLES" -gt 0 ]]; then
  info "Capturing a REAL v1 baseline: $CAPTURE_SAMPLES samples @ ${CAPTURE_INTERVAL}s…"
  BASELINE="data/v1_baseline_live.csv"
  "$PY" scripts/capture_baseline.py \
    --collectors gate/config/collectors.yaml \
    --samples "$CAPTURE_SAMPLES" --interval "$CAPTURE_INTERVAL" \
    --out "$BASELINE"
  ok "Live baseline written to $BASELINE"
else
  info "Using bundled baseline ($BASELINE). Pass --capture-baseline N INTERVAL to build a live one."
fi

# ================================================================ 7. run gate
info "Running the gate in PRODUCTION mode…"
set +e
"$PY" -m gate.cli --mode production \
  --baseline "$BASELINE" \
  --collectors gate/config/collectors.yaml
RC=$?
set -e

echo
if [[ $RC -eq 0 ]]; then
  ok "GATE DECISION: PROMOTE (exit 0)"
else
  warn "GATE DECISION: ROLLBACK (exit $RC)"
fi
info "Done. Exit code $RC is the pipeline signal (0=PROMOTE, 1=ROLLBACK)."
exit $RC
