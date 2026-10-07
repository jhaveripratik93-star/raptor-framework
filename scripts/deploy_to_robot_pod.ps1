<#
Raptor Deployment Gate -- manual deploy into the `robot` pod (robot-poc, berg005)
==================================================================================

WHAT THIS IS
  A manual, ad hoc way to run the Raptor gate (feature store + Isolation Forest
  + SLA engine + FastAPI serving) inside a pod you already own and can modify
  — `robot` in the `robot-poc` namespace on berg005 — without building or
  pushing a container image.

  It works because that pod already has `python3.11` installed (confirmed --
  /usr/bin/python3.11, Python 3.11.15) alongside its default 3.6 interpreter,
  has outbound internet (pip can reach PyPI), runs as root, and has a writable
  filesystem with ~80GB free.

WHAT THIS IS NOT
  - NOT persistent. `robot` is managed by a Deployment/ReplicaSet (release
    cenxcitest9257645). Any rollout, restart, or node drain replaces the pod
    with a fresh one from its original image — everything this script copies
    or installs is gone with it, no warning. Re-run this script after any pod
    restart.
  - NOT the production deployment path. The real artifact for that is the
    Kaniko-built image + `deploy/k8s/deployment.yaml` (separate pod, its own
    Deployment/Service, survives restarts, reproducible). Use this script for
    a live client demo or quick validation while that path is still blocked
    on registry access -- not as a substitute for it.
  - Shares `robot`'s resources (CPU/memory/network) with whatever `robot`
    normally does. Keep an eye on it if that pod has other responsibilities.

WHAT IT DOES
  1. Stages a clean copy of the repo subset the gate needs (gate/, data/,
     requirements.txt) into a local temp folder, mirroring .dockerignore.
  2. kubectl cp's that into /opt/raptor-gate inside the robot pod.
  3. Installs dependencies with python3.11 -m pip (targeted install dir, not
     touching the pod's system Python).
  4. Trains the Isolation Forest model from data/v1_baseline.csv (or
     data/v1_baseline_live.csv if present) so a model.joblib exists.
  5. Reads the existing redis Secret's password and launches
     uvicorn gate.serving.app:app in the background inside the pod, with
     REDIS_HOST/PORT/PASSWORD pointed at the Phase-1 Redis already running in
     robot-poc.
  6. Polls /health until the server responds, then prints quick-start curl
     commands for a PROMOTE / ROLLBACK demo.

PREREQUISITES
  - kubectl on PATH, pointed at the berg005 kubeconfig (adjust $KubeConfig
    below if yours lives somewhere else).
  - Run from the repo root, or pass -RepoRoot.

USAGE
  pwsh ./scripts/deploy_to_robot_pod.ps1
  pwsh ./scripts/deploy_to_robot_pod.ps1 -PodName robot-56d5cb999d-lkvf4
#>

param(
    [string]$KubeConfig = "$env:USERPROFILE\.kube\berg005_config",
    [string]$Namespace = "robot-poc",
    [string]$PodLabelSelector = "app=robot",
    [string]$PodName = "",            # override; otherwise resolved via label
    [string]$ContainerName = "robot",
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$RemoteDir = "/opt/raptor-gate",
    [int]$Port = 8080
)

$ErrorActionPreference = "Stop"
$env:KUBECONFIG = $KubeConfig

function Info($msg) { Write-Host "[i] $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "[OK] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "[x] $msg" -ForegroundColor Red; exit 1 }

function Invoke-Kubectl {
    # NOTE: do not name this parameter $Args -- it collides with PowerShell's
    # reserved automatic $args variable inside a function and silently
    # discards whatever is passed in, causing kubectl to run with no
    # arguments (which just prints the help text instead of failing loudly).
    param([string[]]$KubectlArgs)
    # NOTE: with the script-wide $ErrorActionPreference = "Stop", merging
    # stderr via 2>&1 is dangerous -- Windows PowerShell turns any native
    # command's stderr line into an ErrorRecord, and EAP=Stop makes that
    # throw a terminating NativeCommandError immediately, bypassing the
    # ExitCode check below entirely. This bit us on the /health polling
    # loop (curl legitimately exits non-zero before the server is ready).
    # Scope EAP to Continue for just this call so every call site returns
    # its exit code to us normally instead of throwing.
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & kubectl @KubectlArgs 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEap
    }
    [PSCustomObject]@{ Output = ($out -join "`n"); ExitCode = $code }
}

# ------------------------------------------------------------------ preflight
Info "Preflight checks..."
if (-not (Test-Path $KubeConfig)) { Die "kubeconfig not found: $KubeConfig" }
if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) { Die "kubectl not on PATH" }

$ctx = Invoke-Kubectl -KubectlArgs @("config", "current-context")
if ($ctx.ExitCode -ne 0) { Die "kubectl can't read context from $KubeConfig" }
Ok "Using context: $($ctx.Output)"

