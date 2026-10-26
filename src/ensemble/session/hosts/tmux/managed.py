"""Local guard channels. Only their creating harness holds the lifecycle pipe's write end."""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import select
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque

from ensemble.session.engine.tmux_cli import TuiError
from ensemble.session.engine.types import (
    ConfigError,
    Key,
    KeyPress,
    Launch,
    Liveness,
    NotShowable,
    Paste,
    SessionBusy,
    SessionError,
    Unanswered,
    WorkerExit,
    WorkerHook,
    WorkerOutput,
)
from ensemble.session.harness import guard
from ensemble.session.hosts.base import InteractiveProcess

QUEUE_MAX = 8 << 20
WAIT = 10
GENERATION = re.compile(r"[A-Za-z0-9_-]{1,64}")
KEYS = {
    "Enter": b"\r",
    "Escape": b"\x1b",
    "Tab": b"\t",
    "BTab": b"\x1b[Z",
    "BSpace": b"\x7f",
    "Up": b"\x1b[A",
    "Down": b"\x1b[B",
    "Right": b"\x1b[C",
    "Left": b"\x1b[D",
    "Home": b"\x1b[H",
    "End": b"\x1b[F",
    "PageUp": b"\x1b[5~",
    "PageDown": b"\x1b[6~",
    "F1": b"\x1bOP",
    "F2": b"\x1bOQ",
    "F3": b"\x1bOR",
    "F4": b"\x1bOS",
    **{f"F{i}": f"\x1b[{n}~".encode() for i, n in enumerate((15, 17, 18, 19, 20, 21, 23, 24), 5)},
}


def private_directory(path: str) -> None:
    if not os.path.lexists(path):
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            private_directory(parent)
        with contextlib.suppress(FileExistsError):
            os.mkdir(path, 0o700)
    check_directory(path)


def check_directory(path: str) -> None:
    info = os.lstat(path)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise OSError("guard directory is not private")


def location(host: str, name: str) -> str:
    root = os.environ.get("ENSEMBLE_CONTROL_ROOT", tempfile.gettempdir())
    key = hashlib.sha256(f"{host}\0{name}".encode()).hexdigest()[:16]
    return os.path.join(root, f"eg-{os.getuid()}", key)


def load_json(path: str) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 4096
        ):
            raise OSError("invalid guard record")
        value = json.loads(stream.read(4097))
    if not isinstance(value, dict):
        raise ValueError("invalid guard record")
    return value


def save_json(path: str, value: dict) -> None:
    descriptor = os.open(
        path + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream)
    os.replace(path + ".new", path)


def lease_file(path: str, *, create: bool = True) -> int:
    descriptor = os.open(
        path, os.O_RDWR | (os.O_CREAT if create else 0) | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
    )
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise OSError("invalid guard lease")
    return descriptor


def liveness(host: str, name: str, generation: str) -> Liveness:
    directory = location(host, name)
    try:
        root = os.path.dirname(directory)
        if os.path.lexists(root):
            check_directory(root)
        if not os.path.lexists(directory):
            return Liveness.ABSENT
        check_directory(directory)
        if not GENERATION.fullmatch(generation):
            return Liveness.UNKNOWN
        current = load_json(os.path.join(directory, "current.json"))
        if current != {"generation": generation}:
            return Liveness.UNKNOWN
        run = os.path.join(directory, generation)
        check_directory(run)
        descriptor = lease_file(os.path.join(run, "lease"), create=False)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return Liveness.LIVE
            value = load_json(os.path.join(run, "state.json"))
            return (
                Liveness.ABSENT
                if value == {"generation": generation, "state": "exited"}
                else Liveness.UNKNOWN
            )
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        return Liveness.UNKNOWN


