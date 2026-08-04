# Codex session ritual

Save all running interactive Codex CLI sessions from Ubuntu WSL, then restore
each one into its own Windows Terminal window with its saved directory, title,
and exact session ID.

Fixed requirements:

- Windows PowerShell 5.1 with Python 3.13 and `py.exe`
- `wt.exe` and `wsl.exe` on the Windows `PATH`
- a WSL distribution named `Ubuntu`
- Ubuntu system Python at `/usr/bin/python3`
- `codex` available from the Ubuntu login shell

Install from Windows PowerShell:

```powershell
git clone git@github.com:Szendvics/codex-session-ritual.git
cd codex-session-ritual
.\install.ps1
. $PROFILE
```

The defaults can be changed without editing the program:

```powershell
$env:CODEX_SESSIONS_DISTRO = "Ubuntu-24.04"
$env:CODEX_SESSIONS_WSL_PYTHON = "/usr/bin/python3"
```

```powershell
saveSessions                # save every running interactive WSL Codex session
saveSessions --kill         # save, then send SIGTERM to the found Codex processes
restoreSessions             # reopen each saved session in its own Terminal window
findSessions "topic words"        # suggest matching sessions from conversation history
saveSessions --dry-run      # inspect without replacing the save
restoreSessions --dry-run   # inspect without opening windows
```

Plain `saveSessions` never stops anything. `--kill` saves first, then sends
`SIGTERM`; it may interrupt an active turn.

All three commands print numbered, colored session cards. `findSessions`
searches user and assistant messages only, shows a matching excerpt, and lets
you open a result by entering its number. It does not require a prior
`saveSessions`.
