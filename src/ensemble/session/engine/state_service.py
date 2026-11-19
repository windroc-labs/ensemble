"""One reader service per Docker daemon: Mac processes read client state volumes through its
Unix socket; it reads them with one container per volume owner, each session at /state/<session>.
A request line is {"session", "image", "request"}; replies are framed as state_reader.py's.
"""

import contextlib
import errno
import fcntl
import functools
import hashlib
import json
import os
import re
import secrets
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from types import FrameType
from typing import TypeGuard, cast

from ensemble.session.engine import daemon
from ensemble.session.engine.types import SESSION_NAME, DaemonRecord, SessionError, StateHeader
from ensemble.utils import control_root

READ_MAX = 64 << 20
HEADER_MAX = 32 << 20
ARCHIVE_SLACK = 64 << 10  # tar headers and padding
REQUEST_MAX = 64 << 10
REPLY_TIMEOUT = 10
IDLE = 60  # s with no client
POLL = 0.5
SOCKET_PATH_MAX = 103  # bytes: macOS's AF_UNIX sun_path holds 104 with its NUL, Linux's 108
READER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state_reader.py")
OWNER = re.compile(r"[0-9]{1,10}:[0-9]{1,10}")
SOCKET_NAME = re.compile(r"sr-[0-9a-f]{16}-[0-9a-f]{8}\.sock")
IMAGE_PYTHON = "/opt/ensemble/venv/bin/python"
HEADER_KEYS = {"errno", "message", "size", "mtime", "entries", "length"}
READS = {"read", "stat", "entries", "snapshot"}
RESET = "reset"


@functools.cache
def code_digest() -> str:
    """Changed code answers on another socket."""
    found = hashlib.sha256()
    for path in (os.path.abspath(__file__), READER, os.path.abspath(daemon.__file__)):
        with open(path, "rb") as f:
            found.update(f.read())
    return found.hexdigest()[:8]


def socket_path(daemon_id: str) -> str:
    """EIO past the AF_UNIX limit."""
    path = _base(daemon_id) + ".sock"
    if (size := len(os.fsencode(path))) > SOCKET_PATH_MAX:
        raise OSError(
            errno.EIO,
            f"state service socket {path}: {size} bytes, over the {SOCKET_PATH_MAX}-byte AF_UNIX limit",
        )
    return path


def lock_path(socket_file: str) -> str:
    return socket_file.removesuffix(".sock") + ".lock"


def _base(daemon_id: str) -> str:
    key = hashlib.sha256(daemon_id.encode()).hexdigest()[:16]
    return os.path.join(os.path.abspath(control_root()), f"sr-{key}-{code_digest()}")


def check_private(folder: str) -> None:
    """EPERM: `folder` is not a directory of this user's alone (a link, another owner, mode)."""
    info = os.lstat(folder)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise OSError(errno.EPERM, "not a private directory", folder)


def header(line: bytes) -> StateHeader:
    found = _json_object(line)
    if not _well_formed(found):
        raise OSError(errno.EPROTO, "state reader reply: bad header")
    return found


def frame(head: StateHeader, payload: bytes = b"") -> bytes:
    return json.dumps(head).encode() + b"\n" + payload


def refusal(code: int, message: str) -> bytes:
    return frame({**_ok(), "errno": code, "message": message})


class Frames:
    """Reads lines and sized parts from `fd`; ETIMEDOUT after `timeout` s idle, BrokenPipeError at EOF."""

    def __init__(self, fd: int, timeout: float | None, name: str):
        self.fd, self.timeout, self.name = fd, timeout, name
        self.buffer = bytearray()

    def line(self, limit: int) -> bytes:
        while (end := self.buffer.find(b"\n")) < 0:
            if len(self.buffer) > limit:
                raise OSError(errno.EPROTO, f"{self.name} reply: header over its bound")
            self._fill()
        found = bytes(self.buffer[:end])
        del self.buffer[: end + 1]
        return found

    def take(self, size: int) -> bytes:
        while len(self.buffer) < size:
            self._fill()
        found = bytes(self.buffer[:size])
        del self.buffer[:size]
        return found

    def _fill(self) -> None:
        if not select.select([self.fd], [], [], self.timeout)[0]:
            raise OSError(errno.ETIMEDOUT, f"{self.name}: no reply")
        chunk = os.read(self.fd, 1 << 20)
        if not chunk:
            raise BrokenPipeError(errno.EPIPE, f"{self.name} exited")
        self.buffer += chunk


