"""Recorded tmux transport and shared launch, placement and grid fixtures."""

import fcntl
import io
import json
import os
import re
import shutil
import signal
import stat
import tempfile
import unittest
import unittest.mock
from contextlib import contextmanager, redirect_stderr
from types import SimpleNamespace

from ensemble.session.engine.tmux_cli import EXEC, TmuxCli
from ensemble.session.engine.types import Launch
from ensemble.session.viewer import grid as grids
from ensemble.session.viewer import iterm, placement, types
from ensemble.session.viewer import layout as layouts
from tests.session.case import Case
from tests.utils.process_fixtures import DEVNULL

CLIENT = re.compile(r"claude|codex", re.I)

FORMAT = "#{pane_dead} #{pane_dead_status} #{pane_dead_signal}"

CLIENTS = "#{client_activity} #{client_tty} #{pane_id} #{client_control_mode} #{socket_path}"

PGREP = ["pgrep", "-a", "-x", "iTerm2"]

ITERM = {"ITERM_SESSION_ID": "w0t0p0:ABC", "TERM_PROGRAM": "iTerm.app"}

TMUX_KW = {"capture_output": True, "text": True, "stdin": DEVNULL}

SH_KW = {"stdin": DEVNULL, "timeout": 30}

OSA_KW = {**TMUX_KW, "timeout": 30}

TMUX = '/opt/a"b\\c/bin/tmux'  # the script text must never carry it

ATTACH = "tui: session s: tmux attach -t '=s'\n"

WATCH = "; watch it with tmux attach -t '=s'"

ABSENT = (
    "can't find session: =s",
    "no such session: =s:",
    "no server running on /tmp/tmux-501/default",
    "error connecting to /tmp/tmux-501/default (No such file or directory)",
)

FAILED = ("error connecting to /tmp/tmux-501/default (Permission denied)", "boom", "")

SIGNALS = [int(signal.SIGHUP), int(signal.SIGTERM), int(signal.SIGINT)]

DIED = "set-option @state dead"

DIED_EVENTS = (
    DIED + " ; run-shell -b 'echo \"$(date +%H:%M:%S)\" #{q:session_name} dead >> #{q:@events}'"
)

NEW_PROCESS = "4242 $3 %7 4343"

NEW = ["-P", "-F", "#{pid} #{session_id} #{pane_id} #{pane_pid}"]

EXITED = (
    'run-shell \'echo "$(date +%H:%M:%S)" #{q:session_name} exited'
    " #{?pane_dead_signal,-#{?#{m:*[!0-9]*,#{pane_dead_signal}},$(kill -l #{q:pane_dead_signal}),"
    "#{pane_dead_signal}},#{pane_dead_status}} >> #{q:@events}'"
)
EXITED_HOOK = (
    'run-shell \'echo "$(date +%H:%M:%S)" ##{q:session_name} exited'
    " ##{?pane_dead_signal,-##{?##{m:*[!0-9]*,##{pane_dead_signal}},$(kill -l ##{q:pane_dead_signal}),"
    "##{pane_dead_signal}},##{pane_dead_status}} >> ##{q:@events}' ; kill-pane -t #{pane_id}"
)

INSIDE = {"TMUX": "/tmp/tmux-501/default,1,0", "TMUX_PANE": "%3", "ITERM_SESSION_ID": "w0t0p0:ABC"}

OWN = ["tmux", "display-message", "-p", "-t", "%3", "#{session_name}"]

SESSIONS = [
    "tmux",
    "list-sessions",
    "-F",
    "#{session_id}\t#{session_name}\t#{@opener}\t#{@pane}\t#{socket_path}",
]

LIVE = ["tmux", "list-panes", "-a", "-F", "#{pane_id}"]

HOSTS = ["tmux", "list-panes", "-a", "-F", "#{pane_dead} #{pane_tty} #{pane_id}"]

SOCK = "/tmp/s p"

PREFIXES = (
    ({}, ""),
    ({"TUI_ATTACH_PREFIX": " \t"}, ""),
    ({"TUI_ATTACH_PREFIX": " docker exec -it box "}, "docker exec -it box "),
)

