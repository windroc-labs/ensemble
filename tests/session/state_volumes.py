"""Fake Docker for client_state and state_service: a session's volume is its dir under DOCKER_STATE,
a reader state_reader.answer over pipes, with no process; a reader mounting sessions at
/state/<session> answers ENOENT for a path in a session it does not mount.
"""

import contextlib
import errno
import json
import os
import queue
import subprocess
import threading
from unittest import mock

from ensemble.session.engine import client_state, daemon, state_reader
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
    """A reader's docker run; `mounted`: its sessions (None: one volume at the root); `reply`:
    raw bytes sent instead; `hang`: no answer; `leave`: exits at its next request.
    """

    def __init__(self, argv, root, *, mounted=None, reply=None, hang=False, leave=False):
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
        if self.mounted is not None and _first_part(line) not in self.mounted:
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
    """`volumes`: sessions with a volume (default: each dir); `error`: every call's stderr;
    `owners`: a session's owner label in place of `owner`; `foreign`: sessions whose volume
    names another session; `hold`: an event each `volume ls` waits for; `leave`: new readers
    exit at their first request.
    """

    def __init__(self):
        self.calls, self.readers = [], []
        self.volumes: set[str] | None = None
        self.reply: bytes | None = None
        self.hang, self.error, self.owner, self.leave = False, "", OWNER, False
        self.owners: dict[str, str] = {}
        self.foreign: set[str] = set()
        self.hold: threading.Event | None = None

    def root(self, session):
        return os.path.join(os.path.expanduser(DOCKER_STATE), session)

    def exists(self, session):
        if self.volumes is not None:
            return session in self.volumes
        return os.path.isdir(self.root(session))

    def __call__(self, argv, **kw):
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
            if mounts[0].split("target=", 1)[1].startswith("/state,"):
                root, mounted = self.root(sessions[0]), None
            else:
                root, mounted = os.path.expanduser(DOCKER_STATE), set(sessions)
            reader = ReaderProc(
                argv, root, mounted=mounted, reply=self.reply, hang=self.hang, leave=self.leave
            )
            self.readers.append(reader)
            return reader
        raise AssertionError(f"unexpected docker call {argv}")


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
    with mock.patch.object(subprocess, "Popen", volumes):
        try:
            yield volumes
        finally:
            reset()


def reset():
    """No reader left from an earlier test."""
    client_state._close_readers()
    client_state._READERS.clear()
