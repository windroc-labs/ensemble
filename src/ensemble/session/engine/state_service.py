"""The Mac's one reader service per Docker daemon: every Mac process reads a Docker session's client
state volume through its Unix socket, and it serves them all with one reader container per volume
owner, which runs state_reader.py over each read session's volume mounted at /state/<session>.

Request, one JSON line: {"session", "image", "request"}; `request` is a state_reader.py request whose
paths are <session>/<client>/<rel>, or {"op": "reset"}, which stops the container mounting `session`.
Reply: as state_reader.py's, a header line, then `length` payload bytes; the service's own refusals
use the same header: ENOENT absent, EIO docker failed, EINVAL a bad request.
"""

import contextlib
import errno
import functools
import hashlib
import json
import os
import re
import secrets
import select
import subprocess
import threading
from typing import TypeGuard, cast

from ensemble.session.engine import daemon
from ensemble.session.engine.types import SESSION_NAME, DaemonRecord, SessionError, StateHeader
from ensemble.utils import STATE

READ_MAX = 64 << 20
HEADER_MAX = 32 << 20
ARCHIVE_SLACK = 64 << 10  # tar headers and padding
REQUEST_MAX = 64 << 10
REPLY_TIMEOUT = 10
SOCKET_PATH_MAX = 103  # bytes: macOS's AF_UNIX sun_path holds 104 with its NUL, Linux's 108
SOCKETS = f"{STATE}/state-readers"
READER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state_reader.py")
OWNER = re.compile(r"[0-9]{1,10}:[0-9]{1,10}")
IMAGE_PYTHON = "/opt/ensemble/venv/bin/python"
HEADER_KEYS = {"errno", "message", "size", "mtime", "entries", "length"}
READS = {"read", "stat", "entries", "snapshot"}
RESET = "reset"


@functools.cache
def code_digest() -> str:
    """Of the code a service runs: a changed install answers on another socket."""
    found = hashlib.sha256()
    for path in (os.path.abspath(__file__), READER, os.path.abspath(daemon.__file__)):
        with open(path, "rb") as f:
            found.update(f.read())
    return found.hexdigest()[:8]


def socket_path(daemon_id: str) -> str:
    """The service socket of `daemon_id`; EIO past the AF_UNIX limit."""
    path = _base(daemon_id) + ".sock"
    if (size := len(os.fsencode(path))) > SOCKET_PATH_MAX:
        raise OSError(
            errno.EIO,
            f"state service socket {path}: {size} bytes, over the {SOCKET_PATH_MAX}-byte AF_UNIX limit",
        )
    return path


def lock_path(daemon_id: str) -> str:
    return _base(daemon_id) + ".lock"


def _base(daemon_id: str) -> str:
    key = hashlib.sha256(daemon_id.encode()).hexdigest()[:16]
    return os.path.join(os.path.expanduser(SOCKETS), f"{key}-{code_digest()}")


def header(line: bytes) -> StateHeader:
    """A reply's header; EPROTO: not one."""
    found = _json_object(line)
    if not _well_formed(found):
        raise OSError(errno.EPROTO, "state reader reply: bad header")
    return found


def frame(head: StateHeader, payload: bytes = b"") -> bytes:
    return json.dumps(head).encode() + b"\n" + payload


def refusal(code: int, message: str) -> bytes:
    return frame({**_ok(), "errno": code, "message": message})


