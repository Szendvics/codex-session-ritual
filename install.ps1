$ErrorActionPreference = "Stop"

$installDirectory = Join-Path $env:LOCALAPPDATA "codex-sessions"
$venvPython = Join-Path $installDirectory ".venv\Scripts\python.exe"
$profileImport = '. "$env:LOCALAPPDATA\codex-sessions\profile.ps1"'

$powerShellExecutables = @(
    (Get-Command powershell.exe -ErrorAction Stop).Source
    (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
) | Where-Object { $_ } | Select-Object -Unique

foreach ($powerShellExecutable in $powerShellExecutables) {
    & $powerShellExecutable -NoProfile -NonInteractive -Command @'
$policy = Get-ExecutionPolicy
if ($policy -eq "Restricted") {
    Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force
    $policy = Get-ExecutionPolicy
}
if (@("RemoteSigned", "Unrestricted", "Bypass") -notcontains $policy) {
    Write-Error "PowerShell profiles are blocked by the effective $policy execution policy."
    exit 1
}
'@
    if ($LASTEXITCODE) { throw "Could not enable profiles for $powerShellExecutable" }
}

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
foreach ($powerShellExecutable in $powerShellExecutables) {
    & $powerShellExecutable -NonInteractive -Command `
        "Get-Command saveSessions, restoreSessions, findSessions -ErrorAction Stop | Out-Null"
    if ($LASTEXITCODE) { throw "Profile check failed for $powerShellExecutable" }
}
Write-Host "Installed for:"
$profilePaths | ForEach-Object { Write-Host "  $_" }
Write-Host "Execution policy: CurrentUser RemoteSigned where required"
Write-Host "Reload with: . `$PROFILE"