MAIN_VERTICAL = "7f31,160x48,0,0{80x48,0,0,0,79x48,81,0[79x24,81,0,1,79x23,81,25,2]}"

TILED_SPLIT = (
    "9a18,160x48,0,0[160x15,0,0{79x15,0,0,0,40x15,80,0,1,39x15,121,0,5},"
    "160x15,0,16{79x15,0,16,2,80x15,80,16,3},160x16,0,32,4]"
)

GRID_PANES = (
    "#{window_id}\t#{@grid-manager}\t#{pane_id}\t#{pane_width}\t#{pane_height}\t#{socket_path}"
)

COMMAND = "/opt/a_b.c@1+2-3/bin/ensemble-session"

GRID_HOOKS = ("pane-exited", "window-resized", "window-layout-changed")

MODE_HOOKS = {
    "all": GRID_HOOKS,
    "open-close": ("pane-exited", "window-layout-changed"),
    "off": (),
}  # each mode's

RETILE_ERROR = "^retile must be one of all, open-close, off, not {}$"


def done(argv, rc=0, out="", err=""):
    return SimpleNamespace(args=argv, returncode=rc, stdout=out, stderr=err)


def record(opener, pane):
    """The set-options recording session s's opener (when known) and new pane."""
    return [
        *([["tmux", "set-option", "-t", "=s:", "@opener", opener]] if opener else []),
        ["tmux", "set-option", "-t", "=s:", "@pane", pane],
    ]


def split_window(flag, pane, sock=SOCK):
    return [
        "tmux",
        "split-window",
        "-d",
        flag,
        "-P",
        "-F",
        "#{pane_id}",
        "-t",
        pane,
        "/usr/bin/env",
        "-u",
        "TMUX",
        TMUX,
        "-S",
        sock,
        "attach",
        "-t",
        "=s",
    ]


def clients_of(session):
    return ["tmux", "list-clients", "-t", f"={session}", "-F", CLIENTS]


def grid_read(pane):
    return ["tmux", "list-panes", "-t", pane, "-F", GRID_PANES]


def grid_set(window, manager, n=3, *, hooks=True, retile="all", command=COMMAND):
    """The one tmux call setting a grid window's options, unsetting @grid-busy, then per grid hook
    in order: with hooks, setting each of retile's MODE_HOOKS (each starts a no-wait tile unless
    @grid-busy is the window, under open-close only when the pane set changed); unsetting each other
    one.
    """
    flags = "--no-wait --if-panes-changed" if retile == "open-close" else "--no-wait"
    hook = (
        f"if-shell -F '#{{==:#{{@grid-busy}},{window}}}' '' "
        f"\"run-shell -b '{command} tile {flags} {window} >/dev/null 2>&1 || true'\""
    )
    mode = MODE_HOOKS[retile]
    return [
        "tmux",
        "set-option",
        "-w",
        "-t",
        window,
        "@grid-manager",
        manager,
        ";",
        "set-option",
        "-w",
        "-t",
        window,
        "@grid-per-column",
        str(n),
        ";",
        "set-option",
        "-u",
        "-w",
        "-t",
        window,
        "@grid-busy",
        *(
            a
            for h in GRID_HOOKS
            if hooks or h not in mode
            for a in (
                (";", "set-hook", "-w", "-t", window, h, hook)
                if h in mode
                else (";", "set-hook", "-u", "-w", "-t", window, h)
            )
        ),
    ]


def mute(window):
    return ["tmux", "set-option", "-w", "-t", window, "@grid-busy", window]


def unmute(window):
    return ["tmux", "set-option", "-u", "-w", "-t", window, "@grid-busy"]


def environ(**values):
    return unittest.mock.patch.dict(os.environ, values, clear=True)


