<#
Raptor Deployment Gate -- full environment setup / rebuild (berg005, robot-poc)
=================================================================================

WHAT THIS IS
  A single, idempotent PowerShell script that rebuilds everything the Raptor
  gate needs after the berg005 deployment was removed:

    PHASE 1  Infra        Redis + kube-prometheus-stack Helm releases in
                           robot-poc (InfluxDB optional -- the active
                           collectors config does not use it)
    PHASE 2  Code copy     gate/, data/, scripts/, requirements.txt copied
                           into the existing `robot` pod at /opt/raptor-gate
    PHASE 3  Dependencies  python3.11 -m pip install -r requirements.txt
                           inside that pod
    PHASE 4  Train model   Isolation Forest trained from the baseline CSV
    PHASE 5  Launch        uvicorn gate.serving.app:app started detached
                           inside the pod, pointed at the fresh Redis
    PHASE 6  Verify        poll /health, print demo curl commands
    PHASE 7  (optional)    capture a REAL baseline from the live service and
                           pull it back to your local repo
    PHASE 8  (optional)    build + push the production raptor-gate image via
                           Kaniko and apply the standalone K8s Deployment/
                           Service -- OFF by default (pushes to a registry,
                           a separate higher-risk action), opt in with
                           -BuildProductionImage

WHAT THIS DOES **NOT** DO
  It does not and cannot deploy the target business microservice itself
  (eric-cenx-rest-api / the `robot` pod's own release, `cenxcitest9257645`).
  Those are the client's own workloads, not part of this repo. If Phase 0's
  diagnostic shows either one missing, this script will tell you so and stop
  short of the phases that depend on them -- redeploy those via whatever
  pipeline/chart normally manages them, then re-run this script.

SAFETY
  - Phases 1-7 create/modify resources only inside the robot-poc namespace,
    using the existing `robot` pod as a host (same approach already proven
    in deploy_to_robot_pod.ps1) and well-known public Helm charts.
  - Secrets (Redis/Grafana/InfluxDB passwords) are only ever passed via
    --set on the helm command line or read from the existing Secret
    afterwards -- never written to disk or printed.
  - Phase 8 is OFF by default because it pushes an image to a registry
    (an outbound, harder-to-reverse action). It requires the explicit
    -BuildProductionImage switch.
  - Nothing destructive (delete/uninstall) is performed anywhere in this
    script.

USAGE
  # 1) See what's already there and what's missing, without changing anything:
  pwsh ./scripts/setup_raptor_environment.ps1 -DiagnoseOnly

  # 2) Full rebuild: infra + code copy + train + launch (prompts for secrets):
  pwsh ./scripts/setup_raptor_environment.ps1 -InstallInfra

  # 3) Same, plus capture a real 30-sample baseline afterwards:
  pwsh ./scripts/setup_raptor_environment.ps1 -InstallInfra -CaptureBaselineSamples 30

  # 4) Infra already present -- just (re)do the pod code copy + relaunch:
  pwsh ./scripts/setup_raptor_environment.ps1 -SkipInfra

  # 5) Also build+push the standalone production image (explicit opt-in):
  pwsh ./scripts/setup_raptor_environment.ps1 -InstallInfra -BuildProductionImage `
      -RegistryImage "armdocker.rnd.ericsson.se/proj-cenx/raptor-gate:1.0"
#>

[CmdletBinding()]
param(
    [string]$KubeConfig = "$env:USERPROFILE\.kube\berg005_config",
    [string]$Namespace = "robot-poc",
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$RemoteDir = "/opt/raptor-gate",
    [int]$Port = 8080,

    # --- target host pod (where the gate code runs from) ---
    [string]$PodLabelSelector = "app=robot",
    [string]$PodName = "",
    [string]$ContainerName = "robot",

    # --- diagnostics only, no changes ---
    [switch]$DiagnoseOnly,

    # --- Phase 1: infra ---
    [switch]$SkipInfra,
    [switch]$InstallInfra,
    [switch]$InstallInfluxDB,
    [switch]$CreateNamespace,
    [string]$RedisPassword = "",
    [string]$GrafanaPassword = "",
    [string]$InfluxPassword = "",
    [string]$InfluxToken = "",

    # --- Phases 2-6: pod code copy / deps / train / launch / verify ---
    [switch]$SkipPodDeploy,
    [string]$CollectorsConfig = "gate/config/collectors_incluster_rest-api.yaml",

    # --- Phase 7: optional real baseline capture ---
    [int]$CaptureBaselineSamples = 0,
    [int]$CaptureBaselineInterval = 10,

    # --- Phase 8: optional production image build (OFF by default) ---
    [switch]$BuildProductionImage,
    [string]$RegistryImage = "armdocker.rnd.ericsson.se/proj-cenx/raptor-gate:1.0",

    [switch]$Force
)

$ErrorActionPreference = "Stop"
$env:KUBECONFIG = $KubeConfig

# ============================================================== small helpers
function Info($msg)    { Write-Host "[i] $msg" -ForegroundColor Cyan }
function Ok($msg)      { Write-Host "[OK] $msg" -ForegroundColor Green }
function Warn($msg)    { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Die($msg)     { Write-Host "[x] $msg" -ForegroundColor Red; exit 1 }
function Banner($msg) {
    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor DarkCyan
    Write-Host "  $msg" -ForegroundColor White
    Write-Host ("=" * 78) -ForegroundColor DarkCyan
}

function Confirm-Step($msg) {
    if ($Force) { return $true }
    $resp = Read-Host "$msg [y/N]"
    return ($resp -match '^(y|yes)$')
}

function Invoke-Kubectl {
    # See deploy_to_robot_pod.ps1 for why EAP is scoped to Continue here:
    # with EAP=Stop, any stderr line from a native command becomes a
    # terminating error via 2>&1, bypassing our own exit-code handling.
    param([string[]]$KubectlArgs)
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

function Invoke-Helm {
    param([string[]]$HelmArgs)
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & helm @HelmArgs 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEap
    }
    [PSCustomObject]@{ Output = ($out -join "`n"); ExitCode = $code }
}