if (-not $PodName) {
    $resolved = Invoke-Kubectl -KubectlArgs @("get", "pods", "-n", $Namespace, "-l", $PodLabelSelector,
                                  "-o", "jsonpath={.items[0].metadata.name}")
    if ($resolved.ExitCode -ne 0 -or -not $resolved.Output) {
        Die "could not resolve a pod with selector '$PodLabelSelector' in $Namespace. Pass -PodName explicitly."
    }
    $PodName = $resolved.Output.Trim()
}
Ok "Target pod: $PodName (namespace: $Namespace, container: $ContainerName)"

$ready = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $PodName, "-c", $ContainerName, "--", "true")
if ($ready.ExitCode -ne 0) { Die "cannot exec into $PodName/$ContainerName : $($ready.Output)" }
Ok "Pod is reachable"

$pyCheck = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $PodName, "-c", $ContainerName, "--",
                            "sh", "-c", "command -v python3.11 && python3.11 --version")
if ($pyCheck.ExitCode -ne 0) {
    Die "python3.11 not found in $PodName. This script relies on it being pre-installed (confirmed present on robot-56d5cb999d-lkvf4). If your pod differs, install it first (zypper install python311 python311-pip) or adapt -ContainerName/-PodName."
}
Ok "Remote interpreter: $($pyCheck.Output.Trim())"

$setsidCheck = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $PodName, "-c", $ContainerName, "--",
                            "sh", "-c", "command -v setsid")
if ($setsidCheck.ExitCode -ne 0) {
    Die "setsid not found in $PodName (usually part of util-linux). It is required to launch uvicorn as a fully detached daemon -- without it, kubectl exec hangs waiting on the backgrounded server's inherited session pipe. Install util-linux or adapt the launch step."
}
Ok "setsid available for daemonizing the server"

# ------------------------------------------------------------- 1. stage repo
Info "Staging build context from $RepoRoot ..."
$stage = Join-Path $env:TEMP "raptor_robot_ctx"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Path $stage | Out-Null

Copy-Item (Join-Path $RepoRoot "requirements.txt") (Join-Path $stage "requirements.txt")
Copy-Item -Recurse (Join-Path $RepoRoot "gate") (Join-Path $stage "gate")
Copy-Item -Recurse (Join-Path $RepoRoot "data") (Join-Path $stage "data")
Get-ChildItem -Path $stage -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Ok "Staged at $stage"

# ------------------------------------------------------- 2. copy into the pod
Info "Copying into ${PodName}:${RemoteDir} ..."
Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $PodName, "-c", $ContainerName, "--",
                 "mkdir", "-p", $RemoteDir) | Out-Null

# kubectl cp parses the first ':' as the src:dest separator, which collides
# with Windows drive letters (C:\...). Run from inside the staging dir and
# use relative paths to avoid that.
Push-Location $stage
try {
    foreach ($item in @("requirements.txt", "gate", "data")) {
        $cp = Invoke-Kubectl -KubectlArgs @("cp", ".\$item", "$Namespace/${PodName}:$RemoteDir/$item", "-c", $ContainerName)
        if ($cp.ExitCode -ne 0) { Die "kubectl cp failed for $item : $($cp.Output)" }
    }
} finally {
    Pop-Location
}
Ok "Code copied to $RemoteDir"

# --------------------------------------------------------- 3. install deps
Info "Installing dependencies with python3.11 (this can take a minute)..."
$pipInstall = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $PodName, "-c", $ContainerName, "--",
    "sh", "-c", "cd $RemoteDir && python3.11 -m pip install --quiet --root-user-action=ignore -r requirements.txt")
if ($pipInstall.ExitCode -ne 0) { Die "pip install failed:`n$($pipInstall.Output)" }
Ok "Dependencies installed"

# --------------------------------------------------------- 4. train the model
Info "Training the Isolation Forest model..."
$baseline = "data/v1_baseline.csv"
$liveCheck = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $PodName, "-c", $ContainerName, "--",
    "sh", "-c", "test -f $RemoteDir/data/v1_baseline_live.csv && echo yes || echo no")
if ($liveCheck.Output.Trim() -eq "yes") {
    $baseline = "data/v1_baseline_live.csv"
    Info "Using live baseline: $baseline"
} else {
    Info "Using bundled baseline: $baseline"
}
$train = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $PodName, "-c", $ContainerName, "--",
    "sh", "-c", "cd $RemoteDir && python3.11 -m gate.train --baseline $baseline --out model/model.joblib")
if ($train.ExitCode -ne 0) { Die "model training failed:`n$($train.Output)" }
Ok "Model trained -> $RemoteDir/model/model.joblib"

# ------------------------------------------------- 5. redis password + launch
Info "Reading the existing 'redis' Secret for REDIS_PASSWORD..."
$redisPw = Invoke-Kubectl -KubectlArgs @("get", "secret", "redis", "-n", $Namespace,
    "-o", "jsonpath={.data.redis-password}")
if ($redisPw.ExitCode -ne 0 -or -not $redisPw.Output) {
    Warn "could not read the redis Secret; starting without Redis (feature store will be memory-only)"
    $redisPwPlain = ""
} else {
    $redisPwPlain = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($redisPw.Output.Trim()))
    Ok "REDIS_PASSWORD retrieved from the redis Secret (not printed)"
}

