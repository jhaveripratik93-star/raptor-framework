# Raptor Deployment Gate

An automated canary deployment gate for Kubernetes. It scores a canary's health
across 9 metrics and returns a single **PROMOTE** or **ROLLBACK** decision, so a
CI/CD pipeline can advance or revert a deployment without a human approval step.

The decision combines three signals:

- **Isolation Forest** anomaly detection trained on the v1 production baseline —
  catches cross-metric degradation no single threshold would flag.
- An **SLA rule engine** — 9 explicit thresholds; each breach deducts from the
  health score.
- **Cross-sample persistent-breach tracking** — if the *same* metric breaches
  its SLA across multiple recent deployments (default: 2 of the last 5), the
  gate forces ROLLBACK outright, regardless of what the health score for that
  one sample says. A metric that keeps crossing its line release after
  release is treated as proof of a real application problem — one clean-ish
  sample is not allowed to outvote that pattern. See `gate/signals/trend.py`.

Health score is 0–100; `>= 70` promotes, `< 70` rolls back. The process exit
code is `0` on PROMOTE and `1` on ROLLBACK.

> On Raptor: the framework document describes using the Raptor LabSDK for the
> feature store / model serving. That package is unmaintained and does not run
> on modern Python, so this project implements the same architecture — declared
> features with 5-minute aggregation windows, Redis caching, and a served model
> API — on maintained libraries (FastAPI + scikit-learn). See
> `gate/features/` and `gate/serving/`.

## Architecture

```
                    ┌──────────────────────────────────────────┐
   CI/CD pipeline   │   Kubernetes cluster (namespace: robot-poc)│
  (GitLab/Jenkins)  │                                            │
        │           │   Prometheus   InfluxDB   Redis   K8s API  │
        │ exit code │        │           │        │        │     │
        ▼           │        └────┬──────┴────┬───┴────────┘     │
  ┌───────────┐     │             ▼           ▼                  │
  │   gate    │◄────┼──── collectors ──► feature store ──► model │
  │  (CLI or  │     │   (Phase 3)       (Phase 4)        serving │
  │  service) │     │                                    (Phase 4)│
  └───────────┘     └──────────────────────────────────────────┘
        │
        ├─ PROMOTE (exit 0)  → pipeline advances canary to production
        └─ ROLLBACK (exit 1) → pipeline reverts to v1
```

## The 9 monitored metrics

| Metric | Source | SLA |
|--------|--------|-----|
| CPU usage | Prometheus | < 80% |
| Memory usage | Prometheus | < 75% |
| Pod restarts | Kubernetes API | < 3 |
| HTTP error rate | Prometheus | < 1% |
| p99 latency | InfluxDB | < 200ms |
| Availability | InfluxDB | > 99.5% |
| Redis latency | Redis | < 10ms |
| Endpoint latency | Network probe | < 200ms |
| Packet loss | Network probe | < 1% |

## How it's built — the five phases

| Phase | What | Where |
|-------|------|-------|
| 1 | Infra: Redis, Prometheus, InfluxDB in `robot-poc` (Helm) | `infra/` |
| 2 | Gate logic: SLA engine, Isolation Forest, persistent-breach tracking, health score, CLI | `gate/` (`metrics.py`, `sla.py`, `model.py`, `signals/trend.py`, `scoring.py`, `cli.py`) |
| 3 | Live collectors for all 5 sources + production CLI mode | `gate/collectors/` |
| 4 | Feature store + FastAPI model serving + Docker/K8s | `gate/features/`, `gate/serving/`, `Dockerfile`, `deploy/k8s/` |
| 5 | Pipeline integration (GitLab/Jenkins/webhook, optional) | `gate/pipeline/`, `deploy/pipelines/` |

## Quickstart — local demo (no cluster, no infra)

```bash
python -m venv .venv
# Windows:  .\.venv\Scripts\python.exe -m pip install -r requirements.txt
# Linux:    source .venv/bin/activate && pip install -r requirements.txt

# Healthy canary -> PROMOTE (exit 0)
python -m gate.cli --baseline data/v1_baseline.csv --scenario data/v2_healthy.csv

# Degraded canary -> ROLLBACK (exit 1), lists 8 SLA breaches
python -m gate.cli --baseline data/v1_baseline.csv --scenario data/v2_degraded.csv
```

## Production — against the live cluster

Two options:

