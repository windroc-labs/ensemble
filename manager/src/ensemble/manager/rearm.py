"""The plugin's SessionStart command hook (manager/hooks/hooks.json): in a manager session under
`ensemble-crew hold`, prints the event-monitor re-arm prompt as additionalContext for the
startup, resume, compact and clear sources; anything else prints nothing. Always exits 0.
A manager session: a valid ENSEMBLE_MANAGER, ENSEMBLE_HOLDER_ID, no ENSEMBLE_AGENT_SESSION, and
ENSEMBLE_HOLDER_PID, the hold's pid, among this process's ancestors. The variables alone leak (a
tmux server started under a hold gives them to every later window); the process tree does not.
Standalone: the standard library only, run by path.
`--mode manual` replaces the monitoring tail with turn-based reads and a notifying-tool fallback.
"""

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping

SOURCES = frozenset({"startup", "resume", "compact", "clear"})
MANAGER_NAME = re.compile(r"[A-Za-z0-9_-]+")  # ensemble-crew's own rule (cli.py _roster)
HOLDER_PID = re.compile(r"[1-9][0-9]*")
PS = ("ps", "-A", "-o", "pid=", "-o", "ppid=")  # one -o each: text after = is a header

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


def _proc_parent(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            return int(f.read().rpartition(b")")[2].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _ps_parents() -> dict[int, int]:
    try:
        done = subprocess.run(PS, capture_output=True, timeout=2, check=False)
        if done.returncode != 0:
            return {}
        rows = [line.split() for line in done.stdout.decode().splitlines()]
        return {int(pid): int(ppid) for pid, ppid in rows}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def ancestors(pid: int) -> set[int]:
    """pid's ancestors above it, by /proc where it exists (Linux), else one ps table."""
    parent: Callable[[int], int | None] = (
        _proc_parent if os.path.isdir("/proc/self") else _ps_parents().get
    )
    found: set[int] = set()
    while (pid := parent(pid) or 0) > 1 and pid not in found:
        found.add(pid)
    return found


def manager(environ: Mapping[str, str]) -> str | None:
    name = environ.get("ENSEMBLE_MANAGER", "")
    hold = environ.get("ENSEMBLE_HOLDER_PID", "")
    if not MANAGER_NAME.fullmatch(name) or not environ.get("ENSEMBLE_HOLDER_ID"):
        return None
    if "ENSEMBLE_AGENT_SESSION" in environ or not HOLDER_PID.fullmatch(hold):
        return None
    return name if int(hold) in ancestors(os.getpid()) else None


def prompt(name: str, source: str, *, manual: bool = False) -> str:
    return PROMPT.format(name=name, source=source, monitoring=MANUAL if manual else AUTOMATIC)


def main(argv: tuple[str, ...] = ()) -> int:
    if argv not in ((), ("--mode", "manual")):
        return 0
    name = manager(os.environ)
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
        context = {
            "hookEventName": "SessionStart",
            "additionalContext": prompt(name, source, manual=bool(argv)),
        }
        data = (json.dumps({"hookSpecificOutput": context}) + "\n").encode()
        while data:
            data = data[os.write(1, data) :]
    except (OSError, ValueError, RecursionError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(tuple(sys.argv[1:])))
