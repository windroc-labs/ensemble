"""TmuxCli: every tmux call through runner (subprocess.run's signature) and sleep. start and
detach hand a launch to a new pane (EXEC). Listings skip VIEW sessions.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ensemble.session.engine.status_log import SessionStatusLog
from ensemble.session.engine.types import (
    Launch,
    Liveness,
    PaneStatus,
    SessionError,
)
from ensemble.utils import events_file


class TuiError(Exception):
    pass


NAME = re.compile(r"[A-Za-z0-9_-]+")


PANE = re.compile(r"%[0-9]+")


ITERM_ID = re.compile(r"[A-Za-z0-9-]+")


ITERM_OPENER = re.compile(rf"[A-Za-z0-9]+:{ITERM_ID.pattern}")  # w0t0p0:<unique id>


OPENER = re.compile(rf"{NAME.pattern}|{ITERM_OPENER.pattern}")


SESSION_ID = re.compile(r"\$[0-9]+")


ABSENT = re.compile(
    r"can't find (session|pane).*|no such session: .*|no server running.*|"
    r"error connecting to .* \(No such file or directory\)",
    re.S,
)


CLIENTS = "#{client_activity} #{client_tty} #{pane_id} #{client_control_mode} #{socket_path}"


PANES = "#{pane_dead} #{pane_tty} #{pane_id}"


PROCESS_ID = re.compile(r"[0-9]+ \$[0-9]+ %[0-9]+ [0-9]+")
PROCESS_GONE = re.compile(r"[0-9]+ (\$[0-9]+)? (%[0-9]+ [0-9]*| )| *")


USER_OPTION = re.compile(r"@[A-Za-z0-9_-]+")


PAUSE = 0.5


# lose Ctrl, send nothing or differ between them


PROCESS = "#{pid} #{session_id} #{pane_id} #{pane_pid}"


DEAD = "#{pane_dead} #{pane_dead_status} #{pane_dead_signal}"


SESSIONS = "#{session_id}\t#{session_name}\t#{@opener}\t#{@pane}\t#{socket_path}"


POLL = 0.1


HANDOVER_TIMEOUT = 10


ERRPIPE = "errpipe"


DIED = "set-option @state dead"


VIEW = "@view"
VIEWLESS = ["-f", f"#{{==:#{{{VIEW}}},}}"]
VIEWS = ["-f", f"#{{!=:#{{{VIEW}}},}}"]


CLOCK = f'"$(date +{SessionStatusLog.CLOCK})"'
DEAD_EVENT = (
    "run-shell -b 'echo "
    + SessionStatusLog.line("#{q:session_name}", "dead", at=CLOCK)
    + " >> #{q:@events}'"
)


BORDER = " #{session_name} #{@state} "


TERMINAL_KEYS = (
    "TMUX",
    "TMUX_PANE",
    "TERM",
    "COLORTERM",
    "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION",
    "TERM_SESSION_ID",
    "ITERM_SESSION_ID",
    "ITERM_PROFILE",
    "LC_TERMINAL",
    "LC_TERMINAL_VERSION",
    "COLUMNS",
    "LINES",
)


EXEC = f"""import errno, json, os, signal, sys
with open(sys.argv[1]) as f: h = json.load(f)
w = os.open(sys.argv[2], os.O_WRONLY | os.O_NONBLOCK)
try:
    os.chdir(h['cwd'])
    os.unlink(sys.argv[1])
    h['env'].update((k, os.environ[k]) for k in {TERMINAL_KEYS!r} if k in os.environ)
    for s in (signal.SIGPIPE, signal.SIGXFSZ): signal.signal(s, signal.SIG_DFL)
    os.execve(h['argv'][0], h['argv'], h['env'])
except BaseException as e:
    os.write(w, b'%d' % (isinstance(e, OSError) and e.errno or errno.EINVAL))
    raise"""


LISTING = "#{session_name}\t#{pane_dead}\t#{@sid}\t#{@client}\t#{@state}\t#{@pane}\t#{@opener}\t#{@display}\t#{@process}"


SHOWN = "#{client_activity}\t#{client_tty}\t#{session_name}"


LISTED_PANES = "#{pane_dead}\t#{pane_tty}\t#{session_name}\t#{pane_id}"


LAST_LINES = 2000


def _own_pane(name: str, dead: str, process: str, panes: dict[str, str]) -> tuple[str | None, str]:
    """The pane for a session's last lines, and its pane_dead."""
    if not process:
        return name, dead
    if not PROCESS_ID.fullmatch(process):
        return None, ""
    pane = process.split(" ")[2]
    return (pane, panes[pane]) if pane in panes else (None, "1")


