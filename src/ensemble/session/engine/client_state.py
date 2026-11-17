"""Reads of a Docker session's client home, the state its worker writes: through no link, nothing
outside the home, each bounded. Absent: None; refused: an OSError other than FileNotFoundError.
"""

import contextlib
import errno
import os
import posixpath
import shutil
import stat as stat_module
import tempfile
from collections.abc import Iterator

from ensemble.session.engine.types import StateEntry, StateFile, StateKind, StateStat
from ensemble.utils import DOCKER_STATE

READ_MAX = 64 << 20
ENTRIES_MAX = 65536
SNAPSHOT_ATTEMPTS = 3
CHUNK = 1 << 20
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC


def locate(path: str) -> StateFile | None:
    """Absolute `path`'s StateFile, by its text alone; None below no state root; EXDEV below one
    but not below a client home.
    """
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
    """From byte `offset`: the first `limit` bytes (the last with `tail`), at most READ_MAX."""
    cap = READ_MAX if limit is None else min(limit, READ_MAX)
    try:
        fd, _ = _open(file)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as f:
        f.seek(offset)
        if tail:
            f.seek(max(f.seek(0, os.SEEK_END) - cap, offset))
        return f.read(cap)


def stat(file: StateFile) -> StateStat | None:
    try:
        fd, info = _open(file)
    except FileNotFoundError:
        return None
    os.close(fd)
    return StateStat(info.st_size, info.st_mtime)


def entries(file: StateFile, *, limit: int = ENTRIES_MAX) -> tuple[StateEntry, ...] | None:
    """A directory's entries by name, each as its lstat shows it; EFBIG past `limit` of them."""
    try:
        fd, _ = _open(file, directory=True)
    except FileNotFoundError:
        return None
    found = []
    try:
        with os.scandir(fd) as listing:
            for entry in listing:
                if len(found) == limit:
                    raise OSError(errno.EFBIG, f"over {limit} entries", file.rel)
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                found.append(
                    StateEntry(entry.name, _kind(info.st_mode), info.st_size, info.st_mtime)
                )
    finally:
        os.close(fd)
    return tuple(sorted(found))


@contextlib.contextmanager
def snapshot(
    file: StateFile, *, limit: int, siblings: tuple[str, ...] = ()
) -> Iterator[str | None]:
    """A private copy of `file` and of each `rel + suffix` sibling present, `limit` bytes in all,
    unchanged across the copy; its path while open, then removed. None: no `file`.
    """
    names = [file, *(file._replace(rel=file.rel + suffix) for suffix in siblings)]
    for name in names:
        _parts(name)
    folder = tempfile.mkdtemp(prefix="ensemble-state-")
    try:
        yield _stable_copy(names, limit, folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _stable_copy(names: list[StateFile], limit: int, folder: str) -> str | None:
    for _ in range(SNAPSHOT_ATTEMPTS):
        copied = _copy(names, limit, folder)
        if copied[0] is None:
            return None
        if [_identity(name) for name in names] == copied:
            return os.path.join(folder, posixpath.basename(names[0].rel))
    raise OSError(errno.EAGAIN, "client state changed while copied", names[0].rel)


def _copy(names: list[StateFile], limit: int, folder: str) -> list[tuple[int, int] | None]:
    """Each name's (size, mtime_ns) as copied, None when absent; a missing main copies nothing."""
    copied: list[tuple[int, int] | None] = []
    total = 0
    for name in names:
        target = os.path.join(folder, posixpath.basename(name.rel))
        with contextlib.suppress(FileNotFoundError):
            os.unlink(target)
        try:
            fd, info = _open(name)
        except FileNotFoundError:
            copied.append(None)
            if name is names[0]:
                return copied
            continue
        try:
            out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            try:
                while chunk := os.read(fd, min(CHUNK, limit - total + 1)):
                    total += len(chunk)
                    if total > limit:
                        raise OSError(errno.EFBIG, f"over {limit} bytes", name.rel)
                    os.write(out, chunk)
            finally:
                os.close(out)
        finally:
            os.close(fd)
        copied.append((info.st_size, info.st_mtime_ns))
    return copied


def _identity(name: StateFile) -> tuple[int, int] | None:
    try:
        fd, info = _open(name)
    except FileNotFoundError:
        return None
    os.close(fd)
    return info.st_size, info.st_mtime_ns


def _open(file: StateFile, *, directory: bool = False) -> tuple[int, os.stat_result]:
    """An fd of `file` (`directory`: a dir, else a regular file) and its fstat, opened part by part
    from the state root, no part below it a link; FileNotFoundError: a part is missing.
    """
    parts = _parts(file)
    fd = os.open(_roots()[0], DIR_FLAGS)
    try:
        for part in parts[:-1]:
            fd, parent = os.open(part, DIR_FLAGS | os.O_NOFOLLOW, dir_fd=fd), fd
            os.close(parent)
        _check(os.lstat(parts[-1], dir_fd=fd).st_mode, file, directory=directory)
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
        opened = os.open(parts[-1], flags | (os.O_DIRECTORY if directory else 0), dir_fd=fd)
    finally:
        os.close(fd)
    try:
        info = os.fstat(opened)
        _check(info.st_mode, file, directory=directory)
    except BaseException:
        os.close(opened)
        raise
    return opened, info


def _check(mode: int, file: StateFile, *, directory: bool) -> None:
    if stat_module.S_ISLNK(mode):
        raise OSError(errno.ELOOP, "a link", file.rel)
    if directory and not stat_module.S_ISDIR(mode):
        raise OSError(errno.ENOTDIR, "not a directory", file.rel)
    if not directory and not stat_module.S_ISREG(mode):
        raise OSError(errno.EINVAL, "not a regular file", file.rel)


def _parts(file: StateFile) -> list[str]:
    parts = [file.session, file.client, *file.rel.split("/")]
    if not all(_part(part) for part in parts):
        raise OSError(errno.EINVAL, "invalid client state path", repr(file))
    return parts


def _part(part: str) -> bool:
    return part not in {"", os.curdir, os.pardir} and "/" not in part and "\0" not in part


def _kind(mode: int) -> StateKind:
    if stat_module.S_ISDIR(mode):
        return StateKind.DIR
    return StateKind.FILE if stat_module.S_ISREG(mode) else StateKind.OTHER


def _roots() -> tuple[str, ...]:
    """The state root as named, then its real path when that differs."""
    named = os.path.abspath(os.path.expanduser(DOCKER_STATE))
    return tuple(dict.fromkeys((named, os.path.realpath(named))))
