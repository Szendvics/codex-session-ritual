$ErrorActionPreference = "Stop"

$installDirectory = Join-Path $env:LOCALAPPDATA "codex-sessions"
$venvPython = Join-Path $installDirectory ".venv\Scripts\python.exe"
$profileImport = '. "$env:LOCALAPPDATA\codex-sessions\profile.ps1"'

New-Item -ItemType Directory -Force $installDirectory | Out-Null
py -3.13 -m venv (Join-Path $installDirectory ".venv")
Copy-Item -Path @(
    "$PSScriptRoot\codex_sessions.py",
    "$PSScriptRoot\profile.ps1",
    "$PSScriptRoot\README.md"
) -Destination $installDirectory -Force

$documents = [Environment]::GetFolderPath("MyDocuments")
$profilePaths = @(
    $PROFILE
    (Join-Path $documents "PowerShell\Microsoft.PowerShell_profile.ps1")
    (Join-Path $documents "WindowsPowerShell\Microsoft.PowerShell_profile.ps1")
) | Select-Object -Unique

foreach ($profilePath in $profilePaths) {
    New-Item -ItemType Directory -Force (Split-Path -Parent $profilePath) | Out-Null
    if (-not (Test-Path $profilePath)) { New-Item -ItemType File $profilePath | Out-Null }
    if (-not (Select-String -Path $profilePath -SimpleMatch $profileImport -Quiet)) {
        Add-Content -Path $profilePath -Value "`n$profileImport"
    }
}

& $venvPython (Join-Path $installDirectory "codex_sessions.py") self-test
Write-Host "Installed for:"
$profilePaths | ForEach-Object { Write-Host "  $_" }
Write-Host "Reload with: . `$PROFILE"