Info "Stopping any previous run and starting the server on port $Port ..."
# pkill the old uvicorn if one is already running from a prior invocation of
# this script, then start fresh in the background with nohup (kubectl exec
# has no tty/session to keep a foreground process attached to).
#   NOTE: the bracket on the first letter ('[u]vicorn...') is the standard
#   pkill self-match guard — without it, pkill -f matches against full
#   command lines, including its OWN invocation (which contains this exact
#   search string as a literal argument), causing it to kill its own shell
#   and return exit 143 (SIGTERM) through kubectl exec.
Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $PodName, "-c", $ContainerName, "--",
    "sh", "-c", "pkill -f '[u]vicorn gate.serving.app' 2>/dev/null; sleep 1; true") | Out-Null

#   NOTE on backgrounding over kubectl exec: redirecting stdin/stdout/stderr
#   (nohup ... < /dev/null) is NOT enough here. `kubectl exec` keeps its own
#   session pipe open to the pod until every process that inherited it exits.
#   A plain "cmd &" still shares that pty/pipe at the process-group level, so
#   kubectl exec hangs forever waiting for uvicorn (which never exits) even
#   though stdio is redirected. Confirmed by testing directly against this
#   pod: `nohup ... &` hung the exec call indefinitely while the server
#   itself started fine; swapping to `setsid` (full session detach -- the
#   standard fix for daemonizing under ssh/kubectl exec) returned
#   immediately. `disown` is skipped -- it is a bash builtin, not available
#   in POSIX sh (the default shell on SLES).
# NOTE: this MUST be a single-line, semicolon-separated command, not a
# multi-line heredoc with && / \ continuations. Tested directly against this
# pod: the equivalent multi-line @"..."@ string, passed as the sh -c
# argument, hung kubectl exec indefinitely even though the remote shell
# printed its final "echo" line and the server started successfully. The
# single-line form with semicolons returns in ~3s every time. Root cause
# looks like how a multi-line string gets marshalled as a single argv entry
# through kubectl on Windows, not a shell/redirection issue.
$startCmd = "cd $RemoteDir; " +
    "export GATE_MODEL_PATH=$RemoteDir/model/model.joblib; " +
    "export REDIS_HOST=redis-master.$Namespace.svc.cluster.local; " +
    "export REDIS_PORT=6379; " +
    "export REDIS_PASSWORD='$redisPwPlain'; " +
    "setsid python3.11 -m uvicorn gate.serving.app:app --host 0.0.0.0 --port $Port " +
    "> $RemoteDir/server.log 2>&1 < /dev/null & " +
    "sleep 2; echo started"
$start = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $PodName, "-c", $ContainerName, "--", "sh", "-c", $startCmd)
if ($start.ExitCode -ne 0) { Die "failed to launch the server:`n$($start.Output)" }
Ok "Server launch issued"

# ------------------------------------------------------------- 6. verify
Info "Waiting for /health to respond..."
$healthy = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 2
    $h = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $PodName, "-c", $ContainerName, "--",
        "sh", "-c", "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$Port/health")
    if ($h.Output.Trim() -eq "200") { $healthy = $true; break }
}

if (-not $healthy) {
    $log = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $PodName, "-c", $ContainerName, "--",
        "sh", "-c", "tail -n 40 $RemoteDir/server.log")
    Die "server did not become healthy in time. Last log lines:`n$($log.Output)"
}
Ok "Server is healthy inside the pod on port $Port"

Write-Host ""
Write-Host "============================================================"
Write-Host " Raptor Gate is running inside $PodName (namespace $Namespace)"
Write-Host "============================================================"
Write-Host ""
Write-Host "Reach it from your machine with a port-forward:"
Write-Host "  kubectl --kubeconfig `"$KubeConfig`" port-forward -n $Namespace $PodName ${Port}:${Port}"
Write-Host ""
Write-Host "Then, in another terminal:"
Write-Host "  curl http://127.0.0.1:$Port/health"
Write-Host ""
$promoteJson = '{"component":"v2","sample":{"cpu_usage":46,"memory_usage":53,"pod_restarts":0,"http_error_rate":0.2,"p99_latency":90,"availability":99.8,"redis_latency":3,"endpoint_latency":88,"packet_loss":0.15}}'
$rollbackJson = '{"component":"v2-degraded","sample":{"cpu_usage":78,"memory_usage":82,"pod_restarts":4,"http_error_rate":2.8,"p99_latency":380,"availability":97.5,"redis_latency":18,"endpoint_latency":410,"packet_loss":1.8}}'
$dq = [char]34

Write-Host "PROMOTE demo (healthy canary):"
Write-Host ("  curl -X POST http://127.0.0.1:$Port/score -H " + $dq + "Content-Type: application/json" + $dq + " -d '$promoteJson'")
Write-Host ""
Write-Host "ROLLBACK demo (degraded canary):"
Write-Host ("  curl -X POST http://127.0.0.1:$Port/score -H " + $dq + "Content-Type: application/json" + $dq + " -d '$rollbackJson'")
Write-Host ""
Write-Host "Server log inside the pod: $RemoteDir/server.log"
Write-Host "Re-run this script any time the pod restarts (it is NOT persistent)."
