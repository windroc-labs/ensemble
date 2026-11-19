"""A client home's reads, picked once per session: LocalHome on this machine, VolumeHome in a Docker
session's state volume through its daemon's reader service (state_service.py). No link followed
below the home, bounded. Absent: None; refused: OSError (ELOOP a link, EINVAL a wrong type or name,
EPROTO a broken reply).
"""

import contextlib
import errno
import io
import json
import os
import posixpath
import shutil
import socket
import stat as stat_module
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Generator, Mapping
from typing import TypeGuard, cast

import ensemble.utils
from ensemble.session.engine import daemon, state_service
from ensemble.session.engine.state_service import (
    ARCHIVE_SLACK,
    HEADER_MAX,
    READ_MAX,
    Frames,
    header,
)
from ensemble.session.engine.types import (
    SESSION_NAME,
    HomePath,
    HomeStore,
    StateEntry,
    StateFile,
    StateHeader,
    StateKind,
    StateStat,
)
from ensemble.utils import module_roots, source_path

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
    recorded = daemon.load_record(file.session)
    if recorded is None or recorded.daemon.id is None:
        return None
    image = image or daemon.recorded_image(file.session)
    line = {"session": file.session, "image": image, "request": request}
    found, payload = _connection(recorded.daemon.id).ask(line, bound)
    code = found["errno"]
    if code == errno.ENOENT:
        return None
    if code is not None:
        raise OSError(code, found["message"] or os.strerror(code), file.rel)
    return found, payload


def _path(file: StateFile) -> str:
    """Session, client and rel, every part checked."""
    parts = [file.session, file.client, *file.rel.split("/")]
    if not all(_part(part) for part in parts):
        raise OSError(errno.EINVAL, "invalid client state path", repr(file))
    return "/".join(parts)


def _part(part: str) -> bool:
    return part not in {"", os.curdir, os.pardir} and "/" not in part and "\0" not in part


def _count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _broken(file: StateFile, why: str) -> OSError:
    with contextlib.suppress(OSError):
        recorded = daemon.load_record(file.session)
        if recorded is not None and recorded.daemon.id is not None:
            reset = {"session": file.session, "image": None, "request": {"op": state_service.RESET}}
            _connection(recorded.daemon.id).ask(reset, 0)
    return OSError(errno.EPROTO, f"state reader reply: {why}", file.rel)


CLIENT_WAIT = 30  # s: a queue and a restart
SPAWN_WAIT = 5
_CONNECTIONS: dict[str, "_Connection"] = {}
_CONNECTIONS_LOCK = threading.Lock()


def _connection(daemon_id: str) -> "_Connection":
    """Per daemon; a forked child makes its own."""
    with _CONNECTIONS_LOCK:
        found = _CONNECTIONS.get(daemon_id)
        if found is None or found.pid != os.getpid():
            found = _CONNECTIONS[daemon_id] = _Connection(daemon_id)
        return found


class _Connection:
    def __init__(self, daemon_id: str):
        self.daemon_id, self.pid = daemon_id, os.getpid()
        self.lock = threading.Lock()
        self.sock: socket.socket | None = None
        self.frames: Frames | None = None

    def ask(self, request: Mapping[str, object], bound: int) -> tuple[StateHeader, bytes]:
        line = json.dumps(request).encode() + b"\n"
        with self.lock:
            for attempt in (1, 2):
                try:
                    sock, frames = self.sock or self._connect(), self.frames
                    assert frames is not None
                    sock.sendall(line)
                    found = header(frames.line(HEADER_MAX))
                    if found["length"] > bound:
                        raise OSError(errno.EPROTO, "state reader reply: over its bound")
                    return found, frames.take(found["length"])
                except (BrokenPipeError, ConnectionResetError):
                    self.close()
                    if attempt == 2:
                        raise OSError(errno.EPIPE, "state service gone") from None
                except BaseException:
                    self.close()
                    raise
        raise AssertionError("unreachable")

    def close(self) -> None:
        sock, self.sock, self.frames = self.sock, None, None
        if sock is not None:
            sock.close()

    def _connect(self) -> socket.socket:
        """EIO: no service within SPAWN_WAIT; EPERM: its directory is not private."""
        path = state_service.socket_path(self.daemon_id)
        deadline, starting, started = time.monotonic() + SPAWN_WAIT, None, 0.0
        while (sock := _dial(path)) is None:
            now = time.monotonic()
            if now >= deadline:
                raise OSError(errno.EIO, "state service did not start")
            if starting is None or (starting.poll() is not None and now - started >= 1):
                starting, started = _spawn(self.daemon_id, path), now
            time.sleep(0.05)
        self.sock, self.frames = sock, Frames(sock.fileno(), CLIENT_WAIT, "state service")
        return sock


def _dial(path: str) -> socket.socket | None:
    try:
        state_service.check_private(os.path.dirname(path))
    except FileNotFoundError:
        return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
    except (FileNotFoundError, ConnectionRefusedError):
        sock.close()
        return None
    except BaseException:
        sock.close()
        raise
    return sock


def _spawn(daemon_id: str, path: str) -> subprocess.Popen[bytes]:
    roots = module_roots(state_service, ensemble.utils)
    try:
        proc = subprocess.Popen(
            [os.path.realpath(sys.executable), "-P", "-s", "-m", state_service.__name__]
            + [daemon_id, path],
            env={
                "PATH": os.defpath,
                "HOME": os.path.expanduser("~"),
                **source_path(roots),
                **daemon.ssh_agent(),
            },
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        raise OSError(errno.EIO, f"state service: {error}") from None
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc
