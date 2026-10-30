"""Every manager under a root, with its holder state, session record and entries, read without
a lease, and the plan for selecting one.
"""

import json
import os
import re
import subprocess
from dataclasses import asdict

from ensemble.manager.engine.roster import Roster
from ensemble.manager.engine.types import (
    HolderState,
    Manager,
    Plan,
    Refused,
    RosterError,
    SessionRecord,
    Unread,
    Unreadable,
    Verb,
)
from ensemble.utils import STATE, LayerCommand, one_line, printable, read_regular, save

ROOT = f"{STATE}/managers"
RECORD = "session.json"
RECORD_MAX = 1 << 16
NAME = re.compile(r"[A-Za-z0-9_-]+")
CLIENT = re.compile(r"[a-z][a-z0-9-]*")
RESUME_TIMEOUT = 30


def read_record(path: str) -> SessionRecord | None:
    """None when absent; ValueError naming the path and the bad field otherwise."""
    if not os.path.lexists(path):
        return None
    raw = read_regular(path, limit=RECORD_MAX + 1)
    if raw is None or len(raw) > RECORD_MAX:
        raise ValueError(f"{path}: not a readable regular file")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError(f"{path}: not JSON") from None
    if not isinstance(data, dict) or set(data) != {"client", "sid", "pid", "cwd"}:
        raise ValueError(f"{path}: want exactly client, sid, pid and cwd")
    client, sid, pid, cwd = data["client"], data["sid"], data["pid"], data["cwd"]
    if not isinstance(client, str) or not CLIENT.fullmatch(client):
        raise ValueError(f"{path}: client: want a lowercase client kind")
    if not isinstance(sid, str) or not sid:
        raise ValueError(f"{path}: sid: want a nonempty string")
    if type(pid) is not int or pid < 1:
        raise ValueError(f"{path}: pid: want a positive integer")
    if not isinstance(cwd, str) or not cwd.startswith("/"):
        raise ValueError(f"{path}: cwd: want an absolute path")
    return SessionRecord(client, sid, pid, cwd)


def write_record(directory: str, record: SessionRecord) -> None:
    save(os.path.join(directory, RECORD), json.dumps(asdict(record)) + "\n")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):  # OverflowError: a pid no process can have
        return False
    except PermissionError:
        return True
    return True


def read(directory: str) -> Manager | Unreadable:
    name = os.path.basename(directory)
    try:
        holder, entries = Roster(directory).snapshot()
    except RosterError as error:
        return Unreadable(name, str(error))
    try:
        record = read_record(os.path.join(directory, RECORD))
    except ValueError as error:
        record = Unread(str(error))
    if holder is None:
        state = HolderState.NOT_HELD
    elif isinstance(record, SessionRecord) and alive(record.pid):
        state = HolderState.CLIENT_RUNNING
    else:
        state = HolderState.HOLD_ONLY
    return Manager(name, state, holder, record, entries)


def managers(root: str) -> list[Manager | Unreadable]:
    """Every manager directory under root, by name; [] when root is absent."""
    try:
        with os.scandir(root) as items:
            names = sorted(
                item.name
                for item in items
                if NAME.fullmatch(item.name) and item.is_dir(follow_symlinks=False)
            )
    except FileNotFoundError:
        return []
    return [read(os.path.join(root, name)) for name in names]


def plan(manager: Manager | Unreadable, *, live: bool) -> Plan | Refused:
    """live: whether its session record's client process runs."""
    if isinstance(manager, Unreadable):
        return Refused(manager.error)
    if isinstance(manager.entries, Unread):
        return Refused(manager.entries.error)
    holder, record = manager.holder, manager.record
    if isinstance(holder, Unread):
        return Refused(holder.error)
    if holder is not None and holder.session != manager.name:
        return Refused(f"held by {holder.label}")
    recorded = isinstance(record, SessionRecord)
    if manager.state == HolderState.CLIENT_RUNNING:
        verb = Verb.ATTACH
    elif manager.state == HolderState.HOLD_ONLY:
        verb = Verb.RESUME_HELD if recorded else Verb.ATTACH
    elif isinstance(record, SessionRecord) and live:
        return Refused(f"its client {record.pid} runs without a holder; exit it first")
    else:
        verb = Verb.RESUME_NEW if recorded else Verb.OPEN
    return Plan(verb, manager.name)


def resume_target(directory: str) -> tuple[SessionRecord, tuple[str, ...]]:
    """The session record and the command resuming its client session in its cwd."""
    try:
        record = read_record(os.path.join(directory, RECORD))
    except ValueError as error:
        raise RosterError(str(error)) from None
    if record is None:
        raise RosterError(f"{directory}: no session record to resume")
    if alive(record.pid):
        raise RosterError(f"its client {record.pid} still runs; exit it first")
    if not os.path.isdir(record.cwd):
        raise RosterError(f"recorded cwd {record.cwd!r} is not a directory")
    command = (LayerCommand.SESSION, "resume-argv", record.client, "--", record.sid)
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=RESUME_TIMEOUT,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        reason = one_line(error)
    else:
        try:
            argv = json.loads(done.stdout) if done.returncode == 0 else None
        except ValueError:
            argv = None
        if isinstance(argv, list) and argv and all(isinstance(w, str) and w for w in argv):
            return record, tuple(argv)
        reason = (
            (one_line(done.stderr) or f"exit {done.returncode}")
            if done.returncode
            else ("want a JSON array of words")
        )
    raise RosterError(f"cannot resume its {record.client} session: {printable(reason)}")
