#!/usr/bin/env python3
"""Save running WSL Codex sessions and reopen them in Windows Terminal tabs."""

from __future__ import annotations

import argparse
import base64
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
import uuid
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path


SESSION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
TOKENS = re.compile(r"[^\W\d_]+|\d+")
ROLLOUT_HEADER = re.compile(
    rb'^\{"timestamp":"[^"]+"(?:,"ordinal":\d+)?,"type":"([^"]+)","payload":\{(?:"type":"([^"]+)")?'
)
STATE_VERSION = 1
DISTRO = os.environ.get("CODEX_SESSIONS_DISTRO", "Ubuntu")
WSL_PYTHON = os.environ.get("CODEX_SESSIONS_WSL_PYTHON", "/usr/bin/python3")
AUTOSAVE_INTERVAL_SECONDS = 60
WSL_TIMEOUT_SECONDS = 30
SEARCH_TIMEOUT_SECONDS = 300


class SessionDiscoveryError(RuntimeError):
    """Live identity or required metadata is temporarily incomplete."""


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
        not isinstance(payload, dict)
        or isinstance(payload.get("source"), dict)
        or (payload.get("source") != "cli" and payload.get("originator") != "codex-tui")
        or not isinstance(session_id, str)
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
    codex_home: Path, session_ids: list[str], *, required: bool = False
) -> dict[str, tuple[str | None, bool]]:
    if not session_ids:
        return {}
    placeholders = ",".join("?" for _ in session_ids)
    database = codex_home / "state_5.sqlite"
    try:
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
            rows = connection.execute(
                f"SELECT id, COALESCE(NULLIF(name, ''), title), approval_mode, sandbox_policy "
                f"FROM threads "
                f"WHERE id IN ({placeholders})",
                session_ids,
            ).fetchall()
        metadata = {}
        for session_id, label, approval_mode, sandbox_policy in rows:
            if required:
                try:
                    policy = json.loads(sandbox_policy)
                    if (not isinstance(policy, dict) or not isinstance(policy.get("type"), str)
                            or not isinstance(approval_mode, str) or not approval_mode):
                        raise ValueError("missing sandbox type")
                except (TypeError, ValueError) as error:
                    raise SessionDiscoveryError("Cannot read session permission mode; existing save kept.") from error
            metadata[session_id] = (label, is_yolo(approval_mode, sandbox_policy))
        if required and set(session_ids).difference(metadata):
            raise SessionDiscoveryError("Session metadata is incomplete; existing save kept.")
        return metadata
    except sqlite3.Error as error:
        if required:
            raise SessionDiscoveryError("Cannot read Codex metadata; existing save kept.") from error
        return {}


def thread_rollouts(codex_home: Path) -> dict[str, tuple[Path, str]]:
    try:
        with closing(sqlite3.connect(
            f"file:{codex_home / 'state_5.sqlite'}?mode=ro", uri=True
        )) as connection:
            rows = connection.execute(
                "SELECT id, rollout_path, cwd FROM threads WHERE archived = 0"
            )
            return {
                session_id: (Path(rollout_path), cwd)
                for session_id, rollout_path, cwd in rows
                if isinstance(session_id, str)
                and SESSION_ID.fullmatch(session_id)
                and isinstance(rollout_path, str)
                and isinstance(cwd, str)
            }
    except sqlite3.Error:
        return {}


def nearest_session_snapshot(
    cwd: str,
    started_at: float,
    snapshots: list[tuple[float, str]],
    thread_cwds: dict[str, str],
) -> str | None:
    matches = set()
    for timestamp, session_id in snapshots:
        distance = abs(timestamp - started_at)
        if distance > 30:
            continue
        try:
            same_cwd = os.path.samefile(thread_cwds.get(session_id, ""), cwd)
        except OSError:
            same_cwd = thread_cwds.get(session_id) == cwd
        if same_cwd:
            matches.add(session_id)
    if len(matches) > 1:
        raise SessionDiscoveryError("Ambiguous session snapshots; existing save kept.")
    return next(iter(matches)) if matches else None


def resumed_session(
    codex_home: Path, started_at: float, cwd: str, pid: int
) -> dict[str, object] | None:
    try:
        with closing(sqlite3.connect(
            f"file:{codex_home / 'logs_2.sqlite'}?mode=ro", uri=True
        )) as connection:
            rows = connection.execute(
                "SELECT DISTINCT thread_id FROM logs WHERE ts BETWEEN ? AND ? "
                "AND target = 'codex_app_server::request_processors::thread_lifecycle' "
                "AND feedback_log_body LIKE 'composing running thread resume response %'",
                (int(started_at) - 30, int(started_at) + 30),
            ).fetchall()
        rollouts = thread_rollouts(codex_home)
        matches = {}
        for (session_id,) in rows:
            if session_id not in rollouts:
                continue
            path, thread_cwd = rollouts[session_id]
            if os.path.realpath(thread_cwd) != os.path.realpath(cwd):
                continue
            session = session_from_rollout(path, pid)
            if session:
                matches[session_id] = session
        # shortcut: use a unique nearby resume until Codex exposes a TUI/thread ID.
        if len(matches) > 1:
            raise SessionDiscoveryError(
                f"Ambiguous session for Codex process {pid}. Existing save kept. "
                "Restart that terminal with codex resume <session-id>."
            )
        return next(iter(matches.values())) if len(matches) == 1 else None
    except sqlite3.Error:
        return None