def _printable(value: str) -> str | None:
    return value if value and value.isprintable() else None


def attach_command(session: str) -> str:
    """`tmux attach -t '=<session>'`, after $TUI_ATTACH_PREFIX when not blank."""
    prefix = os.environ.get("TUI_ATTACH_PREFIX", "").strip()
    return f"{prefix} tmux attach -t '={session}'" if prefix else f"tmux attach -t '={session}'"


def session_name(session: str) -> str:
    if not NAME.fullmatch(session):
        raise TuiError(f"invalid session name {session!r}: want [A-Za-z0-9_-]+")
    return session


def address(target: str, suffix: str = ":") -> str:
    if PANE.fullmatch(target):
        return target
    if not NAME.fullmatch(target):
        raise TuiError(
            f"invalid target {target!r}: want a session name [A-Za-z0-9_-]+ or a pane id %N"
        )
    return f"={target}{suffix}"


def error(res: subprocess.CompletedProcess) -> str:
    return (res.stderr or "").strip()


def absent(res: subprocess.CompletedProcess) -> bool:
    return bool(ABSENT.fullmatch(error(res)))


def display_error(value) -> str | None:
    """Why value is no display (None: it is), keeping a tab-separated listing row one row."""
    if value is None or (
        isinstance(value, str) and value.isprintable() and not value.endswith(";")
    ):
        return None
    return f"display {value!r:.80}: want printable text not ending in ;"


def _out_of_mode(target: str, *command: str) -> list[str]:
    """copy-mode -q first: a pane in a mode (scrolled back) would swallow the keys."""
    return ["copy-mode", "-q", "-t", target, ";", *command]


def _process_id(found: str) -> str | None:
    """found: as tmux printed it, less its newline (unstripped); None: its pane is going away."""
    if PROCESS_ID.fullmatch(found):
        return found
    if PROCESS_GONE.fullmatch(found):
        return None
    raise TuiError(f"bad tmux process {found!r:.80}")


def _command(argv: Sequence[str], env: Mapping[str, str]) -> list[str]:
    if not argv:
        raise TuiError("no command")
    exe = shutil.which(argv[0], path=env.get("PATH", os.defpath))
    if exe is None:
        raise TuiError(f"command not found: {argv[0]}")
    return [os.path.abspath(exe), *argv[1:]]


def _wrapper(tmp: str) -> tuple[str, list[str]]:
    path, fifo = os.path.join(tmp, "handover.json"), os.path.join(tmp, ERRPIPE)
    for arg in (sys.executable, path, fifo):
        if "#" in arg or arg.endswith(";"):
            raise TuiError(f"tmux would misread {arg!r}")
    return path, [sys.executable, "-I", "-c", EXEC, path, fifo]


def _handover(
    path: str,
    argv: list[str],
    cwd: str,
    env: Mapping[str, str],
) -> int:
    cwd = os.path.abspath(cwd)
    child = {k: v for k, v in env.items() if k not in TERMINAL_KEYS}
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump(
            {
                "argv": argv,
                "cwd": cwd,
                "env": {**child, "PWD": cwd},
            },
            f,
        )
    fifo = os.path.join(os.path.dirname(path), ERRPIPE)
    os.mkfifo(fifo, 0o600)
    return os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)


def _tags(session: str, tags: Mapping[str, str]) -> list[str]:
    args = []
    for key, value in tags.items():
        if key == "@display" and (why := display_error(value)):
            raise TuiError(why)
        if (
            not USER_OPTION.fullmatch(key)
            or not isinstance(value, str)
            or value.endswith(";")
            or "\0" in value
        ):
            raise TuiError(
                f"tag {key!r}={value!r}: want {USER_OPTION.pattern} and a str tmux takes verbatim"
            )
        args += [";", "set-option", "-t", f"={session}:", key, value]
    return args


def _recorded(session: str) -> list[str]:
    return [";", "set-option", "-F", "-t", f"={session}:", "@process", PROCESS]


