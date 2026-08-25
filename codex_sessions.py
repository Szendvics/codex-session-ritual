#!/usr/bin/env python3
"""Save running WSL Codex sessions and reopen them in Windows Terminal tabs."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path


SESSION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
TOKENS = re.compile(r"[^\W\d_]+|\d+")
STATE_VERSION = 1
DISTRO = os.environ.get("CODEX_SESSIONS_DISTRO", "Ubuntu")
WSL_PYTHON = os.environ.get("CODEX_SESSIONS_WSL_PYTHON", "/usr/bin/python3")
AUTOSAVE_INTERVAL_SECONDS = 60
WSL_TIMEOUT_SECONDS = 30


def short_title(value: str | None, cwd: str, session_id: str) -> str:
    value = " ".join((value or "").split())
    if not value:
        value = Path(cwd).name or session_id[:8]
    return value if len(value) <= 72 else value[:69] + "..."


def paint(code: str, value: object) -> str:
    enabled = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    return f"\033[{code}m{value}\033[0m" if enabled else str(value)


def print_session_card(
    index: int,
    session: dict[str, object],
    metadata: str,
    context: str | None = None,
) -> None:
    print(f"\n{paint('96', f'[{index}]')} {paint('1', session['title'])}")
    if context:
        width = max(40, min(100, shutil.get_terminal_size().columns - 4))
        excerpt = textwrap.fill(
            f"> {context}",
            width,
            initial_indent="    ",
            subsequent_indent="    ",
            break_long_words=False,
            break_on_hyphens=False,
        )
        print(paint("37", excerpt))
    print(f"    {metadata}")
    print(f"    {paint('2', session['cwd'])}")


def session_from_rollout(path: Path, pid: int | None = None) -> dict[str, object] | None:
    try:
        with path.open(encoding="utf-8") as stream:
            payload = json.loads(next(stream))["payload"]
        session_id = payload["id"]
        cwd = payload["cwd"]
    except (OSError, StopIteration, KeyError, TypeError, json.JSONDecodeError):
        return None
    if (
        payload.get("source") != "cli"
        or not SESSION_ID.fullmatch(session_id)
        or not isinstance(cwd, str)
    ):
        return None
    session: dict[str, object] = {"id": session_id, "cwd": cwd}
    if pid is not None:
        session["pid"] = pid
    return session


def is_yolo(approval_mode: str, sandbox_policy: str) -> bool:
    try:
        sandbox_type = json.loads(sandbox_policy).get("type")
    except (AttributeError, TypeError, json.JSONDecodeError):
        return False
    return approval_mode == "never" and sandbox_type in {"disabled", "danger-full-access"}


def thread_metadata(
    codex_home: Path, session_ids: list[str]
) -> dict[str, tuple[str | None, bool]]:
    if not session_ids:
        return {}
    placeholders = ",".join("?" for _ in session_ids)
    database = codex_home / "state_5.sqlite"
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                f"SELECT id, COALESCE(NULLIF(name, ''), title), approval_mode, sandbox_policy "
                f"FROM threads "
                f"WHERE id IN ({placeholders})",
                session_ids,
            )
        metadata = {}
        for session_id, label, approval_mode, sandbox_policy in rows:
            metadata[session_id] = (label, is_yolo(approval_mode, sandbox_policy))
        return metadata
    except sqlite3.Error:
        return {}


def discover_wsl_sessions() -> list[dict[str, object]]:
    codex_home = Path.home() / ".codex"
    sessions_root = str(codex_home / "sessions") + os.sep
    found: dict[str, dict[str, object]] = {}

    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if Path(os.readlink(process / "exe")).name != "codex":
                continue
            descriptors = (process / "fd").iterdir()
        except (OSError, PermissionError):
            continue

        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if not target.startswith(sessions_root) or not target.endswith(".jsonl"):
                continue
            session = session_from_rollout(Path(target), int(process.name))
            if session:
                found[str(session["id"])] = session

    metadata = thread_metadata(codex_home, list(found))
    for session_id, session in found.items():
        label, session["yolo"] = metadata.get(session_id, (None, False))
        session["title"] = short_title(label, str(session["cwd"]), session_id)
    return sorted(found.values(), key=lambda session: (str(session["title"]).lower(), str(session["id"])))


def windows_command(name: str) -> str:
    command = shutil.which(name)
    if not command:
        raise SystemExit(f"Required Windows command is missing from PATH: {name}")
    return command


def run_wsl_command(
    wsl: str, *arguments: str, **options: object
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [wsl, "-d", DISTRO, "--", *arguments],
            timeout=WSL_TIMEOUT_SECONDS,
            **options,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(f"Timed out waiting for {DISTRO} WSL.") from None


def run_in_wsl(*arguments: str) -> str:
    wsl = windows_command("wsl.exe")
    python_check = run_wsl_command(
        wsl,
        "test",
        "-x",
        WSL_PYTHON,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if python_check.returncode:
        raise SystemExit(f"Required dependency missing: {DISTRO} WSL with {WSL_PYTHON}")
    script = Path(__file__).read_text(encoding="utf-8")
    result = run_wsl_command(
        wsl,
        WSL_PYTHON,
        "-",
        *arguments,
        check=True,
        capture_output=True,
        encoding="utf-8",
        input=script,
    )
    return result.stdout


def discover_from_windows() -> list[dict[str, object]]:
    return json.loads(run_in_wsl("_discover"))


def searchable_message(line: str) -> str | None:
    if '"type":"event_msg"' not in line or not (
        '"type":"user_message"' in line or '"type":"agent_message"' in line
    ):
        return None
    try:
        message = json.loads(line)["payload"]["message"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    return message if isinstance(message, str) else None


def context_excerpt(message: str, terms: list[str]) -> str:
    text = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", " ".join(message.split()))
    folded = text.casefold()
    position = next((folded.find(term) for term in reversed(terms) if term in folded), 0)
    start = max(0, position - 60)
    end = min(len(text), start + 180)
    return ("..." if start else "") + text[start:end] + ("..." if end < len(text) else "")


def search_wsl_sessions(query: str) -> list[dict[str, object]]:
    terms = TOKENS.findall(query.casefold())
    if not terms:
        return []
    matches: list[dict[str, object]] = []

    for path in (Path.home() / ".codex" / "sessions").rglob("*.jsonl"):
        counts = dict.fromkeys(terms, 0)
        best_context = ""
        best_quality = (0, 0)
        session = session_from_rollout(path)
        if not session:
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    message = searchable_message(line)
                    if message is None:
                        continue
                    words = TOKENS.findall(message.casefold())
                    hits = [words.count(term) for term in terms]
                    for term, count in zip(terms, hits):
                        counts[term] += count
                    quality = (sum(bool(count) for count in hits), sum(hits))
                    if quality > best_quality:
                        best_quality = quality
                        best_context = context_excerpt(message, terms)
        except OSError:
            continue
        if all(counts.values()):
            session["score"] = min(counts.values())
            session["date"] = "-".join(path.parts[-4:-1])
            session["context"] = best_context
            matches.append(session)

    metadata = thread_metadata(Path.home() / ".codex", [str(item["id"]) for item in matches])
    for session in matches:
        session_id = str(session["id"])
        label, session["yolo"] = metadata.get(session_id, (None, False))
        session["title"] = short_title(label, str(session["cwd"]), session_id)
    return sorted(
        matches,
        key=lambda item: (int(item["score"]), str(item["date"])),
        reverse=True,
    )[:10]


def find_sessions(query: str) -> int:
    sessions = (
        json.loads(run_in_wsl("_search", query))
        if os.name == "nt"
        else search_wsl_sessions(query)
    )
    if not sessions:
        print(f"No sessions found for: {query}")
        return 1

    print(paint("1", f"Found {len(sessions)} likely session(s) for: {query}"))
    for index, session in enumerate(sessions, 1):
        score = paint("93", f"score {session['score']}")
        metadata = (
            f"{score}  "
            f"{paint('92', session['date'])}  {paint('95', session['id'])}"
        )
        print_session_card(index, session, metadata, str(session["context"]))

    if os.name == "nt" and sys.stdin.isatty() and sys.stdout.isatty():
        while True:
            choice = input("\nOpen session [number, Enter to cancel]: ").strip()
            if not choice:
                break
            if choice.isdigit() and 1 <= int(choice) <= len(sessions):
                session = sessions[int(choice) - 1]
                wt = windows_command("wt.exe")
                wsl = windows_command("wsl.exe")
                cwd = str(session["cwd"])
                if not wsl_directory_exists(wsl, cwd):
                    print(paint("91", f"Missing WSL directory: {cwd}"))
                    return 1
                subprocess.Popen(
                    launch_command([session], wt, wsl),
                    close_fds=True,
                )
                print(paint("92", f"Opened: {session['title']}"))
                break
            print(
                paint("91", f"Choose a number from 1 to {len(sessions)}, or press Enter.")
            )
    return 0


def terminate_sessions(sessions: list[dict[str, object]]) -> None:
    pids = [int(session["pid"]) for session in sessions]
    if os.name == "nt":
        run_wsl_command(
            windows_command("wsl.exe"),
            "/bin/kill",
            "-TERM",
            *map(str, pids),
            check=False,
        )
    else:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def state_file() -> Path:
    return Path(__file__).resolve().with_name("sessions.json")


def write_manifest(sessions: list[dict[str, object]]) -> None:
    manifest = {
        "version": STATE_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "sessions": sessions,
    }
    destination = state_file()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=destination.parent, delete=False
    ) as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(destination)


def snapshot_sessions(live_sessions: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "id": session["id"],
            "cwd": session["cwd"],
            "title": session["title"],
            "yolo": bool(session.get("yolo")),
        }
        for session in live_sessions
    ]


def save_sessions(dry_run: bool, kill: bool) -> int:
    live_sessions = discover_from_windows() if os.name == "nt" else discover_wsl_sessions()
    if not live_sessions:
        print(
            paint("91", "No running interactive Codex sessions found; existing save kept."),
            file=sys.stderr,
        )
        return 1
    sessions = snapshot_sessions(live_sessions)

    if not dry_run:
        write_manifest(sessions)

    action = "Would save" if dry_run else "Saved"
    status = paint("93" if dry_run else "92", action.lower())
    print(paint("1", f"{action} {len(sessions)} session(s)"))
    for index, session in enumerate(sessions, 1):
        print_session_card(index, session, f"{status}  {paint('95', session['id'])}")
    if kill:
        if dry_run:
            print(
                paint(
                    "93", f"\nWould send SIGTERM to {len(live_sessions)} Codex process(es)."
                )
            )
        else:
            terminate_sessions(live_sessions)
            print(paint("93", f"\nSent SIGTERM to {len(live_sessions)} Codex process(es)."))
    elif not dry_run:
        print(
            paint(
                "96", "\nSessions were not killed; close Windows Terminal after active turns finish."
            )
        )
    return 0


def load_manifest(path: Path | None = None) -> dict[str, object]:
    path = path or state_file()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit("No saved sessions found. Run saveSessions first.") from None
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"Cannot read {path}: {error}") from error
    if manifest.get("version") != STATE_VERSION or not isinstance(manifest.get("sessions"), list):
        raise SystemExit(f"Unsupported or invalid save file: {path}")
    return manifest


def watch_sessions() -> None:
    print(paint("96", "\nAutosave active every 60 seconds; press Ctrl+C to stop."))
    try:
        while True:
            live_sessions = discover_from_windows()
            if live_sessions:
                sessions = snapshot_sessions(live_sessions)
                write_manifest(sessions)
                status = f"Autosaved {len(sessions)} session(s)"
            else:
                status = "No running sessions; saved snapshot kept"
            timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
            print(f"\r{paint('92', status)} at {timestamp}  ", end="", flush=True)
            time.sleep(AUTOSAVE_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print(paint("93", "\nAutosave stopped."))


def launch_command(
    sessions: list[dict[str, object]], wt: str, wsl: str
) -> list[str]:
    command = [wt, "-w", "new"]
    for session in sessions:
        session_id = str(session.get("id", ""))
        cwd = str(session.get("cwd", ""))
        if not SESSION_ID.fullmatch(session_id) or not cwd.startswith("/"):
            raise ValueError(f"Invalid saved session: {session!r}")
        if len(command) > 3:
            command.append(";")
        resume_command = f"codex {'--yolo ' if session.get('yolo') else ''}resume {session_id}"
        shell_command = (
            f"history -s '{resume_command}' && history -a && "
            f"{resume_command} || true && exec bash -l"
        )
        command.extend(
            [
                "nt",
                "--title",
                short_title(str(session.get("title") or ""), cwd, session_id),
                "--suppressApplicationTitle",
                wsl,
                "-d",
                DISTRO,
                "--cd",
                cwd,
                "bash",
                "-lc",
                shell_command,
            ]
        )
    return command


def wsl_directory_exists(wsl: str, cwd: str) -> bool:
    return run_wsl_command(
        wsl,
        "test",
        "-d",
        cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def restore_sessions(dry_run: bool) -> int:
    if os.name != "nt":
        raise SystemExit("restoreSessions must run from Windows PowerShell.")

    manifest = load_manifest()
    action = "Would restore" if dry_run else "Restoring"
    print(paint("1", f"{action} {len(manifest['sessions'])} saved session(s)"))
    wt = windows_command("wt.exe")
    wsl = windows_command("wsl.exe")
    codex_check = run_wsl_command(
        wsl,
        "bash",
        "-lc",
        "command -v codex >/dev/null",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if codex_check.returncode:
        raise SystemExit(f"Required dependency missing: codex in the {DISTRO} login shell")
    running = {str(session["id"]) for session in discover_from_windows()}
    pending: list[dict[str, object]] = []
    launched = 0

    for index, session in enumerate(manifest["sessions"], 1):
        session_id = str(session.get("id", ""))
        if session_id in running:
            print_session_card(
                index,
                session,
                f"{paint('96', 'already running')}  {paint('95', session_id)}",
            )
            continue
        cwd = str(session["cwd"])
        if not wsl_directory_exists(wsl, cwd):
            print_session_card(
                index,
                session,
                f"{paint('91', 'missing directory')}  {paint('95', session_id)}",
            )
            continue
        if dry_run:
            status = paint("93", "would restore")
        else:
            pending.append(session)
            status = paint("92", "restored")
        print_session_card(index, session, f"{status}  {paint('95', session_id)}")
        launched += 1

    if pending:
        subprocess.Popen(launch_command(pending, wt, wsl), close_fds=True)

    summary = "Would restore" if dry_run else "Restored"
    print(paint("1", f"\n{summary} {launched} session(s)."))
    result = 0 if launched or running else 1
    if not dry_run and result == 0:
        watch_sessions()
    return result


def self_test() -> int:
    session_id = "01234567-89ab-cdef-0123-456789abcdef"
    assert short_title("  a   useful title ", "/tmp/work", session_id) == "a useful title"
    assert short_title(None, "/tmp/work", session_id) == "work"
    assert is_yolo("never", '{"type":"disabled"}')
    assert not is_yolo("on-request", '{"type":"disabled"}')
    with tempfile.TemporaryDirectory() as directory:
        rollout = Path(directory) / "rollout.jsonl"
        metadata = {"payload": {"id": session_id, "cwd": "/tmp/work", "source": "cli"}}
        rollout.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
        assert session_from_rollout(rollout) is not None
        metadata["payload"]["source"] = {"subagent": {}}
        rollout.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
        assert session_from_rollout(rollout) is None
        try:
            load_manifest(Path(directory) / "sessions.json")
        except SystemExit as error:
            assert str(error) == "No saved sessions found. Run saveSessions first."
        else:
            raise AssertionError("Missing manifest should fail clearly")
    session = {"id": session_id, "cwd": "/tmp/a b", "title": "test", "yolo": False}
    command = launch_command(
        [session, session],
        "wt.exe",
        "wsl.exe",
    )
    assert command[:4] == ["wt.exe", "-w", "new", "nt"]
    assert command.count(";") == 1
    assert command[-4:] == [
        "/tmp/a b",
        "bash",
        "-lc",
        f"history -s 'codex resume {session_id}' && history -a && "
        f"codex resume {session_id} || true && exec bash -l",
    ]
    assert ";" not in command[-1]
    other = {
        "id": "fedcba98-7654-3210-fedc-ba9876543210",
        "cwd": "/tmp/other",
        "title": "other",
    }
    assert [item["id"] for item in snapshot_sessions([other])] == [other["id"]]
    yolo_command = launch_command([{**session, "yolo": True}], "wt.exe", "wsl.exe")[-1]
    assert f"codex --yolo resume {session_id}" in yolo_command
    line = json.dumps(
        {"type": "event_msg", "payload": {"type": "user_message", "message": "task42 ready"}},
        separators=(",", ":"),
    )
    assert searchable_message(line) == "task42 ready"
    assert searchable_message('{"type":"response_item","message":"task 42"}') is None
    assert "task42" in context_excerpt("before task42 after", ["task", "42"])
    print("Self-test passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    save = subparsers.add_parser("save", help="save running WSL Codex sessions")
    save.add_argument("--dry-run", action="store_true")
    save.add_argument(
        "--kill",
        action="store_true",
        help="after saving, send SIGTERM to the found Codex processes",
    )
    restore = subparsers.add_parser("restore", help="reopen the saved sessions")
    restore.add_argument("--dry-run", action="store_true")
    find = subparsers.add_parser("find", help="find likely sessions by conversation text")
    find.add_argument("query", nargs="+")
    subparsers.add_parser("self-test")
    subparsers.add_parser("_discover", help=argparse.SUPPRESS)
    search = subparsers.add_parser("_search", help=argparse.SUPPRESS)
    search.add_argument("query")
    args = parser.parse_args()

    if args.command == "save":
        return save_sessions(args.dry_run, args.kill)
    if args.command == "restore":
        return restore_sessions(args.dry_run)
    if args.command == "find":
        return find_sessions(" ".join(args.query))
    if args.command == "_discover":
        print(json.dumps(discover_wsl_sessions(), ensure_ascii=False))
        return 0
    if args.command == "_search":
        print(json.dumps(search_wsl_sessions(args.query), ensure_ascii=False))
        return 0
    return self_test()


if __name__ == "__main__":
    raise SystemExit(main())
