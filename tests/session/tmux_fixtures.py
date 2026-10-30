"""Recorded tmux transport and fixtures."""

import fcntl
import io
import json
import os
import shutil
import signal
import tempfile
import unittest
import unittest.mock
from contextlib import contextmanager, redirect_stderr
from types import SimpleNamespace

from ensemble.session.engine.tmux_cli import TmuxCli
from ensemble.session.viewer import grid as grids
from ensemble.session.viewer import layout as layouts
from ensemble.session.viewer import placement, types
from tests.session.case import SID, Case
from tests.utils.process_fixtures import DEVNULL

CLIENTS = "#{client_activity}\t#{pane_id}"

TMUX_KW = {"capture_output": True, "text": True, "stdin": DEVNULL}

SH_KW = {"stdin": DEVNULL, "timeout": 30}

ARGV = ("/fake/attach", "s")  # the Host's CommandView.argv for worker s

PRINTED = "/fake/attach s"  # its CommandView.printed

WATCH = f"; watch it with {PRINTED}"

ABSENT = (
    "can't find session: =s",
    "can't find window: @1",
    "no such session: =s:",
    "no server running on /tmp/tmux-501/default",
    "error connecting to /tmp/tmux-501/default (No such file or directory)",
)

INSIDE = {"TMUX": "/tmp/tmux-501/default,1,0", "TMUX_PANE": "%3"}

OWN = ["tmux", "display-message", "-p", "-t", "%3", "#{session_name}"]

WORKERS = ["tmux", "list-panes", "-a", "-F", grids.WORKER_PANES]

NAMES = ["tmux", "list-sessions", "-F", "#{session_name}"]

SPLIT = "#{pane_id}\t#{window_id}"

MAIN_VERTICAL = "7f31,160x48,0,0{80x48,0,0,0,79x48,81,0[79x24,81,0,1,79x23,81,25,2]}"

TILED_SPLIT = (
    "9a18,160x48,0,0[160x15,0,0{79x15,0,0,0,40x15,80,0,1,39x15,121,0,5},"
    "160x15,0,16{79x15,0,16,2,80x15,80,16,3},160x16,0,32,4]"
)

GRID_PANES = "#{window_id}\t#{@grid-manager}\t#{pane_id}\t#{pane_width}\t#{pane_height}"

COMMAND = "/opt/a_b.c@1+2-3/bin/ensemble-session"

GENERATION = "g1"

LABEL = "#{?#{@worker-label},[#{@worker-label}],}"

GRID_HOOKS = ("pane-exited", "window-resized", "window-layout-changed")

MODE_HOOKS = {
    "all": GRID_HOOKS,
    "open-close": ("pane-exited", "window-layout-changed"),
    "off": (),
}

RETILE_ERROR = "^retile must be one of all, open-close, off, not {}$"


def done(argv, rc=0, out="", err=""):
    return SimpleNamespace(args=argv, returncode=rc, stdout=out, stderr=err)


def close_hook(name, pane, window, *, sid=SID, generation=GENERATION, command=COMMAND, index=None):
    index = pane[1:] if index is None else index
    return (
        f"if-shell -F '#{{m:* {pane} *,#{{S:#{{W:#{{P: #{{pane_id}} }}}}}}}}' '' "
        f'"set-hook -u -w -t {window} after-kill-pane[{index}] ; '
        f"run-shell -b '{command} hide --if-closed --sid {sid} --generation {generation} -- {name} "
        ">/dev/null 2>&1 || true'\""
    )


def shown_hook(index, name, pane, window="@1"):
    return (
        f'after-kill-pane[{index}] if-shell -F "#{{m:* {pane} *,#{{S:#{{W:#{{P: #{{pane_id}} }}}}}}}}" '
        f"'' \"set-hook -u -w -t {window} after-kill-pane[{index}] ; run-shell -b "
        f"'{COMMAND} hide --if-closed --sid {SID} --generation {GENERATION} -- {name} "
        ">/dev/null 2>&1 || true'\""
    )


