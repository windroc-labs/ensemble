"""SessionStart hook: in a manager session under `ensemble-crew hold`, for the
startup, resume, compact and clear sources, prints the re-arm prompt as
additionalContext and, given `--client KIND --client-pid PID` and stdin `session_id` and `cwd`,
replaces the manager's ~/.ensemble/managers/NAME/session.json (a bad field or write skips only
that). `--hook-daemon WORD`: while PID's argv[1] is WORD, its parent replaces it (none: no
record).
Anything else prints nothing. Always exits 0.
A manager session: a valid ENSEMBLE_MANAGER, ENSEMBLE_HOLDER_ID, no ENSEMBLE_AGENT_SESSION, and
ENSEMBLE_HOLDER_PID (the hold) among this process's ancestors: the variables alone leak to
later tmux windows. Standalone: the standard library only.
`--mode manual` replaces the monitoring tail with turn-based reads and a notifying-tool fallback.
"""

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from functools import cached_property

SOURCES = frozenset({"startup", "resume", "compact", "clear"})
MANAGER_NAME = re.compile(r"[A-Za-z0-9_-]+")  # ensemble-crew's own rule (cli.py _roster)
PID = re.compile(r"[1-9][0-9]*")
CLIENT = re.compile(r"[a-z][a-z0-9-]*")
OPTIONS = frozenset({"--mode", "--client", "--client-pid", "--hook-daemon"})
MANAGERS = "~/.ensemble/managers"
PS = (
    "ps",
    "-A",
    "-ww",
    "-o",
    "pid=",
    "-o",
    "ppid=",
    "-o",
    "args=",
)  # one -o each: text after = is a header

PROMPT = """\
ensemble: SessionStart `{source}` in manager `{name}`'s session (under \
`ensemble-crew hold`). Your crew event monitor may have stopped. Re-arm it now, before other \
work. These steps are complete without /ensemble:manage; they follow its Start step 3, Events \
and Recover.

1. Stop your own listener if one is still running (an event listener or background task running \
`ensemble-crew next-event`).
2. Run `ensemble-crew attach`. Its last line `N=<N> GEN=<GEN>` is the handled cursor; the \
backlog is every event line after N.
   Lease error (`stale holder generation`, `holder no longer alive`, or another holder is \
active): follow /ensemble:manage › Recover; the error's `crew:` lines give the exact commands. \
Another holder active: tell the user which. Never run `hold --force` yourself.
3. Read the backlog with `ensemble-crew pending --content`. In line order: an event whose \
durable result (outcome, sent message, issue state or existing pull request) shows it already \
handled, ack at once; otherwise handle it, then ack. Ack: \
`ensemble-crew ack --through <line> --gen <GEN>`, GEN from your most recent `attach`. Leave an \
unresolved event and everything after it pending.
4. Run `ensemble-crew attach` again.
{monitoring}"""

AUTOMATIC = """\
5. Arm only with the `monitor:` command this second `attach` printed (its N and GEN). Never arm \
from a line number you remember or from the last line printed.
   Monitor may be a deferred tool: if its schema is not loaded, load it first (ToolSearch \
`select:Monitor`). Run the `monitor:` command with Monitor, `timeout_ms` 1800000. Never run \
`--follow` with Bash `run_in_background`.
6. When that Monitor ends or expires, repeat from step 1.
"""

MANUAL = """\
5. Manual monitoring; no idle notifications. Read: ensemble-crew pending --content. \
Recovery: ensemble-crew attach. Run `ensemble-crew pending --content` at the beginning of every \
active turn before status reports or new launches. Handle and ack as in step 3.
6. Only if a background tool's runtime contract guarantees a notification to this conversation, \
run the `next-event:` command this second `attach` printed (its N and GEN) as that tool's task, \
without `--follow`. After handling and acking each event, run `ensemble-crew attach` again and \
use its newly printed `next-event:` command. Never arm from a line number you remember or from \
the last line printed. On reader exit, notification failure or expiration, \
repeat from step 1; without a notifying tool, keep Manual monitoring.
"""


