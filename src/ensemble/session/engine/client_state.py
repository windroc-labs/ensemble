"""Reads of a Docker session's client home in its state volume, through its reader container
(state_reader.py): no link followed, bounded. Absent: None; refused: OSError (ELOOP a link, EINVAL
a wrong type or name, EPROTO a broken reply).
"""

import atexit
import contextlib
import errno
import io
import json
import os
import posixpath
import re
import secrets
import select
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from collections.abc import Iterator

from ensemble.session.engine import daemon
from ensemble.session.engine.types import (
    DockerAccess,
    StateEntry,
    StateFile,
    StateKind,
    StateStat,
)
from ensemble.utils import DOCKER_STATE

READ_MAX = 64 << 20
ENTRIES_MAX = 65536
HEADER_MAX = 32 << 20
ARCHIVE_SLACK = 64 << 10  # tar headers and padding
REPLY_TIMEOUT = 10
READER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state_reader.py")
IMAGE = re.compile(r"ensemble-worker:[0-9a-f]{64}")
IMAGE_PYTHON = "/opt/ensemble/venv/bin/python"
HEADER_KEYS = {"errno", "message", "size", "mtime", "entries", "length"}
ARCHIVE_TYPES = (tarfile.REGTYPE, tarfile.AREGTYPE)


def locate(path: str) -> StateFile | None:
    """`path`'s StateFile by its text; None outside the state root; EXDEV outside a client home."""
    path = os.path.abspath(path)
    for root in _roots():
        if os.path.commonpath((path, root)) != root:
            continue
        parts = os.path.relpath(path, root).split(os.sep)
        if len(parts) < 3:
            raise OSError(errno.EXDEV, "not below a client home", path)
        return StateFile(parts[0], parts[1], "/".join(parts[2:]))
    return None


def read(
    file: StateFile, *, offset: int = 0, limit: int | None = None, tail: bool = False
) -> bytes | None:
    """At most READ_MAX bytes from `offset`; `tail`: the last `limit`."""
    cap = READ_MAX if limit is None else min(limit, READ_MAX)
    request = {"op": "read", "offset": offset, "limit": cap, "tail": tail}
    found = _ask(file, request, cap)
    return None if found is None else found[1]


def stat(file: StateFile) -> StateStat | None:
    found = _ask(file, {"op": "stat"}, 0)
    return None if found is None else StateStat(found[0]["size"], found[0]["mtime"])


def entries(file: StateFile, *, limit: int = ENTRIES_MAX) -> tuple[StateEntry, ...] | None:
    """A dir's entries, by lstat; EFBIG past `limit`."""
    found = _ask(file, {"op": "entries", "limit": limit}, 0)
    if found is None:
        return None
    listed = []
    for item in found[0]["entries"]:
        if not (
            isinstance(item, list)
            and len(item) == 4
            and isinstance(item[0], str)
            and _part(item[0])
            and item[1] in {kind.value for kind in StateKind}
            and _count(item[2])
            and isinstance(item[3], (int, float))
        ):
            raise _broken(file, "bad entry")
        listed.append(StateEntry(item[0], StateKind(item[1]), item[2], item[3]))
    if len(listed) > limit:
        raise _broken(file, "too many entries")
    return tuple(sorted(listed))


