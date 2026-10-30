"""Private tmux fixtures."""

import inspect
import json
import os
import resource
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import NamedTuple
from unittest import mock

from ensemble.session.harness import guard
from ensemble.session.hosts import base as host_base
from ensemble.session.hosts.tmux import TmuxHost
from ensemble.utils import Record
from tests.session import harnesses
from tests.session.case import PLUGIN_BIN, child_env
from tests.session.grid import GridCheck
from tests.session.integration.clients.claude import fake_claude
from tests.utils.jsonl import read_jsonl

IDLE = ("cat", "-")
MANAGER = "mgr"
TIMEOUT = 10
POLL = 0.05
RESEND = 1  # s: a just-listed client may drop input; every POLL would fill its pty
SOCKET_MAX = 100  # sun_path: 104 bytes (macOS), 108 (Linux)
HEADROOM = 256
NPROC_PROBE = os.path.join(os.path.dirname(__file__), "nproc_probe.py")
PANES = "#{pane_id} #{pane_left} #{pane_top} #{pane_width} #{pane_height}"
WORKER_PANES = "#{pane_id}\t#{@worker}\t#{@worker-opener}\t#{window_id}\t#{pane_dead}"
GONE = ("no server running", "error connecting")


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


class WorkerPane(NamedTuple):
    """A pane whose @worker names a worker."""

    pane: str
    name: str
    opener: str
    window: str
    dead: bool


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
        case.addCleanup(harnesses.end, home)  # first: a worker outlives its display
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
        """RLIMIT_NPROC: the uid's processes plus HEADROOM, against a fork loop."""
        self.size = width, height
        _, hard = resource.getrlimit(resource.RLIMIT_NPROC)
        if sys.platform.startswith("linux"):
            probe = [sys.executable, "-I", NPROC_PROBE]
            used = int(subprocess.run(probe, capture_output=True, text=True, check=True).stdout)
        else:
            ps = ["ps", "-U", str(os.getuid()), "-o", "pid="]
            used = len(
                subprocess.run(ps, capture_output=True, text=True, check=True).stdout.split()
            )
        cap = used + HEADROOM
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

    def worker_panes(self, name: str | None = None, *, dead: bool = False) -> list[WorkerPane]:
        """Worker panes (of worker `name`), lowest first; dead: with dead ones."""
        res = self.tmux("list-panes", "-a", "-F", WORKER_PANES)
        if res.returncode:
            if any(word in res.stderr for word in GONE):
                return []
            self.case.fail(f"tmux list-panes -a: exit {res.returncode}: {res.stderr.strip()}")
        found: dict[str, WorkerPane] = {}
        for line in res.stdout.splitlines():
            pane, worker, opener, window, gone = line.split("\t")
            if worker and (name is None or worker == name) and (dead or gone != "1"):
                found.setdefault(pane, WorkerPane(pane, worker, opener, window, gone == "1"))
        return sorted(found.values(), key=lambda p: int(p.pane[1:]))

    def pane_of(self, name: str) -> str:
        """`name`'s newest live worker pane, "" if none."""
        found = self.worker_panes(name)
        return found[-1].pane if found else ""

    def opener_of(self, name: str) -> str:
        """pane_of's @worker-opener, "" if none."""
        found = self.worker_panes(name)
        return found[-1].opener if found else ""

    def sessions(self) -> list[str]:
        return sorted(self._out("list-sessions", "-F", "#{session_name}").split())

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


def crash_owner(ensemble: str, name: str) -> None:
    """kill -9 of tui session `name`'s owning harness, if any."""
    try:
        with open(os.path.join(ensemble, "tui", name, "session.json"), encoding="utf-8") as f:
            owner = json.load(f)["owner"]
        with open(os.path.join(owner, "harness.json"), encoding="utf-8") as f:
            pid = json.load(f)["pid"]
    except (OSError, ValueError, KeyError, TypeError):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    wait(lambda: not alive(pid), what=f"session harness {pid} to end")


def sized(worker, size, what: str = "the worker's terminal size") -> None:
    """Waits until `stty size` in worker answers size (WxH or a function)."""
    asks = [0, -RESEND]  # the latest ask's number and time

    def answered() -> bool:
        if time.monotonic() - asks[1] >= RESEND:
            asks[0] += 1
            worker.write(f"echo ask-{asks[0]} $(stty size)\n".encode())
            asks[1] = time.monotonic()
        width, height = (size() if callable(size) else size).split("x")
        lines = (line.strip() for line in worker.screen().splitlines())
        return f"ask-{asks[0]} {height} {width}" in lines

    wait(answered, what=f"{what} at {size if isinstance(size, str) else 'its display size'}")


