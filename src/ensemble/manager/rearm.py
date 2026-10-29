"""SessionStart re-arm and manager session recording. Standalone, stdlib only.
Direct clients use the current hold's environment and ancestry. `--binding lookup` resolves
stdin session_id through live-client.json, independently of a shared server's environment.
`--hook-daemon WORD` permits direct-client fallback only when WORD is absent from its argv.
`--binding context` returns the caller's live hold; `--binding record` binds a native session
and TUI process under that hold before the first turn (failure: exit 2).
Hooks print additionalContext for startup, resume, compact and clear; otherwise nothing.
Hooks always exit 0. `--mode manual` selects turn-based monitoring.
"""

import contextlib
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from functools import cached_property

SOURCES = frozenset({"startup", "resume", "compact", "clear"})
MANAGER_NAME = re.compile(r"[A-Za-z0-9_-]+")  # ensemble-crew's own rule (cli.py _roster)
PID = re.compile(r"[1-9][0-9]*")
CLIENT = re.compile(r"[a-z][a-z0-9-]*")
OPTIONS = frozenset({"--mode", "--client", "--client-pid", "--hook-daemon", "--binding"})
MANAGERS = "~/.ensemble/managers"
BINDING = "live-client.json"
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

    def birth(self, pid: int) -> str | None:
        try:
            if self.proc:
                with open(f"/proc/{pid}/stat", "rb") as f:
                    fields = f.read().rpartition(b")")[2].split()
                return None if fields[0] == b"Z" else fields[19].decode()
            done = subprocess.run(
                ("ps", "-p", str(pid), "-o", "lstart="),
                capture_output=True,
                timeout=2,
                check=False,
                env={**os.environ, "LC_ALL": "C"},
            )
            return done.stdout.decode().strip() or None if done.returncode == 0 else None
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return None


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
    if found.get("--mode", "manual") != "manual":
        return None
    return found if found.get("--binding", "lookup") in {"lookup", "context", "record"} else None


def record(found: Mapping[str, str], event: Mapping[str, object]) -> dict[str, object] | None:
    client, pid = found.get("--client", ""), found.get("--client-pid", "")
    sid, cwd = event.get("session_id"), event.get("cwd")
    if not (CLIENT.fullmatch(client) and PID.fullmatch(pid)):
        return None
    if not (isinstance(sid, str) and sid and isinstance(cwd, str) and cwd.startswith("/")):
        return None
    return {"client": client, "sid": sid, "pid": int(pid), "cwd": cwd}


def save(name: str, entry: Mapping[str, object], filename: str = "session.json") -> bool:
    directory = os.path.join(os.path.expanduser(MANAGERS), name)
    try:
        fd, temp = tempfile.mkstemp(dir=directory, prefix=".session.", suffix=".tmp")
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        os.replace(temp, os.path.join(directory, filename))
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        return False
    else:
        return True


def read_object(path: str) -> dict:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise ValueError("not a regular file")
        data = json.loads(f.read(65537))
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return data


