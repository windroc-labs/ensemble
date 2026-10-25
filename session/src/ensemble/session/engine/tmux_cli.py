"""TmuxCli: every tmux call through runner (subprocess.run's signature) and sleep. start and
detach hand a launch to a new pane (EXEC); launch STAGEs it through runner for RELAUNCH. Listings
skip VIEW sessions. TmuxPane: a TmuxCli aimed at one session's captured pane.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence

from ensemble.session.engine.status_log import SessionStatusLog
from ensemble.session.engine.types import (
    ConfigError,
    Key,
    KeyPress,
    Launch,
    Liveness,
    NamedKey,
    PaneStatus,
    Paste,
    SessionError,
)


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


USER_OPTION = re.compile(r"@[A-Za-z0-9_-]+")


PAUSE = 0.5


TMUX_KEYS = {NamedKey.PAGE_UP: "PPage", NamedKey.PAGE_DOWN: "NPage", " ": "Space", ";": "\\;"}
# tmux 3.4 (CI) and 3.5+ both send these as the same C0 or DEL byte; other punctuation and digits
# lose Ctrl, send nothing or differ between them
CTRL_PUNCTUATION = frozenset(" /?@[\\]^_")


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
EXIT_STATUS = (
    "#{?pane_dead_signal,-#{?#{m:*[!0-9]*,#{pane_dead_signal}},$(kill -l #{q:pane_dead_signal}),"
    "#{pane_dead_signal}},#{pane_dead_status}}"
)
EXITED_EVENT = (
    "run-shell 'echo "
    + SessionStatusLog.line("#{q:session_name}", "exited", EXIT_STATUS, at=CLOCK)
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


STAGE = """import os, sys, tempfile
fd, path = tempfile.mkstemp(prefix='launch-', dir=sys.argv[1])
with os.fdopen(fd, 'w') as f: f.write(sys.stdin.read())
print(path)"""


RELAUNCH = """import json, os, signal, sys
try:
    with open(sys.argv[1]) as f: h = json.load(f)
finally:
    os.unlink(sys.argv[1])
