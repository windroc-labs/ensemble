"""Shared manager session record fixtures for its writer (rearm.py) and reader (ensemble list)
tests. Standard library only: the integration job has no jsonschema.
"""

import json
import os

SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "src",
    "ensemble",
    "manager",
    "engine",
    "session.schema.json",
)

# Client → a record that client's hook writes.
VALID: dict[str, dict] = {
    "claude": {
        "client": "claude",
        "sid": "b9a9aca2-5641-404a-a59c-c74f236828f7",
        "pid": 4101,
        "cwd": "/home/user/project",
    },
    "codex": {
        "client": "codex",
        "sid": "01a18587-2a04-7791-a81e-78f2dd74b1cc",
        "pid": 4202,
        "cwd": "/home/user/project",
    },
    "opencode": {
        "client": "opencode",
        "sid": "ses_e7a77f660ffe14LXKCMWo1rFyg",
        "pid": 4303,
        "cwd": "/home/user/other project",
    },
}

_RECORD = VALID["claude"]


def _without(field: str) -> str:
    return json.dumps({k: v for k, v in _RECORD.items() if k != field})


def _with(**fields) -> str:
    return json.dumps({**_RECORD, **fields})


# Case → session.json text a reader must report as an error.
INVALID: dict[str, str] = {
    **{f"missing {field}": _without(field) for field in _RECORD},
    "extra field": _with(source="startup"),
    "pid 0": _with(pid=0),
    "pid -1": _with(pid=-1),
    "pid string": _with(pid="4101"),
    "pid fraction": _with(pid=1.5),
    "pid boolean": _with(pid=True),
    "relative cwd": _with(cwd="home/user/project"),
    "empty cwd": _with(cwd=""),
    "client uppercase": _with(client="Claude"),
    "client empty": _with(client=""),
    "client leading digit": _with(client="1x"),
    "empty sid": _with(sid=""),
    "array": json.dumps([_RECORD]),
    "not JSON": '{"client": "claude", "sid": ',
}
