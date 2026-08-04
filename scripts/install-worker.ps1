[CmdletBinding()]
param(
    [string]$Python = "py",
    [string]$InstallRoot = "$env:LOCALAPPDATA\ThreadForge\WorkerRuntime"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$venv = Join-Path $runtimeRoot "venv"
$launcherDir = Join-Path $env:LOCALAPPDATA "ThreadForge\bin"
$launcher = Join-Path $launcherDir "threadforge-worker.cmd"

New-Item -ItemType Directory -Force -Path $runtimeRoot, $launcherDir | Out-Null

if ($Python -eq "py") {
    & py -3.12 -m venv $venv
} else {
    & $Python -m venv $venv
}
if ($LASTEXITCODE -ne 0) { throw "Failed to create the Worker virtual environment" }

$venvPython = Join-Path $venv "Scripts\python.exe"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install (Join-Path $repoRoot "pico-legacy-runtime") (Join-Path $repoRoot "local-worker")
if ($LASTEXITCODE -ne 0) { throw "Failed to install ThreadForge Worker" }

$launcherContent = "@echo off`r`n`"$venvPython`" -m threadforge_worker %*`r`n"
[System.IO.File]::WriteAllText($launcher, $launcherContent, [System.Text.UTF8Encoding]::new($false))

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathParts = @($userPath -split ";" | Where-Object { $_ })
if ($pathParts -notcontains $launcherDir) {
    [Environment]::SetEnvironmentVariable("Path", (($pathParts + $launcherDir) -join ";"), "User")
}

Write-Host "Worker installed: $launcher"
Write-Host "Open a new terminal, then run threadforge-worker pair / workspace add / run."
