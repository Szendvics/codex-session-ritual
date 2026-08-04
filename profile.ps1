$CodexSessionsPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$CodexSessionsScript = Join-Path $PSScriptRoot "codex_sessions.py"

function saveSessions { & $CodexSessionsPython -X utf8 $CodexSessionsScript save @args }
function restoreSessions { & $CodexSessionsPython -X utf8 $CodexSessionsScript restore @args }
function findSessions { & $CodexSessionsPython -X utf8 $CodexSessionsScript find @args }
