# ThreadForge one-click deploy script (Windows PowerShell)
# Usage:
#   .\deploy.ps1            # build and start (attached)
#   .\deploy.ps1 up         # build and start (detached)
#   .\deploy.ps1 stop       # stop services
#   .\deploy.ps1 logs       # follow logs
#   .\deploy.ps1 down       # stop and remove containers
#   .\deploy.ps1 health     # health check
#   .\deploy.ps1 ps         # show container status
param(
    [string]$Action = "up"
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

function Assert-Compose {
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) {
        Write-Error "Docker not found. Install Docker Desktop and ensure 'docker' is on PATH."
        exit 1
    }
    docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Docker Compose v2 is unavailable. Please upgrade Docker."
        exit 1
    }
}

function Ensure-EnvFile {
    # compose provides defaults for every variable; this writes a minimal .env
    # template so users can override GitHub OAuth / model / sandbox as needed.
    $envPath = Join-Path $Root ".env"
    if (Test-Path $envPath) {
        Write-Host ".env exists, keeping it: $envPath"
        return
    }
    $lines = @(
        '# ThreadForge compose runtime config (optional; defaults come from compose)',
        '# PICO_OPENAI_API_BASE=https://api.openai.com/v1',
        '# PICO_OPENAI_API_KEY=',
        '# PICO_OPENAI_MODEL=gpt-5.4',
        '# THREADFORGE_WEB_ORIGIN=http://127.0.0.1:5173',
        '# THREADFORGE_DESKTOP_ORIGIN_ENABLED=false',
        '# THREADFORGE_IDENTITY_MODE=single_owner_instance',
        '# THREADFORGE_GITHUB_OAUTH_CLIENT_ID=',
        '# THREADFORGE_GITHUB_OAUTH_CLIENT_SECRET=',
        '# THREADFORGE_GITHUB_OAUTH_CALLBACK_URL=http://127.0.0.1:18000/api/v1/auth/github/callback',
        '# THREADFORGE_GITHUB_OAUTH_RETURN_URL=http://127.0.0.1:5173/',
        '# THREADFORGE_GITHUB_OWNER_LOGIN=',
        '# THREADFORGE_GITHUB_ACCESS_POLICY=allowlist',
        '# THREADFORGE_GITHUB_ALLOWED_LOGINS=[]',
        '# THREADFORGE_SANDBOX_ENABLED=false',
        '# THREADFORGE_SANDBOX_BACKEND=os',
        '# THREADFORGE_WORKER_RELEASE_DIR=./worker-releases'
    )
    Set-Content -Path $envPath -Value $lines -Encoding UTF8
    Write-Host "Wrote default .env ($envPath). Edit it to enable GitHub OAuth / sandbox, then re-run up."
}

function Show-AccessInfo {
    Write-Host "  api  (control plane): http://127.0.0.1:8000"
    Write-Host "  web  (frontend)     : http://127.0.0.1:5173"
    Write-Host "  health check       : .\deploy.ps1 health"
    Write-Host "  logs               : .\deploy.ps1 logs"
    Write-Host "  stop               : .\deploy.ps1 stop"
    Write-Host "  status             : .\deploy.ps1 ps"
}

function Invoke-Up {
    Ensure-EnvFile
    Write-Host "Building the sandbox image (for the optional 'docker' sandbox backend)..."
    # compose does not declare the sandbox runtime image; it is referenced by
    # DockerSandboxBackend at runtime. Build it so the 'docker' backend works.
    docker build -t threadforge-sandbox:latest ./sandbox-workers
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "sandbox image build failed (non-fatal; 'os' backend needs no image)."
    }
    Write-Host "Building and starting ThreadForge (api + web)..."
    docker compose up -d --build
    if ($LASTEXITCODE -ne 0) {
        Write-Error "compose up failed. Check the output above (image build failure / port in use)."
        exit 1
    }
    Write-Host ""
    Write-Host "Started."
    Show-AccessInfo
}

function Invoke-Health {
    $url = "http://127.0.0.1:8000/api/v1/config"
    try {
        $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 10
        Write-Host ("health OK: HTTP {0} {1}" -f $resp.StatusCode, $url)
    }
    catch {
        Write-Warning ("health check failed: {0} - {1}" -f $url, $_.Exception.Message)
        Write-Warning "Possibly still starting; retry later or run: .\deploy.ps1 logs"
    }
}

Assert-Compose

switch ($Action) {
    "up"    { Invoke-Up }
    "health"{ Invoke-Health }
    "ps"    { docker compose ps; exit $LASTEXITCODE }
    "logs"  { docker compose logs -f; exit $LASTEXITCODE }
    "stop"  { docker compose stop; exit $LASTEXITCODE }
    "down"  { docker compose down; exit $LASTEXITCODE }
    default {
        Write-Host "Unknown action: $Action"
        Write-Host "Available: up | stop | logs | down | health | ps"
        exit 1
    }
}
