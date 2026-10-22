# Codex session ritual

Save all running interactive Codex CLI sessions from Ubuntu WSL, then restore
them as tabs in one Windows Terminal window with their saved directories,
titles, exact session IDs, and YOLO mode when it was active.

Fixed requirements:

- Windows PowerShell 5.1 or PowerShell 7 with Python 3.13 and `py.exe`
- `wt.exe` and `wsl.exe` on the Windows `PATH`
- a WSL distribution named `Ubuntu`
- Ubuntu system Python 3.10+ at `/usr/bin/python3`, WSL2 Linux 5.3+
- `codex` available from the Ubuntu login shell

Install from Windows PowerShell. If `Get-ExecutionPolicy` reports `Restricted`,
first enable local scripts with `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
Do not override an organization-managed policy; ask your administrator instead.

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
restoreSessions --reopen    # also open a new tab for sessions already running
findSessions "topic words"        # suggest matching sessions from conversation history
saveSessions --dry-run      # inspect without replacing the save
restoreSessions --dry-run   # inspect without opening windows
```

Plain `saveSessions` never stops anything. `--kill` saves first, then sends
`SIGTERM` to every matching interactive process, including duplicate tabs.
It also stops existing autosave watchers so they cannot replace the saved list
with a partial shutdown snapshot. Start `restoreSessions` to resume autosaving.
The shared Codex daemon is left running; this is not a stop-all-background-work command.

`restoreSessions` skips sessions that already have an interactive terminal unless
`--reopen` is supplied. New tabs share one window and launch sequentially. Autosave
starts only after all saved sessions are detected; a startup failure keeps the full
snapshot and reports an error. A newer restore replaces the previous watcher.

`restoreSessions` stays open after launching and saves the running session
list every minute, so opened and closed sessions are tracked. If every session
is closed, the last non-empty snapshot is kept. Press `Ctrl+C` to stop autosave
without closing sessions.

Discovery also checks recent Codex resume events when reconnecting does not
create a shell snapshot. Identified processes are cached locally in
`~/.codex/session-ritual-processes.json`, including during discovery dry-runs;
the boot ID and process start time prevent reusing stale PIDs. If an interactive
process cannot be identified, saving stops and keeps the previous snapshot.
Autosave retries transient discovery and metadata errors on the next tick.
Resuming with an explicit session ID avoids this timestamp-based fallback at startup.
Old timestamp-derived cache entries are rechecked on upgrade. Ambiguous startup
records are rejected rather than assigned to whichever process is scanned first.

Known daemon limitation: `/resume` or `/new` inside an existing Codex process can
leave the launch/cached identity stale. The shared daemon does not expose a reliable
current-session-to-terminal mapping. To keep saving accurate, exit Codex and run
`codex resume <session-id>` (or start a new Codex process) instead of switching
inside it. The ritual deliberately does not disable or modify the daemon.

After Codex exits, its WSL tab stays open. The exact resume command is saved in
Bash history, so Up then Enter restarts the session in the same mode.

All three commands print numbered, colored session cards. `findSessions`
searches user and assistant messages in both older and current transcript
formats, shows a matching excerpt, and lets you open a result by entering its
number. The 10 most recently active matching sessions appear first, with local
date and time based on the transcript's last modification time. Search stops
once those 10 matches are found; rarer queries still scan the full history.
It does not require a prior `saveSessions`.
History searches have a separate five-minute timeout; other WSL operations time
out after 30 seconds.

Validation: `python3 -B codex_sessions.py self-test` (or `py -3.13` on Windows).
The installer runs this check before replacing the installed scripts and again
after copying them. Keep changes in this repository and rerun the installer;
editing the installed copy creates changes that a reinstall would overwrite.
