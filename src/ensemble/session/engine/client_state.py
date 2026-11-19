"""A client home's reads, picked once per session: LocalHome on this machine, VolumeHome in a Docker
session's state volume through its reader container (state_reader.py). No link followed below the
home, bounded. Absent: None; refused: OSError (ELOOP a link, EINVAL a wrong type or name, EPROTO a
broken reply).
"""

import atexit
import contextlib
import errno
import io
import json
import os
import posixpath
import secrets
import select
import shutil
import stat as stat_module
import subprocess
import tarfile
import tempfile
import threading
import time
from collections.abc import Generator, Mapping
from typing import TypeGuard, cast

from ensemble.session.engine import daemon
from ensemble.session.engine.state_service import (
    ARCHIVE_SLACK,
    HEADER_MAX,
    IMAGE_PYTHON,
    OWNER,
    READ_MAX,
    READER,
    REPLY_TIMEOUT,
    header,
)
from ensemble.session.engine.types import (
    SESSION_NAME,
    DaemonRecord,
    HomePath,
    HomeStore,
    SessionError,
    StateEntry,
    StateFile,
    StateHeader,
    StateKind,
    StateStat,
)

ENTRIES_MAX = 65536
ARCHIVE_TYPES = (tarfile.REGTYPE, tarfile.AREGTYPE)


LOCAL_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
LOCAL_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC


class LocalHome:
    """A client home on this machine, read below its root part by part with no link followed."""

    def read(
        self, path: HomePath, *, offset: int = 0, limit: int | None = None, tail: bool = False
    ) -> bytes | None:
        cap = READ_MAX if limit is None else min(limit, READ_MAX)
        fd = _open_local(path)
        if fd is None:
            return None
        with os.fdopen(fd, "rb") as f:
            f.seek(offset)
            if tail:
                f.seek(max(f.seek(0, os.SEEK_END) - cap, offset))
            return f.read(cap)

    def stat(self, path: HomePath) -> StateStat | None:
        fd = _open_local(path)
        if fd is None:
            return None
        try:
            info = os.fstat(fd)
        finally:
            os.close(fd)
        return StateStat(info.st_size, info.st_mtime)

    def entries(self, path: HomePath, *, limit: int = ENTRIES_MAX) -> tuple[StateEntry, ...] | None:
        fd = _open_local(path, directory=True)
        if fd is None:
            return None
        listed: list[StateEntry] = []
        try:
            with os.scandir(fd) as found:
                for entry in found:
                    if len(listed) == limit:
                        raise OSError(errno.EFBIG, f"over {limit} entries", path.rel)
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    listed.append(
                        StateEntry(entry.name, _kind(info.st_mode), info.st_size, info.st_mtime)
                    )
        finally:
            os.close(fd)
        return tuple(sorted(listed))

    @contextlib.contextmanager
    def snapshot(
        self, path: HomePath, *, limit: int, siblings: tuple[str, ...] = ()
    ) -> Generator[str | None]:
        """The file itself: its siblings beside it."""
        fd = _open_local(path)
        if fd is not None:
            os.close(fd)
        yield None if fd is None else os.path.join(path.root, *path.rel.split("/"))

    def persisted(self) -> dict[str, str]:
        return {"kind": "local"}


