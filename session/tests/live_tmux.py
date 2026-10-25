"""Private tmux fixtures with checked socket selectors, inherited launchers and cleanup."""

import inspect
import os
import resource
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import NamedTuple
from unittest import mock

from session.tests.case import PLUGIN_BIN, child_env
from session.tests.grid import GridCheck
from session.tests.integration.clients.claude import fake_claude

# two words: tmux execs them, not the user's login shell (and its rc files)
IDLE = ("cat", "-")
TIMEOUT = 10
POLL = 0.05
RESEND = 1  # s: a just-listed client may drop input; every POLL would fill its pty
SOCKET_MAX = 100  # sun_path: 104 bytes (macOS), 108 (Linux)
HEADROOM = 256
PANES = "#{pane_id} #{pane_left} #{pane_top} #{pane_width} #{pane_height}"


_SOCKETS: set[str] = set()
_PROGRAMS: dict[str, str] = {}


def _require_socket(argv, env, sockets):
    if isinstance(argv, (str, bytes)):
        raise RuntimeError("live_tmux: expected a command argument list")
    socket = None
    index = 1
    while index < len(argv):
        option = os.fsdecode(argv[index])
        if not option.startswith("-") or option in {"-", "--"}:
            break
        if option in {"-S", "-f"}:
            index += 1
            if index == len(argv) or not argv[index]:
                raise RuntimeError("live_tmux: missing " + option + " value")
            if option == "-S":
                if socket is not None:
                    raise RuntimeError("live_tmux: duplicate -S selector")
                socket = os.fsdecode(argv[index])
        elif option not in {"-u", "-v", "-q", "-2", "-C", "-CC", "-D", "-N", "-V", "-h"}:
            raise RuntimeError("live_tmux: unsupported global option " + option)
        index += 1
    if socket is None:
        socket = env.get("TMUX", "").partition(",")[0]
        if not socket:
            directory = env.get("TMUX_TMPDIR", "")
            if not directory:
                raise RuntimeError("live_tmux: missing private TMUX_TMPDIR or -S selector")
            socket = os.path.join(directory, f"tmux-{os.getuid()}", "default")
    if not os.path.isabs(socket) or os.path.realpath(socket) not in sockets:
        raise RuntimeError("live_tmux: tmux requires a registered fixture socket")


def _guard_popen():
    popen = subprocess.Popen

    class GuardedPopen(popen):
        def __init__(self, *args, **kwargs):
            argv = args[0] if args else kwargs["args"]
            executable = kwargs.get("executable") or (
                argv if isinstance(argv, (str, bytes)) else argv[0]
            )
            if os.path.basename(os.fsdecode(executable)) == "tmux":
                env = kwargs.get("env")
                _require_socket(argv, os.environ if env is None else env, _SOCKETS)
            super().__init__(*args, **kwargs)

    return mock.patch("subprocess.Popen", new=GuardedPopen)


class Client(NamedTuple):
    proc: subprocess.Popen
    master: int
    thread: threading.Thread
    stop: threading.Event