def clock(test, on_sleep=lambda _n: None):
    """time.monotonic and time.sleep patched for test, the clock at 1000 s: a sleep advances it,
    then calls on_sleep(sleeps so far). Returns the slept seconds.
    """
    now, slept = [1000.0], []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds
        on_sleep(len(slept))

    test.enterContext(unittest.mock.patch("time.monotonic", side_effect=lambda: now[0]))
    test.enterContext(unittest.mock.patch("time.sleep", side_effect=sleep))
    return slept


def hold(test, path):
    """Another holder of the flock on path till test ends (path created when missing). Returns its
    release.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    test.addCleanup(os.close, fd)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return lambda: fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def deadline(seconds=5):
    """Fail after the deadline, interrupting a blocked open."""

    def expire(*_):
        raise AssertionError(
            f"still running after {seconds} s"
        )  # An OSError would become a TuiError.

    old = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def which(tmux=TMUX, session=COMMAND):
    """session None: the real lookup."""
    real = shutil.which

    def found(cmd, *args, **kw):
        if cmd == "tmux":
            return tmux
        return session if cmd == "ensemble-session" and session else real(cmd, *args, **kw)

    return unittest.mock.patch("shutil.which", side_effect=found)


def osa(split, kind, session, *anchors):
    return ["osascript", "-e", iterm.APPLESCRIPT, split, kind, TMUX, session, *anchors]


def decorations(session, events=None, status="off"):
    target = f"={session}:"
    return [
        *([["tmux", "set-option", "-t", target, "@events", events]] if events else []),
        ["tmux", "set-hook", "-p", "-t", target, "pane-died", DIED_EVENTS if events else DIED],
        ["tmux", "set-option", "-w", "-t", target, "pane-border-status", "top"],
        [
            "tmux",
            "set-option",
            "-w",
            "-t",
            target,
            "pane-border-format",
            " #{session_name} #{@state} ",
        ],
        ["tmux", "set-option", "-t", target, "status", status],
    ]


def chained(calls):
    return [arg for call in calls for arg in (";", *call[1:])]


def recorded(session):
    """The creating call's last command: its new pane's identity (NEW's format) as @process."""
    return [";", "set-option", "-F", "-t", f"={session}:", "@process", NEW[2]]


def expected_panes(*rows):
    """Print (pane_dead, pane_tty, pane_id) rows in the list-panes call's -F format."""

    def result(argv):
        fmt = argv[argv.index("-F") + 1]
        values = [
            dict(zip(("pane_dead", "pane_tty", "pane_id"), row, strict=False)) for row in rows
        ]
        return 0, "".join(
            re.sub(r"#\{(\w+)\}", lambda m, *, v=v: v[m.group(1)], fmt) + "\n" for v in values
        )

    return result


class FakeTmux:
    """Plays tmux, and sh and osascript: records each call. `results` maps a tmux command (after any
    -u) or another program to its (rc, stdout, stderr), an exception to raise, or a function of the
    argv giving either. `pane` is what display-message prints (None: no server). On new-session it
    reads the handover file named in its argv (after EXEC); unless `hand_over` is False it plays the
    wrapper on the errpipe named next: opens it, unlinks the handover, then per `wrapper` closes it
    ("exec"), writes that errno (bytes: as given) and closes it, or keeps it open ("hang", fd
    `writer`).
    """

    def __init__(self, pane=None, fail=(), *, hand_over=True, results=None, wrapper="exec"):
        self.pane, self.fail, self.hand_over, self.results = pane, fail, hand_over, results or {}
        self.wrapper = wrapper
        self.calls, self.kwargs = [], []
        self.path = self._handover = self.mode = self.dir_mode = self.dir_at_kill = None
        self.fifo = self.fifo_mode = self.writer = None

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.kwargs.append(kw)
        key = argv[0] if argv[0] != "tmux" else argv[2] if argv[1] == "-u" else argv[1]
        if key in self.results:
            result = self.results[key]
            if callable(result):
                result = result(argv)
            if isinstance(result, BaseException):
                raise result
            return done(argv, *result)
        if argv[1] == "kill-session" and self.path:
            self.dir_at_kill = os.path.isdir(os.path.dirname(self.path))
        if argv[1] in self.fail:
            return done(argv, 1, err="boom\n")
        if argv[1] == "display-message":
            return (
                done(argv, 1, err="no server running\n")
                if self.pane is None
                else done(argv, 0, self.pane + "\n")
            )
        if argv[1] == "new-session":
            self.take_launch(argv)
            if "-P" in argv:
                return done(argv, 0, NEW_PROCESS + "\n")
        return done(argv)

    def take_launch(self, argv):
        at = argv.index(EXEC)
        self.path, self.fifo = argv[at + 1 : at + 3]
        self.mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.dir_mode = stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode)
        self.fifo_mode = os.stat(self.fifo).st_mode
        with open(self.path) as f:
            self._handover = json.load(f)
        if self.hand_over:
            self.writer = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.unlink(self.path)
            if self.wrapper != "hang":
                if self.wrapper != "exec":
                    data = self.wrapper
                    os.write(self.writer, data if isinstance(data, bytes) else b"%d" % data)
                os.close(self.writer)

    def commands(self):
        return [c[1] if c[0] == "tmux" else c[0] for c in self.calls]


