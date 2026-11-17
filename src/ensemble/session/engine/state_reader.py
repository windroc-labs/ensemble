"""state_reader.py ROOT: serves reads of a client state volume mounted at ROOT, one JSON request line
on stdin at a time, until EOF. Runs in a session's reader container, standard library only.

Request: {"op": "read"|"stat"|"entries"|"snapshot", "path": "<client>/<rel>", ...}; read adds
offset, limit and tail, entries a limit, snapshot "paths" (main first) and a total "limit".
Reply: one JSON header line {"errno", "message", "size", "mtime", "entries", "length"}, then
`length` payload bytes: a read's bytes, or a snapshot's tar of regular files named by basename.
Every path is opened part by part from ROOT with no link followed.
"""

import errno
import io
import json
import os
import stat
import sys
import tarfile

SNAPSHOT_ATTEMPTS = 3
CHUNK = 1 << 20
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        return 64
    root = argv[0]
    out = sys.stdout.buffer
    for line in sys.stdin.buffer:
        header, payload = answer(root, line)
        out.write(json.dumps({**header, "length": len(payload)}).encode() + b"\n")
        out.write(payload)
        out.flush()
    return 0


def answer(root: str, line: bytes) -> tuple[dict, bytes]:
    reply: dict = {"errno": None, "message": "", "size": 0, "mtime": 0.0, "entries": []}
    try:
        request = json.loads(line)
        if not isinstance(request, dict):
            raise ValueError
        payload = _serve(root, request, reply)
    except OSError as error:
        reply.update(errno=error.errno or errno.EIO, message=error.strerror or str(error))
        payload = b""
    except (ValueError, KeyError, TypeError):
        reply.update(errno=errno.EINVAL, message="bad request")
        payload = b""
    return reply, payload


def _serve(root: str, request: dict, reply: dict) -> bytes:
    op = request["op"]
    if op == "snapshot":
        return _snapshot(root, request["paths"], _count(request["limit"]))
    path = request["path"]
    if op == "entries":
        reply["entries"] = _entries(root, path, _count(request["limit"]))
        return b""
    fd, info = _open(root, path)
    with os.fdopen(fd, "rb") as f:
        reply.update(size=info.st_size, mtime=info.st_mtime)
        if op == "stat":
            return b""
        if op != "read":
            raise ValueError
        offset = _count(request["offset"])
        cap = _count(request["limit"])
        f.seek(offset)
        if request["tail"] is True:
            f.seek(max(f.seek(0, os.SEEK_END) - cap, offset))
        return f.read(cap)


def _count(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError
    return value


def _entries(root: str, path: str, limit: int) -> list:
    fd, _ = _open(root, path, directory=True)
    found = []
    try:
        with os.scandir(fd) as listing:
            for entry in listing:
                if len(found) == limit:
                    raise OSError(errno.EFBIG, f"over {limit} entries")
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                found.append([entry.name, _kind(info.st_mode), info.st_size, info.st_mtime])
    finally:
        os.close(fd)
    return found


def _kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "dir"
    return "file" if stat.S_ISREG(mode) else "other"


def _snapshot(root: str, paths: list, limit: int) -> bytes:
    if not isinstance(paths, list) or not paths:
        raise ValueError
    for _ in range(SNAPSHOT_ATTEMPTS):
        copied = _copy(root, paths, limit)
        if [_identity(root, path) for path in paths] == [c[0] for c in copied]:
            return _archive(paths, copied)
    raise OSError(errno.EAGAIN, "client state changed while copied")


def _copy(root: str, paths: list, limit: int) -> list[tuple[tuple[int, int] | None, bytes]]:
    copied: list[tuple[tuple[int, int] | None, bytes]] = []
    total = 0
    for index, path in enumerate(paths):
        try:
            fd, info = _open(root, path)
        except FileNotFoundError:
            if index == 0:
                raise
            copied.append((None, b""))
            continue
        chunks = []
        with os.fdopen(fd, "rb") as f:
            while chunk := f.read(min(CHUNK, limit - total + 1)):
                total += len(chunk)
                if total > limit:
                    raise OSError(errno.EFBIG, f"over {limit} bytes")
                chunks.append(chunk)
        copied.append(((info.st_size, info.st_mtime_ns), b"".join(chunks)))
    return copied


def _identity(root: str, path: str) -> tuple[int, int] | None:
    try:
        fd, info = _open(root, path)
    except FileNotFoundError:
        return None
    os.close(fd)
    return info.st_size, info.st_mtime_ns


def _archive(paths: list, copied: list) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for path, (identity, data) in zip(paths, copied, strict=True):
            if identity is None:
                continue
            member = tarfile.TarInfo(path.rsplit("/", 1)[-1])
            member.size, member.mode = len(data), 0o600
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def _open(root: str, path: str, *, directory: bool = False) -> tuple[int, os.stat_result]:
    parts = _parts(path)
    fd = os.open(root, DIR_FLAGS)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, DIR_FLAGS | os.O_NOFOLLOW, dir_fd=fd)
            except NotADirectoryError:  # Linux, for a link
                if stat.S_ISLNK(os.lstat(part, dir_fd=fd).st_mode):
                    raise OSError(errno.ELOOP, "a link", part) from None
                raise
            os.close(fd)
            fd = child
        _check(os.lstat(parts[-1], dir_fd=fd).st_mode, directory=directory)
        opened = os.open(parts[-1], FILE_FLAGS | (os.O_DIRECTORY if directory else 0), dir_fd=fd)
    finally:
        os.close(fd)
    try:
        info = os.fstat(opened)
        _check(info.st_mode, directory=directory)
    except BaseException:
        os.close(opened)
        raise
    return opened, info


def _check(mode: int, *, directory: bool) -> None:
    if stat.S_ISLNK(mode):
        raise OSError(errno.ELOOP, "a link")
    if directory and not stat.S_ISDIR(mode):
        raise OSError(errno.ENOTDIR, "not a directory")
    if not directory and not stat.S_ISREG(mode):
        raise OSError(errno.EINVAL, "not a regular file")


def _parts(path) -> list[str]:
    parts = path.split("/") if isinstance(path, str) else []
    if len(parts) < 2 or not all(
        part not in {"", ".", ".."} and "\0" not in part for part in parts
    ):
        raise OSError(errno.EINVAL, "invalid client state path")
    return parts


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