def key_bytes(key: KeyPress) -> bytes:
    if len(key.key) == 1:
        text = key.key.upper() if key.shift else key.key
        value = (
            bytes([ord(text.upper()) & 31])
            if key.ctrl and "@" <= text.upper() <= "_"
            else text.encode()
        )
    else:
        value = KEYS[key.key]
        modifier = 1 + key.shift + 2 * key.meta + 4 * key.ctrl
        if modifier > 1:
            value = (
                b"\x1b[1;" + str(modifier).encode() + value[-1:]
                if value[-1:] != b"~"
                else value[:-1] + b";" + str(modifier).encode() + b"~"
            )
            return value
    return (b"\x1b" if key.meta else b"") + value


class ManagedProcess(InteractiveProcess):
    def __init__(self, name, generation, proc, *, terminal, tmux, start_reader=True):
        self.name, self.generation, self.proc = name, generation, proc
        self.terminal, self.tmux = terminal, tmux
        self.condition = threading.Condition()
        self.sending = threading.Lock()
        self.query = threading.Lock()
        self.messages = deque()
        self.size = 0
        self.code = None
        self.failure = None
        self.started = False
        self.view = None
        self.reader = None
        if start_reader:
            self.reader = threading.Thread(target=self.pump, name=f"guard-{name}", daemon=True)
            self.reader.start()

    def fail(self, reason: str) -> None:
        with self.condition:
            if self.failure is None:
                self.failure = reason
                self.messages.clear()
                self.size = 0
                # Unbuffered FileIO.close never waits for a consumer or a buffered flush.
                self.proc.stdin.close()
                self.condition.notify_all()

    def accept(self, kind: bytes, data: bytes) -> None:
        with self.condition:
            if self.failure is not None:
                return
            if self.code is not None:
                raise ValueError("guard frame after exit")
            message = None
            if kind == b"s" and not self.started:
                value = json.loads(data)
                if (
                    not isinstance(value, dict)
                    or type(value.get("pid")) is not int
                    or value["pid"] <= 0
                ):
                    raise ValueError("invalid guard start")
                self.started = True
            elif not self.started:
                raise ValueError("guard did not confirm exec")
            elif kind in (b"o", b"e"):
                message = WorkerOutput(data, "stdout" if kind == b"o" else "stderr")
            elif kind == b"h":
                value = guard.hook_message(data)
                message = WorkerHook(value["label"], value["payload"], value["delivery_id"])
            elif kind == b"v":
                value = json.loads(data)
                if not isinstance(value, dict) or not isinstance(value.get("text"), str):
                    raise ValueError("invalid guard view")
                self.view = value["text"]
            elif kind == b"x":
                value = json.loads(data)
                if not isinstance(value, dict) or type(value.get("returncode")) is not int:
                    raise ValueError("invalid guard exit")
                self.code = value["returncode"]
                message = WorkerExit(self.code)
            else:
                raise ValueError("invalid guard frame")
            if message is not None:
                if self.size + len(data) + 5 > QUEUE_MAX:
                    self.fail("guard consumer buffer exceeded 8 MiB; worker closed")
                    return
                self.messages.append((message, len(data) + 5))
                self.size += len(data) + 5
            self.condition.notify_all()

    def pump(self) -> None:
        decoder = guard.Decoder()
        try:
            while data := self.proc.stdout.read(guard.READ_SIZE):
                if self.failure is not None:
                    continue  # Always drain after overflow or protocol failure.
                try:
                    for kind, payload in decoder.feed(data):
                        self.accept(kind, payload)
                except (ValueError, UnicodeError, TypeError) as error:
                    self.fail(str(error))
            if self.code is None or decoder.pending:
                self.fail("guard channel ended without confirmed worker exit")
        except OSError as error:
            self.fail(f"guard channel: {error}")
        finally:
            self.proc.stdout.close()
            try:
                self.proc.wait(timeout=WAIT)
            except subprocess.TimeoutExpired:
                self.fail("guard transport exit unconfirmed")
            with self.condition:
                self.condition.notify_all()
            if self.failure is not None and sys.platform == "darwin":
                self.close_display()

    def checked(self) -> None:
        if self.failure is not None:
            raise Unanswered(f"worker {self.name}: {self.failure}")

    def await_start(self) -> None:
        with self.condition:
            self.condition.wait_for(lambda: self.started or self.failure is not None, WAIT)
            self.checked()
            if not self.started:
                self.fail("guard exec confirmation timed out")
                self.checked()

    def read(self, timeout: float):
        with self.condition:
            self.condition.wait_for(
                lambda: self.messages or self.failure is not None or self.code is not None,
                max(0, timeout),
            )
            self.checked()
            found = [message for message, _ in self.messages]
            self.messages.clear()
            self.size = 0
            return found

    def send_frame(self, kind: bytes, data: bytes) -> None:
        self.checked()
        wire = memoryview(guard.frame(kind, data))
        with self.sending:
            deadline = time.monotonic() + WAIT
            while wire:
                self.checked()
                left = deadline - time.monotonic()
                if left <= 0:
                    self.fail("guard input timed out")
                    self.checked()
                try:
                    if select.select([], [self.proc.stdin], [], max(0, left))[1]:
                        count = os.write(self.proc.stdin.fileno(), wire)
                        wire = wire[count:]
                except BlockingIOError:
                    continue
                except (OSError, ValueError) as error:
                    self.fail(f"guard input: {error}")
                    self.checked()

    def write(self, data: bytes) -> None:
        self.send_frame(b"i", data)

    def require_terminal(self) -> None:
        if not self.terminal:
            raise NotShowable(f"worker {self.name}: headless")

    def send(self, action: Paste | Key) -> None:
        self.require_terminal()
        self.write(
            b"\x1b[200~" + action.text.encode() + b"\x1b[201~"
            if isinstance(action, Paste)
            else key_bytes(KeyPress.parse(action.name))
        )

    def press(self, keys) -> None:
        self.require_terminal()
        self.write(b"".join(key_bytes(key) for key in keys))

    def screen(self, lines: int | None = None) -> str:
        self.require_terminal()
        if lines is not None and (type(lines) is not int or lines < 1):
            raise ConfigError("lines: want a positive integer")
        with self.query:
            with self.condition:
                self.view = None
            self.send_frame(b"q", json.dumps({"lines": lines}).encode())
            with self.condition:
                self.condition.wait_for(
                    lambda: self.view is not None or self.failure is not None, WAIT
                )
                self.checked()
                if self.view is None:
                    raise Unanswered(f"worker {self.name}: screen unanswered")
                return self.view

    def state(self) -> Liveness:
        if self.failure is not None:
            return Liveness.UNKNOWN
        return Liveness.ABSENT if self.code is not None else Liveness.LIVE

    def returncode(self) -> int | None:
        self.checked()
        return self.code

    def close_display(self) -> None:
        with contextlib.suppress(TuiError):
            if self.tmux.option(self.name, "@generation") == self.generation:
                self.tmux.kill(self.name)

    def close(self) -> None:
        self.checked()
        if self.code is None:
            self.send_frame(b"c", b"{}")
            with self.condition:
                self.condition.wait_for(
                    lambda: self.code is not None or self.failure is not None, WAIT
                )
                self.checked()
                if self.code is None:
                    self.fail("worker exit unconfirmed")
                    self.checked()
        try:
            self.proc.wait(timeout=WAIT)
        except subprocess.TimeoutExpired as error:
            raise Unanswered(f"worker {self.name}: guard exit unconfirmed") from error
        if self.reader is not None:
            self.reader.join(timeout=WAIT)
            if self.reader.is_alive():
                raise Unanswered(f"worker {self.name}: channel still open")
        self.checked()
        self.proc.stdin.close()
        self.close_display()

    # These legacy members disappear in WDRC-211 after all callers migrate.
    def alive(self) -> bool:
        return self.state() == Liveness.LIVE

    def wait_exit(self, timeout: float) -> bool:
        with self.condition:
            self.condition.wait_for(
                lambda: self.code is not None or self.failure is not None, timeout
            )
            return self.state() == Liveness.ABSENT

    def kill(self) -> None:
        self.close()

    def record(self):
        return None

    def display(self):
        return None