class _Container:
    def __init__(self, owner: str, proc: subprocess.Popen[bytes], mounted: frozenset[str]):
        self.owner, self.proc, self.mounted = owner, proc, mounted
        self.fresh = True
        fd = proc.stdout.fileno() if proc.stdout else -1
        self.frames = Frames(fd, REPLY_TIMEOUT, "state reader")

    def alive(self) -> bool:
        return self.proc.poll() is None

    def ask(self, request: dict[str, object], bound: int) -> tuple[StateHeader, bytes]:
        if self.proc.stdin is None:
            raise BrokenPipeError(errno.EPIPE, "state reader has no stdin")
        self.proc.stdin.write(json.dumps(request).encode() + b"\n")
        self.proc.stdin.flush()
        self.frames.timeout = REPLY_TIMEOUT
        head = header(self.frames.line(HEADER_MAX))
        if head["length"] > bound:
            raise OSError(errno.EPROTO, "state reader reply: over its bound")
        payload = self.frames.take(head["length"])
        self.fresh = False
        return head, payload

    def stop(self) -> None:
        with contextlib.suppress(OSError):
            if self.proc.stdin:
                self.proc.stdin.close()
        with contextlib.suppress(OSError):
            self.proc.kill()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            self.proc.wait(1)


class Service:
    """Answers one daemon's requests, serially."""

    def __init__(self, daemon_id: str):
        self.daemon_id = daemon_id
        self.lock = threading.Lock()
        self.readers: dict[str, _Container] = {}
        self.wanted: dict[str, int] = {}
        self._wanted_lock = threading.Lock()
        self._clients: set[socket.socket] = set()
        self._clients_lock = threading.Lock()
        self._quiet_since = time.monotonic()
        self._stopped = threading.Event()

    def serve(self, listener: socket.socket, *, idle: float) -> None:
        """Until `stop` or `idle` s with no client; then removes the socket and stops every reader."""
        path = listener.getsockname()
        listener.settimeout(POLL)
        try:
            while not self._stopped.is_set():
                try:
                    client, _ = listener.accept()
                except TimeoutError:
                    with self._clients_lock:
                        if not self._clients and time.monotonic() - self._quiet_since >= idle:
                            return
                    continue
                client.settimeout(None)
                with self._clients_lock:
                    self._clients.add(client)
                threading.Thread(target=self._talk, args=(client,), daemon=True).start()
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)
            listener.close()
            with self._clients_lock:
                clients = list(self._clients)
            for client in clients:
                with contextlib.suppress(OSError):
                    client.shutdown(socket.SHUT_RDWR)
            self.close()

    def stop(self) -> None:
        self._stopped.set()

    def _talk(self, client: socket.socket) -> None:
        try:
            frames = Frames(client.fileno(), None, "state client")
            while True:
                try:
                    line = frames.line(REQUEST_MAX)
                except OSError:
                    return
                try:
                    client.sendall(self.answer(line))
                except OSError:
                    return
        finally:
            with self._clients_lock:
                self._clients.discard(client)
                self._quiet_since = time.monotonic()
            client.close()

    def answer(self, line: bytes) -> bytes:
        try:
            session, image, request = _parse(line)
            if request["op"] == RESET:
                self._reset(session)
                return frame(_ok())
            head, payload = self._serve(session, image, request)
        except OSError as error:
            return refusal(error.errno or errno.EIO, error.strerror or str(error))
        return frame(head, payload)

    def close(self) -> None:
        with self.lock:
            for reader in self.readers.values():
                reader.stop()
            self.readers.clear()

    def _serve(
        self, session: str, image: str, request: dict[str, object]
    ) -> tuple[StateHeader, bytes]:
        record = daemon.load_record(session)
        if record is None:
            raise OSError(errno.ENOENT, "no Docker daemon record")
        if record.daemon.id != self.daemon_id:
            raise OSError(errno.EINVAL, "a session on another Docker daemon")
        bound = _bound(request)
        self._want(session, 1)
        try:
            with self.lock:
                for attempt in (1, 2):
                    reader = self._mounting(session) or self._restart(record, session, image)
                    try:
                        return reader.ask(request, bound)
                    except BrokenPipeError:
                        self._lose(reader)
                        if attempt == 2 or reader.fresh:  # a new one left: volume removed
                            raise OSError(errno.EPIPE, "state reader gone") from None
                    except BaseException:
                        self._lose(reader)
                        raise
        finally:
            self._want(session, -1)
        raise AssertionError("unreachable")

    def _want(self, session: str, step: int) -> None:
        with self._wanted_lock:
            count = self.wanted.get(session, 0) + step
            if count:
                self.wanted[session] = count
            else:
                self.wanted.pop(session, None)

    def _mounting(self, session: str) -> _Container | None:
        for reader in list(self.readers.values()):
            if session in reader.mounted:
                if reader.alive():
                    return reader
                self._lose(reader)
        return None

    def _reset(self, session: str) -> None:
        with self.lock:
            for reader in list(self.readers.values()):
                if session in reader.mounted:
                    self._lose(reader)

    def _lose(self, reader: _Container) -> None:
        reader.stop()
        if self.readers.get(reader.owner) is reader:
            del self.readers[reader.owner]

    def _restart(self, record: DaemonRecord, session: str, image: str) -> _Container:
        """Mounts `session`, its owner's mounted and waiting sessions, each verified: a run creates a missing volume."""
        for reader in list(self.readers.values()):
            if not reader.alive():
                self._lose(reader)
        with self._wanted_lock:
            wanted = set(self.wanted)
        mounted = {s for reader in self.readers.values() for s in reader.mounted}
        owners = self._owners(record, sorted({session} | wanted | mounted))
        if session not in owners:
            raise FileNotFoundError(errno.ENOENT, "no state volume", daemon.state_volume(session))
        owner = owners[session]
        if owner is None:
            raise OSError(errno.EIO, "state volume has no owner", daemon.state_volume(session))
        current = self.readers.get(owner)
        kept = set(current.mounted if current else ()) | wanted | {session}
        mounts = frozenset(s for s, found in owners.items() if found == owner and s in kept)
        if current is not None:
            self._lose(current)
        reader = _Container(owner, self._start(record, owner, image, sorted(mounts)), mounts)
        self.readers[owner] = reader
        return reader

    @staticmethod
    def _owners(record: DaemonRecord, sessions: list[str]) -> dict[str, str | None]:
        """Each session's volume owner (None: none) by one `volume ls` and one `volume inspect`;
        an absent volume or another session's is left out.
        """
        volumes = {daemon.state_volume(session): session for session in sessions}
        filters = [arg for volume in volumes for arg in ("--filter", f"name={volume}")]
        listed = set(_run(record, "volume", "ls", *filters, "--format", "{{.Name}}").split())
        present = [volume for volume in volumes if volume in listed]
        if not present:
            return {}
        found = _run(record, "volume", "inspect", "--format", "{{json .Labels}}", *present)
        lines = found.splitlines()
        if len(lines) != len(present):
            raise OSError(errno.EIO, "docker volume inspect: unexpected output")
        owners: dict[str, str | None] = {}
        for volume, line in zip(present, lines, strict=True):
            labels = _json_object(line)
            if labels.get(daemon.STATE_LABEL) == volumes[volume]:
                user = labels.get(daemon.STATE_OWNER_LABEL)
                owners[volumes[volume]] = (
                    user if isinstance(user, str) and OWNER.fullmatch(user) else None
                )
        return owners

    @staticmethod
    def _start(
        record: DaemonRecord, owner: str, image: str, sessions: list[str]
    ) -> subprocess.Popen[bytes]:
        mounts = [
            arg
            for session in sessions
            for arg in (
                "--mount",
                (
                    f"type=volume,source={daemon.state_volume(session)},target=/state/{session},"
                    "readonly,volume-nocopy"
                ),
            )
        ]
        name = f"ensemble-reader-{secrets.token_hex(8)}"
        try:
            with open(READER, encoding="utf-8") as f:
                program = f.read()
            argv, env = daemon.docker_call(
                record.docker,
                *("run", "--rm", "--interactive", "--name", name),
                *("--label", f"{daemon.STATE_READER_LABEL}=1", "--log-driver", "none"),
                *("--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"),
                *("--user", owner, "--read-only", *mounts),
                *("--entrypoint", IMAGE_PYTHON, image, "-I", "-c", program, "/state"),
                daemon=record.daemon,
            )
            pipe, quiet = subprocess.PIPE, subprocess.DEVNULL
            return subprocess.Popen(
                argv, env=env, stdin=pipe, stdout=pipe, stderr=quiet, start_new_session=True
            )
        except (OSError, SessionError) as error:
            raise OSError(errno.EIO, f"state reader: {error}") from None