class VolumeHome:
    """A Docker session's client home in its state volume; a path's root is not read. `image`:
    its reader's image in place of the session's (a resume's check before its start).
    """

    def __init__(self, session: str, client: str, *, image: str | None = None):
        self.session, self.client, self.image = session, client, image

    def read(
        self, path: HomePath, *, offset: int = 0, limit: int | None = None, tail: bool = False
    ) -> bytes | None:
        """At most READ_MAX bytes from `offset`; `tail`: the last `limit`."""
        cap = READ_MAX if limit is None else min(limit, READ_MAX)
        request: dict[str, object] = {"op": "read", "offset": offset, "limit": cap, "tail": tail}
        found = _ask(self._file(path), request, cap, self.image)
        return None if found is None else found[1]

    def stat(self, path: HomePath) -> StateStat | None:
        found = _ask(self._file(path), {"op": "stat"}, 0, self.image)
        return None if found is None else StateStat(found[0]["size"], found[0]["mtime"])

    def entries(self, path: HomePath, *, limit: int = ENTRIES_MAX) -> tuple[StateEntry, ...] | None:
        """A dir's entries, by lstat; EFBIG past `limit`."""
        file = self._file(path)
        found = _ask(file, {"op": "entries", "limit": limit}, 0, self.image)
        if found is None:
            return None
        listed: list[StateEntry] = []
        for item in found[0]["entries"]:
            entry = cast("list[object]", item) if isinstance(item, list) else []
            if not (
                len(entry) == 4
                and isinstance(entry[0], str)
                and _part(entry[0])
                and entry[1] in {kind.value for kind in StateKind}
                and _count(entry[2])
                and isinstance(entry[3], (int, float))
            ):
                raise _broken(file, "bad entry")
            listed.append(StateEntry(entry[0], StateKind(entry[1]), entry[2], entry[3]))
        if len(listed) > limit:
            raise _broken(file, "too many entries")
        return tuple(sorted(listed))

    def persisted(self) -> dict[str, str]:
        return {"kind": "volume", "session": self.session, "client": self.client}

    @contextlib.contextmanager
    def snapshot(
        self, path: HomePath, *, limit: int, siblings: tuple[str, ...] = ()
    ) -> Generator[str | None]:
        """A private copy of the file and its present siblings, `limit` bytes in all, unchanged
        while copied, removed on exit.
        """
        file = self._file(path)
        names = [file, *(file._replace(rel=file.rel + suffix) for suffix in siblings)]
        paths = [_path(name) for name in names]
        found = _ask(
            file,
            {"op": "snapshot", "paths": paths, "limit": limit},
            limit + ARCHIVE_SLACK,
            self.image,
        )
        folder = tempfile.mkdtemp(prefix="ensemble-state-")
        try:
            if found is None:
                yield None
                return
            bases = [posixpath.basename(each) for each in paths]
            _unpack(file, found[1], bases, limit, folder)
            main = os.path.join(folder, bases[0])
            if not os.path.isfile(main):
                raise _broken(file, "no main file in the snapshot")
            yield main
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _file(self, path: HomePath) -> StateFile:
        return StateFile(self.session, self.client, path.rel)


def home_path(root: str, path: str) -> HomePath:
    """`path` below `root`, by text alone; EXDEV: not below it."""
    root = os.path.normpath(root)
    found = os.path.normpath(path)
    prefix = root.rstrip(os.sep) + os.sep
    if not (os.path.isabs(root) and found.startswith(prefix) and len(found) > len(prefix)):
        raise OSError(errno.EXDEV, "not below the client home", path)
    return HomePath(root, found[len(prefix) :].replace(os.sep, "/"))


def home_store(fields: object) -> HomeStore:
    """persisted()'s inverse; ValueError: anything else."""
    if fields == {"kind": "local"}:
        return LocalHome()
    if isinstance(fields, Mapping):
        found = cast("Mapping[object, object]", fields)
        if set(found) == {"kind", "session", "client"} and found["kind"] == "volume":
            session, client = found["session"], found["client"]
            if (
                isinstance(session, str)
                and isinstance(client, str)
                and _part(session)
                and _part(client)
            ):
                return VolumeHome(session, client)
    raise ValueError("not a client home store")


def recorded_home(name: str | None, client: str) -> HomeStore:
    """The store a Docker Host picked for session `name` when it recorded the session's daemon
    at start, else local; OSError: a malformed record.
    """
    if name is None or not SESSION_NAME.fullmatch(name) or daemon.load_record(name) is None:
        return LocalHome()
    return VolumeHome(name, client)