def cached_session_id(records: list[dict[str, object]], pid: int, process_start: str) -> str | None:
    for record in records:
        if (
            isinstance(record, dict)
            and record.get("pid") == pid
            and record.get("process_start") == process_start
            and record.get("identity_version") == 2
            and isinstance(record.get("id"), str)
        ):
            return record["id"]
    return None


def discover_wsl_sessions() -> list[dict[str, object]]:
    codex_home = Path.home() / ".codex"
    cache_path = codex_home / "session-ritual-processes.json"
    try:
        cached = load_manifest(cache_path)["sessions"]
    except SystemExit:
        cached = []
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    process_starts = {}
    sessions_root = str(codex_home / "sessions") + os.sep
    rollouts = thread_rollouts(codex_home)
    found: dict[int, dict[str, object]] = {}
    unresolved: list[tuple[float, str, int]] = []
    booted_at = time.time() - float(Path("/proc/uptime").read_text().split()[0])
    clock_ticks = os.sysconf("SC_CLK_TCK")

    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            executable = Path(os.readlink(process / "exe")).name.removesuffix(" (deleted)")
            if executable != "codex":
                continue
            arguments = (
                (process / "cmdline").read_bytes().decode(errors="replace").split("\0")
            )
            if "app-server" in arguments or not os.readlink(process / "fd" / "0").startswith(
                "/dev/pts/"
            ):
                continue
            stat_fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
            started_ticks = int(stat_fields[19])
            process_starts[int(process.name)] = f"{boot_id}:{started_ticks}"
            descriptors = list((process / "fd").iterdir())
        except (OSError, ValueError, IndexError):
            continue

        session = None
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if not target.startswith(sessions_root) or not target.endswith(".jsonl"):
                continue
            session = session_from_rollout(Path(target), int(process.name))
            if session:
                found[int(process.name)] = session
                break

        if session:
            continue
        # shortcut: launch identity cannot track /resume or /new inside daemon TUIs.
        # Upgrade when Codex exposes the current primary thread per TUI process.
        session_id = next((value for value in arguments if SESSION_ID.fullmatch(value)), None)
        if session_id in rollouts:
            session = session_from_rollout(rollouts[session_id][0], int(process.name))
            if session:
                found[int(process.name)] = session
                continue
        try:
            unresolved.append(
                (
                    booted_at + started_ticks / clock_ticks,
                    os.readlink(process / "cwd"),
                    int(process.name),
                )
            )
        except (OSError, PermissionError, ValueError, IndexError):
            continue

    snapshots = []
    for path in (codex_home / "shell_snapshots").glob("*.sh"):
        session_id = path.name.split(".", 1)[0]
        if session_id in rollouts:
            try:
                snapshots.append((path.stat().st_mtime, session_id))
            except OSError:
                pass
    thread_cwds = {session_id: record[1] for session_id, record in rollouts.items()}
    missing = []
    inferred: dict[str, int] = {}
    for started_at, cwd, pid in sorted(unresolved):
        session_id = cached_session_id(cached, pid, process_starts[pid])
        session = session_from_rollout(rollouts[session_id][0], pid) if session_id in rollouts else None
        if session is None:
            resumed = resumed_session(codex_home, started_at, cwd, pid)
            session_id = nearest_session_snapshot(
                cwd, started_at, snapshots, thread_cwds,
            )
            if resumed and session_id and resumed["id"] != session_id:
                raise SessionDiscoveryError("Conflicting session startup records; existing save kept.")
            session = resumed
            if session_id:
                session = session_from_rollout(rollouts[session_id][0], pid)
            if session:
                session_id = str(session["id"])
                if session_id in inferred or any(item["id"] == session_id for item in found.values()):
                    raise SessionDiscoveryError("Session startup matches multiple processes; existing save kept.")
                inferred[session_id] = pid
        if session:
            found[pid] = session
        else:
            missing.append(str(pid))

    if any(sum(item["id"] == session_id for item in found.values()) > 1 for session_id in inferred):
        raise SessionDiscoveryError("Session startup matches multiple processes; existing save kept.")
    if missing:
        raise SessionDiscoveryError(
            f"Cannot identify interactive Codex process(es): {', '.join(missing)}. "
            "Existing save kept. Select a session in the resume picker, or restart "
            "that terminal with codex resume <session-id>, then save again."
        )

    metadata = thread_metadata(codex_home, [str(item["id"]) for item in found.values()], required=True)
    for pid, session in found.items():
        session_id = str(session["id"])
        label, session["yolo"] = metadata[session_id]
        session["title"] = short_title(label, str(session["cwd"]), session_id)
        session["process_start"] = process_starts[pid]
    write_manifest(
        [{**session, "identity_version": 2} for session in found.values()], cache_path,
    )
    return sorted(found.values(), key=lambda session: (str(session["title"]).lower(), str(session["id"])))


def windows_command(name: str) -> str:
    command = shutil.which(name)
    if not command:
        raise SystemExit(f"Required Windows command is missing from PATH: {name}")
    return command


def run_wsl_command(
    wsl: str, *arguments: str, timeout: float = WSL_TIMEOUT_SECONDS, **options: object
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [wsl, "-d", DISTRO, "--exec", *arguments],
            timeout=timeout,
            **options,
        )
    except subprocess.TimeoutExpired:
        raise SessionDiscoveryError(f"Timed out waiting for {DISTRO} WSL; saved snapshot unchanged.") from None


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
        timeout=SEARCH_TIMEOUT_SECONDS if arguments[0] == "_search" else WSL_TIMEOUT_SECONDS,
        capture_output=True,
        encoding="utf-8",
        input=script,
    )
    if result.returncode:
        if result.returncode == 3:
            raise SessionDiscoveryError(result.stderr.strip())
        raise SystemExit(result.stderr.strip() or "WSL session discovery failed.")
    return result.stdout