def _parse(line: bytes) -> tuple[str, str, dict[str, object]]:
    """EINVAL: a bad request or a path outside its session."""
    found = _json_object(line)
    session, image, request = found.get("session"), found.get("image"), found.get("request")
    if not (
        set(found) == {"session", "image", "request"}
        and isinstance(session, str)
        and SESSION_NAME.fullmatch(session)
        and isinstance(request, dict)
    ):
        raise OSError(errno.EINVAL, "bad state service request")
    request = cast("dict[str, object]", request)
    op = request.get("op")
    if op == RESET and image is None:
        return session, "", request
    if not isinstance(image, str) or not daemon.DIGEST.fullmatch(image):
        raise OSError(errno.EINVAL, "bad state service request")
    paths = request.get("paths") if op == "snapshot" else [request.get("path")]
    if op not in READS or not isinstance(paths, list) or not paths:
        raise OSError(errno.EINVAL, "bad state service request")
    for path in cast("list[object]", paths):
        if not isinstance(path, str) or path.split("/", 1)[0] != session or "/" not in path:
            raise OSError(errno.EINVAL, "a path outside the request's session")
    return session, image, request


def _bound(request: dict[str, object]) -> int:
    """EINVAL: a read over READ_MAX or a bad limit."""
    if request["op"] not in {"read", "snapshot"}:
        return 0
    limit = request.get("limit")
    if not _count(limit) or (request["op"] == "read" and limit > READ_MAX):
        raise OSError(errno.EINVAL, "bad state service request")
    return limit if request["op"] == "read" else limit + ARCHIVE_SLACK