class Frames:
    """Lines and sized parts read from `fd`: ETIMEDOUT after `timeout` s with no byte (None:
    no limit), BrokenPipeError at its end.
    """

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
    """One owner's reader container, over the sessions it mounts."""

    def __init__(self, owner: str, proc: subprocess.Popen[bytes], mounted: frozenset[str]):
        self.owner, self.proc, self.mounted = owner, proc, mounted
        self.fresh = True  # no reply read yet
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
    """Answers one daemon's state requests; reads are serial across its sessions."""

    def __init__(self, daemon_id: str):
        self.daemon_id = daemon_id
        self.lock = threading.Lock()
        self.readers: dict[str, _Container] = {}
        self.wanted: dict[str, int] = {}
        self._wanted_lock = threading.Lock()

    def answer(self, line: bytes) -> bytes:
        """The framed reply to one request line."""
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
                        if (
                            attempt == 2 or reader.fresh
                        ):  # a new reader that left: its volume removed
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
        """The live container mounting `session`; one found ended is a loss."""
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
        """Stops `reader`; its sessions are mounted again only once read again."""
        reader.stop()
        if self.readers.get(reader.owner) is reader:
            del self.readers[reader.owner]

    def _restart(self, record: DaemonRecord, session: str, image: str) -> _Container:
        """A container of `session`'s owner mounting the sessions it had, the waiting ones of its
        owner and `session`, each verified now: `docker run` would create a removed volume.
        """
        owner = self._verify(record, session)
        if owner is None:
            raise OSError(errno.EIO, "state volume has no owner", daemon.state_volume(session))
        current = self.readers.get(owner)
        if current is not None and not current.alive():
            self._lose(current)
            current = None
        with self._wanted_lock:
            others = set(self.wanted)
        others |= current.mounted if current else frozenset()
        mounts = {session}
        for other in sorted(others - mounts):
            with contextlib.suppress(FileNotFoundError):
                if self._verify(record, other) == owner:
                    mounts.add(other)
        if current is not None:
            self._lose(current)
        reader = _Container(
            owner, self._start(record, owner, image, sorted(mounts)), frozenset(mounts)
        )
        self.readers[owner] = reader
        return reader

    @staticmethod
    def _verify(record: DaemonRecord, session: str) -> str | None:
        """The owner of `session`'s state volume, None: none; ENOENT: absent or not its."""
        volume = daemon.state_volume(session)
        listed = _run(record, "volume", "ls", "--filter", f"name={volume}", "--format", "{{.Name}}")
        if volume not in listed.split():
            raise FileNotFoundError(errno.ENOENT, "no state volume", volume)
        labels = _run(record, "volume", "inspect", "--format", "{{json .Labels}}", volume)
        found = _json_object(labels)
        if found.get(daemon.STATE_LABEL) != session:
            raise FileNotFoundError(errno.ENOENT, "not this session's state volume", volume)
        user = found.get(daemon.STATE_OWNER_LABEL)
        return user if isinstance(user, str) and OWNER.fullmatch(user) else None

    @staticmethod
    def _start(
        record: DaemonRecord, owner: str, image: str, sessions: list[str]
    ) -> subprocess.Popen[bytes]:
        with open(READER, encoding="utf-8") as f:
            program = f.read()
        mounts = [
            arg
            for session in sessions
            for arg in (
                "--mount",
                f"type=volume,source={daemon.state_volume(session)},target=/state/{session},"
                "readonly,volume-nocopy",
            )
        ]
        name = f"ensemble-reader-{secrets.token_hex(8)}"
        try:
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
    """A request's session, image and reader request; EINVAL: anything else."""
    found = _json_object(line)
    session, image, request = found.get("session"), found.get("image"), found.get("request")
    if not (
        set(found) == {"session", "image", "request"}
        and isinstance(session, str)
        and SESSION_NAME.fullmatch(session)
        and isinstance(image, str)
        and daemon.DIGEST.fullmatch(image)
        and isinstance(request, dict)
    ):
        raise OSError(errno.EINVAL, "bad state service request")
    request = cast("dict[str, object]", request)
    op = request.get("op")
    if op == RESET:
        return session, image, request
    paths = request.get("paths") if op == "snapshot" else [request.get("path")]
    if op not in READS or not isinstance(paths, list) or not paths:
        raise OSError(errno.EINVAL, "bad state service request")
    for path in cast("list[object]", paths):
        if not isinstance(path, str) or path.split("/", 1)[0] != session or "/" not in path:
            raise OSError(errno.EINVAL, "a path outside the request's session")
    return session, image, request


def _bound(request: dict[str, object]) -> int:
    """The payload bytes `request` may get; EINVAL: a read over READ_MAX or a bad limit."""
    if request["op"] not in {"read", "snapshot"}:
        return 0
    limit = request.get("limit")
    if not _count(limit) or (request["op"] == "read" and limit > READ_MAX):
        raise OSError(errno.EINVAL, "bad state service request")
    return limit if request["op"] == "read" else limit + ARCHIVE_SLACK


def _run(record: DaemonRecord, *args: str) -> str:
    """`docker <args>`'s stdout; EIO: it failed."""
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
    """`text`'s JSON object; {}: anything else."""
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