def discover_from_windows() -> list[dict[str, object]]:
    return json.loads(run_in_wsl("_discover"))


def searchable_message(line: bytes) -> str | None:
    # Skip tool events without scanning or decoding their large payloads.
    header = ROLLOUT_HEADER.match(line)
    if header:
        record_type, item_type = header.groups()
        if record_type not in {b"event_msg", b"response_item"} or (
            item_type is not None and (record_type, item_type) not in {
                (b"event_msg", b"user_message"), (b"event_msg", b"agent_message"),
                (b"response_item", b"message"),
            }
        ):
            return None
    if not any(tag in line for tag in (b'"user_message"', b'"agent_message"', b'"message"')):
        return None
    try:
        record = json.loads(line)
        payload = record["payload"]
        if record["type"] == "event_msg" and payload.get("type") in {"user_message", "agent_message"}:
            message = payload.get("message")
        elif (
            record["type"] == "response_item" and payload.get("type") == "message"
            and payload.get("role") in {"user", "assistant"}
        ):
            message = "\n".join(
                part["text"] for part in payload.get("content", [])
                if isinstance(part, dict) and part.get("type") in {"input_text", "output_text"}
                and isinstance(part.get("text"), str)
            )
        else:
            return None
    except (AttributeError, KeyError, TypeError, ValueError):
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
    terms = list(dict.fromkeys(TOKENS.findall(query.casefold())))
    if not terms:
        return []
    patterns = [
        rf"(?<!\d){re.escape(term)}(?!\d)" if term.isdecimal()
        else rf"(?<![^\W\d_]){re.escape(term)}(?![^\W\d_])"
        for term in terms
    ]
    matching_terms = re.compile("|".join(patterns))
    matches: list[dict[str, object]] = []
    paths = []
    for path in (Path.home() / ".codex" / "sessions").rglob("*.jsonl"):
        try:
            paths.append((path.stat().st_mtime, path))
        except OSError:
            continue

    for updated_at, path in sorted(paths, reverse=True):
        found_terms = set()
        best_context = ""
        best_quality = 0
        session = session_from_rollout(path)
        if not session:
            continue
        try:
            with path.open("rb") as stream:
                for line in stream:
                    message = searchable_message(line)
                    if message is None:
                        continue
                    folded = message.casefold()
                    if not any(term in folded for term in terms):
                        continue
                    hits = set(matching_terms.findall(folded))
                    found_terms.update(hits)
                    quality = len(hits)
                    if quality > best_quality:
                        best_quality = quality
                        best_context = context_excerpt(message, terms)
                    if found_terms.issuperset(terms):
                        break
        except OSError:
            continue
        if found_terms.issuperset(terms):
            session["updated_at"] = updated_at
            session["context"] = best_context
            matches.append(session)
            if len(matches) == 10:
                break

    metadata = thread_metadata(Path.home() / ".codex", [str(item["id"]) for item in matches])
    for session in matches:
        session_id = str(session["id"])
        label, session["yolo"] = metadata.get(session_id, (None, False))
        session["title"] = short_title(label, str(session["cwd"]), session_id)
    return matches


def find_sessions(query: str) -> int:
    print(paint("96", "Searching session history (newest activity first)..."), flush=True)
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
        updated_at = datetime.fromtimestamp(float(session["updated_at"])).astimezone()
        date = updated_at.strftime("%Y-%m-%d %H:%M:%S %z")
        metadata = f"{paint('92', date)}  {paint('95', session['id'])}"
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
                    launch_command(session, wt, wsl),
                    close_fds=True,
                )
                print(paint("92", f"Opened: {session['title']}"))
                break
            print(
                paint("91", f"Choose a number from 1 to {len(sessions)}, or press Enter.")
            )
    return 0


def terminate_sessions(sessions: list[dict[str, object]]) -> None:
    if os.name == "nt":
        targets = [{key: session[key] for key in ("pid", "process_start")} for session in sessions]
        run_in_wsl("_terminate", json.dumps(targets))
        return
    for session in sessions:
        pid = int(session["pid"])
        descriptor = None
        try:
            descriptor = os.pidfd_open(pid)
            process = Path("/proc") / str(pid)
            executable = Path(os.readlink(process / "exe")).name.removesuffix(" (deleted)")
            if process_identity(pid) != session["process_start"] or executable != "codex":
                raise SessionDiscoveryError(f"Process {pid} changed; no signal sent to it.")
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        except (ProcessLookupError, FileNotFoundError):
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)


def process_identity(pid: int) -> str:
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    started = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]
    return f"{boot}:{started}"


def state_file() -> Path:
    return Path(__file__).resolve().with_name("sessions.json")


@contextmanager
def state_lock():
    path = state_file().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + WSL_TIMEOUT_SECONDS + 5
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise SessionDiscoveryError("Another session operation is still running; existing save kept.")
                time.sleep(0.1)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def saved_generation() -> str:
    return str(load_manifest().get("stop_generation", "")) if state_file().exists() else ""


def write_manifest(
    sessions: list[dict[str, object]], destination: Path | None = None, *, stop_generation: str = ""
) -> None:
    manifest = {
        "version": STATE_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "sessions": sessions,
        "stop_generation": stop_generation,
    }
    destination = destination or state_file()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=destination.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def snapshot_sessions(live_sessions: list[dict[str, object]]) -> list[dict[str, object]]:
    return list({
        str(session["id"]): {
            "id": session["id"],
            "cwd": session["cwd"],
            "title": session["title"],
            "yolo": bool(session.get("yolo")),
        }
        for session in live_sessions
    }.values())