def _open_local(path: HomePath, *, directory: bool = False) -> int | None:
    """An fd of `path` (`directory`: a dir), opened part by part below its root; None: absent."""
    parts = path.rel.split("/")
    if not all(_part(part) for part in parts):
        raise OSError(errno.EINVAL, "invalid client home path", path.rel)
    try:
        fd = os.open(path.root, LOCAL_DIR_FLAGS)
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        for part in parts[:-1]:
            _check_local(os.lstat(part, dir_fd=fd).st_mode, directory=True)
            child = os.open(part, LOCAL_DIR_FLAGS | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        _check_local(os.lstat(parts[-1], dir_fd=fd).st_mode, directory=directory)
        flags = LOCAL_FILE_FLAGS | (os.O_DIRECTORY if directory else 0)
        opened = os.open(parts[-1], flags, dir_fd=fd)
    except FileNotFoundError:
        return None
    finally:
        os.close(fd)
    try:
        _check_local(os.fstat(opened).st_mode, directory=directory)
    except BaseException:
        os.close(opened)
        raise
    return opened


def _check_local(mode: int, *, directory: bool) -> None:
    if stat_module.S_ISLNK(mode):
        raise OSError(errno.ELOOP, "a link")
    if directory and not stat_module.S_ISDIR(mode):
        raise OSError(errno.ENOTDIR, "not a directory")
    if not directory and not stat_module.S_ISREG(mode):
        raise OSError(errno.EINVAL, "not a regular file")


def _kind(mode: int) -> StateKind:
    if stat_module.S_ISDIR(mode):
        return StateKind.DIR
    return StateKind.FILE if stat_module.S_ISREG(mode) else StateKind.OTHER


def _unpack(file: StateFile, payload: bytes, bases: list[str], limit: int, folder: str) -> None:
    """Writes into `folder` each member, once checked: a regular file named by `bases`, once."""
    total, seen = 0, set[str]()
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


def _ask(
    file: StateFile, request: dict[str, object], bound: int, image: str | None
) -> tuple[StateHeader, bytes] | None:
    """The reply to `request`, payload at most `bound` bytes; None: absent."""
    if request["op"] != "snapshot":
        request = {**request, "path": _path(file)}
    try:
        header, payload = _reader(file.session, image).ask(request, bound)
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


def _count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _broken(file: StateFile, why: str) -> OSError:
    with _READERS_LOCK:
        readers = [r for (session, _), r in _READERS.items() if session == file.session]
    for reader in readers:
        with reader.lock:
            reader.close()
    return OSError(errno.EPROTO, f"state reader reply: {why}", file.rel)


_READERS: dict[tuple[str, str | None], "_Reader"] = {}
_READERS_LOCK = threading.Lock()


def _reader(session: str, image: str | None) -> "_Reader":
    with _READERS_LOCK:
        found = _READERS.get((session, image))
        if found is None:
            found = _READERS[session, image] = _Reader(session, image)
        return found


def _close_readers() -> None:
    with _READERS_LOCK:
        for reader in _READERS.values():
            reader.close()


atexit.register(_close_readers)


class _Reader:
    """A session's reader container, started on first use; one exchange at a time."""

    def __init__(self, session: str, image: str | None):
        self.session, self.image = session, image
        self.lock = threading.Lock()
        self.proc: subprocess.Popen[bytes] | None = None
        self.buffer = bytearray()

    def ask(self, request: dict[str, object], bound: int) -> tuple[StateHeader, bytes]:
        line = json.dumps(request).encode() + b"\n"
        with self.lock:
            for attempt in (1, 2):
                fresh = self.proc is None
                proc = self.proc or self._start()
                try:
                    _send(proc, line)
                    return self._reply(proc, bound)
                except BrokenPipeError:
                    self.close()
                    if attempt == 2 or fresh:  # a reached reader that left: removed with its volume
                        raise OSError(errno.EPIPE, "state reader gone") from None
                except BaseException:
                    self.close()
                    raise
        raise AssertionError("unreachable")

    def close(self) -> None:
        proc, self.proc, self.buffer = self.proc, None, bytearray()
        if proc is None:
            return
        with contextlib.suppress(OSError):
            if proc.stdin:
                proc.stdin.close()
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            proc.wait(1)

    def _start(self) -> subprocess.Popen[bytes]:
        recorded = daemon.load_record(self.session)
        if recorded is None:
            raise FileNotFoundError(errno.ENOENT, "no Docker daemon record", self.session)
        volume = daemon.state_volume(self.session)
        listed = self._run(
            recorded, "volume", "ls", "--filter", f"name={volume}", "--format", "{{.Name}}"
        )
        if volume not in listed.split():
            raise FileNotFoundError(errno.ENOENT, "no state volume", volume)
        labels = self._run(recorded, "volume", "inspect", "--format", "{{json .Labels}}", volume)
        found = _json_object(labels)
        if found.get(daemon.STATE_LABEL) != self.session:
            raise FileNotFoundError(errno.ENOENT, "not this session's state volume", volume)
        user = found.get(daemon.STATE_OWNER_LABEL)
        if not isinstance(user, str) or not OWNER.fullmatch(user):
            raise OSError(errno.EIO, "state volume has no owner", volume)
        image = self.image or daemon.recorded_image(self.session)
        with open(READER, encoding="utf-8") as f:
            program = f.read()
        argv, env = daemon.docker_call(
            recorded.docker,
            *("run", "--rm", "--interactive", "--name", f"ensemble-reader-{secrets.token_hex(8)}"),
            *("--label", f"{daemon.STATE_READER_LABEL}={self.session}", "--log-driver", "none"),
            *("--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"),
            *("--user", user, "--read-only"),
            *("--mount", f"type=volume,source={volume},target=/state,readonly,volume-nocopy"),
            *("--entrypoint", IMAGE_PYTHON, image, "-I", "-c", program, "/state"),
            daemon=recorded.daemon,
        )
        try:
            pipe, quiet = subprocess.PIPE, subprocess.DEVNULL
            self.proc = subprocess.Popen(
                argv, env=env, stdin=pipe, stdout=pipe, stderr=quiet, start_new_session=True
            )
        except OSError as error:
            raise OSError(errno.EIO, f"state reader: {error}") from None
        return self.proc

    def _run(self, recorded: DaemonRecord, *args: str) -> str:
        """`docker <args>`'s stdout."""
        try:
            argv, env = daemon.docker_call(recorded.docker, *args, daemon=recorded.daemon)
            done = daemon.capped(argv, env)
        except (OSError, subprocess.TimeoutExpired, SessionError) as error:
            raise OSError(errno.EIO, f"docker {args[0]} {args[1]}: {error}") from None
        if done.returncode == 0:
            return done.stdout
        lines = done.stderr.strip().splitlines()
        raise OSError(errno.EIO, f"docker {args[0]} {args[1]}: {lines[-1] if lines else 'failed'}")

    def _reply(self, proc: subprocess.Popen[bytes], bound: int) -> tuple[StateHeader, bytes]:
        head = header(self._take(proc, until_newline=HEADER_MAX))
        if head["length"] > bound:
            raise OSError(errno.EPROTO, "state reader reply: over its bound")
        return head, self._take(proc, size=head["length"])

    def _take(
        self, proc: subprocess.Popen[bytes], *, until_newline: int = 0, size: int = 0
    ) -> bytes:
        """The next line or `size` bytes; ETIMEDOUT after REPLY_TIMEOUT with no byte."""
        fd = proc.stdout.fileno() if proc.stdout else -1
        while True:
            if until_newline:
                end = self.buffer.find(b"\n")
                if end >= 0:
                    found = bytes(self.buffer[:end])
                    del self.buffer[: end + 1]
                    return found
                if len(self.buffer) > until_newline:
                    raise OSError(errno.EPROTO, "state reader reply: header over its bound")
            elif len(self.buffer) >= size:
                found = bytes(self.buffer[:size])
                del self.buffer[:size]
                return found
            deadline = time.monotonic() + REPLY_TIMEOUT
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                raise OSError(errno.ETIMEDOUT, "state reader: no reply")
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                raise BrokenPipeError(errno.EPIPE, "state reader exited")
            self.buffer += chunk


def _send(proc: subprocess.Popen[bytes], line: bytes) -> None:
    if proc.stdin is None:
        raise OSError(errno.EPIPE, "state reader has no stdin")
    proc.stdin.write(line)
    proc.stdin.flush()


def _json_object(text: str | bytes) -> dict[str, object]:
    """`text`'s JSON object; {}: anything else."""
    try:
        found: object = json.loads(text)
    except ValueError:
        return {}
    return cast("dict[str, object]", found) if isinstance(found, dict) else {}