class Processes:
    """Parents and argvs: /proc where it exists, else one ps table."""

    def __init__(self) -> None:
        self.proc = os.path.isdir("/proc/self")

    @cached_property
    def table(self) -> dict[int, tuple[int, list[str]]]:
        try:
            done = subprocess.run(PS, capture_output=True, timeout=2, check=False)
            if done.returncode != 0:
                return {}
            rows = [line.split() for line in done.stdout.decode(errors="replace").splitlines()]
            return {int(row[0]): (int(row[1]), row[2:]) for row in rows}
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return {}

    def parent(self, pid: int) -> int | None:
        if not self.proc:
            return self.table[pid][0] if pid in self.table else None
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                return int(f.read().rpartition(b")")[2].split()[1])
        except (OSError, ValueError, IndexError):
            return None

    def argv(self, pid: int) -> list[str] | None:
        if not self.proc:
            return self.table[pid][1] if pid in self.table else None
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read().decode(errors="replace").split("\0")[:-1]
        except OSError:
            return None

    def ancestors(self, pid: int) -> set[int]:
        found: set[int] = set()
        while (pid := self.parent(pid) or 0) > 1 and pid not in found:
            found.add(pid)
        return found

    def client(self, pid: int, daemon: str | None) -> int | None:
        seen: set[int] = set()
        while daemon is not None and (self.argv(pid) or [])[1:2] == [daemon]:
            seen.add(pid)
            pid = self.parent(pid) or 0
            if pid <= 1 or pid in seen or self.argv(pid) is None:
                return None
        return pid


def manager(environ: Mapping[str, str], processes: Processes) -> str | None:
    name = environ.get("ENSEMBLE_MANAGER", "")
    hold = environ.get("ENSEMBLE_HOLDER_PID", "")
    if not MANAGER_NAME.fullmatch(name) or not environ.get("ENSEMBLE_HOLDER_ID"):
        return None
    if "ENSEMBLE_AGENT_SESSION" in environ or not PID.fullmatch(hold):
        return None
    return name if int(hold) in processes.ancestors(os.getpid()) else None


def prompt(name: str, source: str, *, manual: bool = False) -> str:
    return PROMPT.format(name=name, source=source, monitoring=MANUAL if manual else AUTOMATIC)


def options(argv: tuple[str, ...]) -> dict[str, str] | None:
    keys = argv[::2]
    if len(argv) % 2 or len(set(keys)) != len(keys) or not OPTIONS.issuperset(keys):
        return None
    found = dict(zip(keys, argv[1::2], strict=True))
    return found if found.get("--mode", "manual") == "manual" else None


def record(found: Mapping[str, str], event: Mapping[str, object]) -> dict[str, object] | None:
    client, pid = found.get("--client", ""), found.get("--client-pid", "")
    sid, cwd = event.get("session_id"), event.get("cwd")
    if not (CLIENT.fullmatch(client) and PID.fullmatch(pid)):
        return None
    if not (isinstance(sid, str) and sid and isinstance(cwd, str) and cwd.startswith("/")):
        return None
    return {"client": client, "sid": sid, "pid": int(pid), "cwd": cwd}


def save(name: str, entry: Mapping[str, object]) -> None:
    directory = os.path.join(os.path.expanduser(MANAGERS), name)
    try:
        fd, temp = tempfile.mkstemp(dir=directory, prefix=".session.", suffix=".tmp")
    except OSError:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        os.replace(temp, os.path.join(directory, "session.json"))
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temp)


def main(argv: tuple[str, ...] = ()) -> int:
    found = options(argv)
    if found is None:
        return 0
    processes = Processes()
    name = manager(os.environ, processes)
    if name is None:
        return 0
    # Raw fds: a closed or broken stdout then fails here, not in a flush at exit.
    try:
        chunks = []
        while chunk := os.read(0, 65536):
            chunks.append(chunk)
        event = json.loads(b"".join(chunks).decode("utf-8"))
        source = event.get("source") if isinstance(event, dict) else None
        if not isinstance(source, str) or source not in SOURCES:
            return 0
        if (entry := record(found, event)) is not None:
            pid = processes.client(int(found["--client-pid"]), found.get("--hook-daemon"))
            if pid is not None:
                save(name, {**entry, "pid": pid})
        context = {
            "hookEventName": "SessionStart",
            "additionalContext": prompt(name, source, manual="--mode" in found),
        }
        data = (json.dumps({"hookSpecificOutput": context}) + "\n").encode()
        while data:
            data = data[os.write(1, data) :]
    except (OSError, ValueError, RecursionError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(tuple(sys.argv[1:])))