def save_sessions(dry_run: bool, kill: bool) -> int:
    with nullcontext() if dry_run else state_lock():
        live_sessions = discover_from_windows() if os.name == "nt" else discover_wsl_sessions()
        if not live_sessions:
            print(
                paint("91", "No running interactive Codex sessions found; existing save kept."),
                file=sys.stderr,
            )
            return 1
        sessions = snapshot_sessions(live_sessions)
        if not dry_run:
            generation = uuid.uuid4().hex if kill else saved_generation()
            write_manifest(sessions, stop_generation=generation)
            if kill:
                # Invalidate old watchers before any process starts shutting down.
                terminate_sessions(live_sessions)

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
            print(paint("93", f"\nSent SIGTERM to {len(live_sessions)} Codex process(es)."))
            print("Existing autosave watchers will stop; the pre-shutdown snapshot is kept.")
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
    if not isinstance(manifest, dict) or manifest.get("version") != STATE_VERSION or not isinstance(manifest.get("sessions"), list):
        raise SystemExit(f"Unsupported or invalid save file: {path}")
    return manifest


def watch_sessions(generation: str) -> None:
    print(paint("96", "\nAutosave active every 60 seconds; press Ctrl+C to stop."))
    try:
        while True:
            try:
                with state_lock():
                    if saved_generation() != generation:
                        print("\nAutosave stopped by save --kill or a newer restore; snapshot kept.")
                        return
                    live_sessions = discover_from_windows()
                    if live_sessions:
                        sessions = snapshot_sessions(live_sessions)
                        write_manifest(sessions, stop_generation=generation)
                        status = f"Autosaved {len(sessions)} session(s)"
                    else:
                        status = "No running sessions; saved snapshot kept"
            except (SessionDiscoveryError, OSError) as error:
                print(paint("93", f"\nAutosave postponed: {error} Retrying in 60 seconds."), flush=True)
                time.sleep(AUTOSAVE_INTERVAL_SECONDS)
                continue
            timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
            print(f"\r{paint('92', status)} at {timestamp}  ", end="", flush=True)
            time.sleep(AUTOSAVE_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print(paint("93", "\nAutosave stopped."))


def launch_command(
    session: dict[str, object], wt: str, wsl: str, window: str = "new"
) -> list[str]:
    session_id = str(session.get("id", ""))
    cwd = str(session.get("cwd", ""))
    if not SESSION_ID.fullmatch(session_id) or not cwd.startswith("/"):
        raise ValueError(f"Invalid saved session: {session!r}")
    resume_command = f"codex {'--yolo ' if session.get('yolo') else ''}resume {session_id}"
    shell_command = (
        f"history -s '{resume_command}' && history -a || true\n"
        f"{resume_command}\n"
        f"exec bash -l"
    )
    # Keep paths and shell text out of Terminal/WSL's nested command-line parsers.
    payload = base64.b64encode(json.dumps([cwd, shell_command]).encode()).decode("ascii")
    bootstrap = (
        "import base64,json,os\n"
        f"cwd,command=json.loads(base64.b64decode('{payload}'))\n"
        "os.chdir(cwd)\n"
        "os.execvp('bash',['bash','-lc',command])"
    )
    arguments = [
        "-w", window, "nt", "--title",
        short_title(str(session.get("title") or ""), cwd, session_id),
        "--suppressApplicationTitle", wsl, "-d", DISTRO,
        "--exec", WSL_PYTHON, "-c", bootstrap,
    ]
    # Terminal splits semicolons inside argv, even when subprocess quoted them.
    return [wt, *(argument.replace(";", "\\;") for argument in arguments)]


def wait_for_sessions(previous: dict[str, set[tuple[int, str]]]) -> None:
    deadline = time.monotonic() + WSL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            live = discover_from_windows()
            seen: dict[str, set[tuple[int, str]]] = {}
            for session in live:
                seen.setdefault(str(session["id"]), set()).add(
                    (int(session["pid"]), str(session["process_start"]))
                )
            if all(seen.get(session_id, set()) - old for session_id, old in previous.items()):
                return
        except SessionDiscoveryError:
            pass
        time.sleep(1)
    raise SessionDiscoveryError("Not all restored sessions became ready; original snapshot kept, autosave not started.")


def wsl_directory_exists(wsl: str, cwd: str) -> bool:
    return run_wsl_command(
        wsl,
        "test",
        "-d",
        cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def restore_sessions(dry_run: bool, reopen: bool = False) -> int:
    if os.name != "nt":
        raise SystemExit("restoreSessions must run from Windows PowerShell.")

    with state_lock():
        manifest = load_manifest()
        action = "Would restore" if dry_run else "Restoring"
        print(paint("1", f"{action} {len(manifest['sessions'])} saved session(s)"))
        wt = windows_command("wt.exe")
        wsl = windows_command("wsl.exe")
        codex_check = run_wsl_command(
            wsl, "bash", "-lc", "command -v codex >/dev/null",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if codex_check.returncode:
            raise SystemExit(f"Required dependency missing: codex in the {DISTRO} login shell")
        running: dict[str, set[tuple[int, str]]] = {}
        for session in discover_from_windows():
            running.setdefault(str(session["id"]), set()).add(
                (int(session["pid"]), str(session["process_start"]))
            )
        pending = []
        expected = {}
        window = f"codex-sessions-{uuid.uuid4().hex}"
        for index, session in enumerate(manifest["sessions"], 1):
            session_id = str(session.get("id", ""))
            command = launch_command(session, wt, wsl, window)
            expected[session_id] = running.get(session_id, set()) if reopen else set()
            if session_id in running and not reopen:
                status = paint("96", "already running")
            else:
                if not wsl_directory_exists(wsl, str(session["cwd"])):
                    raise SessionDiscoveryError(f"Missing WSL directory: {session['cwd']}; original snapshot kept.")
                pending.append((session_id, command))
                status = paint("93", ("would " if dry_run else "will ") +
                               ("reopen" if session_id in running else "restore"))
            print_session_card(index, session, f"{status}  {paint('95', session_id)}")
        if dry_run:
            print(paint("1", f"\nWould restore {len(pending)} session(s)."))
            return 0
        generation = uuid.uuid4().hex
        write_manifest(manifest["sessions"], stop_generation=generation)
        for session_id, command in pending:
            subprocess.Popen(command, close_fds=True)
            wait_for_sessions({session_id: expected[session_id]})
        if expected:
            wait_for_sessions(expected)
        print(paint("1", f"\nRestored {len(pending)} session(s)."))
    if not expected:
        return 1
    watch_sessions(generation)
    return 0


def self_test() -> int:
    from unittest import TestCase, mock

    check = TestCase()

    def launch_payload(command):
        encoded = re.search(r"b64decode\('([^']+)'\)", command[-1]).group(1)
        return json.loads(base64.b64decode(encoded))

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
        metadata["payload"].update(source="vscode", originator="codex-tui")
        rollout.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
        assert session_from_rollout(rollout) is not None
        root = Path(directory)
        with closing(sqlite3.connect(root / "state_5.sqlite")) as connection, connection:
            connection.execute(
                "CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, archived INTEGER)"
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, 0)",
                (session_id, str(rollout), "/tmp/work"),
            )
        with closing(sqlite3.connect(root / "logs_2.sqlite")) as connection, connection:
            connection.execute(
                "CREATE TABLE logs (ts INTEGER, thread_id TEXT, target TEXT, feedback_log_body TEXT)"
            )
            connection.execute(
                "INSERT INTO logs VALUES (100, ?, ?, ?)",
                (session_id, "codex_app_server::request_processors::thread_lifecycle",
                 "composing running thread resume response thread_id=" + session_id),
            )
        # A live reattach produces no shell snapshot, but does log the resume.
        assert resumed_session(root, 101, "/tmp/work", 42) == {
            "id": session_id, "cwd": "/tmp/work", "pid": 42
        }
        assert resumed_session(root, 200, "/tmp/work", 42) is None
        assert resumed_session(root, 101, "/tmp/other", 42) is None
        other_id = "fedcba98-7654-3210-fedc-ba9876543210"
        other_rollout = root / "other.jsonl"
        metadata["payload"]["id"] = other_id
        other_rollout.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
        with closing(sqlite3.connect(root / "state_5.sqlite")) as connection, connection:
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, 0)",
                (other_id, str(other_rollout), "/tmp/work"),
            )
        with closing(sqlite3.connect(root / "logs_2.sqlite")) as connection, connection:
            connection.execute(
                "INSERT INTO logs SELECT ts, ?, target, feedback_log_body FROM logs",
                (other_id,),
            )
        try:
            resumed_session(root, 101, "/tmp/work", 42)
        except SessionDiscoveryError as error:
            assert "Ambiguous session" in str(error)
        else:
            raise AssertionError("Concurrent resumes must not be guessed")
        metadata["payload"]["source"] = {"subagent": {}}
        rollout.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
        other_rollout.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
        assert resumed_session(root, 101, "/tmp/work", 42) is None
        cache = [{"id": session_id, "pid": 42, "process_start": "boot:99", "identity_version": 2}]
        write_manifest(cache, root / "cache.json")
        cached = load_manifest(root / "cache.json")["sessions"]
        assert cached_session_id(cached, 42, "boot:99") == session_id
        assert cached_session_id(cached, 43, "boot:99") is None
        assert cached_session_id(cached, 42, "boot:100") is None
        assert cached_session_id(cached, 42, "other-boot:99") is None
        assert cached_session_id([{**cache[0], "identity_version": 1}], 42, "boot:99") is None
        try:
            load_manifest(Path(directory) / "sessions.json")
        except SystemExit as error:
            assert str(error) == "No saved sessions found. Run saveSessions first."
        else:
            raise AssertionError("Missing manifest should fail clearly")
    session = {"id": session_id, "cwd": "/tmp/a b", "title": "test", "yolo": False}
    command = launch_command(
        session,
        "wt.exe",
        "wsl.exe",
    )
    assert command[:4] == ["wt.exe", "-w", "new", "nt"]
    assert ";" not in command
    assert command[-4:-1] == ["--exec", WSL_PYTHON, "-c"]
    assert launch_payload(command) == [
        "/tmp/a b",
        f"history -s 'codex resume {session_id}' && history -a || true\n"
        f"codex resume {session_id}\n"
        f"exec bash -l",
    ]
    assert ";" not in command[-1]
    other = {
        "id": "fedcba98-7654-3210-fedc-ba9876543210",
        "cwd": "/tmp/other",
        "title": "other",
    }
    assert [item["id"] for item in snapshot_sessions([other])] == [other["id"]]
    snapshots = [(10.0, session_id), (20.0, str(other["id"]))]
    thread_cwds = {session_id: "/tmp/work", str(other["id"]): "/tmp/work"}
    try:
        nearest_session_snapshot("/tmp/work", 12.0, snapshots, thread_cwds)
    except SessionDiscoveryError:
        pass
    else:
        raise AssertionError("Overlapping snapshots must not be guessed")
    assert nearest_session_snapshot(
        "/tmp/work", 18.0, [snapshots[1]], thread_cwds
    ) == other["id"]
    assert nearest_session_snapshot(
        "/tmp/work", 18.0, [snapshots[1], snapshots[1]], thread_cwds
    ) == other["id"]
    assert (
        nearest_session_snapshot("/tmp/work", 51.0, snapshots, thread_cwds)
        is None
    )
    assert nearest_session_snapshot(
        "/tmp/work", 1.0, [(100.0, session_id)], thread_cwds
    ) is None
    assert (
        nearest_session_snapshot("/tmp/elsewhere", 5.0, snapshots, thread_cwds)
        is None
    )
    yolo_command = launch_payload(launch_command({**session, "yolo": True}, "wt.exe", "wsl.exe"))[1]
    assert f"codex --yolo resume {session_id}" in yolo_command
    escaped = launch_command({**session, "title": "one; two", "cwd": "/tmp/a;b"}, "wt.exe", "wsl.exe")
    assert "one\\; two" in escaped and launch_payload(escaped)[0] == "/tmp/a;b"
    quoted_cwd = '/tmp/one; two\'s "quoted" space\\'
    escaped = launch_command({**session, "cwd": quoted_cwd}, "wt.exe", "wsl.exe")
    assert launch_payload(escaped)[0] == quoted_cwd
    assert quoted_cwd not in " ".join(escaped)
    assert '"' not in escaped[-1] and ";" not in escaped[-1]
    assert "--no-daemon" not in yolo_command
    line = json.dumps(
        {"type": "event_msg", "payload": {"type": "user_message", "message": "task42 ready"}},
        separators=(",", ":"),
    )
    assert searchable_message(line.encode()) == "task42 ready"
    assert searchable_message(b'{"type":"response_item","message":"task 42"}') is None
    for role, kind in (("user", "input_text"), ("assistant", "output_text")):
        message = {"type": "response_item", "payload": {
            "type": "message", "role": role,
            "content": [{"type": kind, "text": "crash context"}, {"type": "input_image"}],
        }}
        assert searchable_message(json.dumps(message).encode()) == "crash context"
    message["payload"]["role"] = "developer"
    assert searchable_message(json.dumps(message).encode()) is None
    message["payload"].update(type="agent_message", author="/root/helper", recipient="/root")
    assert searchable_message(json.dumps(message).encode()) is None
    assert searchable_message(b'{"type":"event_msg","payload":{"type":"agent_message"') is None
    tool_event = json.dumps({
        "timestamp": "2026-01-01T00:00:00Z", "ordinal": 1, "type": "event_msg",
        "payload": {"type": "item_completed", "item": message["payload"]},
    }, separators=(",", ":")).encode()
    with mock.patch.object(json, "loads") as parse:
        assert searchable_message(tool_event) is None
        assert searchable_message(
            b'{"timestamp":"2026-01-01T00:00:00Z","type":"compacted","payload":{"message":"crash"}}'
        ) is None
        parse.assert_not_called()
    assert "task42" in context_excerpt("before task42 after", ["task", "42"])
    with tempfile.TemporaryDirectory() as directory:
        search_home = Path(directory)
        history = search_home / ".codex" / "sessions" / "2000" / "01" / "01"
        history.mkdir(parents=True)
        for index in range(14):
            search_id = f"00000000-0000-0000-0000-{index:012d}"
            metadata = {"payload": {
                "id": search_id, "cwd": "/tmp/work", "originator": "codex-tui",
                "source": {"subagent": {}} if index == 13 else "cli",
            }}
            event = {
                "type": "response_item" if index == 12 else "event_msg",
                "payload": {"type": "user_message",
                            "message": "crash " * (100 if index == 0 else 1)},
            }
            if index == 11:
                event = {"type": "response_item", "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "CRASH context"}],
                }}
            path = history / f"{search_id}.jsonl"
            path.write_text(
                json.dumps(metadata) + "\n"
                + json.dumps(event, separators=(",", ":")) + "\n", encoding="utf-8",
            )
            updated_at = 1_700_000_000 + index * 60
            os.utime(path, (updated_at, updated_at))
        with mock.patch.object(Path, "home", return_value=search_home):
            with mock.patch(
                f"{__name__}.session_from_rollout", wraps=session_from_rollout
            ) as read_session:
                results = search_wsl_sessions("CRASH")
                assert read_session.call_count == 12
            assert [item["id"] for item in results] == [
                f"00000000-0000-0000-0000-{index:012d}" for index in range(11, 1, -1)
            ]
            assert results[0]["updated_at"] == 1_700_000_000 + 11 * 60
            assert search_wsl_sessions("no such query") == []
            assert search_wsl_sessions("crashes") == []
            assert search_wsl_sessions("crash crash") == results
        token_rollout = history / "tokens.jsonl"
        token_rollout.write_text(json.dumps({"payload": {
            "id": session_id, "cwd": "/tmp/work", "source": "cli",
        }}) + "\n" + json.dumps({"type": "event_msg", "payload": {
            "type": "user_message", "message": "PR253 árvíz Straße crash_report",
        }}) + "\n", encoding="utf-8")
        with mock.patch.object(Path, "home", return_value=search_home):
            for query in ("PR 253", "pr253", "ÁRVÍZ", "strasse", "crash report"):
                assert search_wsl_sessions(query)[0]["id"] == session_id
            assert search_wsl_sessions("PR25") == []
            assert search_wsl_sessions("árví") == []

    # These checks use only temporary state; no real windows or processes are touched.
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        destination = root / "sessions.json"
        live = [{**session, "pid": 11, "process_start": "boot:11"},
                {**session, "pid": 22, "process_start": "boot:22"},
                {**other, "pid": 33, "process_start": "boot:33"}]
        saved = snapshot_sessions(live)
        assert [item["id"] for item in saved] == [session_id, other["id"]]
        with mock.patch(f"{__name__}.state_file", return_value=destination), \
                mock.patch(f"{__name__}.discover_wsl_sessions", return_value=live), \
                mock.patch(f"{__name__}.discover_from_windows", return_value=live), \
                mock.patch("builtins.print"):
            write_manifest(saved, stop_generation="old")
            before = destination.read_bytes()
            assert save_sessions(True, True) == 0
            assert destination.read_bytes() == before
            with mock.patch(f"{__name__}.terminate_sessions") as terminate:
                assert save_sessions(False, True) == 0
                terminate.assert_called_once_with(live)
            assert load_manifest()["sessions"] == saved
            assert saved_generation() != "old"
            with mock.patch(f"{__name__}.discover_from_windows") as discover:
                watch_sessions("old")
                discover.assert_not_called()
            generation = saved_generation()
            with mock.patch(f"{__name__}.discover_from_windows", side_effect=[
                SessionDiscoveryError("temporarily unresolved"), [live[0]], [],
            ]) as discover, mock.patch.object(time, "sleep", side_effect=[None, None, KeyboardInterrupt]):
                watch_sessions(generation)
                assert discover.call_count == 3
                assert load_manifest()["sessions"] == snapshot_sessions([live[0]])
            before = destination.read_bytes()
            with mock.patch(f"{__name__}.discover_from_windows", side_effect=SessionDiscoveryError("metadata unavailable")), \
                    mock.patch(f"{__name__}.discover_wsl_sessions", side_effect=SessionDiscoveryError("metadata unavailable")):
                with check.assertRaises(SessionDiscoveryError):
                    save_sessions(False, False)
            assert destination.read_bytes() == before
            with mock.patch.object(Path, "replace", side_effect=OSError("replace failed")):
                with check.assertRaises(OSError):
                    write_manifest(saved)
            assert destination.read_bytes() == before
            assert {p.name for p in root.iterdir()} == {"sessions.json", "sessions.lock"}
        with check.assertRaises(SessionDiscoveryError):
            thread_metadata(root, [session_id], required=True)
        with closing(sqlite3.connect(root / "state_5.sqlite")) as connection, connection:
            connection.execute("CREATE TABLE threads (id TEXT, name TEXT, title TEXT, approval_mode TEXT, sandbox_policy TEXT)")
            connection.execute("INSERT INTO threads VALUES (?, NULL, 'saved', 'never', ?)",
                               (session_id, '{"type":"disabled"}'))
        assert thread_metadata(root, [session_id], required=True)[session_id] == ("saved", True)
        with check.assertRaises(SessionDiscoveryError):
            thread_metadata(root, [other["id"]], required=True)
        with closing(sqlite3.connect(root / "state_5.sqlite")) as connection, connection:
            connection.execute("UPDATE threads SET sandbox_policy = '{}'")
        with check.assertRaises(SessionDiscoveryError):
            thread_metadata(root, [session_id], required=True)

    with mock.patch(f"{__name__}.discover_from_windows", side_effect=[
        SessionDiscoveryError("starting"), [live[0]], [live[0], live[1]],
    ]) as discover, mock.patch.object(time, "sleep"):
        wait_for_sessions({session_id: {(11, "boot:11")}})
        assert discover.call_count == 3
    with mock.patch(f"{__name__}.discover_from_windows", return_value=[live[0]]), \
            mock.patch.object(time, "sleep"), \
            mock.patch.object(time, "monotonic", side_effect=[0, 0, 100]):
        with check.assertRaises(SessionDiscoveryError):
            wait_for_sessions({other["id"]: set()})

    # Use the actual restore path with staged readiness, but no Terminal launch.
    windows_os = mock.Mock(wraps=os)
    windows_os.name = "nt"
    with mock.patch(f"{__name__}.os", windows_os), \
            mock.patch(f"{__name__}.state_lock", nullcontext), \
            mock.patch(f"{__name__}.load_manifest", return_value={"sessions": saved}), \
            mock.patch(f"{__name__}.windows_command", side_effect=lambda name: name), \
            mock.patch(f"{__name__}.run_wsl_command", return_value=mock.Mock(returncode=0)), \
            mock.patch(f"{__name__}.wsl_directory_exists", return_value=True), \
            mock.patch(f"{__name__}.discover_from_windows", side_effect=[
                [], SessionDiscoveryError("starting"), [live[0]],
                [live[0]], [live[0], live[2]], [live[0], live[2]],
            ]), mock.patch.object(subprocess, "Popen") as launch, \
            mock.patch(f"{__name__}.write_manifest") as write, \
            mock.patch(f"{__name__}.watch_sessions") as watch, \
            mock.patch.object(time, "sleep"), mock.patch("builtins.print"):
        assert restore_sessions(False) == 0
        assert launch.call_count == 2
        assert launch.call_args_list[0].args[0][2] == launch.call_args_list[1].args[0][2]
        write.assert_called_once()
        assert write.call_args.args[0] == saved
        watch.assert_called_once_with(write.call_args.kwargs["stop_generation"])
    with mock.patch(f"{__name__}.windows_command", return_value="wsl.exe"), \
            mock.patch(f"{__name__}.run_wsl_command", return_value=mock.Mock(returncode=0, stdout="[]")) as run:
        assert run_in_wsl("_search", "crash") == "[]"
        assert run.call_args.kwargs["timeout"] == SEARCH_TIMEOUT_SECONDS
        run_in_wsl("_discover")
        assert run.call_args.kwargs["timeout"] == WSL_TIMEOUT_SECONDS

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        native_read_text = Path.read_text
        processes = [Path("/proc/101"), Path("/proc/202")]

        def proc_text(path, *args, **kwargs):
            if path == Path("/proc/sys/kernel/random/boot_id"):
                return "boot"
            if path == Path("/proc/uptime"):
                return "1000 0"
            if path.name == "stat" and path.parent in processes:
                return "1 (codex) " + " ".join(["S"] + ["0"] * 18 + ["100"])
            return native_read_text(path, *args, **kwargs)

        def proc_link(path):
            return {"exe": "/usr/bin/codex", "cwd": "/tmp/work", "0": "/dev/pts/9"}[path.name]

        with mock.patch.object(Path, "home", return_value=root), \
                mock.patch.object(Path, "iterdir", lambda path: iter(processes if path == Path("/proc") else [])), \
                mock.patch.object(Path, "read_text", proc_text), \
                mock.patch.object(Path, "read_bytes", return_value=f"codex\0resume\0{session_id}\0".encode()), \
                mock.patch.object(os, "readlink", side_effect=proc_link), \
                mock.patch.object(os, "sysconf", return_value=100, create=True), \
                mock.patch(f"{__name__}.thread_rollouts", return_value={session_id: (root / "rollout.jsonl", "/tmp/work")}), \
                mock.patch(f"{__name__}.session_from_rollout", side_effect=lambda path, pid: {**session, "pid": pid}), \
                mock.patch(f"{__name__}.thread_metadata", return_value={session_id: ("test", True)}):
            discovered = discover_wsl_sessions()
            assert [item["pid"] for item in discovered] == [101, 202]
            assert all(item["process_start"] == "boot:100" and item["yolo"] for item in discovered)
            assert len(load_manifest(root / ".codex" / "session-ritual-processes.json")["sessions"]) == 2
            def vanished_directory(path):
                if path == Path("/proc"):
                    yield from processes
                else:
                    raise FileNotFoundError("process exited during FD enumeration")
            with mock.patch.object(Path, "iterdir", vanished_directory):
                assert discover_wsl_sessions() == []

    with mock.patch(f"{__name__}.state_lock", nullcontext), \
            mock.patch(f"{__name__}.saved_generation", return_value="test"), \
            mock.patch(f"{__name__}.windows_command", return_value="wsl.exe"), \
            mock.patch(f"{__name__}.run_wsl_command", side_effect=[
                mock.Mock(returncode=0), mock.Mock(returncode=3, stderr="temporary discovery I/O failure"),
                mock.Mock(returncode=0), mock.Mock(returncode=0, stdout=json.dumps([session])),
            ]), mock.patch(f"{__name__}.write_manifest") as write, \
            mock.patch.object(time, "sleep", side_effect=[None, KeyboardInterrupt]), \
            mock.patch("builtins.print"):
        watch_sessions("test")
        write.assert_called_once_with(snapshot_sessions([session]), stop_generation="test")

    if os.name != "nt":
        with mock.patch.object(os, "pidfd_open", return_value=99) as open_pid, \
                mock.patch.object(os, "readlink", return_value="/usr/bin/codex"), \
                mock.patch(f"{__name__}.process_identity", return_value="boot:11"), \
                mock.patch.object(signal, "pidfd_send_signal") as send, \
                mock.patch.object(os, "close") as close_pid:
            terminate_sessions([live[0]])
            send.assert_called_once_with(99, signal.SIGTERM)
            close_pid.assert_called_once_with(99)
            send.reset_mock()
            with check.assertRaises(SessionDiscoveryError):
                terminate_sessions([{**live[0], "process_start": "old:11"}])
            send.assert_not_called()
            assert open_pid.call_count == 2
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
    restore.add_argument("--reopen", action="store_true", help="also reopen sessions with an existing interactive terminal")
    find = subparsers.add_parser("find", help="find likely sessions by conversation text")
    find.add_argument("query", nargs="+")
    subparsers.add_parser("self-test")
    subparsers.add_parser("_discover", help=argparse.SUPPRESS)
    search = subparsers.add_parser("_search", help=argparse.SUPPRESS)
    search.add_argument("query")
    terminate = subparsers.add_parser("_terminate", help=argparse.SUPPRESS)
    terminate.add_argument("targets")
    args = parser.parse_args()

    if args.command == "save":
        return save_sessions(args.dry_run, args.kill)
    if args.command == "restore":
        return restore_sessions(args.dry_run, args.reopen)
    if args.command == "find":
        return find_sessions(" ".join(args.query))
    if args.command == "_discover":
        print(json.dumps(discover_wsl_sessions(), ensure_ascii=False))
        return 0
    if args.command == "_search":
        print(json.dumps(search_wsl_sessions(args.query), ensure_ascii=False))
        return 0
    if args.command == "_terminate":
        terminate_sessions(json.loads(args.targets))
        return 0
    return self_test()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SessionDiscoveryError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(3) from None