def record(
    opener,
    pane,
    *,
    window="@1",
    name="s",
    sid=SID,
    generation=GENERATION,
    label=False,
    hook=True,
    printed=PRINTED,
    pruned=(),
    index=None,
    read=None,
):
    """_record's calls tagging `pane`."""
    args = ["tmux", "set-option", "-p", "-t", pane, "@worker", name]
    args += [";", "set-option", "-p", "-t", pane, "@worker-attach", printed]
    if opener:
        args += [";", "set-option", "-p", "-t", pane, "@worker-opener", opener]
    reads = []
    if label:
        reads = [own(window, "pane-border-format"), own(window, "pane-border-status")]
        args += [
            *(";", "set-option", "-p", "-t", pane, "@worker-label", name),
            *(";", "set-option", "-u", "-w", "-t", window, "@worker-saved-pane-border-status"),
            *(";", "set-option", "-u", "-w", "-t", window, "@worker-saved-pane-border-format"),
            *(";", "set-option", "-w", "-t", window, "pane-border-status", "bottom"),
            *(";", "set-option", "-w", "-t", window, "pane-border-format", LABEL),
        ]
    hooks = ["tmux", "show-hooks", "-w", "-t", window, "after-kill-pane"]
    hooks += [";", "list-panes", "-a", "-F", "#{pane_id}"]
    if not hook:
        return [*reads, hooks, args] if read else [*reads, args]
    for stale in pruned:
        args += [";", "set-hook", "-u", "-w", "-t", window, f"after-kill-pane[{stale}]"]
    index = pane[1:] if index is None else index
    args += [
        *(";", "set-hook", "-w", "-t", window, f"after-kill-pane[{index}]"),
        close_hook(name, pane, window, sid=sid, generation=generation, index=index),
    ]
    return [*reads, hooks, args]


def own(window, option):
    """Placement's read of window's own value of option."""
    return ["tmux", "show-options", "-w", "-q", "-v", "-t", window, option]


LABELS = ["tmux", "list-panes", "-a", "-F", "#{window_id}\t#{@worker-label}\t#{pane-border-format}"]


def split_window(flag, pane, argv=ARGV):
    return ["tmux", "split-window", "-d", flag, "-P", "-F", SPLIT, "-t", pane, "--", *argv]


def split(pane="%20", window="@1"):
    return 0, f"{pane}\t{window}\n"


def worker_rows(*rows):
    """A list-panes -a result: rows of (pane, name[, opener, window, session, attach]) or lines."""
    lines = []
    for row in rows:
        if isinstance(row, str):
            lines.append(row)
            continue
        pane, name, opener, window, session, attach = (
            *row,
            *("", "@1", "mgr", PRINTED)[len(row) - 2 :],
        )
        lines.append("\t".join(("0", pane, name, opener, window, session, attach)))
    return 0, "".join(line + "\n" for line in lines)


def clients(*rows):
    return 0, "".join(f"{at}\t{pane}\n" for at, pane in rows)


def clients_of(session):
    return ["tmux", "list-clients", "-t", f"={session}", "-F", CLIENTS]


def grid_read(pane):
    return ["tmux", "list-panes", "-t", pane, "-F", GRID_PANES]


def grid_set(window, manager, n=3, *, hooks=True, retile="all", command=COMMAND):
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
    """Starts at 1000 s; sleeps advance it."""
    now, slept = [1000.0], []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds
        on_sleep(len(slept))

    test.enterContext(unittest.mock.patch("time.monotonic", side_effect=lambda: now[0]))
    test.enterContext(unittest.mock.patch("time.sleep", side_effect=sleep))
    return slept


def hold(test, path):
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


def which(session=COMMAND):
    """ensemble-session at `session`; None: the real lookup."""
    real = shutil.which

    def found(cmd, *args, **kw):
        return session if cmd == "ensemble-session" and session else real(cmd, *args, **kw)

    return unittest.mock.patch("shutil.which", side_effect=found)


class FakeTmux:
    """Records tmux/sh calls; results maps commands to outcomes."""

    def __init__(self, pane=None, fail=(), *, results=None):
        self.pane, self.fail, self.results = pane, fail, results or {}
        self.calls, self.kwargs = [], []

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
        if argv[1] in self.fail:
            return done(argv, 1, err="boom\n")
        if argv[1] == "display-message":
            return (
                done(argv, 1, err="no server running\n")
                if self.pane is None
                else done(argv, 0, self.pane + "\n")
            )
        return done(argv)

    def commands(self):
        return [c[1] if c[0] == "tmux" else c[0] for c in self.calls]


def slot(pane, *children):
    return types.Slot(pane, children)


def body(cell):
    return layouts.render_layout(cell).split(",", 1)[1]


def dump(text):
    return f"{layouts._checksum(text)},{text}"


