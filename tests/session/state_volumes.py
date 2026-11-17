"""Fake Docker for client_state: a session's volume is its dir under DOCKER_STATE, its reader
state_reader.answer over pipes, with no process.
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
from ensemble.session.engine.types import DockerAccess
from ensemble.utils import DOCKER_STATE
from tests.session.unit.hosts.docker.support import FakeProc

DOCKER = "/usr/local/bin/docker"
HOST = "unix:///var/run/docker.sock"
IMAGE = "ensemble-worker:" + "a" * 64


class ReaderProc:
    """A reader's docker run; `reply`: raw bytes sent instead; `hang`: no answer."""

    def __init__(self, argv, root, *, reply=None, hang=False):
        self.args, self.root, self.reply, self.hang = argv, root, reply, hang
        stdout_read, sink = os.pipe()
        self.stdout = os.fdopen(stdout_read, "rb")
        self.stdin = _Stdin(self)
        self.killed = False
        self.replies: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._write, args=(sink,))
        self.thread.daemon = True
        self.thread.start()

    def request(self, line: bytes) -> None:
        if self.hang:
            return
        if self.reply is not None:
            self.replies.put(self.reply)
            return
        header, payload = state_reader.answer(self.root, line)
        self.replies.put(json.dumps({**header, "length": len(payload)}).encode() + b"\n" + payload)

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


class Volumes:
    """`volumes`: sessions with a volume (default: each dir); `images`: image ls output."""

    def __init__(self):
        self.calls, self.readers = [], []
        self.volumes: set[str] | None = None
        self.images = IMAGE + "\n"
        self.reply: bytes | None = None
        self.hang, self.inspect_error = False, ""

    def root(self, session):
        return os.path.join(os.path.expanduser(DOCKER_STATE), session)

    def exists(self, session):
        if self.volumes is not None:
            return session in self.volumes
        return os.path.isdir(self.root(session))

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        args = argv[3:]
        if args[:2] == ["volume", "inspect"]:
            session = args[-1].removeprefix("ensemble-state-")
            if self.inspect_error:
                return FakeProc(argv, b"", self.inspect_error.encode(), 1)
            if not self.exists(session):
                return FakeProc(argv, b"", f"Error: no such volume: {args[-1]}".encode(), 1)
            return FakeProc(argv, json.dumps({"ensemble.state": session}).encode() + b"\n")
        if args[:2] == ["image", "ls"]:
            return FakeProc(argv, self.images.encode())
        if args[:1] == ["run"]:
            mount = args[args.index("--mount") + 1]
            session = mount.split("source=ensemble-state-", 1)[1].split(",", 1)[0]
            reader = ReaderProc(argv, self.root(session), reply=self.reply, hang=self.hang)
            self.readers.append(reader)
            return reader
        raise AssertionError(f"unexpected docker call {argv}")


def record(session):
    daemon.save_access(session, DockerAccess(DOCKER, HOST))


@contextlib.contextmanager
def serve(sessions=("s1", "v", "w")):
    """Volumes as docker; each of `sessions` gets an access record."""
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