MARK_KW = {**TMUX_KW, "timeout": 5}

VIEWLESS = ["-f", "#{==:#{@view},}"]

LISTING = [
    "tmux",
    "-u",
    "list-sessions",
    *VIEWLESS,
    "-F",
    "#{session_name}\t#{pane_dead}\t#{@sid}\t#{@client}\t#{@state}\t#{@pane}\t#{@opener}\t#{@display}\t#{@process}",
]

COLUMNS = ("name", "dead", "sid", "client", "state", "pane", "opener", "display", "process")

ALL_CLIENTS = [
    "tmux",
    "-u",
    "list-clients",
    "-F",
    "#{client_activity}\t#{client_tty}\t#{session_name}",
]

HOST_PANES = [
    "tmux",
    "-u",
    "list-panes",
    "-a",
    "-F",
    "#{pane_dead}\t#{pane_tty}\t#{session_name}\t#{pane_id}",
]


def tabbed(*rows, width=3):
    """A tmux result printing each row's fields tab-separated, padded with empty ones to width."""
    return 0, "".join("\t".join((*row, *[""] * (width - len(row)))) + "\n" for row in rows)


def listed(*rows):
    return tabbed(*rows, width=len(COLUMNS))


def panes(*rows):
    return tabbed(*rows, width=4)


def captured(target):
    target = target if target.startswith("%") else f"={target}:"
    return ["tmux", "capture-pane", "-p", "-J", "-t", target, "-S", "-2000"]


def expected_row(name, **fields):
    return {
        "name": name,
        "sid": None,
        "client": None,
        "state": None,
        "pane": None,
        "opener": None,
        "display": None,
        "dead": False,
        "shown": None,
        "last_lines": None,
        **fields,
    }


def seq(*results):
    """A FakeTmux result giving each of results in turn."""
    it = iter(results)
    return lambda _argv: next(it)


def slot(pane, *children):
    return types.Slot(pane, children)


def body(cell):
    return layouts.render_layout(cell).split(",", 1)[1]


def dump(text):
    """A layout body as #{window_layout} prints it, with its checksum."""
    return f"{layouts._checksum(text)},{text}"


def json_layout(cell):
    """A cell tree as tmux 3.8 prints #{window_layout}: JSON, panes with index, active and id."""
    leaves = layouts.leaf_cells(cell)

    def node(c):
        out = {"t": {"{}": "h", "[]": "v"}.get(c.kind, "p"), "w": c.w, "h": c.h, "x": c.x, "y": c.y}
        if c.kind:
            return {**out, "c": [node(child) for child in c.children]}
        return {**out, "i": leaves.index(c), **({"a": True} if c is leaves[0] else {}), "I": c.pane}

    return json.dumps({"V": 2, "L": node(cell)}, separators=(",", ":"))


