# Codex session ritual

Save all running interactive Codex CLI sessions from Ubuntu WSL, then restore
them as tabs in one Windows Terminal window with their saved directories,
titles, exact session IDs, and YOLO mode when it was active.

Fixed requirements:

- Windows PowerShell 5.1 or PowerShell 7 with Python 3.13 and `py.exe`
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

The installer registers the commands in both the PowerShell 7 and Windows
PowerShell 5.1 user profiles, regardless of which shell runs the installer.
It changes the per-user execution policy from the Windows default `Restricted`
to `RemoteSigned` where required, so those profiles load on future shell starts.
It does not override stricter explicit or organization policies.
Clone into a Windows directory: `RemoteSigned` correctly blocks an unsigned
installer launched directly from a `\\wsl.localhost\...` path.

The defaults can be changed without editing the program:

```powershell
$env:CODEX_SESSIONS_DISTRO = "Ubuntu-24.04"
$env:CODEX_SESSIONS_WSL_PYTHON = "/usr/bin/python3"
```

```powershell
saveSessions                # save every running interactive WSL Codex session
saveSessions --kill         # save, then send SIGTERM to the found Codex processes
restoreSessions             # reopen tabs, then autosave every minute until Ctrl+C
findSessions "topic words"        # suggest matching sessions from conversation history
saveSessions --dry-run      # inspect without replacing the save
restoreSessions --dry-run   # inspect without opening windows
```

Plain `saveSessions` never stops anything. `--kill` saves first, then sends
`SIGTERM`; it may interrupt an active turn.

`restoreSessions` stays open after launching and saves the exact running session
list every minute, so opened and closed sessions are tracked. If every session
is closed, the last non-empty snapshot is kept. Press `Ctrl+C` to stop autosave
without closing sessions.

Restore starts one tab at a time, waits until each exact session ID is running,
then gives it three seconds to settle before it starts the next tab.

After Codex exits, its WSL tab stays open. The exact resume command is saved in
Bash history, so Up then Enter restarts the session in the same mode.

All three commands print numbered, colored session cards. `findSessions`
searches user and assistant messages only, shows a matching excerpt, and lets
you open a result by entering its number. It does not require a prior
`saveSessions`.
