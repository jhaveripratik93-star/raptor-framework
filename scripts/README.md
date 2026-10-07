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
