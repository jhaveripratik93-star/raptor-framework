# Phase 5 — Pipeline Integration

Two integration styles, use whichever fits how your gate runs.

## Style 1 — Gate runs inside the pipeline (recommended)

The pipeline runs the gate as a stage and branches on its **exit code**:

- `exit 0` → PROMOTE → advance the canary
- `exit 1` → ROLLBACK → revert to v1

No tokens or push config needed — the exit code is the whole contract. Ready-to-
use examples:

| File | Tool |
|------|------|
| `.gitlab-ci.yml` | GitLab CI — `gate` job sets `GATE_EXIT`; `promote`/`rollback` jobs run on it |
| `Jenkinsfile` | Jenkins — `Gate` stage captures exit code; `Promote`/`Rollback` stages branch |

Copy the file into your app repo, set the deploy commands in the promote/rollback
steps, and provide the collector secrets (`INFLUX_TOKEN`, `REDIS_PASSWORD`).

## Style 2 — Gate runs elsewhere and pushes to the pipeline

If the gate runs outside your CI/CD (e.g. as the `raptor-gate` service, or a
scheduled job), it can **trigger** GitLab/Jenkins after scoring using `--notify`:

```bash
# Uses the tool set in gate/config/pipeline.yaml:
python -m gate.cli --mode production --baseline data/v1_baseline.csv --notify

# Or force a specific tool:
python -m gate.cli ... --notify gitlab
python -m gate.cli ... --notify jenkins
python -m gate.cli ... --notify webhook
```

Configure the target in `gate/config/pipeline.yaml` (endpoints + which env var
holds each secret) and export the secret:

```bash
# GitLab
export GITLAB_TRIGGER_TOKEN='<trigger token>'
# Jenkins
export JENKINS_USER='<user>'; export JENKINS_API_TOKEN='<token>'
# Webhook
export GATE_WEBHOOK_URL='https://…'
```

The gate passes the outcome as pipeline variables / build parameters:

| Variable | Meaning |
|----------|---------|
| `GATE_COMPONENT` | the canary component name |
| `GATE_DECISION` | PROMOTE or ROLLBACK |
| `GATE_HEALTH_SCORE` | 0–100 |
| `GATE_ANOMALY_SCORE` | 0–100 |
| `GATE_EXIT_CODE` | 0 or 1 |
| `GATE_VIOLATIONS` | `; `-joined breach details |

### Fail-open on notify

If the notification call fails (network, bad token), the gate logs a warning but
**does not change its decision or exit code**. Notification is additive; the
exit code remains the source of truth. Conversely, metric collection in
production mode is fail-**closed** (no metrics → ROLLBACK). The two are
deliberately different: never promote without data, but never let a messaging
hiccup override a real decision.

## Security note

Tokens are read only from environment variables named in `pipeline.yaml` — never
stored in the config or committed. In GitLab use CI/CD variables (masked); in
Jenkins use credentials bindings (as shown in the `Jenkinsfile`).