function Get-SecretOrPrompt([string]$Value, [string]$PromptLabel) {
    if ($Value) { return $Value }
    $secure = Read-Host -Prompt $PromptLabel -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr) }
    finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

# ==================================================================== PHASE 0
function Invoke-Preflight {
    Banner "PHASE 0 -- Preflight & diagnostics"

    if (-not (Test-Path $KubeConfig)) { Die "kubeconfig not found: $KubeConfig" }
    if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) { Die "kubectl not on PATH" }
    if (-not (Get-Command helm -ErrorAction SilentlyContinue)) { Die "helm not on PATH" }

    $ctx = Invoke-Kubectl -KubectlArgs @("config", "current-context")
    if ($ctx.ExitCode -ne 0) { Die "kubectl can't read context from $KubeConfig" }
    Ok "Context: $($ctx.Output.Trim())"

    $info = Invoke-Kubectl -KubectlArgs @("cluster-info")
    if ($info.ExitCode -ne 0) { Die "kubectl cannot reach the cluster. Check VPN / kubeconfig." }
    Ok "Cluster reachable"

    $nsCheck = Invoke-Kubectl -KubectlArgs @("get", "ns", $Namespace)
    $script:NamespaceExists = ($nsCheck.ExitCode -eq 0)
    if (-not $script:NamespaceExists) {
        Warn "Namespace '$Namespace' does not exist"
        if ($CreateNamespace) {
            if (Confirm-Step "Create namespace '$Namespace'?") {
                $create = Invoke-Kubectl -KubectlArgs @("create", "namespace", $Namespace)
                if ($create.ExitCode -ne 0) { Die "failed to create namespace: $($create.Output)" }
                Ok "Namespace '$Namespace' created"
                $script:NamespaceExists = $true
            } else { Die "Namespace required to proceed. Re-run with -CreateNamespace to create it." }
        } else {
            Die "Namespace '$Namespace' not found. Re-run with -CreateNamespace to create it, or create it yourself first."
        }
    } else {
        Ok "Namespace '$Namespace' exists"
    }

    # --- diagnostic status table (non-fatal, informational) ---
    function Test-HelmRelease([string]$name) {
        $r = Invoke-Helm -HelmArgs @("status", $name, "-n", $Namespace)
        return ($r.ExitCode -eq 0)
    }
    # Redis is a plain manifest now (infra/k8s/redis.yaml), not a Helm
    # release -- see the comment in Invoke-InfraPhase for why. Check for its
    # Deployment directly instead of `helm status redis`.
    $redisDeploy = Invoke-Kubectl -KubectlArgs @("get", "deployment", "redis-master", "-n", $Namespace)
    $script:HasRedis   = ($redisDeploy.ExitCode -eq 0)
    $script:HasProm    = Test-HelmRelease "kube-prom"
    $script:HasInflux  = Test-HelmRelease "influxdb"

    $robotPod = Invoke-Kubectl -KubectlArgs @("get", "pods", "-n", $Namespace, "-l", $PodLabelSelector,
                                               "-o", "jsonpath={.items[0].metadata.name}")
    $script:RobotPodFound = ($robotPod.ExitCode -eq 0 -and $robotPod.Output)
    if ($script:RobotPodFound) { $script:ResolvedPodName = $robotPod.Output.Trim() }

    $targetSvc = Invoke-Kubectl -KubectlArgs @("get", "svc", "eric-cenx-rest-api", "-n", $Namespace)
    $script:TargetSvcFound = ($targetSvc.ExitCode -eq 0)

    $gateDeploy = Invoke-Kubectl -KubectlArgs @("get", "deployment", "raptor-gate", "-n", $Namespace)
    $script:GateDeployFound = ($gateDeploy.ExitCode -eq 0)

    Write-Host ""
    Write-Host "  Component                                Status" -ForegroundColor DarkGray
    Write-Host "  ----------------------------------------  --------------------------"
    Write-Host ("  Namespace '{0}'{1}Present" -f $Namespace, (" " * (42 - $Namespace.Length)))
    Write-Host ("  Helm release: redis                      {0}" -f $(if ($script:HasRedis)  {"Present"} else {"MISSING"}))
    Write-Host ("  Helm release: kube-prom                  {0}" -f $(if ($script:HasProm)   {"Present"} else {"MISSING"}))
    Write-Host ("  Helm release: influxdb (optional)         {0}" -f $(if ($script:HasInflux) {"Present"} else {"not installed"}))
    Write-Host ("  Pod '{0}' (host for the gate)        {1}" -f $PodLabelSelector, $(if ($script:RobotPodFound) {"Present ($($script:ResolvedPodName))"} else {"MISSING"}))
    Write-Host ("  Service 'eric-cenx-rest-api' (canary target) {0}" -f $(if ($script:TargetSvcFound) {"Present"} else {"MISSING"}))
    Write-Host ("  Deployment 'raptor-gate' (standalone prod)  {0}" -f $(if ($script:GateDeployFound) {"Present"} else {"not deployed"}))
    Write-Host ""

    if (-not $script:TargetSvcFound) {
        Warn "eric-cenx-rest-api is the CLIENT'S OWN microservice, not part of this repo."
        Warn "This script cannot create it. If it's missing, redeploy it via its own"
        Warn "release/pipeline (previously 'cenxcitest9257645') before continuing --"
        Warn "the collectors will fail (fail-closed -> ROLLBACK) without it."
    }
    if (-not $script:RobotPodFound) {
        Warn "No pod matches selector '$PodLabelSelector'. Phases 2-7 (code copy,"
        Warn "train, launch, verify, baseline capture) will be skipped. Infra (Phase 1)"
        Warn "can still proceed on its own."
    }
}