**A. One-shot script (recommended for first run).** From a Linux/Git Bash shell
with `kubectl` pointed at the cluster:

```bash
./scripts/run_production.sh
```

It verifies (or installs) the Helm infra, sets up the venv, port-forwards the
services, optionally captures a real v1 baseline, and runs the gate in
production mode. See `scripts/README.md`.

**B. Serve the gate as an API in the cluster** (Phase 4):

```bash
docker build -t raptor-gate:1.0 .
# push to a registry the cluster can pull, set the image in deploy/k8s/deployment.yaml
kubectl apply -f deploy/k8s/
```

## Pipeline integration (Phase 5, optional)

Run the gate as a stage and branch on its exit code (see
`deploy/pipelines/.gitlab-ci.yml` and `Jenkinsfile`), or have the gate push a
trigger to GitLab/Jenkins after scoring:

```bash
python -m gate.cli --mode production --baseline data/v1_baseline.csv --notify gitlab
```

Configure the tool in `gate/config/pipeline.yaml`; secrets come from environment
variables only. Details in `deploy/pipelines/README.md`.

## Tests

```bash
python -m pytest        # 75 tests: gate logic, collectors, features, serving, pipeline, trend
```

Everything runs offline — collectors and pipeline notifiers use mocked clients,
so no cluster or network is needed to validate the suite.

## Safety model

- **Metric collection fails closed**: in production mode, if metrics can't be
  collected, the gate returns ROLLBACK — never PROMOTE without data.
- **A single metric breaching repeatedly forces ROLLBACK**: a persistent
  breach of the same metric across recent samples (default: 2 of the last 5)
  overrides the health score outright — no amount of "the rest looks fine"
  outvotes a metric that keeps crossing its line. See `gate/signals/trend.py`.
- **Pipeline notification fails open**: if notifying GitLab/Jenkins fails, it's
  logged but never changes the gate decision or exit code.
- **Secrets from env only**: tokens/passwords are never stored in config or
  committed; config files name the env var that holds each secret.

## Project layout

```
gate/                core gate package
  metrics.py         the 9 metric specs (single source of truth)
  sla.py             SLA rule engine
  model.py           Isolation Forest wrapper
  signals/           opt-in override signals on top of the core engine
    trend.py         cross-sample persistent-breach tracking (forces ROLLBACK
                      when one metric keeps breaching across samples)
    volatility.py     within-window breach-fraction checking (forces ROLLBACK
                      when raw ticks breach but aggregation hides it)
    adaptive.py       ML-enhanced, opt-in drop-in replacements for trend.py
                      and volatility.py
  scoring.py         health score + PROMOTE/ROLLBACK verdict
  dataio.py          CSV loaders (local mode)
  cli.py             command-line runner (local + production, --notify)
  train.py           train & save the model artifact
  collectors/        live metric collectors (Phase 3)
  features/          feature store + @feature/@aggregation (Phase 4)
  serving/           FastAPI model-serving app (Phase 4)
  pipeline/          CI/CD notifiers (Phase 5)
  config/            production.yaml, collectors.yaml, pipeline.yaml
data/                v1 baseline + demo scenarios (CSV)
infra/               Helm values + install/verify docs (Phase 1)
deploy/              Dockerfile targets, k8s manifests, pipeline snippets
scripts/             end-to-end production run + baseline capture
  demos/             standalone narrative scripts behind the scoring doc's
                      worked examples (not used to operate the gate)
tests/               pytest suite
```

## Current limitations (honest status)

- The Helm infra must be installed on the cluster (files ready in `infra/`); the
  `scripts/run_production.sh` helper can do this for you.
- `gate/config/collectors.yaml` is templated for a canary workload named
  `raptor-gate-canary`. Point it at your real service before production mode
  returns real numbers.
- The bundled `data/v1_baseline.csv` is a representative synthetic baseline. Use
  `scripts/capture_baseline.py` to build a real one from your live v1 service.
- The serving image needs a Docker build environment; build/push it wherever
  Docker is available if your VDI can't build images.
- `gate/signals/trend.py` catches a metric that *crosses* its SLA threshold repeatedly
  across samples. It does **not** catch a metric that is steadily climbing but
  has never actually crossed its threshold (e.g. memory creeping 40% → 74%
  while the limit is 75%) — that is genuine slope/trend detection on the raw
  values themselves, a separate feature not yet implemented. See
  `docs/how_the_scoring_actually_works.md` for a worked example of this
  specific gap.
