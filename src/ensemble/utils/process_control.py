"""Generation-scoped process control over a private Unix socket. A controller never reaps its
anchor until that process group has no live members, so its numeric identity cannot be reused.
After the group ends it retires once a client accepted it, its directory is gone, or TIMEOUT passed.
The standalone entry point runs prepare()'s command with inherited terminal, environment and mask.
Control directories live under the caller's root, else $ENSEMBLE_CONTROL_ROOT, else /tmp; the
controller records the anchor's start, exit and retirement in its state.json (STATE).
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence

POLL = 0.05
TIMEOUT = 10
ROOT_ENV = "ENSEMBLE_CONTROL_ROOT"
STATE = "state.json"  # {"started": message, "exited": rc, "done": rc}, each key once written


def _write(path, value):
    temporary = path + ".tmp"
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
        json.dump(value, stream)
    os.replace(temporary, path)


def _root(root):
    return root or os.environ.get(ROOT_ENV) or "/tmp"


def _read(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return None


class _Group:
    def __init__(self, pid: int, channel: int):
        self.pid: int | None = pid
        self.channel = channel
        self.pending = b""
        self.returncode: int | None = None
        self.eof = False
        os.set_blocking(channel, False)

    @classmethod
    def launch(cls, argv, cwd, **options):
        read, write = os.pipe()
        try:
            pid = os.fork()
        except BaseException:
            os.close(read)
            os.close(write)
            raise
        if pid == 0:
            os.close(read)
            cls._anchor(write, argv, cwd, **options)
        os.close(write)
        return cls(pid, read)

    @staticmethod
    def _anchor(channel, argv, cwd, *, blocked_signals=(), **options):  # pragma: no cover
        try:
            os.setpgid(0, 0)
            signal.pthread_sigmask(signal.SIG_BLOCK, blocked_signals)
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, lambda *_: None)
            if os.isatty(0):
                prior = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
                os.tcsetpgrp(0, os.getpid())
                signal.signal(signal.SIGTTOU, prior)
            program = subprocess.Popen(argv, cwd=cwd, **options)
            os.write(channel, b'{"started": true}\n')
            rc = program.wait()
            os.write(channel, json.dumps({"rc": rc}).encode() + b"\n")
        except (OSError, ValueError) as error:
            message = {
                "error": error.strerror if isinstance(error, OSError) else str(error),
                "errno": error.errno if isinstance(error, OSError) else errno.EINVAL,
                "filename": error.filename if isinstance(error, OSError) else None,
            }
            os.write(channel, json.dumps(message).encode() + b"\n")
        finally:
            os._exit(0)

    def receive(self) -> list[dict]:
        with contextlib.suppress(BlockingIOError):
            data = os.read(self.channel, 65536)
            self.eof = not data
            self.pending += data
        messages = []
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            message = json.loads(line)
            if "rc" in message:
                self.returncode = message["rc"]
            elif "error" in message:
                self.returncode = 1
            messages.append(message)
        return messages

    def live(self) -> bool:
        if self.pid is None:
            return False
        self.receive()
        if self.returncode is None and not self.eof:
            return True
        result = subprocess.run(
            ["ps", "-A", "-o", "pid=,pgid=,stat="],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": os.defpath, "LC_ALL": "C"},
        )
        if result.returncode:
            raise OSError(f"cannot inspect process group: {result.stderr.strip()}")
        for line in result.stdout.splitlines():
            _, group, state = line.split()
            if int(group) == self.pid and not state.startswith("Z"):
                return True
        self._reap()
        return False

    def send(self, sig: int) -> None:
        if self.live() and self.pid is not None:
            try:
                os.killpg(self.pid, sig)
            except ProcessLookupError:
                if self.eof or self.returncode is not None:
                    self._reap()

    def kill(self) -> None:
        self.send(signal.SIGKILL)

    def _reap(self) -> None:
        if self.pid is not None:
            _, status = os.waitpid(self.pid, 0)
            self.pid = None
            self.receive()
            if self.returncode is None:
                self.returncode = os.waitstatus_to_exitcode(status)


class ProcessControl:
    def __init__(self, path: str):
        self.path = path
        self.ended = False
        self.accepted = False
        self.requested = False
        self.cancelled = False
        self._recorded: dict = {}

    @classmethod
    def attach(cls, path: str) -> ProcessControl:
        real = os.path.realpath(path)
        attached = cls(real)
        try:
            folder = os.stat(real)
        except FileNotFoundError:
            attached.ended = True
            return attached
        if not stat.S_ISDIR(folder.st_mode):
            raise PermissionError("process controller is not a directory")
        try:
            control = os.stat(os.path.join(real, "control"))
        except FileNotFoundError:
            attached.ended = True
            return attached
        if not stat.S_ISSOCK(control.st_mode):
            raise PermissionError("process controller control is not a socket")
        return attached

    @classmethod
    def prepare(
        cls,
        argv: Sequence[str],
        cwd: str,
        *,
        env: Mapping[str, str] | None = None,
        inherit_env: Sequence[str] = (),
        blocked_signals: Sequence[int] = (),
        root: str | None = None,
    ) -> ProcessControl:
        """Persist the launch; inherit_env replaces explicit entries with controller values."""
        path = tempfile.mkdtemp(prefix="apm-group-", dir=_root(root))
        try:
            launch = {"argv": list(argv), "cwd": cwd}
            if env is not None:
                launch["env"] = dict(env)
            if inherit_env:
                launch["inherit_env"] = list(inherit_env)
            if blocked_signals:
                launch["blocked_signals"] = list(blocked_signals)
            _write(os.path.join(path, "launch.json"), launch)
            _write(os.path.join(path, "lock"), {})
        except BaseException:
            shutil.rmtree(path)
            raise
        return cls(path)

    def command(self) -> list[str]:
        return [sys.executable, "-I", os.path.abspath(__file__), self.path]

    def remove(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def cancel(self) -> None:
        try:
            with open(os.path.join(self.path, "lock"), encoding="utf-8") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(os.path.join(self.path, "launch.json"))
                ready = os.path.exists(os.path.join(self.path, "control"))
        except FileNotFoundError:
            return
        if not ready:
            self.remove()
            return
        try:
            self._request("cancel")
        except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError):
            self.remove()
            return
        deadline = time.monotonic() + TIMEOUT
        while os.path.exists(self.path):
            if time.monotonic() >= deadline:
                raise TimeoutError("process controller did not cancel")
            time.sleep(POLL)

    def wait_started(self, timeout: float = TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while (message := self._state().get("started")) is None:
            if time.monotonic() >= deadline:
                raise TimeoutError("process controller did not start")
            if os.path.exists(os.path.join(self.path, "control")):
                with contextlib.suppress(ConnectionRefusedError):
                    self._request("start")
            time.sleep(POLL)
        self._request("started")
        if "error" in message:
            raise OSError(message["errno"], message["error"], message.get("filename"))

    def running(self) -> bool:
        return (
            not self.ended
            and os.path.isdir(self.path)
            and not {"exited", "done"} & self._state().keys()
        )

    def live(self) -> bool:
        return self._request("live")

    def kill(self) -> None:
        self._request("kill")

    def terminate(self) -> None:
        self._request(f"signal {signal.SIGTERM}")

    def _request(self, command: str) -> bool:
        if self.ended or not os.path.isdir(self.path) or "done" in self._state():
            self.ended = True
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(TIMEOUT)
                connection.connect(os.path.join(self.path, "control"))
                connection.sendall(command.encode() + b"\n")
                with connection.makefile("rb") as stream:
                    line = stream.readline()
                if not line:
                    raise ConnectionResetError("process controller disconnected")
                response = json.loads(line)
        except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError):
            try:
                os.stat(os.path.join(self.path, "control"), follow_symlinks=False)
            except FileNotFoundError:
                self.ended = True
                return False
            if not os.path.isdir(self.path) or "done" in self._state():
                self.ended = True
                return False
            raise
        if "error" in response:
            raise OSError(response["errno"], response["error"])
        self.ended = not response["live"]
        return response["live"]

    def run(self) -> int:
        read, write = os.pipe()
        parent = os.getpid()
        pid = os.fork()
        if pid == 0:
            os.close(read)

            def exited(rc):
                with contextlib.suppress(OSError):
                    os.write(write, str(rc).encode() + b"\n")
                os.close(write)

            try:
                self.serve(on_exit=exited, detached=True, parent=parent)
            finally:
                os._exit(0)
        os.close(write)

        def forward(signum, _frame):
            with contextlib.suppress(OSError):
                self._request(f"signal {signum}")

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, forward)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
        with os.fdopen(read, "rb") as channel:
            result = channel.readline()
        os.waitpid(pid, os.WNOHANG)
        return int(result) if result else 1

    def serve(  # pragma: no cover
        self,
        *,
        on_exit: Callable[[int], None] | None = None,
        detached: bool = False,
        parent: int | None = None,
        **options,
    ) -> int:
        group = None
        if detached:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            try:
                with open(os.path.join(self.path, "lock"), encoding="utf-8") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    if _read(os.path.join(self.path, "launch.json")) is None:
                        raise FileNotFoundError("missing process launch")
                    listener.bind(os.path.join(self.path, "control"))
                    listener.listen()
                    fcntl.flock(lock, fcntl.LOCK_UN)
                if not self._await_start(listener, parent=parent):
                    if on_exit is not None:
                        on_exit(1)
                    return 1
                with open(os.path.join(self.path, "lock"), encoding="utf-8") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    launch = _read(os.path.join(self.path, "launch.json"))
                    if launch is None:
                        raise FileNotFoundError("cancelled process launch")
                    os.unlink(os.path.join(self.path, "launch.json"))
                    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
                    if "env" in launch:
                        inherited = launch.get("inherit_env", ())
                        options["env"] = {
                            **{
                                key: value
                                for key, value in launch["env"].items()
                                if key not in inherited
                            },
                            **{key: os.environ[key] for key in inherited if key in os.environ},
                        }
                    group = _Group.launch(
                        launch["argv"],
                        launch["cwd"],
                        blocked_signals=launch.get("blocked_signals", ()),
                        **options,
                    )
                    fcntl.flock(lock, fcntl.LOCK_UN)
                if detached:
                    os.setsid()
                    with open(os.devnull, "r+b") as null:
                        for fd in (0, 1, 2):
                            os.dup2(null.fileno(), fd)
                return self._serve(listener, group, on_exit, detached=detached, parent=parent)
            finally:
                if group is not None:
                    os.close(group.channel)
                self.remove()

    def _await_start(self, listener, *, parent=None) -> bool:
        deadline = time.monotonic() + TIMEOUT
        while not self.requested and not self.cancelled:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (parent is not None and os.getppid() != parent):
                self.cancelled = True
                break
            ready, _, _ = select.select([listener], [], [], min(POLL, remaining))
            if ready:
                self._answer(listener, None, reported=False)
        return self.requested and not self.cancelled

    def _serve(self, listener, group, on_exit, *, detached=False, parent=None) -> int:
        stopping: int | None = None

        def stop(signum, _frame):
            nonlocal stopping
            stopping = signum

        for sig in (signal.SIGINT, signal.SIGTERM, *(() if detached else (signal.SIGHUP,))):
            signal.signal(sig, stop)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
        started, reported = False, False
        live, probe_at, probe_delay = True, 0.0, POLL
        ended_at: float | None = None
        while True:
            for message in group.receive():
                if "started" in message or "error" in message:
                    self._record(started=message)
                    started = True
            if group.eof and not started:
                self._record(
                    started={"error": "anchor exited before launch", "errno": errno.ECHILD}
                )
                started = True
            if group.returncode is not None and not reported:
                self._record(exited=group.returncode)
                if on_exit is not None:
                    on_exit(group.returncode)
                reported = True
            try:
                if parent is not None and not reported and os.getppid() != parent:
                    stopping = signal.SIGTERM
                    parent = None
                if self.cancelled:
                    group.kill()
                if stopping is not None and started:
                    group.send(stopping)
                    stopping = None
                now = time.monotonic()
                if started and (not reported or now >= probe_at):
                    probe_at = now + probe_delay
                    probe_delay = min(1.0, probe_delay * 2) if reported else POLL
                    live = group.live()
            except OSError:
                live = True
            if started and not live:
                ended_at = time.monotonic() if ended_at is None else ended_at
                if (
                    self.accepted
                    or not os.path.isdir(self.path)
                    or time.monotonic() - ended_at >= TIMEOUT
                ):
                    rc = group.returncode if group.returncode is not None else 1
                    if not reported:
                        self._record(exited=rc)
                        if on_exit is not None:
                            on_exit(rc)
                    self._record(done=rc)
                    return rc
            ready, _, _ = select.select([listener], [], [], POLL)
            if ready:
                self._answer(listener, group, reported=reported)

    def _state(self) -> dict:
        found = _read(os.path.join(self.path, STATE))
        return found if isinstance(found, dict) else {}

    def _record(self, **fields):
        """Its one writer: the whole state, atomically replaced."""
        self._recorded.update(fields)
        with contextlib.suppress(FileNotFoundError):
            _write(os.path.join(self.path, STATE), self._recorded)

    def _answer(self, listener, group, *, reported):
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(TIMEOUT)
            try:
                with connection.makefile("rb") as stream:
                    command = stream.readline(32)
                if command == b"start\n":
                    self.requested = True
                elif command == b"started\n":
                    self.accepted = True
                elif command == b"cancel\n":
                    self.accepted = self.cancelled = True
                    if group is not None:
                        group.kill()
                elif command == b"kill\n":
                    if group is None:
                        self.accepted = self.cancelled = True
                    else:
                        self.accepted = True
                        group.kill()
                elif command.startswith(b"signal "):
                    signum = int(command.removeprefix(b"signal "))
                    if signum not in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
                        raise ValueError("unsupported process control signal")
                    if group is None:
                        self.accepted = self.cancelled = True
                    else:
                        self.accepted = True
                        group.send(signum)
                elif command != b"live\n":
                    raise ValueError("unknown process control command")
                if group is None:
                    live = not self.cancelled
                else:
                    # A client that hears "not live" stops sending, so no retiring answer before
                    # a client accepts the group (started, cancel, kill or a signal).
                    live = group.live() if reported and self.accepted else True
                response = {"live": live}
            except OSError as error:
                response = {"error": str(error), "errno": error.errno or errno.EIO}
            except ValueError as error:
                response = {"error": str(error), "errno": errno.EINVAL}
            with contextlib.suppress(OSError):
                connection.sendall(json.dumps(response).encode() + b"\n")


if __name__ == "__main__":
    result = ProcessControl(sys.argv[1]).run()
    if result < 0:
        sig = -result
        if sig != signal.SIGKILL:
            signal.signal(sig, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {sig})
        os.kill(os.getpid(), sig)
    sys.exit(result)
