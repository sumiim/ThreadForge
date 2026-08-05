param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')]
    [string]$Version,

    [string]$OutputFile = "threadforge-worker-windows-x86_64.exe"
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$outputPath = [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot $OutputFile))

Push-Location $repositoryRoot
try {
    python -m PyInstaller `
        --clean `
        --noconfirm `
        --onefile `
        --name threadforge-worker `
        --paths local-worker/src `
        --paths pico-legacy-runtime `
        --collect-submodules threadforge_worker `
        --hidden-import tkinter `
        --hidden-import tkinter.filedialog `
        --hidden-import win32api `
        --hidden-import win32con `
        --hidden-import win32crypt `
        --hidden-import win32security `
        --hidden-import ntsecuritycon `
        local-worker/src/threadforge_worker/__main__.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }

    python -m PyInstaller `
        --clean `
        --noconfirm `
        --onefile `
        --noconsole `
        --name threadforge-worker-service `
        --paths local-worker/src `
        --paths pico-legacy-runtime `
        --collect-submodules threadforge_worker `
        --hidden-import tkinter `
        --hidden-import tkinter.filedialog `
        --hidden-import win32api `
        --hidden-import win32con `
        --hidden-import win32crypt `
        --hidden-import win32security `
        --hidden-import ntsecuritycon `
        local-worker/src/threadforge_worker/__main__.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller service build failed with exit code $LASTEXITCODE"
    }

    $makensis = @(
        (Join-Path ${env:ProgramFiles(x86)} "NSIS\makensis.exe"),
        (Join-Path $env:ProgramFiles "NSIS\makensis.exe")
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $makensis) {
        $command = Get-Command makensis.exe -ErrorAction SilentlyContinue
        if ($command) { $makensis = $command.Source }
    }
    if (-not $makensis) {
        throw "NSIS compiler is unavailable"
    }

    $workerExe = (Resolve-Path "dist/threadforge-worker.exe").Path
    $serviceExe = (Resolve-Path "dist/threadforge-worker-service.exe").Path
    & $makensis `
        "/DWORKER_EXE=$workerExe" `
        "/DWORKER_SERVICE_EXE=$serviceExe" `
        "/DOUTPUT_FILE=$outputPath" `
        "/DWORKER_VERSION=$Version" `
        scripts/worker-installer.nsi
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $outputPath)) {
        throw "Failed to build Worker installer"
    }
    Write-Output $outputPath
}
finally {
    Pop-Location
}
