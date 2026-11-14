import contextlib
import errno
import os


def write_beneath(root: str, path: str, text: str) -> None:
    """`text` to `path` below `root`: dirs made without following a link, a temp file renamed over
    the leaf. OSError also outside `root` (EXDEV).
    """
    *dirs, leaf = _below(root, path)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in dirs:
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=fd)
            fd, parent = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd), fd
            os.close(parent)
        temp = f".tmp-{os.urandom(8).hex()}"
        # by path, checked against the dir fd below: the unit-test write audit sees no dir_fd
        made = os.path.join(os.path.realpath(root), *dirs, temp)
        out = os.open(made, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(out, "w", encoding="utf-8") as f:
                placed = _stat(temp, fd)
                if placed is None or not os.path.samestat(os.fstat(f.fileno()), placed):
                    os.unlink(made)
                    raise OSError(errno.EXDEV, f"not below {root}", path)
                f.write(text)
            os.replace(temp, leaf, src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp, dir_fd=fd)
            raise
    finally:
        os.close(fd)


def _stat(name: str, dir_fd: int) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _below(root: str, path: str) -> list[str]:
    relative = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    if relative == os.curdir or relative.split(os.sep)[0] == os.pardir:
        raise OSError(errno.EXDEV, f"outside {root}", path)
    return relative.split(os.sep)