@contextlib.contextmanager
def snapshot(
    file: StateFile, *, limit: int, siblings: tuple[str, ...] = ()
) -> Iterator[str | None]:
    """A private copy of `file` and its present siblings, `limit` bytes in all, unchanged while
    copied, removed on exit.
    """
    names = [file, *(file._replace(rel=file.rel + suffix) for suffix in siblings)]
    paths = [_path(name) for name in names]
    found = _ask(file, {"op": "snapshot", "paths": paths, "limit": limit}, limit + ARCHIVE_SLACK)
    folder = tempfile.mkdtemp(prefix="ensemble-state-")
    try:
        if found is None:
            yield None
            return
        bases = [posixpath.basename(path) for path in paths]
        _unpack(file, found[1], bases, limit, folder)
        main = os.path.join(folder, bases[0])
        if not os.path.isfile(main):
            raise _broken(file, "no main file in the snapshot")
        yield main
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _unpack(file: StateFile, payload: bytes, bases: list[str], limit: int, folder: str) -> None:
    """Writes into `folder` each member, once checked: a regular file named by `bases`, once."""
    total, seen = 0, set()
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            for member in archive:
                if member.type not in ARCHIVE_TYPES or member.name not in bases:
                    raise _broken(file, f"archive member {member.name!r}")
                if member.name in seen or total + member.size > limit:
                    raise _broken(file, f"archive member {member.name!r}")
                seen.add(member.name)
                total += member.size
                source = archive.extractfile(member)
                data = source.read(member.size + 1) if source else b""
                if len(data) != member.size:
                    raise _broken(file, f"archive member {member.name!r}")
                target = os.open(
                    os.path.join(folder, member.name),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                )
                with os.fdopen(target, "wb") as out:
                    out.write(data)
    except tarfile.TarError as error:
        raise _broken(file, f"archive: {error}") from None


def _ask(file: StateFile, request: dict, bound: int) -> tuple[dict, bytes] | None:
    """The reply to `request`, payload at most `bound` bytes; None: absent."""
    if request["op"] != "snapshot":
        request = {**request, "path": _path(file)}
    try:
        header, payload = _reader(file.session).ask(request, bound)
    except FileNotFoundError:
        return None
    code = header["errno"]
    if code == errno.ENOENT:
        return None
    if code is not None:
        raise OSError(code, header["message"] or os.strerror(code), file.rel)
    return header, payload


def _path(file: StateFile) -> str:
    """`file`'s path in its session's volume, every part checked."""
    parts = [file.session, file.client, *file.rel.split("/")]
    if not all(_part(part) for part in parts):
        raise OSError(errno.EINVAL, "invalid client state path", repr(file))
    return "/".join(parts[1:])


def _part(part: str) -> bool:
    return part not in {"", os.curdir, os.pardir} and "/" not in part and "\0" not in part


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _broken(file: StateFile, why: str) -> OSError:
    reader = _reader(file.session)
    with reader.lock:
        reader.close()
    return OSError(errno.EPROTO, f"state reader reply: {why}", file.rel)


def _roots() -> tuple[str, ...]:
    named = os.path.abspath(os.path.expanduser(DOCKER_STATE))
    return tuple(dict.fromkeys((named, os.path.realpath(named))))


_READERS: dict[str, "_Reader"] = {}
_READERS_LOCK = threading.Lock()


def _reader(session: str) -> "_Reader":
    with _READERS_LOCK:
        found = _READERS.get(session)
        if found is None:
            found = _READERS[session] = _Reader(session)
        return found


def _close_readers() -> None:
    with _READERS_LOCK:
        for reader in _READERS.values():
            reader.close()


atexit.register(_close_readers)


