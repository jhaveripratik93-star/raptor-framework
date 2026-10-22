# Client demo — live baseline capture, metric capture, and dashboard plotting

Runbook for capturing real metrics from the live `eric-cenx-rest-api` service
in `robot-poc` (via the `robot` pod) and turning them into a client-facing
PNG dashboard that shows the gate's PROMOTE/ROLLBACK decision against a
naive SLA-only comparison. See `scripts/plot_dashboard.py` and
`scripts/live_sample_and_score.py` for the underlying code.

## 0. One-time setup (per terminal session)

```powershell
$env:KUBECONFIG = "$env:USERPROFILE\.kube\berg005_config"
$Namespace = "robot-poc"
$Pod = "robot-56d5cb999d-lkvf4"   # confirm with: kubectl get pods -n robot-poc -l app=robot
$Container = "robot"

# Redis password, needed for both capture steps
$redisPw = kubectl get secret redis -n $Namespace -o jsonpath='{.data.redis-password}'
$redisPwPlain = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($redisPw))
```

## 1. Capture a real baseline (replaces the thin 10-row one)

30 samples at 10s apart ≈ 5 minutes. Run this while the service is
healthy/normal.

```powershell
$cmd = "cd /opt/raptor-gate; export REDIS_PASSWORD='$redisPwPlain'; " +
       "python3.11 scripts/capture_baseline.py " +
       "--collectors gate/config/collectors_incluster_rest-api.yaml " +
       "--samples 30 --interval 10 " +
       "--out data/v1_baseline_live.csv"
kubectl exec -n $Namespace $Pod -c $Container -- sh -c $cmd
```

This overwrites `data/v1_baseline_live.csv` **inside the pod** with 30
fresh rows. Nothing in your local repo changes yet — pull it back if you
want a copy:

```powershell
kubectl cp "${Namespace}/${Pod}:/opt/raptor-gate/data/v1_baseline_live.csv" data\v1_baseline_live.csv -c $Container
```

If this command runs longer than a default terminal timeout, run it as a
background process instead:

```powershell
# alternative: non-blocking
Start-Process pwsh -ArgumentList "-NoExit","-Command",$cmd
```

## 1.5. (Optional) Generate real load to actually trigger a divergence

With the service idle, every sample PROMOTEs and the chart has nothing
dramatic to show. `cpu_usage` is sourced from
`java_lang_operatingsystem_processcpuload` -- the target JVM's own process
CPU load (see `gate/config/collectors_incluster_rest-api.yaml`). That only
moves when the JVM itself does work, so stressing the node or a sidecar
does nothing; you need to send the service real concurrent requests.

Open a **second** `kubectl exec` session into the pod (keep step 2 running
in the first) and run:

```powershell
$cmd2 = "cd /opt/raptor-gate; python3.11 scripts/generate_load.py " +
        "--url http://eric-cenx-rest-api.robot-poc.svc.cluster.local:8080/ " +
        "--concurrency 20 --duration 90"
kubectl exec -n $Namespace $Pod -c $Container -- sh -c $cmd2
```

Start this a few seconds after step 2's capture begins so the capture
window actually overlaps with the load. Start with low concurrency (10-20)
and watch the error rate it prints; raise concurrency gradually if you want
a bigger spike. This sends real traffic to a real service -- do not point
it at anything beyond what you're explicitly demoing against.

## 2. Run the live capture + auto-generate the dashboard

Scores N real samples through the actual gate and writes CSV + PNG inside
the pod. Example: 8 samples, 20s apart (~3 min). Increase
`--duration`/`--interval` for a longer, richer chart (e.g.
`--duration 600 --interval 30` for 20 samples over 10 minutes).

```powershell
$cmd = "cd /opt/raptor-gate; export REDIS_PASSWORD='$redisPwPlain'; " +
       "python3.11 scripts/live_sample_and_score.py " +
       "--collectors gate/config/collectors_incluster_rest-api.yaml " +
       "--baseline data/v1_baseline_live.csv " +
       "--duration 160 --interval 20"
kubectl exec -n $Namespace $Pod -c $Container -- sh -c $cmd
```

This prints the full collect → score → verdict log to your terminal and,
at the end, writes two files inside the pod:
`data/live_results_<app>_<timestamp>.csv` and the matching `.png`
dashboard (auto-rendered by `scripts/plot_dashboard.py`, which runs
automatically at the end — pass `--no-plot` to skip it).

## 3. Pull the results back to your machine

The pod prints the exact filename it wrote (e.g.
`data/live_results_eric-cenx-rest-api_20261016_125759.csv`) — copy that
timestamp into both lines below. Once you've picked the run you want to
keep as your reference scenario, rename it to something stable like
`data/scenario_positive_promote.csv` / `data/scenario_negative_rollback.csv`
(see below) so repeat demo runs don't pile up under `data/`.

```powershell
$ts = "eric-cenx-rest-api_20261016_125759"   # <- replace with the actual filename printed by step 2
kubectl cp "${Namespace}/${Pod}:/opt/raptor-gate/data/live_results_$ts.csv" "data\live_results_$ts.csv" -c $Container
kubectl cp "${Namespace}/${Pod}:/opt/raptor-gate/data/live_results_$ts.png" "data\live_results_$ts.png" -c $Container
```

## 4. (Optional) Re-render the PNG locally without re-running the capture

If you want to tweak the title or re-plot without hitting the cluster
again, once the CSV is local:

```powershell
& "$PWD\.venv\Scripts\python.exe" scripts\plot_dashboard.py --csv "data\live_results_$ts.csv" --title "Client Demo — eric-cenx-rest-api"
```

## Notes on getting a demo-worthy chart

- With the service healthy and under no load, every sample will PROMOTE
  with 0 divergence — accurate, but not a dramatic chart. To show the "why
  ML is needed" moment, run step 2 while something is actually degraded (a
  load test, a deliberate CPU spike, or a canary with elevated resource
  usage) so the SLA-only vs. gate decisions actually diverge.
- `--duration`/`--interval` trade off chart resolution vs. runtime — more
  samples = smoother lines, longer wait.
- Steps 1 and 2 both need `REDIS_PASSWORD` exported in the pod's shell;
  the `$redisPwPlain` variable from step 0 handles that automatically in
  these commands.
