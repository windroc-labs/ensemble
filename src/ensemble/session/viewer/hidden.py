"""Whether a tmux session is hidden, kept per session name as {sid, hidden} in DIR so it outlives
the tmux session a restart or resume rebuilds; an entry of another sid is another run's. Each write
drops the entries unwritten for STALE_AGE but those of `live` tmux sessions (None: drops none).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Collection

from ensemble.utils import STATE, drop_stale, read_regular, save

DIR = f"{STATE}/hidden"
LIMIT = 4096


def _path(name: str) -> str:
    return os.path.join(os.path.expanduser(DIR), f"{name}.json")


def _entry(name: str) -> dict | None:
    data = read_regular(_path(name), limit=LIMIT + 1)
    if data is None or len(data) > LIMIT:
        return None
    try:
        entry = json.loads(data)
    except ValueError:
        return None
    if not isinstance(entry, dict) or set(entry) != {"sid", "hidden"}:
        return None
    return entry if isinstance(entry["hidden"], bool) else None


def read(name: str, sid: str) -> bool:
    entry = _entry(name)
    return entry is not None and entry["sid"] == sid and entry["hidden"]


def write(name: str, sid: str, *, hidden: bool, live: Collection[str] | None) -> None:
    save(_path(name), json.dumps({"sid": sid, "hidden": hidden}) + "\n")
    if live is not None:
        for error in drop_stale(os.path.expanduser(DIR), keep={f"{n}.json" for n in live}):
            print(f"hidden: {error}", file=sys.stderr)


def start(name: str, sid: str, *, live: Collection[str] | None) -> None:
    """Marks sid hidden unless an entry of sid's exists: a restart or resume keeps its view."""
    entry = _entry(name)
    if entry is None or entry["sid"] != sid:
        write(name, sid, hidden=True, live=live)