def start(
    host, name, launch, *, terminal, display=None, events=None, metadata=None
) -> ManagedProcess:
    host.validate(name, launch)
    generation = (metadata or {}).get("generation") or uuid.uuid4().hex
    if not GENERATION.fullmatch(generation):
        raise ConfigError("worker generation: invalid")
    directory = location(host.name, name)
    private_directory(os.path.dirname(directory))
    fresh = not os.path.lexists(directory)
    private_directory(directory)
    lock = lease_file(os.path.join(directory, "start.lock"))
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if os.path.lexists(os.path.join(directory, "current.json")) or not fresh:
            try:
                old = load_json(os.path.join(directory, "current.json"))["generation"]
            except (OSError, ValueError, KeyError) as error:
                raise Unanswered(f"worker {name}: previous generation unknown") from error
            if not isinstance(old, str) or liveness(host.name, name, old) != Liveness.ABSENT:
                raise SessionBusy(name, "previous worker generation (live or unknown)")
        run = os.path.join(directory, generation)
        os.mkdir(run, 0o700)
        save_json(os.path.join(directory, "current.json"), {"generation": generation})
        save_json(os.path.join(run, "state.json"), {"generation": generation, "state": "starting"})
        lease = lease_file(os.path.join(run, "lease"))
        try:
            fcntl.flock(lease, fcntl.LOCK_EX)
            argv = [
                sys.executable,
                "-I",
                guard.__file__,
                "--hook-socket",
                os.path.join(run, "hook"),
            ]
            if terminal:
                argv += ["--terminal", "--display-socket", os.path.join(run, "display")]
            argv += ["--", *launch.argv]
            environment = {
                **os.environ,
                **launch.env,
                "ENSEMBLE_GUARD_LEASE_FD": str(lease),
                "ENSEMBLE_GUARD_HOOK_SOCKET": os.path.join(run, "hook"),
            }
            for key in ("TMUX", "TMUX_PANE"):
                environment.pop(key, None)
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=launch.cwd,
                    env=environment,
                    close_fds=True,
                    pass_fds=(lease,),
                    start_new_session=True,
                    bufsize=0,
                )
            except OSError as error:
                save_json(
                    os.path.join(run, "state.json"), {"generation": generation, "state": "exited"}
                )
                raise SessionError(f"worker {name}: cannot start guard: {error}") from error
        finally:
            os.close(lease)
        handle = None
        try:
            assert proc.stdin is not None
            os.set_blocking(proc.stdin.fileno(), False)
            handle = ManagedProcess(name, generation, proc, terminal=terminal, tmux=host.tmux)
            handle.await_start()
            if terminal:
                host.tmux.start(
                    name,
                    Launch(
                        (
                            sys.executable,
                            "-I",
                            guard.__file__,
                            "--attach",
                            os.path.join(run, "display"),
                        ),
                        cwd=launch.cwd,
                    ),
                    events=events,
                    status_line=host.tui_settings().status_line,
                    tags={
                        "@kind": "guard",
                        "@generation": generation,
                        **{
                            f"@{key}": value
                            for key, value in (metadata or {}).items()
                            if key in ("sid", "client")
                        },
                        **({"@display": display} if display is not None else {}),
                    },
                )
                if sys.platform == "darwin":
                    host.tmux.checked(
                        ["set-hook", "-p", "-t", name, "pane-died", f"kill-session -t ={name}"]
                    )
        except BaseException:
            if handle is not None:
                with contextlib.suppress(SessionError):
                    handle.close()
            else:
                assert proc.stdin is not None and proc.stdout is not None
                proc.stdin.close()
                proc.stdout.close()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=WAIT)
            raise
        return handle
    finally:
        os.close(lock)
