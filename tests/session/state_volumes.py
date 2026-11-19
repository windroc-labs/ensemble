"""Fake Docker: volumes are dirs under DOCKER_STATE, readers state_reader.answer, services
threads over in-process sockets.
"""

import _socket
import contextlib
import errno
import json
import os
import queue
import socket
import subprocess
import threading
from unittest import mock

from ensemble.session.engine import client_state, daemon, state_reader, state_service
from ensemble.session.engine.types import Daemon, DaemonRecord
from ensemble.utils import DOCKER_STATE
from tests.session.unit.hosts.docker.support import FakeProc

DOCKER = "/usr/local/bin/docker"
HOST = "unix:///var/run/docker.sock"
LOCAL = Daemon("local", HOST, remote=False, path="/usr/bin:/bin:/usr/sbin:/sbin", id="daemon-a")
IMAGE = "sha256:" + "a" * 64
GENERATION = "0123456789abcdef0123456789abcdef"
OWNER = "1001:1002"


class ReaderProc:
    """A reader's docker run over `mounted` sessions; `reply`, `hang`, `leave`: misbehaviours."""

    def __init__(self, argv, root, *, mounted, reply=None, hang=False, leave=False):
        self.args, self.root, self.reply, self.hang = argv, root, reply, hang
        self.mounted = mounted
        stdout_read, sink = os.pipe()
        self.stdout = os.fdopen(stdout_read, "rb")
        self.stdin = _Stdin(self)
        self.killed, self.leave = False, leave
        self.replies: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._write, args=(sink,))
        self.thread.daemon = True
        self.thread.start()

    def request(self, line: bytes) -> None:
        if self.leave:  # exits: its stdout ends, a later write breaks
            self.killed = True
            self.replies.put(None)
            return
        if self.hang:
            return
        if self.reply is not None:
            self.replies.put(self.reply)
            return
        if _first_part(line) not in self.mounted:
            header, payload = state_reader.answer(self.root, b"{}")
            header.update(errno=errno.ENOENT, message="not mounted")
        else:
            header, payload = state_reader.answer(self.root, line)
        self.replies.put(json.dumps({**header, "length": len(payload)}).encode() + b"\n" + payload)

    def poll(self):
        return 0 if self.killed else None

    def _write(self, sink: int) -> None:
        try:
            while (data := self.replies.get()) is not None:
                view = memoryview(data)
                while view:
                    view = view[os.write(sink, view) :]
        except OSError:
            pass
        finally:
            os.close(sink)

    def kill(self):
        if self.killed:
            return
        self.killed = True
        self.replies.put(None)
        self.stdout.close()

    def wait(self, timeout=None):
        self.thread.join(timeout)
        return 0


class _Stdin:
    def __init__(self, proc: ReaderProc):
        self.proc, self.pending = proc, b""

    def write(self, data: bytes) -> int:
        if self.proc.killed:
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")
        self.pending += data
        return len(data)

    def flush(self) -> None:
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            self.proc.request(line + b"\n")

    def close(self) -> None:
        self.proc.kill()


def _first_part(line: bytes) -> str:
    request = json.loads(line)
    path = (request.get("paths") or [None])[0] if request.get("op") == "snapshot" else None
    return str(path or request.get("path", "")).split("/", 1)[0]


