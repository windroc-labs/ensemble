import contextlib
import errno
import fcntl
import os
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Container, Iterable, Iterator
from enum import StrEnum
from pathlib import Path

STALE_AGE = 30 * 24 * 60 * 60


def read_regular(
    path: str | os.PathLike,
    *,
    offset: int = 0,
    limit: int | None = None,
    tail: bool = False,
) -> bytes | None:
    """From byte `offset`: to the end, else the first `limit` bytes (the last with `tail`); None: no
    regular file, an OSError. Never waits on a FIFO.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    return _read_fd(fd, offset, limit, tail=tail)


def open_beneath(root: str, path: str, *, directory: bool = False) -> int:
    """A read-only fd of `path` below `root` (`directory`: it must be one), opened part by part
    from `root` without following a link. OSError also for a path outside `root` (EXDEV).
    """
    parts = os.path.relpath(os.path.abspath(path), os.path.abspath(root)).split(os.sep)
    if parts[0] in {os.curdir, os.pardir}:
        raise OSError(errno.EXDEV, f"not below {root}", path)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            fd, parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd), fd
            os.close(parent)
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0)
        return os.open(parts[-1], flags, dir_fd=fd)
    finally:
        os.close(fd)


def read_beneath(
    root: str, path: str, *, offset: int = 0, limit: int | None = None, tail: bool = False
) -> bytes | None:
    """read_regular of open_beneath's `path`; None also for any OSError of open_beneath."""
    try:
        fd = open_beneath(root, path)
    except OSError:
        return None
    return _read_fd(fd, offset, limit, tail=tail)


def _read_fd(fd: int, offset: int, limit: int | None, *, tail: bool) -> bytes | None:
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            f.seek(offset)
            if limit is None:
                return f.read()
            if tail:
                f.seek(max(f.seek(0, os.SEEK_END) - limit, offset))
            return f.read(limit)
    except OSError:
        return None
    finally:
        os.close(fd)


def holds(path: str | Path, text: str) -> bool:
    """Whether `path` is a regular file holding exactly `text`."""
    data = text.encode()
    return read_regular(path, limit=len(data) + 1) == data


@contextlib.contextmanager
def file_lock(path: str | Path, *, wait: bool = True, shared: bool = False) -> Iterator[None]:
    """Exclusive (`shared`: shared) stable <path>.lock, non-inheritable; held, `wait=False` raises
    BlockingIOError.
    Event writers acquire it before opening the event file and hold it through descriptor close,
    without acquiring a roster lock. Rotators hold it through physical changes and durable ledger
    save; never replace this lock.
    """
    lock = str(path) + ".lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK, 0o600)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(errno.EINVAL, "not a regular lock file", lock)
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(fd, mode if wait else mode | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def inode_lock(path: str | Path, *, wait: bool = True) -> Iterator[bool]:
    """Exclusive flock on `path` itself, held once `path` still names the locked file, so a holder
    may unlink it; False without waiting when another holds it.
    """
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "not a regular lock file", str(path))
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                same = os.path.samestat(os.fstat(fd), os.stat(path))
            except FileNotFoundError:
                same = False
            if same:
                yield True
                return
        finally:
            os.close(fd)


def idle(paths: Iterable[str | Path], seconds: float) -> bool:
    """Every existing path (not followed) last modified over `seconds` ago."""
    cutoff = time.time() - seconds
    for path in paths:
        try:
            if os.lstat(path).st_mtime >= cutoff:
                return False
        except FileNotFoundError:
            continue
    return True


def drop_stale(directory: str, keep: Container[str] = ()) -> list[OSError]:
    """Unlinks the regular files directly in `directory`, but those named in `keep`, last modified
    over STALE_AGE ago, never following symlinks; returns the errors, each skipping only its entry
    (a listing error: all).
    """
    errors: list[OSError] = []
    cutoff = time.time() - STALE_AGE
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if (
                        entry.name not in keep
                        and entry.is_file(follow_symlinks=False)
                        and entry.stat(follow_symlinks=False).st_mtime < cutoff
                    ):
                        os.unlink(entry.path)
                except FileNotFoundError:
                    continue
                except OSError as error:
                    errors.append(error)
    except OSError as error:
        errors.append(error)
    return errors


def remove_tree(path: str, *, dir_fd: int | None = None) -> None:
    """shutil.rmtree of `path` (relative to `dir_fd`); refused (PermissionError), it gives the user
    rwx on each directory of the tree, never following a link, and tries once more.
    """
    try:
        shutil.rmtree(path, dir_fd=dir_fd)
    except PermissionError:
        _owner_rwx(path, dir_fd)
        shutil.rmtree(path, dir_fd=dir_fd)