# ==================================================================== PHASE 1
function Invoke-InfraPhase {
    if ($SkipInfra) { Info "Skipping Phase 1 (infra) -- -SkipInfra set"; return }
    Banner "PHASE 1 -- Infra (Redis + Prometheus stack in $Namespace)"

    $missing = @()
    if (-not $script:HasRedis) { $missing += "redis" }
    if (-not $script:HasProm)  { $missing += "kube-prom" }
    if ($InstallInfluxDB -and -not $script:HasInflux) { $missing += "influxdb" }

    if ($missing.Count -eq 0) {
        Ok "All required Helm releases already present"
        return
    }

    if (-not $InstallInfra) {
        Warn "Missing Helm releases: $($missing -join ', ')"
        Warn "Re-run with -InstallInfra to install them (you'll be prompted for secrets)."
        return
    }

    Info "About to install: $($missing -join ', ') into namespace '$Namespace'."
    Info "This creates StatefulSets/Deployments/PVCs/Secrets -- nothing is deleted."
    if (-not (Confirm-Step "Proceed with Helm install?")) { Warn "Skipped infra install."; return }

    Info "Adding/updating Helm repos..."
    Invoke-Helm -HelmArgs @("repo", "add", "prometheus-community", "https://prometheus-community.github.io/helm-charts") | Out-Null
    if ($missing -contains "influxdb") {
        Invoke-Helm -HelmArgs @("repo", "add", "influxdata", "https://helm.influxdata.com/") | Out-Null
    }
    Invoke-Helm -HelmArgs @("repo", "update") | Out-Null
    Ok "Helm repos ready"

    if ($missing -contains "redis") {
        $RedisPassword = Get-SecretOrPrompt $RedisPassword "Set Redis password"
    }
    if ($missing -contains "kube-prom") {
        $GrafanaPassword = Get-SecretOrPrompt $GrafanaPassword "Set Grafana admin password"
    }
    if ($missing -contains "influxdb") {
        $InfluxPassword = Get-SecretOrPrompt $InfluxPassword "Set InfluxDB admin password"
        $InfluxToken    = Get-SecretOrPrompt $InfluxToken    "Set InfluxDB admin token"
    }

    foreach ($rel in $missing) {
        switch ($rel) {
            "redis" {
                # NOTE: NOT the bitnami/redis Helm chart. Since Broadcom's Sept
                # 2025 restructuring of the Bitnami catalog, that chart's images
                # resolve through an OCI path that now requires a paid Bitnami
                # Secure Images subscription ("401 Unauthorized" on
                # auth.docker.io for bitnamicharts/redis). Instead we apply a
                # plain manifest (infra/k8s/redis.yaml) using the official,
                # freely-pullable `redis` image from Docker Hub. It creates the
                # same Secret name/key, Service name, and port the rest of the
                # repo already expects, so nothing downstream needs to change.
                Info "Installing redis via plain manifest (infra/k8s/redis.yaml) -- bitnami/redis is no longer freely pullable, see comment in that file..."
                $secretCheck = Invoke-Kubectl -KubectlArgs @("get", "secret", "redis", "-n", $Namespace)
                if ($secretCheck.ExitCode -ne 0) {
                    $createSecret = Invoke-Kubectl -KubectlArgs @("create", "secret", "generic", "redis",
                        "-n", $Namespace, "--from-literal=redis-password=$RedisPassword")
                    if ($createSecret.ExitCode -ne 0) { Die "failed to create the redis Secret:`n$($createSecret.Output)" }
                    Ok "Secret 'redis' created"
                } else {
                    Info "Secret 'redis' already exists -- leaving it as-is (password unchanged)"
                }
                $r = Invoke-Kubectl -KubectlArgs @("apply", "-f", (Join-Path $RepoRoot "infra\k8s\redis.yaml"))
                if ($r.ExitCode -ne 0) { Die "redis apply failed:`n$($r.Output)" }
                Ok "redis deployment/service applied"
            }
            "kube-prom" {
                Info "Installing kube-prom (prometheus-community/kube-prometheus-stack)..."
                Info "(First boot can take several minutes -- WAL replay + startup probe grace period.)"
                $r = Invoke-Helm -HelmArgs @("install", "kube-prom", "prometheus-community/kube-prometheus-stack", "-n", $Namespace,
                    "-f", (Join-Path $RepoRoot "infra\helm\prometheus-values.yaml"),
                    "--set", "grafana.adminPassword=$GrafanaPassword")
                if ($r.ExitCode -ne 0) { Die "kube-prom install failed:`n$($r.Output)" }
                Ok "kube-prom installed"
            }
            "influxdb" {
                Info "Installing influxdb (influxdata/influxdb2)..."
                $r = Invoke-Helm -HelmArgs @("install", "influxdb", "influxdata/influxdb2", "-n", $Namespace,
                    "-f", (Join-Path $RepoRoot "infra\helm\influxdb-values.yaml"),
                    "--set", "adminUser.password=$InfluxPassword",
                    "--set", "adminUser.token=$InfluxToken")
                if ($r.ExitCode -ne 0) { Die "influxdb install failed:`n$($r.Output)" }
                Ok "influxdb installed"
            }
        }
    }

    Info "Waiting for redis to become ready (up to 3 min)..."
    Invoke-Kubectl -KubectlArgs @("wait", "--for=condition=ready", "pod",
        "-l", "app=redis-master", "-n", $Namespace, "--timeout=180s") | Out-Null

    Info "Waiting for the Prometheus pod to become ready (up to 10 min on first boot)..."
    $pw = Invoke-Kubectl -KubectlArgs @("wait", "--for=condition=ready", "pod",
        "-l", "app.kubernetes.io/name=prometheus", "-n", $Namespace, "--timeout=600s")
    if ($pw.ExitCode -ne 0) {
        Warn "Prometheus did not report ready within the timeout. Check: kubectl get pods -n $Namespace"
    } else {
        Ok "Prometheus ready"
    }

    $script:HasRedis = $true
    $script:HasProm = $true
    Ok "Phase 1 complete"
}

