param(
    [string]$ChocolateyVersion = "3.12.0",
    [string]$InstallerVersion = "3.12",
    [string]$InstallerSha256 = "3BC2B06253A7E4957111BE152AC6A536E0C7478A706E19DA814038DB5D706495"
)

$ErrorActionPreference = "Stop"

function Find-MakeNsis {
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "NSIS\makensis.exe"),
        (Join-Path $env:ProgramFiles "NSIS\makensis.exe")
    )
    $installed = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($installed) {
        return $installed
    }

    $command = Get-Command makensis.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    return $null
}

$makensis = Find-MakeNsis
if ($makensis) {
    Write-Output "NSIS is already available at $makensis"
    exit 0
}

for ($attempt = 1; $attempt -le 3; $attempt++) {
    Write-Output "Installing NSIS $ChocolateyVersion with Chocolatey (attempt $attempt of 3)"
    & choco install nsis --version=$ChocolateyVersion --no-progress -y
    if ($LASTEXITCODE -eq 0) {
        $makensis = Find-MakeNsis
        if ($makensis) {
            Write-Output "NSIS installed at $makensis"
            exit 0
        }
    }

    if ($attempt -lt 3) {
        Start-Sleep -Seconds (10 * $attempt)
    }
}

$installerUrl = "https://sourceforge.net/projects/nsis/files/NSIS%203/$InstallerVersion/nsis-$InstallerVersion-setup.exe/download"
$temporaryDirectory = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [System.IO.Path]::GetTempPath() }
$installerPath = Join-Path $temporaryDirectory "nsis-$InstallerVersion-setup.exe"

try {
    Write-Warning "Chocolatey could not install NSIS; downloading the checksum-pinned official installer"
    & curl.exe `
        --location `
        --fail `
        --show-error `
        --retry 4 `
        --retry-all-errors `
        --connect-timeout 20 `
        --max-time 180 `
        --output $installerPath `
        $installerUrl
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $installerPath)) {
        throw "Failed to download the NSIS installer"
    }

    $actualSha256 = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash
    if ($actualSha256 -ne $InstallerSha256) {
        throw "NSIS installer checksum mismatch: expected $InstallerSha256, got $actualSha256"
    }

    $process = Start-Process -FilePath $installerPath -ArgumentList "/S" -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "NSIS installer exited with code $($process.ExitCode)"
    }

    $makensis = Find-MakeNsis
    if (-not $makensis) {
        throw "NSIS installation completed but makensis.exe is unavailable"
    }
    Write-Output "NSIS installed at $makensis"
}
finally {
    Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue
}