def _owner_rwx(name: str, dir_fd: int | None) -> None:
    """Best effort: a directory it cannot open or change is skipped, its subtree with it."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        return
    try:
        mode = os.fstat(fd).st_mode
        if mode & stat.S_IRWXU != stat.S_IRWXU:
            os.fchmod(fd, stat.S_IMODE(mode) | stat.S_IRWXU)
        with os.scandir(fd) as entries:
            subdirs = [e.name for e in entries if e.is_dir(follow_symlinks=False)]
        for subdir in subdirs:
            _owner_rwx(subdir, fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def sync_directory(path: str | Path) -> None:
    """Persist directory entries after rename or unlink; filesystem errors propagate."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append_line(
    path: str | Path,
    text: str | bytes,
    *,
    durable: bool = False,
    locked: bool = False,
) -> None:
    """Refuses anything but a regular file, never waits on a FIFO; `durable` syncs it and its
    directory; `locked` uses file_lock.
    """
    data = memoryview(text.encode(errors="replace") if isinstance(text, str) else text)
    with file_lock(path) if locked else contextlib.nullcontext():
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(errno.EINVAL, "not a regular file", str(path))
            while data:
                data = data[os.write(fd, data) :]
            if durable:
                os.fsync(fd)
        finally:
            os.close(fd)
        if durable:
            sync_directory(os.path.dirname(os.path.abspath(path)))


def validate_events(path: str) -> str:
    """An events file's absolute path, checked; never opens it for writing or creates it
    (events_file does). A guard display's pane-died line goes through tmux, which misreads # and a
    trailing ;. ValueError names the path and why.
    """
    path = os.path.abspath(path)
    if "#" in path or path.endswith(";"):
        raise ValueError(f"events file {path}: tmux would misread it")
    try:
        info = os.stat(path)
    except FileNotFoundError:
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            raise ValueError(f"events file {path}: No such file or directory") from None
        if not os.access(parent, os.W_OK):
            raise ValueError(f"events file {path}: Permission denied") from None
    except OSError as error:
        raise ValueError(f"events file {path}: {error.strerror}") from error
    else:
        if stat.S_ISDIR(info.st_mode):
            raise ValueError(f"events file {path}: {os.strerror(errno.EISDIR)}")
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"events file {path}: not a regular file")
        if not os.access(path, os.W_OK):
            raise ValueError(f"events file {path}: Permission denied")
    return path


def events_file(path: str) -> str:
    """validate_events, then creates it 0600 when missing; ValueError unless a regular file."""
    path = validate_events(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK, 0o600)
        try:
            st = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError as e:
        raise ValueError(f"events file {path}: {e.strerror}") from e
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"events file {path}: not a regular file")
    return path


def save(path: str | Path, text: str | bytes, *, durable: bool = False) -> None:
    """Writes atomically, replacing a symlink without following. `durable` syncs the replacement
    and its directory.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=".tmp-", delete=False) as f:
        try:
            f.write(text.encode() if isinstance(text, str) else text)
            if durable:
                f.flush()
                os.fsync(f.fileno())
            f.close()
            os.replace(f.name, path)
        except BaseException:
            os.unlink(f.name)
            raise
    if durable:
        sync_directory(path.parent)


class ConfinedReason(StrEnum):
    REGULAR = "regular"
    SUFFIX = "suffix"
    OUTSIDE = "outside"
    TOO_LARGE = "too_large"


class ConfinedFileError(ValueError):
    def __init__(self, reason: ConfinedReason, path: str, *, limit: int | None = None):
        self.reason = reason
        self.path = path
        self.limit = limit
        super().__init__(
            f"{path!r}: {reason.value}" + (f" (limit {limit})" if limit is not None else "")
        )


def _descriptor_path(fd: int) -> Path:
    if sys.platform == "darwin":
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0]
        return Path(os.fsdecode(raw)).resolve()
    return Path(os.readlink(f"/proc/self/fd/{fd}")).resolve()


def _checked(fd: int, path: str, root: Path, suffix: str) -> os.stat_result:
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode):
        raise ConfinedFileError(ConfinedReason.REGULAR, path)
    real = _descriptor_path(fd)
    if real.suffix != suffix:
        raise ConfinedFileError(ConfinedReason.SUFFIX, path)
    if not real.is_relative_to(root):
        raise ConfinedFileError(ConfinedReason.OUTSIDE, path)
    return opened


def _within_limit(path: str, size: int, limit: int) -> None:
    if size > limit:
        raise ConfinedFileError(ConfinedReason.TOO_LARGE, path, limit=limit)


def read_confined(path: str, root: str, *, limit: int, suffix: str = ".md") -> bytes:
    """
    Reads at most `limit` bytes of a regular `suffix` file under `root` (by its open descriptor's
    path). OSError reports filesystem failures; ConfinedFileError reports confinement failures.
    """
    base = Path(root).resolve()
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        opened = _checked(fd, path, base, suffix)
        _within_limit(path, opened.st_size, limit)
        with os.fdopen(fd, "rb", closefd=False) as source:
            data = source.read(limit + 1)
        _within_limit(path, len(data), limit)
        return data
    finally:
        os.close(fd)