# ==================================================================== PHASE 2-6
function Invoke-PodDeployPhases {
    if ($SkipPodDeploy) { Info "Skipping Phases 2-6 (pod code deploy) -- -SkipPodDeploy set"; return }
    if (-not $script:RobotPodFound -and -not $PodName) {
        Warn "No host pod available -- skipping Phases 2-6 (code copy/train/launch/verify)."
        return
    }

    $targetPod = if ($PodName) { $PodName } else { $script:ResolvedPodName }

    $ready = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $targetPod, "-c", $ContainerName, "--", "true")
    if ($ready.ExitCode -ne 0) { Die "cannot exec into $targetPod/$ContainerName : $($ready.Output)" }

    # NOTE: this pod is NOT persistent (see the script header) -- any
    # rollout/restart replaces it with a fresh instance from its original
    # image, which has neither python3.11 nor setsid installed. Rather than
    # failing and asking you to install them by hand every time, install
    # them automatically here via zypper (confirmed available on this pod:
    # SLES 15 SP7 with working repos) when missing.
    $pyCheck = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $targetPod, "-c", $ContainerName, "--",
        "sh", "-c", "command -v python3.11 && python3.11 --version")
    if ($pyCheck.ExitCode -ne 0) {
        Warn "python3.11 not found in $targetPod -- installing via zypper (fresh pod instances don't have it pre-installed)..."
        $pyInstall = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $targetPod, "-c", $ContainerName, "--",
            "sh", "-c", "zypper --non-interactive install python311 python311-pip 2>&1 | tail -30")
        if ($pyInstall.ExitCode -ne 0) {
            Die "zypper install python311 python311-pip failed:`n$($pyInstall.Output)`nInstall it manually or point -ContainerName/-PodName at a different pod."
        }
        $pyCheck = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $targetPod, "-c", $ContainerName, "--",
            "sh", "-c", "command -v python3.11 && python3.11 --version")
        if ($pyCheck.ExitCode -ne 0) { Die "python3.11 still not found after zypper install. Output:`n$($pyInstall.Output)" }
        Ok "python3.11 installed: $($pyCheck.Output.Trim())"
    }
    $setsidCheck = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $targetPod, "-c", $ContainerName, "--",
        "sh", "-c", "command -v setsid")
    if ($setsidCheck.ExitCode -ne 0) {
        Warn "setsid not found in $targetPod -- installing util-linux via zypper..."
        $utilInstall = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $targetPod, "-c", $ContainerName, "--",
            "sh", "-c", "zypper --non-interactive install util-linux 2>&1 | tail -30")
        if ($utilInstall.ExitCode -ne 0) { Die "zypper install util-linux failed:`n$($utilInstall.Output)" }
        $setsidCheck = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $targetPod, "-c", $ContainerName, "--",
            "sh", "-c", "command -v setsid")
        if ($setsidCheck.ExitCode -ne 0) { Die "setsid still not found after installing util-linux." }
        Ok "setsid installed"
    }

    # --------------------------------------------------------------- PHASE 2
    Banner "PHASE 2 -- Copy code into ${targetPod}:${RemoteDir}"
    $stage = Join-Path $env:TEMP "raptor_full_setup_ctx"
    if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
    New-Item -ItemType Directory -Path $stage | Out-Null

    Copy-Item (Join-Path $RepoRoot "requirements.txt") (Join-Path $stage "requirements.txt")
    Copy-Item -Recurse (Join-Path $RepoRoot "gate") (Join-Path $stage "gate")
    Copy-Item -Recurse (Join-Path $RepoRoot "data") (Join-Path $stage "data")
    Copy-Item -Recurse (Join-Path $RepoRoot "scripts") (Join-Path $stage "scripts")
    Get-ChildItem -Path $stage -Recurse -Directory -Filter "__pycache__" |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Get-ChildItem -Path $stage -Recurse -Directory -Filter ".pytest_cache" |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Ok "Staged subset (gate/, data/, scripts/, requirements.txt) at $stage"

    Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $targetPod, "-c", $ContainerName, "--",
        "mkdir", "-p", $RemoteDir) | Out-Null

    Push-Location $stage
    try {
        foreach ($item in @("requirements.txt", "gate", "data", "scripts")) {
            $cp = Invoke-Kubectl -KubectlArgs @("cp", ".\$item", "$Namespace/${targetPod}:$RemoteDir/$item", "-c", $ContainerName)
            if ($cp.ExitCode -ne 0) { Die "kubectl cp failed for $item : $($cp.Output)" }
        }
    } finally { Pop-Location }
    Ok "Code copied to $RemoteDir"

    # --------------------------------------------------------------- PHASE 3
    Banner "PHASE 3 -- Install Python dependencies"
    $pipInstall = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $targetPod, "-c", $ContainerName, "--",
        "sh", "-c", "cd $RemoteDir && python3.11 -m pip install --quiet --root-user-action=ignore -r requirements.txt")
    if ($pipInstall.ExitCode -ne 0) { Die "pip install failed:`n$($pipInstall.Output)" }
    Ok "Dependencies installed"

    # --------------------------------------------------------------- PHASE 4
    Banner "PHASE 4 -- Train the Isolation Forest model"
    $liveCheck = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $targetPod, "-c", $ContainerName, "--",
        "sh", "-c", "test -f $RemoteDir/data/v1_baseline_live.csv && echo yes || echo no")
    $baseline = if ($liveCheck.Output.Trim() -eq "yes") { "data/v1_baseline_live.csv" } else { "data/v1_baseline.csv" }
    Info "Training from: $baseline"
    $train = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $targetPod, "-c", $ContainerName, "--",
        "sh", "-c", "cd $RemoteDir && python3.11 -m gate.train --baseline $baseline --out model/model.joblib")
    if ($train.ExitCode -ne 0) { Die "model training failed:`n$($train.Output)" }
    Ok "Model trained -> $RemoteDir/model/model.joblib"

    # --------------------------------------------------------------- PHASE 5
    Banner "PHASE 5 -- Launch the serving app"
    $redisPw = Invoke-Kubectl -KubectlArgs @("get", "secret", "redis", "-n", $Namespace, "-o", "jsonpath={.data.redis-password}")
    $redisPwPlain = ""
    if ($redisPw.ExitCode -eq 0 -and $redisPw.Output) {
        $redisPwPlain = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($redisPw.Output.Trim()))
        Ok "REDIS_PASSWORD retrieved from the 'redis' Secret (not printed)"
    } else {
        Warn "Could not read the 'redis' Secret -- launching without Redis (feature store will be memory-only)"
    }

    Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $targetPod, "-c", $ContainerName, "--",
        "sh", "-c", "pkill -f '[u]vicorn gate.serving.app' 2>/dev/null; sleep 1; true") | Out-Null

    $startCmd = "cd $RemoteDir; " +
        "export GATE_MODEL_PATH=$RemoteDir/model/model.joblib; " +
        "export REDIS_HOST=redis-master.$Namespace.svc.cluster.local; " +
        "export REDIS_PORT=6379; " +
        "export REDIS_PASSWORD='$redisPwPlain'; " +
        "setsid python3.11 -m uvicorn gate.serving.app:app --host 0.0.0.0 --port $Port " +
        "> $RemoteDir/server.log 2>&1 < /dev/null & " +
        "sleep 2; echo started"
    $start = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $targetPod, "-c", $ContainerName, "--", "sh", "-c", $startCmd)
    if ($start.ExitCode -ne 0) { Die "failed to launch the server:`n$($start.Output)" }
    Ok "Server launch issued"

    # --------------------------------------------------------------- PHASE 6
    Banner "PHASE 6 -- Verify"
    $healthy = $false
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 2
        $h = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $targetPod, "-c", $ContainerName, "--",
            "sh", "-c", "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$Port/health")
        if ($h.Output.Trim() -eq "200") { $healthy = $true; break }
    }
    if (-not $healthy) {
        $log = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $targetPod, "-c", $ContainerName, "--",
            "sh", "-c", "tail -n 40 $RemoteDir/server.log")
        Die "server did not become healthy in time. Last log lines:`n$($log.Output)"
    }
    Ok "Server is healthy on port $Port inside $targetPod"

    $script:DeployedPodName = $targetPod
}

