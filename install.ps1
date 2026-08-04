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

$profileDirectory = Split-Path -Parent $PROFILE
New-Item -ItemType Directory -Force $profileDirectory | Out-Null
if (-not (Test-Path $PROFILE)) { New-Item -ItemType File $PROFILE | Out-Null }
if (-not (Select-String -Path $PROFILE -SimpleMatch $profileImport -Quiet)) {
    Add-Content -Path $PROFILE -Value "`n$profileImport"
}

& $venvPython (Join-Path $installDirectory "codex_sessions.py") self-test
Write-Host "Installed. Reload with: . `$PROFILE"