def _identity(res: subprocess.CompletedProcess) -> str:
    found = res.stdout.strip()
    if not PROCESS_ID.fullmatch(found):
        raise TuiError(f"tmux: bad new session {found!r:.80}")
    return found


def _started(path: str, pipe: int, command: str, sleep) -> bool:
    """
    Whether the handover went, then execve closed the errpipe, in time; errpipe data raises.
    """
    polls, out = 0, b""
    while os.path.exists(path):
        if polls == round(HANDOVER_TIMEOUT / POLL):
            return False
        sleep(POLL)
        polls += 1
    polls = 0
    while True:  # only now: a FIFO no writer opened reads EOF
        try:
            chunk = os.read(pipe, 64)
        except BlockingIOError:
            if polls >= round(HANDOVER_TIMEOUT / POLL):
                return False
            sleep(POLL)
            polls += 1
            continue
        if not chunk:
            break
        out += chunk
    if out:
        try:
            reason = os.strerror(int(out))
        except (ValueError, OverflowError):
            reason = "bad errpipe data"
        raise TuiError(f"cannot run {command}: {reason}")
    return True


def _events(path: str) -> str:
    try:
        return events_file(path)
    except ValueError as error:
        raise TuiError(str(error)) from None


def validate_start(
    session: str, argv: Sequence[str], env: Mapping[str, str], tags: Mapping[str, str]
) -> None:
    session_name(session)
    _tags(session, tags)
    _command(argv, env)


def _decorations(session: str, events: str | None, *, status_line: bool) -> list[list[str]]:
    target = f"={session}:"
    died = DIED if events is None else f"{DIED} ; {DEAD_EVENT}"
    return [
        *([["set-option", "-t", target, "@events", events]] if events is not None else []),
        ["set-hook", "-p", "-t", target, "pane-died", died],
        ["set-option", "-w", "-t", target, "pane-border-status", "top"],
        ["set-option", "-w", "-t", target, "pane-border-format", BORDER],
        ["set-option", "-t", target, "status", "on" if status_line else "off"],
    ]