# ==================================================================== PHASE 7
function Invoke-BaselineCapturePhase {
    if ($CaptureBaselineSamples -le 0) { return }
    if (-not $script:DeployedPodName) { Warn "Skipping baseline capture -- no deployed pod from Phase 2-6."; return }
    Banner "PHASE 7 -- Capture a real baseline ($CaptureBaselineSamples samples @ ${CaptureBaselineInterval}s)"

    $pod = $script:DeployedPodName
    $redisPw = Invoke-Kubectl -KubectlArgs @("get", "secret", "redis", "-n", $Namespace, "-o", "jsonpath={.data.redis-password}")
    $redisPwPlain = ""
    if ($redisPw.ExitCode -eq 0 -and $redisPw.Output) {
        $redisPwPlain = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($redisPw.Output.Trim()))
    }

    $cmd = "cd $RemoteDir; export REDIS_PASSWORD='$redisPwPlain'; " +
        "python3.11 scripts/capture_baseline.py --collectors $CollectorsConfig " +
        "--samples $CaptureBaselineSamples --interval $CaptureBaselineInterval " +
        "--out data/v1_baseline_live.csv"
    Info "Running capture_baseline.py inside the pod (this takes ~$($CaptureBaselineSamples * $CaptureBaselineInterval)s)..."
    $cap = Invoke-Kubectl -KubectlArgs @("exec", "-n", $Namespace, $pod, "-c", $ContainerName, "--", "sh", "-c", $cmd)
    if ($cap.ExitCode -ne 0) { Warn "Baseline capture failed:`n$($cap.Output)"; return }
    Write-Host $cap.Output
    Ok "Live baseline captured inside the pod"

    $localOut = Join-Path $RepoRoot "data\v1_baseline_live.csv"
    $pull = Invoke-Kubectl -KubectlArgs @("cp", "$Namespace/${pod}:$RemoteDir/data/v1_baseline_live.csv", $localOut, "-c", $ContainerName)
    if ($pull.ExitCode -eq 0) {
        Ok "Pulled baseline back to $localOut"
        Info "Re-run Phase 4+5 (or the whole script with -SkipInfra) to retrain/relaunch against it."
    } else {
        Warn "Could not pull the baseline file back locally: $($pull.Output)"
    }
}