class Volumes:
    """Toggles: `volumes`, `error`, `owners`, `foreign`, `hold`, `leave`, `silent`, `spawn_error`."""

    def __init__(self):
        self.calls, self.readers, self.spawns = [], [], []
        self.volumes: set[str] | None = None
        self.reply: bytes | None = None
        self.hang, self.error, self.owner, self.leave = False, "", OWNER, False
        self.owners: dict[str, str] = {}
        self.foreign: set[str] = set()
        self.hold: threading.Event | None = None
        self.silent, self.spawn_error = False, None

    def root(self, session):
        return os.path.join(os.path.expanduser(DOCKER_STATE), session)

    def exists(self, session):
        if self.volumes is not None:
            return session in self.volumes
        return os.path.isdir(self.root(session))

    @staticmethod
    def handles(argv) -> bool:
        return argv[3:4] in (["volume"], ["run"]) or argv[1:5] == SPAWN

    def __call__(self, argv, **kw):
        if argv[1:5] == SPAWN:
            return self.spawn(argv, kw)
        self.calls.append(list(argv))
        args = argv[3:]
        if self.error and args[:1] != ["run"]:
            return FakeProc(argv, b"", self.error.encode(), 1)
        if args[:2] == ["volume", "ls"]:
            if self.hold is not None:
                self.hold.wait(10)
            volume = args[args.index("--filter") + 1].removeprefix("name=")
            session = volume.removeprefix("ensemble-state-")
            return FakeProc(argv, f"{volume}\n".encode() if self.exists(session) else b"")
        if args[:2] == ["volume", "inspect"]:
            session = args[-1].removeprefix("ensemble-state-")
            if not self.exists(session):
                return FakeProc(argv, b"", f"Error: no such volume: {args[-1]}".encode(), 1)
            owner = self.owners.get(session, self.owner)
            named = "other" if session in self.foreign else session
            labels = {"ensemble.state": named, "ensemble.state-owner": owner}
            return FakeProc(argv, json.dumps(labels).encode() + b"\n")
        if args[:1] == ["run"]:
            mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
            sessions = [m.split("source=ensemble-state-", 1)[1].split(",", 1)[0] for m in mounts]
            reader = ReaderProc(
                argv,
                os.path.expanduser(DOCKER_STATE),
                mounted=set(sessions),
                reply=self.reply,
                hang=self.hang,
                leave=self.leave,
            )
            self.readers.append(reader)
            return reader
        raise AssertionError(f"unexpected docker call {argv}")

    def spawn(self, argv, kw):
        self.spawns.append((list(argv), kw))
        if self.spawn_error is not None:
            raise self.spawn_error
        path = state_service.socket_path(argv[5])
        if path not in LISTENING and not self.silent:
            service, listener = state_service.Service(argv[5]), Listener(path)
            thread = threading.Thread(
                target=service.serve, args=(listener,), kwargs={"idle": 60}, daemon=True
            )
            thread.start()
            SERVICES.append((service, listener, thread))
        return FakeProc(argv)

    def kill_services(self):
        stop_services()


SPAWN = ["-P", "-s", "-m", state_service.__name__]
SERVICES: list[tuple[state_service.Service, "Listener", threading.Thread]] = []


def stop_services():
    while SERVICES:
        service, listener, thread = SERVICES.pop()
        service.stop()
        listener.waiting.put(WAKE)
        thread.join(10)


_SOCKET = socket.socket


class Listener:
    """A bound service socket; connect() reaches it while in LISTENING."""

    def __init__(self, path):
        self.path, self.timeout, self.waiting = path, None, queue.Queue()
        LISTENING[path] = self

    def settimeout(self, timeout):
        self.timeout = timeout

    def getsockname(self):
        return self.path

    def accept(self):
        try:
            found = self.waiting.get(timeout=self.timeout)
        except queue.Empty:
            raise TimeoutError from None
        if found is WAKE:
            raise TimeoutError
        return found, self.path

    def close(self):
        if LISTENING.get(self.path) is self:
            del LISTENING[self.path]


LISTENING: dict[str, Listener] = {}
WAKE = object()


class Dialer:
    """socket.socket whose connect() gets a pair with a Listener."""

    def __init__(self, *_args, **_kw):
        self.sock = None

    def connect(self, path):
        listener = LISTENING.get(path)
        if listener is None:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
        mine, theirs = (
            _SOCKET(socket.AF_UNIX, socket.SOCK_STREAM, 0, end.detach())
            for end in _socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        )
        listener.waiting.put(theirs)
        self.sock = mine

    def close(self):
        if self.sock is not None:
            self.sock.close()

    def __getattr__(self, name):
        if self.sock is None:
            raise OSError(errno.ENOTCONN, "not connected")
        return getattr(self.sock, name)


@contextlib.contextmanager
def sockets():
    with mock.patch("socket.socket", Dialer):
        try:
            yield
        finally:
            LISTENING.clear()


def stored(session, client):
    """`client`'s dir in `session`'s fake volume; also a test's client home path."""
    return os.path.join(os.path.expanduser(DOCKER_STATE), session, client)


def record(session, found=LOCAL):
    daemon.save_record(session, DaemonRecord(found, DOCKER))
    started(session)


def started(session):
    folder = os.path.join(os.path.expanduser(DOCKER_STATE), session)
    with open(os.path.join(folder, daemon.SESSION_RECORD), "w") as f:
        json.dump({"generation": GENERATION}, f)
    with open(os.path.join(folder, daemon.result_file(GENERATION)), "w") as f:
        json.dump({"image_digest": IMAGE}, f)


@contextlib.contextmanager
def serve(sessions=("s1", "v", "w")):
    """Volumes as docker; each of `sessions` gets a daemon record and a start on IMAGE."""
    volumes = Volumes()
    for session in sessions:
        record(session)
    with mock.patch.object(subprocess, "Popen", volumes), sockets():
        try:
            yield volumes
        finally:
            reset()


def reset():
    """No service, reader or connection left from an earlier test."""
    stop_services()
    for connection in list(client_state._CONNECTIONS.values()):
        connection.close()
    client_state._CONNECTIONS.clear()