for s in (signal.SIGPIPE, signal.SIGXFSZ): signal.signal(s, signal.SIG_DFL)
os.execvpe(h['argv'][0], h['argv'], {**os.environ, **h['env']})"""


STAGED = re.compile(r"launch-[A-Za-z0-9_]+")


LISTING = "#{session_name}\t#{pane_dead}\t#{@sid}\t#{@client}\t#{@state}\t#{@pane}\t#{@opener}\t#{@display}\t#{@process}"


SHOWN = "#{client_activity}\t#{client_tty}\t#{session_name}"


LISTED_PANES = "#{pane_dead}\t#{pane_tty}\t#{session_name}\t#{pane_id}"


LAST_LINES = 2000


MARK_TIMEOUT = 5


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


def tmux_path() -> str | None:
    """The real path (symlinks resolved) of the tmux on PATH, else None."""
    found = shutil.which("tmux")
    return None if found is None else os.path.realpath(found)


def _out_of_mode(target: str, *command: str) -> list[str]:
    """copy-mode -q first: a pane in a mode (scrolled back) would swallow the keys."""
    return ["copy-mode", "-q", "-t", target, ";", *command]


def _process_id(found: str) -> str | None:
    if " " not in found or re.fullmatch(r"[0-9]+  %[0-9]+ [0-9]+", found):
        # A pane can outlive its session while tmux formats its remaining identity fields.
        return None
    if not PROCESS_ID.fullmatch(found):
        raise TuiError(f"bad tmux process {found!r:.80}")
    return found


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


def validate_events(path: str) -> str:
    """The absolute path, checked; never opens it for writing or creates it (events_file does)."""
    path = os.path.abspath(path)
    if "#" in path or path.endswith(";"):
        raise TuiError(f"events file {path}: tmux would misread it")
    try:
        info = os.stat(path)
    except FileNotFoundError:
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            raise TuiError(f"events file {path}: No such file or directory") from None
        if not os.access(parent, os.W_OK):
            raise TuiError(f"events file {path}: Permission denied") from None
    except OSError as error:
        raise TuiError(f"events file {path}: {error.strerror}") from error
    else:
        if stat.S_ISDIR(info.st_mode):
            raise TuiError(f"events file {path}: {os.strerror(errno.EISDIR)}")
        if not stat.S_ISREG(info.st_mode):
            raise TuiError(f"events file {path}: not a regular file")
        if not os.access(path, os.W_OK):
            raise TuiError(f"events file {path}: Permission denied")
    return path


def validate_start(
    session: str, argv: Sequence[str], env: Mapping[str, str], tags: Mapping[str, str]
) -> None:
    session_name(session)
    _tags(session, tags)
    _command(argv, env)


def events_file(path: str) -> str:
    """validate_events, then creates it 0600 when missing; TuiError unless a regular file."""
    path = validate_events(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK, 0o600)
        try:
            st = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError as e:
        raise TuiError(f"events file {path}: {e.strerror}") from e
    if not stat.S_ISREG(st.st_mode):
        raise TuiError(f"events file {path}: not a regular file")
    return path


def _floor(session: str, min_width: int) -> list[str]:
    """The window follows its latest client, at least min_width wide (0: no floor)."""
    if not min_width:
        return []
    target = f"={session}:"
    width = f"#{{?#{{e|<:#{{client_width}},{min_width}}},{min_width},#{{client_width}}}}"
    resize = (
        f'run-shell -C "resize-window -t =#{{q:session_name}}: -x {width} -y #{{client_height}}"'
    )
    return [
        *(";", "set-option", "-w", "-t", target, "window-size", "manual"),
        *(
            arg
            for hook in ("client-attached", "client-resized")
            for arg in (";", "set-hook", "-t", target, hook, resize)
        ),
    ]


def _decorations(
    session: str, events: str | None, *, status_line: bool, died: str = DIED
) -> list[list[str]]:
    target = f"={session}:"
    died = died if events is None else f"{died} ; {DEAD_EVENT}"
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

    def process_status(self, target: str):
        """(process(), status()) in one tmux call; (None, None) when absent."""
        res = self.run(["display-message", "-p", "-t", address(target), f"{PROCESS}\t{DEAD}"])
        if res.returncode:
            if absent(res):
                return None, None
            raise TuiError(f"tmux: {error(res)}")
        identity, _, dead = res.stdout.rstrip("\n").partition("\t")
        found = _process_id(identity.strip())
        return found, None if found is None else self._dead_status(dead)

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
            # dead, not reaped: tmux 3.4 with utempter can lose the SIGCHLD, then reaps (and runs
            # pane-died) only when another child exits; a no-op job is one
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

    def paste(self, target: str, text: str) -> None:
        buffer = f"tui-{target}-{uuid.uuid4().hex}"
        target = address(target)
        # load-buffer from stdin keeps text exact: send-keys -l lost long text's head, set-buffer a
        # trailing ';'
        self.checked(["load-buffer", "-b", buffer, "-"], text)
        try:
            self.checked(
                _out_of_mode(target, "paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", target)
            )
        except TuiError:
            with contextlib.suppress(TuiError):
                self.run(["delete-buffer", "-b", buffer])
            raise

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

    def set_option(self, target: str, name: str, value: str) -> None:
        target = address(target)
        if not USER_OPTION.fullmatch(name):
            raise TuiError(f"option {name!r}: want {USER_OPTION.pattern}")
        if value.endswith(";") or "\n" in value:
            raise TuiError(f"tmux would misread {value!r:.80}")
        self.checked(["set-option", "-t", target, name, value])

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
        return _process_id(res.stdout.strip())

    def sessions(self) -> list[str] | None:
        """Names: [] without a server, None when tmux fails."""
        found = self._table(["list-sessions", *VIEWLESS, "-F", "#{session_name}"], 1)
        return None if found is None else [name for (name,) in found if name]

    def listing(self, match=None, *, views: bool = False) -> list[dict] | None:
        """NAME sessions `match` fullmatches (None: all), by name: tags, pane state, `shown` (no
        client: None; else the newest client's host session, "" if none or not NAME), a dead pane's
        last LAST_LINES lines; bad or empty fields None, wrong-width rows dropped; [] without a
        server, None when tmux fails. Reads @process's pane (gone: dead; bad: dead None), else the active.
        views: only VIEW sessions, else none.
        """
        found = self._table(["list-sessions", *(VIEWS if views else VIEWLESS), "-F", LISTING], 9)
        if found is None:
            return None
        rows = sorted(
            f for f in found if NAME.fullmatch(f[0]) and (match is None or match.fullmatch(f[0]))
        )
        if not rows:
            return []
        clients = self._table(["list-clients", "-F", SHOWN], 3)
        if clients is None:
            return None
        recent = {}
        for _, tty, session in sorted(
            (c for c in clients if c[0].isdigit() and c[1]), key=lambda c: int(c[0])
        ):
            recent[session] = tty
        hosts, launched = {}, {}
        if any(f[0] in recent or f[8] for f in rows):
            panes = self._table(["list-panes", "-a", "-F", LISTED_PANES], 4)
            if panes is None:
                return None
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

    def _table(self, args: list[str], width: int) -> list[list[str]] | None:
        try:
            # -u: else, without a UTF-8 locale or $TMUX, tmux prints tabs as _
            res = self.run(["-u", *args])
        except TuiError:
            return None
        if res.returncode:
            return [] if absent(res) else None
        return [f for f in (line.split("\t") for line in res.stdout.split("\n")) if len(f) == width]

    def _last_lines(self, target: str) -> str | None:
        try:
            return self.read(target, LAST_LINES) or None
        except TuiError:
            return None

    def mark(
        self,
        word: str,
        events: str | None,
        *,
        pane: str | None = None,
        tmux: str = "tmux",
        session: str | None = None,
    ) -> None:
        """Hook protocol: pane ($TMUX_PANE when None) @state = word; done and blocked also append
        `HH:MM:SS <session> <word>` (session: the pane's unless given) to events (None: no line).
        """
        pane = os.environ.get("TMUX_PANE", "") if pane is None else pane
        if not pane:
            return
        if not PANE.fullmatch(pane):
            raise TuiError(f"bad tmux pane {pane!r}")
        self._mark_tmux([tmux, "set-option", "-t", pane, "@state", word])
        if word not in ("done", "blocked") or events is None:
            return
        if session is None:
            session = self._mark_tmux([tmux, "display-message", "-p", "-t", pane, "#S"]).strip()
        if not NAME.fullmatch(session):
            raise TuiError(f"tmux session {session!r}: want [A-Za-z0-9_-]+")
        try:
            # O_NONBLOCK: a FIFO without a reader fails instead of hanging the hook
            fd = os.open(events, os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise TuiError(f"events file {events}: not a regular file")
                os.write(fd, (SessionStatusLog.line(session, word) + "\n").encode())
            finally:
                os.close(fd)
        except OSError as e:
            raise TuiError(f"events file {events}: {e.strerror}") from e

    def _mark_tmux(self, argv: list[str]) -> str:
        try:
            res = self.runner(
                argv,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=MARK_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise TuiError(f"tmux: timed out after {MARK_TIMEOUT} s") from None
        except OSError as e:
            raise TuiError(f"tmux: {e}") from e
        if res.returncode:
            raise TuiError(f"tmux: {error(res)}")
        return res.stdout

    def launch(
        self,
        session: str,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        python: str,
        handover_dir: str,
        tags: Mapping[str, str] | None = None,
        died: str = DIED,
        min_width: int = 0,
    ) -> str:
        """start's setup in one call, without events, pane-died `died`: the runner's `python` STAGEs
        argv and env (over the server's) in a 0600 file in its private `handover_dir` for RELAUNCH,
        keeping tmux's command line short; a failed launch deletes it. Returns process().
        Keeps 50,000 history lines and default mouse bindings. Forwards OSC 52 copies.
        A `min_width` window is never narrower (_floor).
        """
        session_name(session)
        tag_options = _tags(session, tags or {})
        if not argv:
            raise TuiError("no command")
        for arg in (cwd, handover_dir):
            if not os.path.isabs(arg) or "#" in arg or arg.endswith(";"):
                raise TuiError(f"tmux would misread {arg!r:.80}")
        if any("\0" in arg for arg in (*argv, *env, *env.values())):
            raise TuiError("argv or env holds a NUL character")
        path = self._stage(python, handover_dir, json.dumps({"argv": list(argv), "env": dict(env)}))
        cols, rows = shutil.get_terminal_size()
        cols = max(cols, min_width)
        decorations = [
            arg
            for args in _decorations(session, None, status_line=False, died=died)
            for arg in (";", *args)
        ]
        try:
            res = self.run(
                [
                    # history-limit is captured at pane creation, not when it is attached.
                    "start-server",
                    *(";", "set-option", "-g", "history-limit", "50000"),
                    *(";", "set-option", "-g", "mouse", "on"),
                    ";",
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
                    "-c",
                    cwd,
                    "--",
                    python,
                    "-I",
                    "-c",
                    RELAUNCH,
                    path,
                    *(";", "set-option", "-p", "-t", f"={session}:", "remain-on-exit", "on"),
                    *(";", "set-option", "-s", "set-clipboard", "on"),
                    *(";", "set-option", "-qp", "-t", f"={session}:", "allow-passthrough", "on"),
                    *tag_options,
                    *decorations,
                    *_floor(session, min_width),
                    *_recorded(session),
                ]
            )
            if res.returncode:
                raise TuiError(f"tmux: {error(res)}")
            return _identity(res)
        except TuiError:
            with contextlib.suppress(OSError):
                self.runner(
                    ["rm", "-f", "--", path],
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                )
            raise

    def _stage(self, python: str, directory: str, handover: str) -> str:
        try:
            res = self.runner(
                [python, "-I", "-c", STAGE, directory],
                capture_output=True,
                text=True,
                input=handover,
            )
        except OSError as e:
            raise TuiError(f"handover: {e}") from e
        if res.returncode:
            why = error(res).splitlines()
            raise TuiError(f"handover: {why[-1] if why else f'exit status {res.returncode}'}")
        path = res.stdout.strip()
        if os.path.dirname(path) != os.path.normpath(directory) or not STAGED.fullmatch(
            os.path.basename(path)
        ):
            raise TuiError(f"handover: bad staged file {path!r:.80}")
        return path

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
            events = events_file(events)
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

    def detach(
        self,
        session: str,
        launch: Launch,
        *,
        display: str | None = None,
        events: str | None = None,
        record: str | None = None,
        sid: str | None = None,
        control: str | None = None,
    ) -> str:
        """As start, keeping env's iTerm2 $ITERM_SESSION_ID; display, record (a line) become
        @display, @record. No decoration; events: a kept pane whose pane-died hook appends
        `HH:MM:SS <session> exited <rc>` (signal n: -n) to them, then kills it.
        """
        argv, cwd, env = launch.argv, launch.cwd, launch.env
        session_name(session)
        named = {
            "@kind": "detach",
            "@display": display,
            "@record": record,
            "@sid": sid,
            "@control": control,
        }
        tag_options = _tags(session, {k: v for k, v in named.items() if v is not None})
        exited = []
        if events is not None:
            target = f"={session}:"
            # -F stores the launched pane's id (## keeps the event's formats for the hook): a bare
            # kill-pane, its pane closed during run-shell, kills another session's pane
            on_exit = EXITED_EVENT.replace("#", "##") + " ; kill-pane -t #{pane_id}"
            exited = [
                *(";", "set-option", "-p", "-t", target, "remain-on-exit", "on"),
                *(";", "set-option", "-t", target, "@events", events_file(events)),
                *(";", "set-option", "-p", "-F", "-t", target, "pane-died", on_exit),
            ]
        for arg in argv:
            if "\0" in arg:
                raise TuiError("an argv item holds a NUL character")
            try:
                os.fsencode(arg)
            except UnicodeEncodeError:
                raise TuiError("an argv item cannot be encoded") from None
        argv = _command(argv, env)
        iterm = env.get("ITERM_SESSION_ID", "")
        terminal = ["-e", f"ITERM_SESSION_ID={iterm}"] if ITERM_OPENER.fullmatch(iterm) else []
        tmp = pipe = None
        may_run = False
        try:
            tmp = tempfile.mkdtemp()
            path, wrapper = _wrapper(tmp)
            pipe = _handover(path, argv, cwd, env)
            may_run = True
            res = self.run(
                [
                    "new-session",
                    "-d",
                    "-P",
                    "-F",
                    PROCESS,
                    *terminal,
                    "-s",
                    session,
                    *wrapper,
                    *exited,
                    *tag_options,
                    *_recorded(session),
                ],
            )
            if res.returncode:
                if error(res).startswith("duplicate session:"):
                    may_run = False
                    raise TuiError(f"session {session} exists")
                raise TuiError(f"tmux: {error(res)}")
            found = _identity(res)
            if not _started(path, pipe, argv[0], self.sleep):
                raise TuiError(f"session {session} did not start")
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


def _tmux_key(key) -> str:
    """KeyPress → tmux's key name: modifiers as C-, M-, S-; PageUp, PageDown, space and ; renamed."""
    if not isinstance(key, KeyPress):
        raise ConfigError(f"key {key!r:.80}: want a KeyPress")
    char = len(key.key) == 1
    letter = key.key.isascii() and key.key.isalpha()
    if char and (key.shift or key.ctrl and not (letter or key.key in CTRL_PUNCTUATION)):
        raise ConfigError(f"key {key}: tmux cannot send it")  # it would lose its modifier
    prefixes = ("C-" * key.ctrl, "M-" * key.meta, "S-" * key.shift)
    return "".join(prefixes) + TMUX_KEYS.get(key.key, key.key)


class TmuxPane:
    """Session name's captured pane (id), owned while it still runs process (its PROCESS)."""

    def __init__(self, name: str, process: str, tmux: TmuxCli) -> None:
        self.name, self.process, self.tmux = name, process, tmux
        _, _, self.id, _ = process.split(" ")
        self._pasted = False

    def own(self) -> bool:
        try:
            return self.tmux.process(self.id) == self.process
        except TuiError as e:
            raise SessionError(f"tmux session {self.name}: {e}") from None

    def _state(self):
        """Whether it is still owned, and the pane's status(): one call."""
        try:
            found, value = self.tmux.process_status(self.id)
        except TuiError as e:
            raise SessionError(f"tmux session {self.name}: {e}") from None
        return found == self.process, value

    def state(self) -> Liveness:
        try:
            own, value = self._state()
        except SessionError:
            return Liveness.UNKNOWN
        if not own or value is None:
            return Liveness.ABSENT
        return Liveness.UNKNOWN if value == PaneStatus.UNKNOWN else Liveness.LIVE

    def returncode(self) -> int | None:
        try:
            own, value = self._state()
        except SessionError:
            return None
        if not own or value is None:
            return -signal.SIGHUP
        return value if isinstance(value, int) else None

    def alive(self) -> bool:
        own, state = self._state()
        if not own:
            return False
        if state == PaneStatus.UNKNOWN:
            raise SessionError(f"tmux session {self.name}: tmux failed")
        return state == PaneStatus.RUNNING

    def option(self, key: str) -> str | None:
        if not self.own():
            return None
        try:
            return self.tmux.option(self.id, key)
        except TuiError as e:
            raise SessionError(f"tmux session {self.name}: {e}") from None

    def send(self, action: Paste | Key, alive: Callable[[], bool]) -> None:
        """alive: its Process's, checked first."""
        self._running(alive)
        try:
            if isinstance(action, Paste):
                self.tmux.paste(self.id, action.text)
                self._pasted = True
            else:
                if self._pasted:
                    time.sleep(self.tmux.PAUSE)
                self.tmux.keys(self.id, action.name)
                self._pasted = False
        except TuiError as error:
            raise SessionError(f"send to tui session {self.name}: {error}") from error

    def press(self, keys: Sequence[KeyPress], alive: Callable[[], bool]) -> None:
        names = [_tmux_key(key) for key in keys]
        self._running(alive)
        try:
            if self._pasted:
                time.sleep(self.tmux.PAUSE)
            self.tmux.press(self.id, *names)
            self._pasted = False
        except TuiError as error:
            raise SessionError(f"send to tui session {self.name}: {error}") from error

    def _running(self, alive: Callable[[], bool]) -> None:
        if not alive():
            raise SessionError(f"tui session {self.name} is not running")

    def read(self, lines: int | None = None) -> str:
        return self.tmux.read(self.id, lines)

    def mark(self, word: str, events: str | None, *, session: str | None = None) -> None:
        if self.own():
            try:
                self.tmux.mark(word, events, pane=self.id, session=session)
            except TuiError as error:
                raise SessionError(str(error)) from error

    def kill(self) -> None:
        if not self.own():
            return
        try:
            self.tmux.kill(self.id)
        except TuiError as e:
            if self.own():
                raise SessionError(str(e)) from None
