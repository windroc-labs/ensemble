"""Writing to a local board: the one lock its programs share, an issue file's atomic replace and the
next issue number."""

import contextlib
import fcntl
import os
import re
import tempfile
from collections.abc import Iterator

from ensemble.orchestrator.engine.flow.types import ISSUE_ID
from ensemble.orchestrator.engine.types import TrackerFileError


@contextlib.contextmanager
def locked(board: str) -> Iterator[None]:
    """Holds <board>/.lock exclusively; another holder waits. Not reentrant."""
    fd = os.open(
        os.path.join(board, ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def replace(path: str, text: str) -> None:
    """path holds the old or the new full text, never part of one; the dot temp is never scanned."""
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def next_id(board: str, prefix: str) -> str:
    """`<prefix>-<n>` from .next-id (absent: 1), which then holds n+1; call it inside locked(board)."""
    path = os.path.join(board, ".next-id")
    try:
        with open(path, encoding="utf-8") as file:
            text = file.read()
    except FileNotFoundError:
        text = "1"
    if not re.fullmatch(r"[0-9]+\n?", text):
        raise TrackerFileError(path, f"bad .next-id text {text!r}")
    number = int(text)
    ident = f"{prefix}-{number}"
    if not re.fullmatch(ISSUE_ID, ident):
        raise TrackerFileError(path, f"bad issue id {ident!r}")
    replace(path, f"{number + 1}\n")
    return ident