def running(name: str, host: str = "tmux") -> bool:
    """Whether tmux Host `host` lists worker `name`, its guard live."""
    return name in TmuxHost(host).list()


def attached(server: Server, pane: str, command: str, timeout: float = TIMEOUT) -> str:
    """pane, once live and running `command`; it may show nothing yet."""
    argv = shlex.split(command)

    def live() -> bool:
        res = server.tmux(
            "display-message", "-p", "-t", pane, "#{pane_dead}\t#{pane_start_command}"
        )
        dead, _, start = res.stdout.removesuffix("\n").partition("\t")
        return res.returncode == 0 and dead == "0" and shlex.split(start) == argv

    wait(live, timeout, what=f"{pane} running {command}")
    return pane


def alive(pid: int) -> bool:
    """Not ended: a zombie has, its parent not reaping it yet."""
    info = guard.process_info(pid)
    return info is not None and not info[2]


def activity(name: str, home: str | None = None) -> str | None:
    """Tui worker `name`'s harness's last mark this generation, or None."""
    with mock.patch.dict(os.environ, {} if home is None else {"HOME": home}):
        found = host_base.worker_state(name)
        if found is None or found.generation is None:
            return None
        word = host_base.activity(name, found.generation)
    return None if word is None else str(word)


def events(path: str) -> list[str]:
    try:
        with open(path) as f:
            return [line.removesuffix("\n") for line in f if line.endswith("\n")]
    except FileNotFoundError:
        return []


_events = events


def wait_events(path: str, expected, timeout: float = TIMEOUT) -> list[str]:
    """All (session, word) notifications, in either order."""

    def complete():
        lines = events(path)
        found = {tuple(line.split()[1:3]) for line in lines}
        return lines if set(expected) <= found else None

    return wait(complete, timeout, f"events {expected!r}")


def logged(path: str) -> list[dict]:
    return read_jsonl(path, missing_ok=True)


def wait(check, timeout: float = TIMEOUT, what: str = "check() to hold"):
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
    """Assert manager's grid matches columns."""
    return GridCheck(case, server, manager, columns).check(manager_width)


class Calls:
    WAIT = TIMEOUT
    EVENT = "{count} x {name} {word}"

    def bins(self) -> tuple:
        return ()

    def env(self, *, inside: str | None = MANAGER, **extra: str | None) -> dict[str, str]:
        """A caller's env in `inside`; an extra None drops its key."""
        env = self.server.env(**(self.server.inside(inside) if inside else {}))
        env["PATH"] = os.pathsep.join(
            (*self.bins(), self.server.bin, os.path.dirname(sys.executable), env["PATH"])
        )
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def ok(self, result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
        self.assertEqual(result.returncode, 0, self.ok_message(result))
        return result

    def ok_message(self, result: subprocess.CompletedProcess) -> str:
        return f"{shlex.join(result.args)}\n{result.stdout}\n{result.stderr}"

    def record(self, result: subprocess.CompletedProcess) -> Record:
        lines = self.ok(result).stdout.splitlines()
        self.assertEqual(len(lines), 1, result.stdout)
        return Record.parse(lines[0])

    def scene(self, name: str, steps=None, turns=(), **more) -> dict[str, str]:
        path = os.path.join(self.root, f"{name}.claude.json")
        steps = [f"Hi from {name}."] if steps is None else steps
        scenario = {"steps": steps, "turns": list(turns), "log": self.log(name)}
        with open(path, "w", encoding="utf-8") as f:
            json.dump({**scenario, "commands": self.log(name, "commands"), **more}, f)
        return {fake_claude.ENV: path}

    def log(self, name: str, kind: str = "claude") -> str:
        return os.path.join(self.root, f"{name}.{kind}.jsonl")

    def wait_event(self, name: str, word: str, count: int = 1, events: str | None = None):
        def found():
            self.poke()
            lines = _events(events or self.events)
            hits = [line for line in lines if line.split(" ")[1:3] == [name, word]]
            return lines if len(hits) >= count else None

        return wait(found, self.WAIT, what=self.EVENT.format(count=count, name=name, word=word))

    def poke(self) -> None:
        # a no-op job reaps a dead pane tmux 3.4 may leave
        self.server.tmux("run-shell", "-b", "true")

    @contextmanager
    def poking(self):
        stop = threading.Event()

        def run():
            while not stop.wait(POLL * 4):
                self.poke()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()