@contextlib.contextmanager
def lease_guard(name: str, binding: Mapping[str, object]):
    directory = os.path.join(os.path.expanduser(MANAGERS), name)
    with contextlib.ExitStack() as stack:
        valid = False
        try:
            lock = stack.enter_context(open(os.path.join(directory, "roster.lock"), "rb"))
            fcntl.flock(lock, fcntl.LOCK_SH)
            data = read_object(os.path.join(directory, "roster.json"))
            holder = data.get("holder")
            if (
                isinstance(holder, dict)
                and holder.get("id") == binding.get("holder_id")
                and data.get("lease_gen") == binding.get("holder_generation")
            ):
                held = stack.enter_context(open(os.path.join(directory, "holder.lock"), "rb"))
                try:
                    fcntl.flock(held, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    valid = True
        except (OSError, ValueError, RecursionError):
            pass
        yield valid


def live(binding: dict, processes: Processes) -> bool:
    pid, hold, birth = binding.get("pid"), binding.get("holder_pid"), binding.get("birth")
    return (
        type(pid) is int
        and pid > 1
        and type(hold) is int
        and hold > 1
        and isinstance(birth, str)
        and bool(birth)
        and processes.birth(pid) == birth
        and hold in processes.ancestors(pid)
    )


def lookup(client: str, sid: str, processes: Processes) -> tuple[str, dict] | None:
    matches = []
    try:
        with os.scandir(os.path.expanduser(MANAGERS)) as directories:
            names = [
                d.name
                for d in directories
                if MANAGER_NAME.fullmatch(d.name) and d.is_dir(follow_symlinks=False)
            ]
    except OSError:
        return None
    for name in names:
        try:
            binding = read_object(os.path.join(os.path.expanduser(MANAGERS), name, BINDING))
            if (
                binding.get("client") != client
                or binding.get("sid") != sid
                or not live(binding, processes)
            ):
                continue
            with lease_guard(name, binding) as valid:
                if valid:
                    matches.append((name, binding))
        except (OSError, ValueError, RecursionError):
            continue
    return matches[0] if len(matches) == 1 else None


def identity(environ: Mapping[str, str]) -> dict:
    generation = environ.get("ENSEMBLE_HOLDER_GENERATION", "")
    if not PID.fullmatch(generation):
        return {}
    return {
        "holder_pid": int(environ["ENSEMBLE_HOLDER_PID"]),
        "holder_id": environ["ENSEMBLE_HOLDER_ID"],
        "holder_generation": int(generation),
    }


def emit(value: object) -> None:
    data = (json.dumps(value) + "\n").encode()
    while data:
        data = data[os.write(1, data) :]


def main(argv: tuple[str, ...] = ()) -> int:
    found = options(argv)
    if found is None:
        return 0
    processes = Processes()
    action = found.get("--binding") or ("lookup" if "--hook-daemon" in found else None)
    name = None if action == "lookup" else manager(os.environ, processes)
    if action != "lookup" and name is None:
        return 0
    # Raw fds: a closed or broken stdout then fails here, not in a flush at exit.
    try:
        if action == "context":
            assert name is not None
            binding = identity(os.environ)
            with lease_guard(name, binding) as valid:
                if valid:
                    emit({"name": name, **binding})
            return 0
        chunks = []
        while chunk := os.read(0, 65536):
            chunks.append(chunk)
        event = json.loads(b"".join(chunks).decode("utf-8"))
        if action == "record":
            assert name is not None
            entry = record(found, event) if isinstance(event, dict) else None
            if entry is None:
                return 2
            binding = {
                **entry,
                **identity(os.environ),
                "birth": processes.birth(int(found["--client-pid"])),
            }
            if not live(binding, processes):
                return 2
            other = lookup(str(entry["client"]), str(entry["sid"]), processes)
            if other is not None and other[0] != name:
                return 2
            with lease_guard(name, binding) as valid:
                if not valid or not save(name, binding, BINDING) or not save(name, entry):
                    return 2
            emit({"recorded": True})
            return 0
        source = event.get("source") if isinstance(event, dict) else None
        if not isinstance(source, str) or source not in SOURCES:
            return 0
        if action == "lookup":
            sid = event.get("session_id")
            match = (
                lookup(found.get("--client", ""), sid, processes) if isinstance(sid, str) else None
            )
            if match is None:
                pid = found.get("--client-pid", "")
                args = processes.argv(int(pid)) if PID.fullmatch(pid) else None
                daemon = found.get("--hook-daemon")
                if not args or not daemon or daemon in args[1:] or "--managed-daemon" in args:
                    return 0
                name = manager(os.environ, processes)
                if name is None:
                    return 0
                bound = found
            else:
                name, binding = match
                bound = {**found, "--client-pid": str(binding["pid"])}
            if (entry := record(bound, event)) is not None:
                save(name, entry)
        elif (entry := record(found, event)) is not None:
            assert name is not None
            save(name, entry)
        assert name is not None
        context = {
            "hookEventName": "SessionStart",
            "additionalContext": prompt(name, source, manual="--mode" in found),
        }
        emit({"hookSpecificOutput": context})
    except (OSError, ValueError, RecursionError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(tuple(sys.argv[1:])))
