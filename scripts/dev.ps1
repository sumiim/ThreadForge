param(
    [string]$SshHost = "threadforge-server",
    [int]$LocalApiPort = 18000,
    [int]$FrontendPort = 5173,
    [switch]$NoTunnel,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$clientRoot = Join-Path $repoRoot "client"

function Test-LocalPort([int]$Port) {
    return $null -ne (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Wait-Http([string]$Uri, [int]$TimeoutSeconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 300
        }
    }
    throw "Timed out waiting for $Uri"
}

function Use-Node24 {
    $node = Get-Command node -ErrorAction SilentlyContinue
    $major = 0
    if ($null -ne $node) {
        $major = [int]((& $node.Source --version).TrimStart("v").Split(".")[0])
    }
    if ($major -lt 24) {
        $bundledBin = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin"
        if (Test-Path (Join-Path $bundledBin "node.exe")) {
            $env:PATH = "$bundledBin;$env:PATH"
            $node = Get-Command node -ErrorAction Stop
            $major = [int]((& $node.Source --version).TrimStart("v").Split(".")[0])
        }
    }
    if ($major -lt 24) {
        throw "ThreadForge frontend requires Node.js 24; found major version $major"
    }
}

if (-not $NoTunnel -and -not (Test-LocalPort $LocalApiPort)) {
    $ssh = Get-Command ssh -ErrorAction Stop
    $forward = "${LocalApiPort}:127.0.0.1:8000"
    Start-Process -FilePath $ssh.Source -WindowStyle Hidden -ArgumentList @(
        "-N",
        "-L", $forward,
        "-o", "ExitOnForwardFailure=yes",
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        $SshHost
    )
    Wait-Http "http://127.0.0.1:${LocalApiPort}/health/ready" 20
}

if (-not (Test-LocalPort $FrontendPort)) {
    Use-Node24
    $pnpm = Get-Command pnpm.cmd -ErrorAction SilentlyContinue
    if ($null -eq $pnpm) {
        $pnpm = Get-Command pnpm -ErrorAction Stop
    }
    Start-Process -FilePath $pnpm.Source -WorkingDirectory $clientRoot -WindowStyle Hidden -ArgumentList @(
        "dev", "--", "--host", "127.0.0.1", "--port", $FrontendPort
    )
}

$frontendUrl = "http://127.0.0.1:${FrontendPort}/"
Wait-Http $frontendUrl 30
Write-Host "ThreadForge frontend: $frontendUrl"
if (-not $NoTunnel) {
    Write-Host "ThreadForge API tunnel: http://127.0.0.1:${LocalApiPort}"
}
if (-not $NoBrowser) {
    Start-Process $frontendUrl
}
