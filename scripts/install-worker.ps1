[CmdletBinding()]
param(
    [string]$Python = "py",
    [string]$InstallRoot = "$env:LOCALAPPDATA\ThreadForge\WorkerRuntime"
)

$ErrorActionPreference = "Stop"
$runtimeRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$venv = Join-Path $runtimeRoot "venv"
$launcherDir = Join-Path $env:LOCALAPPDATA "ThreadForge\bin"
$launcher = Join-Path $launcherDir "threadforge-worker.cmd"
$serviceLauncher = Join-Path $launcherDir "threadforge-worker-service.cmd"

New-Item -ItemType Directory -Force -Path $runtimeRoot, $launcherDir | Out-Null

if ($Python -eq "py") {
    & py -3.12 -m venv $venv
} else {
    & $Python -m venv $venv
}
if ($LASTEXITCODE -ne 0) { throw "Failed to create the Worker virtual environment" }

$venvPython = Join-Path $venv "Scripts\python.exe"
$venvPythonw = Join-Path $venv "Scripts\pythonw.exe"
$bundlePackages = Join-Path $PSScriptRoot "packages"
if (Test-Path -LiteralPath $bundlePackages -PathType Container) {
    $picoWheel = Get-ChildItem -LiteralPath $bundlePackages -Filter "pico-*.whl" | Select-Object -First 1
    $workerWheel = Get-ChildItem -LiteralPath $bundlePackages -Filter "threadforge_worker-*.whl" | Select-Object -First 1
    if (-not $picoWheel -or -not $workerWheel) {
        throw "The signed Worker bundle is incomplete"
    }
    & $venvPython -m pip install --no-index --find-links $bundlePackages $picoWheel.FullName $workerWheel.FullName
} else {
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install (Join-Path $repoRoot "pico-legacy-runtime") (Join-Path $repoRoot "local-worker")
}
if ($LASTEXITCODE -ne 0) { throw "Failed to install ThreadForge Worker" }

$launcherContent = "@echo off`r`n`"$venvPython`" -m threadforge_worker %*`r`n"
[System.IO.File]::WriteAllText($launcher, $launcherContent, [System.Text.UTF8Encoding]::new($false))
$serviceLauncherContent = "@echo off`r`nstart `"`" /b `"$venvPythonw`" -m threadforge_worker service`r`n"
[System.IO.File]::WriteAllText($serviceLauncher, $serviceLauncherContent, [System.Text.UTF8Encoding]::new($false))

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathParts = @($userPath -split ";" | Where-Object { $_ })
if ($pathParts -notcontains $launcherDir) {
    [Environment]::SetEnvironmentVariable("Path", (($pathParts + $launcherDir) -join ";"), "User")
}

$startupDir = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDir "ThreadForge Worker.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $serviceLauncher
$shortcut.WorkingDirectory = $runtimeRoot
$shortcut.Save()

$protocolRoot = "HKCU:\Software\Classes\threadforge"
New-Item -Force -Path $protocolRoot | Out-Null
Set-Item -Path $protocolRoot -Value "URL:ThreadForge Worker"
New-ItemProperty -Force -Path $protocolRoot -Name "URL Protocol" -Value "" | Out-Null
$protocolCommand = Join-Path $protocolRoot "shell\open\command"
New-Item -Force -Path $protocolCommand | Out-Null
Set-Item -Path $protocolCommand -Value "`"$serviceLauncher`" `"%1`""

Start-Process -FilePath $serviceLauncher -WindowStyle Hidden

Write-Host "Worker installed: $launcher"
Write-Host "Pair once with threadforge-worker pair. The per-user service starts automatically after sign-in."