# ==================================================================== PHASE 8
function Invoke-ProductionImagePhase {
    if (-not $BuildProductionImage) { return }
    Banner "PHASE 8 -- Build & push the standalone production image (Kaniko)"
    Warn "This PUSHES an image to: $RegistryImage"
    Warn "This is a separate, higher-risk, outbound action (registry write)."
    if (-not (Confirm-Step "Proceed with the Kaniko build + push to $RegistryImage ?")) {
        Warn "Skipped Phase 8."; return
    }

    Info "Applying build-context PVC + staging pod..."
    Invoke-Kubectl -KubectlArgs @("apply", "-f", (Join-Path $RepoRoot "deploy\k8s\kaniko-build-pvc.yaml")) | Out-Null
    Invoke-Kubectl -KubectlArgs @("delete", "pod", "raptor-build-stage", "-n", $Namespace, "--ignore-not-found") | Out-Null
    Invoke-Kubectl -KubectlArgs @("apply", "-f", (Join-Path $RepoRoot "deploy\k8s\kaniko-staging-pod.yaml")) | Out-Null
    $wait = Invoke-Kubectl -KubectlArgs @("wait", "--for=condition=ready", "pod", "raptor-build-stage", "-n", $Namespace, "--timeout=120s")
    if ($wait.ExitCode -ne 0) { Die "staging pod did not become ready: $($wait.Output)" }

    Info "Copying build context (Dockerfile, requirements.txt, gate/, data/) into the staging pod..."
    Push-Location $RepoRoot
    try {
        foreach ($item in @("Dockerfile", "requirements.txt", "gate", "data")) {
            $cp = Invoke-Kubectl -KubectlArgs @("cp", ".\$item", "$Namespace/raptor-build-stage:/workspace/$item")
            if ($cp.ExitCode -ne 0) { Die "kubectl cp failed for $item : $($cp.Output)" }
        }
    } finally { Pop-Location }
    Ok "Build context staged"

    Invoke-Kubectl -KubectlArgs @("delete", "pod", "raptor-build-stage", "-n", $Namespace, "--ignore-not-found") | Out-Null

    Info "Submitting the Kaniko build job (destination: $RegistryImage)..."
    Invoke-Kubectl -KubectlArgs @("delete", "job", "raptor-gate-kaniko-build", "-n", $Namespace, "--ignore-not-found") | Out-Null
    # Plain literal substitution (not -replace/regex) so nothing in
    # $RegistryImage needs escaping on either the pattern or replacement side.
    $jobYaml = Get-Content (Join-Path $RepoRoot "deploy\k8s\kaniko-build-job.yaml") -Raw
    $jobYaml = $jobYaml.Replace("armdocker.rnd.ericsson.se/proj-cenx/raptor-gate:1.0", $RegistryImage)
    $tmpJob = Join-Path $env:TEMP "raptor-gate-kaniko-build.yaml"
    Set-Content -Path $tmpJob -Value $jobYaml
    $apply = Invoke-Kubectl -KubectlArgs @("apply", "-f", $tmpJob)
    if ($apply.ExitCode -ne 0) { Die "failed to apply the Kaniko job: $($apply.Output)" }

    Info "Waiting for the build to complete (this can take a few minutes)..."
    $jobWait = Invoke-Kubectl -KubectlArgs @("wait", "--for=condition=complete", "job/raptor-gate-kaniko-build", "-n", $Namespace, "--timeout=600s")
    if ($jobWait.ExitCode -ne 0) {
        $logs = Invoke-Kubectl -KubectlArgs @("logs", "-n", $Namespace, "job/raptor-gate-kaniko-build", "--tail=60")
        Die "Kaniko build did not complete: $($jobWait.Output)`n--- logs ---`n$($logs.Output)"
    }
    Ok "Image built and pushed to $RegistryImage"

    Info "Applying the standalone Deployment/Service and pointing it at the new image..."
    Invoke-Kubectl -KubectlArgs @("apply", "-f", (Join-Path $RepoRoot "deploy\k8s\deployment.yaml")) | Out-Null
    Invoke-Kubectl -KubectlArgs @("apply", "-f", (Join-Path $RepoRoot "deploy\k8s\service.yaml")) | Out-Null
    Invoke-Kubectl -KubectlArgs @("set", "image", "deployment/raptor-gate", "raptor-gate=$RegistryImage", "-n", $Namespace) | Out-Null
    $rollout = Invoke-Kubectl -KubectlArgs @("rollout", "status", "deployment/raptor-gate", "-n", $Namespace, "--timeout=180s")
    if ($rollout.ExitCode -ne 0) { Warn "Rollout did not confirm in time: $($rollout.Output)" }
    else { Ok "Deployment/raptor-gate is live, serving via Service/raptor-gate" }
}

