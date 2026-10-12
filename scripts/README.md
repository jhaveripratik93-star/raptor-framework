# Scripts — production run & baseline capture

Two helpers for running the gate against the live cluster. Run them from a Linux
shell or **Windows Git Bash** with `kubectl` already pointed at the cluster.

Narrative/worked-example scripts that only exist to produce the numbers in
`docs/how_the_scoring_actually_works.md` (not used to operate the gate) live
in `scripts/demos/` — see that folder for the Isolation Forest baseline
calibration, penalty-change, trend-blindspot, and volatility demos.

## `run_production.sh` — end-to-end runner

Verifies (or installs) the infra, sets up Python, port-forwards the services,
optionally captures a real baseline, and runs the gate in production mode.

```bash
# Verify infra is present, then run the gate (uses the bundled baseline)
./scripts/run_production.sh

# Also install any missing Helm releases (prompts for passwords/token)
./scripts/run_production.sh --install

# Capture a REAL v1 baseline first: 30 samples, 10s apart, then run the gate
./scripts/run_production.sh --capture-baseline 30 10
```

What it does, in order:

1. **Preflight** — checks `kubectl`, `helm`, `python`, cluster reachability, and
   that the `robot-poc` namespace exists.
2. **Infra** — confirms the `redis`, `kube-prom`, `influxdb` Helm releases exist.
   With `--install` it creates any that are missing (prompting for secrets).
3. **venv** — creates `.venv` and installs `requirements.txt` if needed.
4. **Secrets** — needs `REDIS_PASSWORD` (the current collector set uses
   Prometheus + Kubernetes + Redis + the HTTP prober; no InfluxDB token). It
   reads `REDIS_PASSWORD` from the chart's `redis` secret automatically when it
   can; otherwise it prompts. Export it beforehand to skip the prompt.
5. **Port-forwards** — opens Prometheus (9090), Redis (6379), and the target
   service `eric-cenx-rest-api` (8080) to localhost, cleaned up on exit.
6. **Baseline** — with `--capture-baseline` it builds a live baseline (see below);
   otherwise uses `data/v1_baseline.csv`.
7. **Gate** — runs `gate.cli --mode production` and reports PROMOTE / ROLLBACK.
   Exit code is the pipeline signal (0 = PROMOTE, 1 = ROLLBACK).

The script is idempotent and asks before installing anything. It never deletes
resources.

## `capture_baseline.py` — build a real v1 baseline

The Isolation Forest learns "normal" from the baseline, so a real one beats the
synthetic default. Run this while v1 is healthy and taking normal traffic.

```bash
# Needs the same access + secrets as production mode (port-forwards, INFLUX_TOKEN,
# REDIS_PASSWORD). run_production.sh --capture-baseline calls this for you.
python scripts/capture_baseline.py \
  --collectors gate/config/collectors.yaml \
  --samples 30 --interval 10 \
  --out data/v1_baseline_live.csv
```

- `--samples` / `--interval` — how many readings and how far apart. 30×10s ≈ 5
  minutes of coverage; more samples give a more robust baseline.
- `--allow-partial` — keep going if a collector fails on some ticks (incomplete
  rows are dropped from the final CSV to keep the training matrix rectangular).

Then train/point the gate at it:

```bash
python -m gate.cli --mode production --baseline data/v1_baseline_live.csv \
  --collectors gate/config/collectors.yaml
```

## `discover_collectors.py` — draft a collectors.yaml instead of hand-typing it

Lists Deployments in a namespace and, for a chosen one, resolves its Services
(ClusterIP/ports) and tries to confirm Prometheus/HTTP endpoints live, then
writes a **draft** `collectors.yaml` for review — rather than hand-typing
`target.workload`, `pod_label_selector`, `redis.host`, and
`http_probe.base_url` the way the bundled configs currently are.

```bash
# Step 1: see what Deployments exist in the namespace
python scripts/discover_collectors.py --namespace robot-poc

# Step 2: draft a config for one of them
python scripts/discover_collectors.py --namespace robot-poc \
  --workload eric-cenx-rest-api \
  --prometheus http://localhost:9090 \
  --out gate/config/collectors_discovered_eric-cenx-rest-api.yaml
```

**What it fills in automatically** (mechanical, reading structured K8s
objects — no guessing): the pod label selector, which Services belong to the
workload, their ClusterIP/ports, and which port actually answers HTTP.

**What it deliberately does NOT guess:** which Prometheus metric name means
"cpu_usage" or "memory_usage" for an arbitrary service — that depends on
what the service actually exposes (cAdvisor, a JMX exporter, its own
`/metrics`, or nothing), and guessing wrong means the gate silently scores
the wrong metric with no error anywhere. It tries the one PromQL pattern
already proven twice in this repo (the JMX exporter shape) and only writes
it in if a live query actually returns a value; otherwise it lists candidate
series names as a hint and leaves the query as an explicit `TODO` for a
human to fill in.

The output is always a new file (never overwrites an existing one) and is
marked `DRAFT` with `⚠ UNVERIFIED` on anything that wasn't confirmed live —
review those sections before pointing `gate.cli --mode production` at it.

## `gate/config/collectors_template.yaml` — hand-fillable alternative

Use this instead of `discover_collectors.py` when you don't have live
cluster/Prometheus access yet (e.g. RBAC not granted, drafting a config
before the service is even deployed) or just want a documented starting
point to fill in by hand rather than running discovery.

```bash
cp gate/config/collectors_template.yaml \
   gate/config/collectors_incluster_<your-service-name>.yaml
```

Every field is a `<PLACEHOLDER>`, with an in-file checklist (numbered steps,
each with the exact `kubectl`/`curl` command to confirm it) and the same
`⚠ MUST CONFIRM` banner style as `collectors_incluster_analytics.yaml`'s
unverified sections. Fill in each placeholder, work through the checklist,
and remove the warning comments once confirmed.

## Before you run — point the collectors at your real workload

`gate/config/collectors.yaml` is templated for a canary named
`raptor-gate-canary`. Update these to your actual service or the queries return
nothing (and the gate fail-closes to ROLLBACK):

- `target.workload`, `target.pod_label_selector`
- the `pod=~"…"` regex in each PromQL query
- `influxdb.measurements` — the measurement/field names in your bucket
- `network_probe.target_host` / `target_port`

## Notes

- **Windows**: use Git Bash (bundled with Git for Windows), not PowerShell — the
  scripts are bash. The venv python is auto-detected at `.venv/Scripts/python.exe`.
- **kubeconfig**: the script uses whatever context `kubectl` currently targets.
  Confirm with `kubectl config current-context` first.