def _run(record: DaemonRecord, *args: str) -> str:
    try:
        argv, env = daemon.docker_call(record.docker, *args, daemon=record.daemon)
        done = daemon.capped(argv, env)
    except (OSError, subprocess.TimeoutExpired, SessionError) as error:
        raise OSError(errno.EIO, f"docker {args[0]} {args[1]}: {error}") from None
    if done.returncode == 0:
        return done.stdout
    lines = done.stderr.strip().splitlines()
    raise OSError(errno.EIO, f"docker {args[0]} {args[1]}: {lines[-1] if lines else 'failed'}")


def _ok() -> StateHeader:
    return {"errno": None, "message": "", "size": 0, "mtime": 0.0, "entries": [], "length": 0}


def _json_object(text: str | bytes) -> dict[str, object]:
    try:
        found: object = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    return cast("dict[str, object]", found) if isinstance(found, dict) else {}


def _count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _well_formed(found: dict[str, object]) -> TypeGuard[StateHeader]:
    return (
        set(found) == HEADER_KEYS
        and (found["errno"] is None or (_count(found["errno"]) and found["errno"] > 0))
        and isinstance(found["message"], str)
        and _count(found["size"])
        and isinstance(found["mtime"], (int, float))
        and isinstance(found["entries"], list)
        and _count(found["length"])
    )


def bind(path: str) -> tuple[socket.socket, int] | None:
    """A listener at `path` and its held lock; None: another service holds it."""
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    check_private(folder)
    lock = os.open(lock_path(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        return None
    try:
        os.fchmod(lock, 0o600)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(path)
            os.chmod(path, 0o600)
            listener.listen()
        except BaseException:
            listener.close()
            raise
    except BaseException:
        os.close(lock)
        raise
    return listener, lock


def main(argv: list[str]) -> int:
    """state_service <daemon id> <socket>: the socket its spawning client dials; 0 also when
    another service serves it.
    """
    if not (
        len(argv) == 2
        and daemon.DAEMON.fullmatch(argv[0])
        and os.path.isabs(argv[1])
        and SOCKET_NAME.fullmatch(os.path.basename(argv[1]))
        and len(os.fsencode(argv[1])) <= SOCKET_PATH_MAX
    ):
        return 64
    try:
        bound = bind(argv[1])
    except OSError:
        return 1
    if bound is None:
        return 0
    listener, lock = bound
    service = Service(argv[0])

    def stop(_signal: int, _frame: FrameType | None) -> None:
        service.stop()

    signal.signal(signal.SIGTERM, stop)
    try:
        service.serve(listener, idle=IDLE)
    finally:
        os.close(lock)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