# ======================================================================= MAIN
Banner "RAPTOR DEPLOYMENT GATE -- FULL ENVIRONMENT SETUP (namespace: $Namespace)"
Invoke-Preflight

if ($DiagnoseOnly) {
    Ok "Diagnostics complete (-DiagnoseOnly set). No changes made."
    exit 0
}

Invoke-InfraPhase
Invoke-PodDeployPhases
Invoke-BaselineCapturePhase
Invoke-ProductionImagePhase

Banner "DONE"
if ($script:DeployedPodName) {
    Write-Host ""
    Write-Host "Reach the gate from your machine with a port-forward:"
    Write-Host "  kubectl --kubeconfig `"$KubeConfig`" port-forward -n $Namespace $($script:DeployedPodName) ${Port}:${Port}"
    Write-Host ""
    Write-Host "Then:"
    Write-Host "  curl http://127.0.0.1:$Port/health"
    $promoteJson = '{"component":"v2","sample":{"cpu_usage":46,"memory_usage":53,"pod_restarts":0,"http_error_rate":0.2,"p99_latency":90,"availability":99.8,"redis_latency":3,"endpoint_latency":88,"packet_loss":0.15}}'
    $dq = [char]34
    Write-Host ("  curl -X POST http://127.0.0.1:$Port/score -H " + $dq + "Content-Type: application/json" + $dq + " -d '$promoteJson'")
    Write-Host ""
    Write-Host "Server log inside the pod: $RemoteDir/server.log"
    Write-Host "Re-run this script (without -InstallInfra) any time the pod restarts -- it is NOT persistent."
}
if (-not $BuildProductionImage) {
    Write-Host ""
    Write-Host "Standalone production image/Deployment was NOT built (opt-in only)."
    Write-Host "Re-run with -BuildProductionImage -RegistryImage <registry/path:tag> when ready."
}