class Server:
    def __init__(self, case, home: str, *, bin_dir: str | None = None):
        self.case, self.home, self.size, self.clients = case, home, (200, 50), []
        program = shutil.which("tmux")
        if program is None:
            case.skipTest("tmux not installed")
        self._program = _PROGRAMS.get(os.path.realpath(program), program)
        self.root = os.path.realpath(tempfile.mkdtemp())
        case.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.mkdir(os.path.join(self.root, f"tmux-{os.getuid()}"), 0o700)
        self.socket = os.path.join(self.root, f"tmux-{os.getuid()}", "default")
        self.bin = bin_dir if bin_dir is not None else os.path.join(self.root, "bin")
        os.mkdir(self.bin)
        self.path = fake_claude.install(
            self.bin,
            tail=os.pathsep.join(
                (
                    PLUGIN_BIN,
                    os.path.dirname(sys.executable),
                    os.path.dirname(self._program),
                    "/usr/bin",
                    "/bin",
                )
            ),
        )
        self.program = os.path.join(self.bin, "tmux")
        launcher = (
            f"#!{sys.executable} -I\nimport os, sys\n"
            + inspect.getsource(_require_socket)
            + f"\ntry: _require_socket(['tmux', *sys.argv[1:]], os.environ, {{{self.socket!r}}})\n"
            + "except RuntimeError as error: print(error, file=sys.stderr); raise SystemExit(2)\n"
            + f"os.execv({self._program!r}, [{self._program!r}, '-S', {self.socket!r}, *sys.argv[1:]])\n"
        )
        with open(self.program, "w", encoding="utf-8") as stream:
            stream.write(launcher)
        os.chmod(self.program, 0o700)
        _SOCKETS.add(self.socket)
        _PROGRAMS[os.path.realpath(self.program)] = self._program
        case.enterContext(_guard_popen())
        case.addCleanup(_PROGRAMS.pop, os.path.realpath(self.program))
        case.addCleanup(_SOCKETS.discard, self.socket)
        case.addCleanup(self._close_clients)
        case.addCleanup(self.kill)
        if len(os.fsencode(self.socket)) > SOCKET_MAX:
            case.fail(f"tmux socket path over {SOCKET_MAX} bytes: {self.socket}")

    def env(self, **extra: str) -> dict[str, str]:
        root = os.environ.get("ENSEMBLE_CONTROL_ROOT")
        return {
            "PATH": self.path,
            "HOME": self.home,
            "PYTHONUTF8": "1",
            "TMUX_TMPDIR": self.root,
            **({"ENSEMBLE_CONTROL_ROOT": root} if root else {}),
            **extra,
        }

    def child_env(self) -> dict[str, str]:
        env = child_env()
        return {
            **env,
            "PATH": self.bin + os.pathsep + env.get("PATH", os.defpath),
            "TMUX_TMPDIR": self.root,
        }

    def start(self, manager: str, width: int = 200, height: int = 50) -> None:
        """Capped (RLIMIT_NPROC) at the user's processes plus HEADROOM against a fork loop."""
        self.size = width, height
        _, hard = resource.getrlimit(resource.RLIMIT_NPROC)
        uid = str(os.getuid())
        ps = (
            ["ps", "-L", "-U", uid, "-o", "lwp="]
            if sys.platform.startswith("linux")
            else ["ps", "-U", uid, "-o", "pid="]
        )
        cap = (
            len(subprocess.run(ps, capture_output=True, text=True, check=True).stdout.split())
            + HEADROOM
        )
        cap = cap if hard == resource.RLIM_INFINITY else min(cap, hard)
        subprocess.run(
            [
                self._program,
                "-u",
                "-S",
                self.socket,
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                manager,
                "-x",
                str(width),
                "-y",
                str(height),
                *IDLE,
            ],
            env=self.env(),
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT,
            check=True,
            preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_NPROC, (cap, hard)),
        )

    def attach(self, session: str) -> None:
        master, slave = os.openpty()
        try:
            proc = subprocess.Popen(
                [self._program, "-S", self.socket, "-CC", "attach", "-t", f"={session}"],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=self.env(TERM="xterm-256color"),
                start_new_session=True,
            )
        except OSError:
            os.close(master)
            raise
        finally:
            os.close(slave)
        stop = threading.Event()
        thread = threading.Thread(target=_drain, args=(master, stop), daemon=True)
        thread.start()
        self.clients.append(Client(proc, master, thread, stop))

        def listed() -> bool:
            if proc.poll() is not None:
                self.case.fail(f"tmux -CC attach -t ={session} exited {proc.returncode}")
            rows = self._out(
                "list-clients",
                "-t",
                f"={session}",
                "-F",
                "#{client_pid} #{client_control_mode} #{client_tty}",
            ).splitlines()
            return any(r.startswith(f"{proc.pid} 1 /") for r in rows)

        wait(listed, what=f"a control-mode client of {session}")
        width, height = self.size
        sent = -RESEND

        def sized() -> bool:
            nonlocal sent
            if time.monotonic() - sent >= RESEND:
                os.write(master, f"refresh-client -C {width}x{height}\n".encode())
                sent = time.monotonic()
            return (
                self._out(
                    "display-message",
                    "-p",
                    "-t",
                    f"={session}:",
                    "#{window_width}x#{window_height}",
                )
                == f"{width}x{height}\n"
            )

        wait(sized, what=f"{session}'s window at {width}x{height}")

    def tmux(self, *args: str) -> subprocess.CompletedProcess:
        # -u: without locale or TMUX, tmux prints tabs and non-ASCII as "_"
        return subprocess.run(
            [self._program, "-u", "-S", self.socket, *args],
            env=self.env(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT,
        )

    def inside(self, session: str) -> dict[str, str]:
        pid = self._out("display-message", "-p", "#{pid}").strip()
        pane = self._out("display-message", "-p", "-t", f"={session}:", "#{pane_id}").strip()
        return {"TMUX": f"{self.socket},{pid},0", "TMUX_PANE": pane}

    def panes(self, window: str) -> dict[str, tuple[int, int, int, int]]:
        rows = (
            line.split() for line in self._out("list-panes", "-t", window, "-F", PANES).splitlines()
        )
        return {r[0]: (int(r[1]), int(r[2]), int(r[3]), int(r[4])) for r in rows}

    def pane_of(self, session: str) -> str:
        return self.option(session, "@pane")

    def option(self, session: str, key: str) -> str:
        return self._out("show-options", "-qv", "-t", f"={session}:", key).removesuffix("\n")

    def _out(self, *args: str) -> str:
        res = self.tmux(*args)
        if res.returncode:
            self.case.fail(f"tmux {' '.join(args)}: exit {res.returncode}: {res.stderr.strip()}")
        return res.stdout

    def kill(self) -> None:
        res = self.tmux("list-panes", "-a", "-F", "#{pid} #{pane_pid}")
        self.tmux("kill-server")
        pids = {int(p) for p in res.stdout.split() if p.isdigit()} if res.returncode == 0 else set()
        wait(
            lambda: not any(map(alive, pids)),
            what=f"the tmux server and its panes' processes {sorted(pids)} to end",
        )

    def _close_clients(self) -> None:
        for c in self.clients:
            try:
                c.proc.wait(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                c.proc.kill()
                c.proc.wait()
            c.stop.set()
            c.thread.join()
            os.close(c.master)


def _drain(master: int, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            if select.select([master], [], [], POLL)[0] and not os.read(master, 65536):
                return
        except OSError:
            return


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def events(path: str) -> list[str]:
    try:
        with open(path) as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def wait(check, timeout: float = TIMEOUT, what: str = "check() to hold"):
    """check()'s first truthy value; an AssertionError counts as falsy."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = check()
        except AssertionError as e:
            value = e
        else:
            if value:
                return value
        if time.monotonic() > deadline:
            raise AssertionError(
                f"timed out after {timeout} s waiting for {what}; last value: {value!r}"
            )
        time.sleep(POLL)


def assert_grid(
    case, server: Server, manager: str, columns: list, manager_width: int | None = None
) -> bool:
    """Assert manager's grid matches columns, including cell ownership and geometry."""
    return GridCheck(case, server, manager, columns).check(manager_width)