class TmuxCli:
    PAUSE = PAUSE

    def __init__(self, runner=subprocess.run, *, sleep=time.sleep) -> None:
        self.runner, self.sleep = runner, sleep

    def checked(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        res = self.run(args, stdin)
        if res.returncode:
            raise TuiError(f"tmux: {error(res)}")
        return res

    def run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        try:
            if stdin is not None:
                return self.runner(["tmux", *args], capture_output=True, text=True, input=stdin)
            return self.runner(
                ["tmux", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL
            )
        except OSError as e:
            raise TuiError(f"tmux: {e}") from e

    def read(self, target: str, lines: int | None = None) -> str:
        """The visible text, or the last `lines` with history; trailing blank lines dropped."""
        args = ["capture-pane", "-p", "-J", "-t", address(target)]
        if lines is not None:
            if isinstance(lines, bool) or not isinstance(lines, int) or lines < 1:
                raise TuiError(f"lines must be a positive int, not {lines!r}")
            args += ["-S", f"-{lines}"]
        text = self.checked(args).stdout.splitlines()
        while text and not text[-1].strip():
            text.pop()
        return "\n".join(text if lines is None else text[-lines:])

    def status(self, target: str):
        """RUNNING, None (target or server absent), UNKNOWN, or a dead pane's status (signal n: -n)."""
        target = address(target)
        try:
            res = self.run(["display-message", "-p", "-t", target, DEAD])
        except TuiError:
            return PaneStatus.UNKNOWN
        if res.returncode:
            return None if absent(res) else PaneStatus.UNKNOWN
        return self._dead_status(res.stdout.rstrip("\n"))

    def _dead_status(self, text: str):
        fields = text.split(" ")
        if fields == ["", "", ""]:  # tmux (3.7) exits 0 with empty fields for a missing session
            return None
        if len(fields) != 3 or fields[0] not in ("0", "1"):
            return PaneStatus.UNKNOWN
        dead, code, sig = fields
        if dead == "0":
            return PaneStatus.RUNNING
        if not (code or sig):
            with contextlib.suppress(TuiError):
                self.run(["run-shell", "-b", "true"])
            return PaneStatus.RUNNING
        if sig:  # libc's sys_signame (macOS: "kill"), else the number
            found = (
                int(sig) if sig.isdecimal() else signal.Signals.__members__.get(f"SIG{sig.upper()}")
            )
            return PaneStatus.UNKNOWN if found is None else -found
        return int(code) if code.isdecimal() else PaneStatus.UNKNOWN

    def kill(self, target: str) -> None:
        """Kills the pane a pane id names, else the whole named session."""
        if PANE.fullmatch(target):
            self.checked(["kill-pane", "-t", target])
        else:
            self.checked(["kill-session", "-t", address(target, "")])

    def keys(self, target: str, *names: str) -> None:
        """Presses tmux keys (Enter, Escape, C-c); a name tmux would misread raises TuiError first."""
        target = address(target)
        for name in names:
            if name.startswith("-") or name.endswith(";"):
                raise TuiError(f"tmux would misread {name!r}")
        self.checked(_out_of_mode(target, "send-keys", "-t", target, *names))

    def press(self, target: str, *names: str) -> None:
        """Key names after `--`, so a `-` key is no flag; `;` must come escaped (`\\;`)."""
        target = address(target)
        for name in names:
            if not name or name.endswith(";") and not name.endswith("\\;"):
                raise TuiError(f"tmux would misread {name!r}")
        self.checked(_out_of_mode(target, "send-keys", "-t", target, "--", *names))

    def option(self, target: str, name: str) -> str | None:
        """None when unset or the target absent."""
        target = address(target)
        if not USER_OPTION.fullmatch(name):
            raise TuiError(f"option {name!r}: want {USER_OPTION.pattern}")
        res = self.run(["show-options", "-v", "-t", target, name])
        if res.returncode:
            if error(res).startswith("invalid option:") or absent(res):
                return None
            raise TuiError(f"tmux: {error(res)}")
        return res.stdout.removesuffix("\n")

    def process(self, target: str) -> str | None:
        """PROCESS of target's pane (a session's active one); None when absent."""
        res = self.run(["display-message", "-p", "-t", address(target), PROCESS])
        if res.returncode:
            if absent(res):
                return None
            raise TuiError(f"tmux: {error(res)}")
        return _process_id(res.stdout.removesuffix("\n"))

    def sessions(self) -> list[str] | None:
        """Names: [] without a server, None when tmux fails."""
        try:
            found = self._table(["list-sessions", *VIEWLESS, "-F", "#{session_name}"], 1)
        except TuiError:
            return None
        return [name for (name,) in found if name]

    def listing(self, match=None, *, views: bool = False) -> list[dict]:
        """NAME sessions `match` fullmatches (None: all), by name: tags, pane state, `shown` (no
        client: None; else the newest client's host session, "" if none or not NAME), a dead pane's
        last LAST_LINES lines; bad or empty fields None, wrong-width rows dropped; [] without a
        server, TuiError (why) when tmux fails. Reads @process's pane (gone: dead; bad: dead None), else the active.
        views: only VIEW sessions, else none.
        """
        found = self._table(["list-sessions", *(VIEWS if views else VIEWLESS), "-F", LISTING], 9)
        rows = sorted(
            f for f in found if NAME.fullmatch(f[0]) and (match is None or match.fullmatch(f[0]))
        )
        if not rows:
            return []
        clients = self._table(["list-clients", "-F", SHOWN], 3)
        recent = {}
        for _, tty, session in sorted(
            (c for c in clients if c[0].isdigit() and c[1]), key=lambda c: int(c[0])
        ):
            recent[session] = tty
        hosts, launched = {}, {}
        if any(f[0] in recent or f[8] for f in rows):
            panes = self._table(["list-panes", "-a", "-F", LISTED_PANES], 4)
            hosts = {tty: session for dead, tty, session, _ in panes if dead != "1"}
            launched = {pane_id: dead for dead, _, _, pane_id in panes}
        out = []
        for name, dead, sid, client, state, pane, parent, display, process in rows:
            target, dead = _own_pane(name, dead, process, launched)
            tty = recent.get(name)
            host = "" if tty is None else hosts.get(tty, "")
            out.append(
                {
                    "name": name,
                    "sid": _printable(sid),
                    "client": _printable(client),
                    "state": _printable(state),
                    "pane": pane if PANE.fullmatch(pane) or ITERM_ID.fullmatch(pane) else None,
                    "opener": parent if OPENER.fullmatch(parent) else None,
                    "display": display if display and not display_error(display) else None,
                    "dead": {"1": True, "0": False}.get(dead),
                    "shown": None if name not in recent else host if NAME.fullmatch(host) else "",
                    "last_lines": self._last_lines(target) if dead == "1" and target else None,
                }
            )
        return out

    def _table(self, args: list[str], width: int) -> list[list[str]]:
        res = self.run(["-u", *args])
        if res.returncode:
            if absent(res):
                return []
            why = " ".join(error(res).split())
            raise TuiError(f"tmux {args[0]} exited {res.returncode}" + (f": {why}" if why else ""))
        return [f for f in (line.split("\t") for line in res.stdout.split("\n")) if len(f) == width]

    def _last_lines(self, target: str) -> str | None:
        try:
            return self.read(target, LAST_LINES) or None
        except TuiError:
            return None

    def start(
        self,
        session: str,
        launch: Launch,
        *,
        events: str | None = None,
        status_line: bool = False,
        tags: Mapping[str, str] | None = None,
    ) -> str:
        """argv in a new detached session keeping its pane after exit, in cwd with env (terminal keys
        the pane's, PWD cwd). Every option and the pane-died hook (@state dead, dead line to
        @events) go in that one tmux call, so instant deaths log. Opens no pane; refuses an existing
        session untouched; argv goes via a 0600 handover file. Returns process() once argv exec'd;
        raising, leaves no own session.
        """
        argv, cwd, env = launch.argv, launch.cwd, launch.env
        session_name(session)
        tag_options = _tags(session, tags or {})
        argv = _command(argv, env)
        if events is not None:
            events = _events(events)
        tmp = pipe = None
        may_run = False
        try:
            tmp = tempfile.mkdtemp()
            path, wrapper = _wrapper(tmp)
            state = self.status(session)
            if state == PaneStatus.RUNNING:
                raise TuiError(f"session {session} is running; {attach_command(session)}")
            if isinstance(state, int):
                raise TuiError(f"session {session} exists")
            pipe = _handover(path, argv, cwd, env)
            cols, rows = shutil.get_terminal_size()
            decorations = [
                arg
                for args in _decorations(session, events, status_line=status_line)
                for arg in (";", *args)
            ]
            # new-session may have run even when the tmux call fails
            may_run = True
            res = self.run(
                [
                    "new-session",
                    "-d",
                    "-P",
                    "-F",
                    PROCESS,
                    "-s",
                    session,
                    "-x",
                    str(cols),
                    "-y",
                    str(rows),
                    *wrapper,
                    ";",
                    "set-option",
                    "-p",
                    "-t",
                    f"={session}:",
                    "remain-on-exit",
                    "on",
                    *tag_options,
                    *decorations,
                    *_recorded(session),
                ],
            )
            if res.returncode:
                if error(res).startswith(
                    "duplicate session:"
                ):  # another start took the name: not ours to kill
                    may_run = False
                raise TuiError(f"tmux: {error(res)}")
            found = _identity(res)
            if not _started(path, pipe, argv[0], self.sleep):
                raise TuiError("the session did not start")
            may_run = False
        except OSError as e:
            raise TuiError(f"handover: {e}") from e
        else:
            return found
        finally:
            if tmp:
                shutil.rmtree(
                    tmp, ignore_errors=True
                )  # first, so a late wrapper finds no file to run
            if pipe is not None:
                os.close(pipe)
            if may_run:
                with contextlib.suppress(TuiError):
                    self.kill(session)


@dataclass(frozen=True)
class NoAnswer:
    """A probe tmux did not answer."""

    message: str


Probe = tuple[bool, PaneStatus | int | None] | NoAnswer


def liveness(found: Probe) -> Liveness:
    if isinstance(found, NoAnswer):
        return Liveness.UNKNOWN
    own, value = found
    if not own or value is None:
        return Liveness.ABSENT
    return Liveness.UNKNOWN if value == PaneStatus.UNKNOWN else Liveness.LIVE


def exit_code(found: Probe) -> int | None:
    if isinstance(found, NoAnswer):
        return None
    own, value = found
    if not own or value is None:
        return -signal.SIGHUP
    return value if isinstance(value, int) else None


def running(found: Probe, name: str) -> bool:
    if isinstance(found, NoAnswer):
        raise SessionError(found.message)
    own, value = found
    if not own:
        return False
    if value == PaneStatus.UNKNOWN:
        raise SessionError(f"tmux session {name}: tmux failed")
    return value == PaneStatus.RUNNING