def grid(cells, per_column=3, width=160, height=48, manager_width=79):
    """The grid of manager %0 in a width x height window; cells: main cells, or n for %1..%n."""
    if isinstance(cells, int):
        cells = [slot(f"%{i}") for i in range(1, cells + 1)]
    return layouts.grid_layout(
        cells, "%0", width=width, height=height, manager_width=manager_width, per_column=per_column
    )


def tiled(window):
    return [
        ["tmux", "display-message", "-p", "-t", window, "#{socket_path}"],
        ["tmux", "display-message", "-p", "-t", window, grids.GRID_WINDOW],
        unmute(window),
    ]


def opening_commands(*, window="@1", manager="%3", opener="mgr", n=3, retile="all", hooks=True):
    return [
        *record(opener, "%20"),
        grid_set(window, manager, n, hooks=hooks, retile=retile),
        *([] if retile == "off" else tiled(window)),
    ]


class StartFixture(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.bin, self.temp, self.cwd = (os.path.join(self.root, d) for d in ("bin", "temp", "cwd"))
        for d in (self.bin, self.temp, self.cwd):
            os.makedirs(d)
        self.tool = os.path.join(self.bin, "tool")
        with open(self.tool, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(self.tool, 0o755)
        for p in (
            unittest.mock.patch.object(tempfile, "tempdir", self.temp),
            unittest.mock.patch(
                "shutil.get_terminal_size", return_value=os.terminal_size((120, 40))
            ),
            unittest.mock.patch.dict(os.environ),
        ):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("TUI_ATTACH_PREFIX", None)
        self.sleep = unittest.mock.Mock()
        self.stderr = io.StringIO()

    def start(self, fake, session="s", argv=("tool", "-x", "a b"), env=None, **kw):
        env = {"PATH": self.bin, "HOME": "/h"} if env is None else env
        with redirect_stderr(self.stderr):
            return TmuxCli(fake, sleep=self.sleep).start(
                session, Launch(tuple(argv), env, cwd=self.cwd), **kw
            )


def placed(fake) -> placement.Placement:
    """A Placement whose TmuxCli, Grid and Iterm run fake."""
    tmux = TmuxCli(fake)
    return placement.Placement(tmux=tmux, grid=grids.Grid(tmux), iterm=iterm.Iterm(fake))


class PaneFixture(Case):
    def open(self, fake, env, **kw):
        self.stderr = io.StringIO()
        with environ(**env), which(kw.pop("tmux", TMUX)), redirect_stderr(self.stderr):
            return placed(fake).open_pane("s", **kw)

    def clients(self, *rows):
        """A list-clients result: rows of (activity, tty, pane[, control mode, default "0"])."""
        rows = [(*r, "0") if len(r) == 3 else r for r in rows]
        return 0, "".join(
            f"{at} {tty} {pane} {control} {SOCK}\n" for at, tty, pane, control in rows
        )


class TileFixture(Case):
    """Window @3, 160x48: manager %0 (session mgr) left, 79 wide; w<i>'s @pane is %<i>. The server's
    socket is in a temp dir, @3's lock file beside it; the clock (clock()) at 1000 s, i.e. 1000000
    ms.
    """

    SOCKET = ["tmux", "display-message", "-p", "-t", "@3", "#{socket_path}"]

    READ = [
        "tmux",
        "display-message",
        "-p",
        "-t",
        "@3",
        (
            "#{window_width}\t#{window_height}\t#{window_zoomed_flag}\t#{window_layout}\t#{@grid-manager}\t"
            "#{@grid-per-column}\t#{@grid-panes}\t#{@grid-tiles}"
        ),
    ]

    PANES = ["tmux", "list-panes", "-t", "@3", "-F", "#{pane_id}"]

    TEARDOWN = [
        "tmux",
        "set-hook",
        "-u",
        "-w",
        "-t",
        "@3",
        "pane-exited",
        ";",
        "set-hook",
        "-u",
        "-w",
        "-t",
        "@3",
        "window-resized",
        ";",
        "set-hook",
        "-u",
        "-w",
        "-t",
        "@3",
        "window-layout-changed",
        ";",
        "set-option",
        "-u",
        "-w",
        "-t",
        "@3",
        "@grid-manager",
        ";",
        "set-option",
        "-u",
        "-w",
        "-t",
        "@3",
        "@grid-per-column",
        ";",
        "set-option",
        "-u",
        "-w",
        "-t",
        "@3",
        "@grid-tiles",
        ";",
        "set-option",
        "-u",
        "-w",
        "-t",
        "@3",
        "@grid-busy",
        ";",
        "set-option",
        "-u",
        "-w",
        "-t",
        "@3",
        "@grid-panes",
    ]

    UNMUTE = ["tmux", "set-option", "-u", "-w", "-t", "@3", "@grid-busy"]

    THREE = "0a6c,160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17,2,80x15,80,33,3]}"

    W3 = [("$2", "w1", "mgr", "%1"), ("$3", "w2", "mgr", "%2"), ("$4", "w3", "mgr", "%3")]

    WIDE = (
        "8b77,160x48,0,0{100x48,0,0,0,59x48,101,0[59x24,101,0,1,59x23,101,25,2]}",
        ["%0", "%1", "%2"],
        [("$2", "w1", "mgr", "%1"), ("$3", "a", "w1", "%2")],
    )

    WIDE_SENT = [
        "tmux",
        "select-layout",
        "-t",
        "@3",
        "e37e,160x48,0,0{100x48,0,0,0,59x48,101,0{29x48,101,0,1,29x48,131,0,2}}",
    ]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sock = os.path.join(tmp.name, "default")
        self.lock = self.sock + "-3.lock"
        clock(self)

    def fake(
        self,
        layout,
        panes,
        rows=(),
        *,
        manager="%0",
        n="",
        zoomed="0",
        size=(160, 48),
        tiles="",
        sock=None,
        recorded=None,
        fail=(),
        message=(0,),
        **results,
    ):
        """A tmux printing these for @3, its socket `sock` (default self.sock), `message` the result
        of another display-message; rows: (session id, name, @opener, @pane) besides mgr's;
        recorded: @grid-panes (None: panes' pane set, kept as self.set).
        """
        rows = [("$1", "mgr", "", ""), *rows]
        self.set = " ".join(sorted(panes, key=lambda p: int(p[1:])))
        recorded = self.set if recorded is None else recorded
        printed = {
            self.SOCKET[-1]: (0, f"{self.sock if sock is None else sock}\n"),
            self.READ[-1]: (
                0,
                "\t".join((*map(str, size), zoomed, layout, manager, n, recorded, tiles)) + "\n",
            ),
        }
        return FakeTmux(
            fail=fail,
            results={
                "display-message": lambda argv: printed.get(argv[-1], message),
                "list-panes": (0, "".join(f"{p}\n" for p in panes), ""),
                "list-sessions": (0, "".join("\t".join((*row, SOCK)) + "\n" for row in rows), ""),
                **results,
            },
        )

    def tile(
        self, layout, panes, rows=(), *, per_column=None, wait=True, if_panes_changed=False, **kw
    ):
        """tile @3 given what tmux prints (fake's arguments). Returns the fake."""
        fake = self.fake(layout, panes, rows, **kw)
        grids.Grid(TmuxCli(fake)).tile(
            "@3", per_column=per_column, wait=wait, if_panes_changed=if_panes_changed
        )
        return fake

    def applied(self, send, times="1000000"):
        """The two tmux calls of a re-tile sending `send` (a tmux call of swap-panes, then
        select-layout): @grid-tiles set to times, @grid-panes to the last fake's pane set and
        @grid-busy to @3 before it in one command, then @grid-busy unset.
        """
        return [
            [
                "tmux",
                "set-option",
                "-w",
                "-t",
                "@3",
                "@grid-tiles",
                times,
                ";",
                "set-option",
                "-w",
                "-t",
                "@3",
                "@grid-panes",
                self.set,
                ";",
                "set-option",
                "-w",
                "-t",
                "@3",
                "@grid-busy",
                "@3",
                ";",
                *send[1:],
            ],
            self.UNMUTE,
        ]
