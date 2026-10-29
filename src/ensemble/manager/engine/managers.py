"""Every manager under a root, with its holder state, session record and entries, read without
a lease.
"""

import json
import os
import re

from ensemble.manager.engine.roster import Roster
from ensemble.manager.engine.types import (
    HolderState,
    Manager,
    RosterError,
    SessionRecord,
    Unread,
    Unreadable,
)
from ensemble.utils import read_regular

RECORD = "session.json"
RECORD_MAX = 1 << 16
NAME = re.compile(r"[A-Za-z0-9_-]+")
CLIENT = re.compile(r"[a-z][a-z0-9-]*")


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


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read(directory: str) -> Manager | Unreadable:
    name = os.path.basename(directory)
    try:
        holder, entries = Roster(directory).snapshot()
    except (RosterError, OSError) as error:
        return Unreadable(name, str(error))
    try:
        record = read_record(os.path.join(directory, RECORD))
    except (ValueError, OSError) as error:
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