class _Reader:
    """A session's reader container, started on first use; one exchange at a time."""

    def __init__(self, session: str):
        self.session = session
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.buffer = b""

    def ask(self, request: dict, bound: int) -> tuple[dict, bytes]:
        line = json.dumps(request).encode() + b"\n"
        with self.lock:
            for attempt in (1, 2):
                proc = self.proc or self._start()
                try:
                    _send(proc, line)
                except OSError:
                    self.close()
                    if attempt == 1:
                        continue
                    raise OSError(errno.EPIPE, "state reader gone") from None
                try:
                    return self._reply(proc, bound)
                except BaseException:
                    self.close()
                    raise
        raise AssertionError("unreachable")

    def close(self) -> None:
        proc, self.proc, self.buffer = self.proc, None, b""
        if proc is None:
            return
        with contextlib.suppress(OSError):
            if proc.stdin:
                proc.stdin.close()
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            proc.wait(1)

    def _start(self) -> subprocess.Popen:
        access = daemon.load_access(self.session)
        if access is None:
            raise FileNotFoundError(errno.ENOENT, "no Docker state access record", self.session)
        volume = daemon.state_volume(self.session)
        labels = self._run(access, "volume", "inspect", "--format", "{{json .Labels}}", volume)
        if labels is None:
            raise FileNotFoundError(errno.ENOENT, "no state volume", volume)
        try:
            owner = json.loads(labels).get("ensemble.state")
        except (ValueError, AttributeError):
            owner = None
        if owner != self.session:
            raise FileNotFoundError(errno.ENOENT, "not this session's state volume", volume)
        listed = self._run(
            access,
            *("image", "ls", "--filter", "reference=ensemble-worker"),
            *("--format", "{{.Repository}}:{{.Tag}}"),
        )
        image = next((ref for ref in (listed or "").split() if IMAGE.fullmatch(ref)), None)
        if image is None:
            raise OSError(errno.EIO, "no worker image on the Docker target")
        with open(READER, encoding="utf-8") as f:
            program = f.read()
        argv, env = daemon.docker_call(
            access.docker,
            *("run", "--rm", "--interactive", "--name", f"ensemble-reader-{secrets.token_hex(8)}"),
            *("--label", f"{daemon.STATE_READER_LABEL}={self.session}", "--log-driver", "none"),
            *("--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"),
            *("--user", "agent", "--read-only"),
            *("--mount", f"type=volume,source={volume},target=/state,readonly,volume-nocopy"),
            *("--entrypoint", IMAGE_PYTHON, image, "-I", "-c", program, "/state"),
            host=access.host,
        )
        try:
            self.proc = subprocess.Popen(
                argv,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            raise OSError(errno.EIO, f"state reader: {error}") from None
        return self.proc

    def _run(self, access: DockerAccess, *args: str) -> str | None:
        """`docker <args>`'s stdout; None when Docker says the volume is absent."""
        argv, env = daemon.docker_call(access.docker, *args, host=access.host)
        try:
            done = daemon.capped(argv, env)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise OSError(errno.EIO, f"docker {args[0]} {args[1]}: {error}") from None
        if done.returncode == 0:
            return done.stdout
        if "no such volume" in done.stderr.lower():
            return None
        lines = done.stderr.strip().splitlines()
        raise OSError(errno.EIO, f"docker {args[0]} {args[1]}: {lines[-1] if lines else 'failed'}")

    def _reply(self, proc: subprocess.Popen, bound: int) -> tuple[dict, bytes]:
        deadline = time.monotonic() + REPLY_TIMEOUT
        header = _header(self._take(proc, deadline, until_newline=HEADER_MAX))
        if header["length"] > bound:
            raise OSError(errno.EPROTO, "state reader reply: over its bound")
        return header, self._take(proc, deadline, size=header["length"])

    def _take(
        self, proc: subprocess.Popen, deadline: float, *, until_newline: int = 0, size: int = 0
    ) -> bytes:
        fd = proc.stdout.fileno() if proc.stdout else -1
        while True:
            if until_newline:
                end = self.buffer.find(b"\n")
                if end >= 0:
                    found, self.buffer = self.buffer[:end], self.buffer[end + 1 :]
                    return found
                if len(self.buffer) > until_newline:
                    raise OSError(errno.EPROTO, "state reader reply: header over its bound")
            elif len(self.buffer) >= size:
                found, self.buffer = self.buffer[:size], self.buffer[size:]
                return found
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                raise OSError(errno.ETIMEDOUT, "state reader: no reply")
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                raise OSError(errno.EPIPE, "state reader exited")
            self.buffer += chunk


def _send(proc: subprocess.Popen, line: bytes) -> None:
    if proc.stdin is None:
        raise OSError(errno.EPIPE, "state reader has no stdin")
    proc.stdin.write(line)
    proc.stdin.flush()


def _header(line: bytes) -> dict:
    try:
        header = json.loads(line)
    except ValueError:
        header = None
    if not isinstance(header, dict) or not _well_formed(header):
        raise OSError(errno.EPROTO, "state reader reply: bad header")
    return header


def _well_formed(header: dict) -> bool:
    return (
        set(header) == HEADER_KEYS
        and (header["errno"] is None or (_count(header["errno"]) and header["errno"] > 0))
        and isinstance(header["message"], str)
        and _count(header["size"])
        and isinstance(header["mtime"], (int, float))
        and isinstance(header["entries"], list)
        and _count(header["length"])
    )
