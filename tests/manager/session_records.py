"""Manager session record fixtures for rearm.py and ensemble list tests. Standard library only:
the integration job has no jsonschema.
"""

import json
import os
from dataclasses import dataclass

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


@dataclass(frozen=True)
class HookCall:
    """rearm.py's argv after its path, its stdin and the record it writes (None: none)."""

    argv: tuple[str, ...]
    stdin: dict
    record: dict | None


def _call(record: dict, mode: tuple[str, ...], **stdin) -> HookCall:
    event = {"session_id": record["sid"], "cwd": record["cwd"], "source": "startup", **stdin}
    argv = (*mode, "--client", record["client"], "--client-pid", str(record["pid"]))
    return HookCall(argv, event, record)


_MANUAL = ("--mode", "manual")

# Client → its SessionStart call: sh -c's "$PPID", or the in-client plugin's pid, is the client.
CALLS: dict[str, HookCall] = {
    "claude": _call(
        VALID["claude"],
        (),
        transcript_path="/home/user/.claude/projects/p/b9a9aca2.jsonl",
        hook_event_name="SessionStart",
    ),
    "codex": _call(
        VALID["codex"],
        _MANUAL,
        transcript_path="/home/user/.codex/sessions/rollout-01a18587.jsonl",
        hook_event_name="SessionStart",
        model="gpt-5",
        permission_mode="default",
    ),
    "opencode": _call(VALID["opencode"], _MANUAL, hook_event_name="SessionStart"),
}

_CLAUDE = CALLS["claude"]


def _rejected(argv: tuple[str, ...] = _CLAUDE.argv, **stdin) -> HookCall:
    event = {k: v for k, v in {**_CLAUDE.stdin, **stdin}.items() if v is not None}
    return HookCall(argv, event, None)


# Case → a call that writes no record; rearm.py's output and exit code are unaffected.
REJECTED: dict[str, HookCall] = {
    "no arguments": _rejected(()),
    "no --client": _rejected(("--client-pid", "4101")),
    "no --client-pid": _rejected(("--client", "claude")),
    "--client uppercase": _rejected(("--client", "Claude", "--client-pid", "4101")),
    "--client empty": _rejected(("--client", "", "--client-pid", "4101")),
    "--client-pid empty": _rejected(("--client", "claude", "--client-pid", "")),
    "--client-pid 0": _rejected(("--client", "claude", "--client-pid", "0")),
    "--client-pid negative": _rejected(("--client", "claude", "--client-pid", "-1")),
    "--client-pid not a number": _rejected(("--client", "claude", "--client-pid", "x")),
    "no session_id": _rejected(session_id=None),
    "no cwd": _rejected(cwd=None),
}