def json_layout(cell):
    leaves = layouts.leaf_cells(cell)

    def node(c):
        out = {"t": {"{}": "h", "[]": "v"}.get(c.kind, "p"), "w": c.w, "h": c.h, "x": c.x, "y": c.y}
        if c.kind:
            return {**out, "c": [node(child) for child in c.children]}
        return {**out, "i": leaves.index(c), **({"a": True} if c is leaves[0] else {}), "I": c.pane}

    return json.dumps({"V": 2, "L": node(cell)}, separators=(",", ":"))


def grid(cells, per_column=3, width=160, height=48, manager_width=79):
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
    """A grid show's calls after its split."""
    return [
        *record(opener, "%20", window=window, hook=hooks),
        grid_set(window, manager, n, hooks=hooks, retile=retile),
        *([] if retile == "off" else tiled(window)),
    ]


def unknown(_: str) -> bool:
    return False


def placed(fake) -> placement.Placement:
    tmux = TmuxCli(fake)
    return placement.Placement(tmux=tmux, grid=grids.Grid(tmux, known=unknown))


class PaneFixture(Case):
    def open(self, fake, env, command=COMMAND, **kw):
        """open_pane s; ensemble-session at `command` (None: on PATH)."""
        self.stderr = io.StringIO()
        kw = {"sid": SID, "generation": GENERATION, **kw}
        with environ(**env), which(command), redirect_stderr(self.stderr):
            return placed(fake).open_pane("s", ARGV, PRINTED, **kw)


class GridFixture(Case):
    """The caller is in tmux session mgr, pane %3, whose most recent client shows pane %9."""

    NEW = [("@1", "", "%3", "160", "48")]  # mgr's window, no grid yet

    NOTICE = "tui: show: grid: --split/--split-from ignored\n"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sock = os.path.join(tmp.name, "default")

    SHOWN = clients((5, "%9"))

    def fake(self, windows, rows=(), own="mgr", clients=SHOWN, **results):
        printed = {
            "#{socket_path}": (0, self.sock + "\n"),
            grids.GRID_WINDOW: (0, "160\t48\t1\t\t%3\t\t\t\n"),  # zoomed
        }

        def list_panes(argv):
            if "-a" in argv:
                return worker_rows(*rows)
            if argv[3] not in windows:
                return 1, "", f"can't find pane: {argv[3]}\n"
            return 0, "".join("\t".join(r) + "\n" for r in windows[argv[3]])

        return FakeTmux(
            results={
                "display-message": lambda argv: printed.get(argv[-1], (0, own + "\n")),
                "list-clients": clients,
                "list-panes": list_panes,
                "split-window": split(),
                **results,
            }
        )

    def open(self, fake, env=INSIDE, retile="all", command=COMMAND, argv=ARGV, **kw):
        """open_pane s; ensemble-session at `command` (None: on PATH)."""
        self.stderr = io.StringIO()
        kw = {"sid": SID, "generation": GENERATION, **kw}
        with environ(**env), which(command), redirect_stderr(self.stderr):
            return placed(fake).open_pane("s", argv, PRINTED, retile=retile, **kw)

    def split(self, flag, pane, window="@1"):
        """Hooks muted first, so a previous mode's hook starts no tile; grid_set unmutes."""
        return [mute(window), split_window(flag, pane)]


class TileFixture(Case):
    """Window @3, manager %0, workers %1..%n; clock starts at 1000000 ms."""

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

    W3 = [("%1", "w1", "mgr"), ("%2", "w2", "mgr"), ("%3", "w3", "mgr")]

    WIDE = (
        "8b77,160x48,0,0{100x48,0,0,0,59x48,101,0[59x24,101,0,1,59x23,101,25,2]}",
        ["%0", "%1", "%2"],
        [("%1", "w1", "mgr"), ("%2", "a", "w1")],
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
        sessions=("mgr",),
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
        """rows: worker_rows' rows, in window @3."""
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
                "list-panes": lambda argv: (
                    worker_rows(*(r if isinstance(r, str) else (*r, "@3") for r in rows))
                    if "-a" in argv
                    else (0, "".join(f"{p}\n" for p in panes))
                ),
                "list-sessions": (0, "".join(f"{name}\n" for name in sessions)),
                **results,
            },
        )

    def tile(
        self,
        layout,
        panes,
        rows=(),
        *,
        per_column=None,
        wait=True,
        if_panes_changed=False,
        known=unknown,
        **kw,
    ):
        fake = self.fake(layout, panes, rows, **kw)
        grids.Grid(TmuxCli(fake), known=known).tile(
            "@3", per_column=per_column, wait=wait, if_panes_changed=if_panes_changed
        )
        return fake

    def applied(self, send, times="1000000"):
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
