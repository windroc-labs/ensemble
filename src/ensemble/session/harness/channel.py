"""A session harness's dir, HARNESS_ROOT/<32 hex>, and its files (docs/sessions.md). Readers
treat them as untrusted: a malformed line is skipped, a malformed file absent.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import logging
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Mapping

from ensemble.session.engine.types import (
    Activity,
    Blocked,
    ClientBound,
    Exited,
    LimitHit,
    Reply,
    SessionError,
    SessionEvent,
    Stderr,
    TurnEnd,
    TurnFailed,
    TurnStarted,
    Unanswered,
)
from ensemble.session.harness.types import Logged, Op
from ensemble.session.hosts.base import HARNESS_ROOT
from ensemble.utils import STALE_AGE, append_line, read_regular, save

HEX = re.compile(r"[0-9a-f]{32}")
REQUEST_WAIT = 15
SLICE = 0.05
DAY = 24 * 60 * 60
SAVING = ".tmp-"  # utils.save's temp prefix
FILE_MAX = 1 << 20
LOG = logging.getLogger("session")


TYPES: dict[str, type] = {
    kind.__name__: kind
    for kind in (
        TurnStarted,
        Activity,
        Reply,
        TurnEnd,
        TurnFailed,
        Blocked,
        LimitHit,
        Exited,
        Stderr,
        ClientBound,
    )
}
FIELDS = {
    "detail": lambda v: isinstance(v, str),
    "text": lambda v: isinstance(v, str),
    "reply": lambda v: isinstance(v, str),
    "pending": lambda v: type(v) is int,
    "error": lambda v: isinstance(v, str),
    "reason": lambda v: isinstance(v, str),
    "resets_at": lambda v: v is None or type(v) in (int, float),
    "returncode": lambda v: type(v) is int,
    "client_id": lambda v: isinstance(v, str),
}


def encode(event: SessionEvent | Stderr | ClientBound) -> dict:
    return {"type": type(event).__name__, "fields": dataclasses.asdict(event)}


def decode(record) -> SessionEvent | Stderr | ClientBound | None:
    """None unless exactly one of TYPES."""
    if not isinstance(record, Mapping):
        return None
    name, fields = record.get("type"), record.get("fields")
    kind = TYPES.get(name) if isinstance(name, str) else None
    if kind is None or not isinstance(fields, Mapping):
        return None
    names = {field.name for field in dataclasses.fields(kind)}
    if set(fields) != names or not all(FIELDS[name](fields[name]) for name in names):
        return None
    return kind(**fields)


def _logged(record) -> Logged | None:
    if not isinstance(record, dict):
        return None
    seq, ident, turn = record.get("seq"), record.get("id"), record.get("turn")
    event = decode(record)
    if (
        type(seq) is not int
        or not isinstance(ident, str)
        or not (turn is None or isinstance(turn, str))
        or event is None
    ):
        return None
    return Logged(seq, ident, turn, event)


def _json(path: str):
    data = read_regular(path, limit=FILE_MAX + 1)
    if data is None or len(data) > FILE_MAX:
        return None
    try:
        return json.loads(data)
    except (ValueError, RecursionError):
        return None


def _remove(path: str) -> None:
    with contextlib.suppress(OSError):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)


class Tie:
    """A headless start's caller pipe: its harness gets `far`, the caller's view keeps the other
    end. Every copy of that end closed (the view dropped or its process ended, however) is EOF on
    `far`, and the harness closes its worker: a headless run dies with its caller, as a piped
    client did.
    """

    def __init__(self) -> None:
        self.far = self._near = -1  # for __del__ when os.pipe fails
        self.far, self._near = os.pipe()  # neither inheritable

    def handed(self) -> None:
        """The harness has its copy of `far`."""
        self.far = _closed(self.far)

    def close(self) -> None:
        self.far, self._near = _closed(self.far), _closed(self._near)

    __del__ = close


def _closed(fd: int) -> int:
    if fd >= 0:
        with contextlib.suppress(OSError):
            os.close(fd)
    return -1


def hung_up(fd: int) -> bool:
    """EOF on non-blocking `far`; its caller writes nothing, so anything read is ignored."""
    try:
        return os.read(fd, 1 << 10) == b""
    except BlockingIOError:
        return False
    except OSError:
        return True


class Channel:
    """One harness dir: its harness holds its lock; views read its log and send requests."""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        self.lock = os.path.join(directory, "harness.lock")
        self.events = os.path.join(directory, "events.jsonl")
        self.deliveries = os.path.join(directory, "deliveries.jsonl")
        self.status_file = os.path.join(directory, "harness.json")
        self.log = os.path.join(directory, "harness.log")
        self._request = os.path.join(directory, "request.json")
        self._requests = os.path.join(directory, "requests")
        self._replies = os.path.join(directory, "replies")
        self._held: int | None = None

    @classmethod
    def create(cls) -> Channel:
        root = os.path.expanduser(HARNESS_ROOT)
        try:
            os.makedirs(root, 0o700, exist_ok=True)
            sweep()
            directory = os.path.join(root, uuid.uuid4().hex)
            os.mkdir(directory, 0o700)
        except OSError as error:
            raise SessionError(f"session harness dir: {error.strerror or error}") from None
        return cls(directory)

    def write_request(self, request: Mapping) -> None:
        """0600, never over a file or through a link."""
        data = json.dumps(request)  # before the file: a value JSON cannot hold writes nothing
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(self._request, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)

    def take_request(self) -> dict:
        found = _json(self._request)
        self.discard_request()
        if not isinstance(found, dict):
            raise SessionError(f"{self._request}: missing or not a JSON object")
        return found

    def discard_request(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._request)

    def append(self, record: Mapping) -> None:
        append_line(self.events, json.dumps(record, ensure_ascii=False) + "\n")

    def note(self, record: Mapping) -> None:
        append_line(self.deliveries, json.dumps(record, ensure_ascii=False) + "\n")

    def read(self, cursor: int) -> tuple[list[Logged], int]:
        """The valid records of the complete lines past `cursor`, and the next cursor."""
        data = read_regular(self.events, offset=cursor) or b""
        end = data.rfind(b"\n") + 1
        found = []
        for line in data[:end].split(b"\n"):
            try:
                record = json.loads(line) if line else None
            except (ValueError, RecursionError):
                continue
            if (logged := _logged(record)) is not None:
                found.append(logged)
        return found, cursor + end

    def alive(self) -> bool:
        try:
            fd = os.open(self.lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        finally:
            os.close(fd)
        return False

    def hold(self) -> None:
        """SessionError when another holds it."""
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise SessionError(f"{self.lock}: not a regular file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise SessionError(f"session harness {self.directory} is already held") from None
        except BaseException:
            os.close(fd)
            raise
        self._held = fd

    def release(self) -> None:
        if self._held is not None:
            os.close(self._held)
            self._held = None

    def request(self, op: Op, timeout: float = REQUEST_WAIT) -> bool:
        """True once done; False with no harness; Unanswered unconfirmed or unanswered;
        SessionError refused.
        """
        if op not in tuple(Op):
            raise ValueError(f"request {op!r}: want one of {', '.join(Op)}")
        if not self.alive():
            return False
        ident = uuid.uuid4().hex
        reply = os.path.join(self._replies, ident + ".json")
        os.makedirs(self._requests, 0o700, exist_ok=True)
        save(os.path.join(self._requests, ident + ".json"), json.dumps({"op": op}))
        deadline = time.monotonic() + timeout
        while True:
            answer = _json(reply)
            if isinstance(answer, dict):
                _remove(reply)
                if answer.get("ok") is True:
                    return True
                error = answer.get("error")
                text = error if isinstance(error, str) else f"session harness: {op}"
                raise (Unanswered if answer.get("unanswered") is True else SessionError)(text)
            if not self.alive():
                _remove(os.path.join(self._requests, ident + ".json"))
                if isinstance(_json(reply), dict):  # answered just before it let go
                    continue
                return False
            if time.monotonic() >= deadline:
                _remove(os.path.join(self._requests, ident + ".json"))
                raise Unanswered(f"session harness {self.directory}: no answer to {op}")
            time.sleep(SLICE)

    def requests(self) -> list[tuple[str, Op]]:
        """Pending requests (id, op), oldest first, taken; malformed ones deleted."""
        try:
            entries = list(os.scandir(self._requests))
        except FileNotFoundError:
            return []
        found = []
        for entry in entries:
            if entry.name.startswith(SAVING):
                continue  # still being saved
            ident, suffix = entry.name[:-5], entry.name[-5:]
            body = None
            if suffix == ".json" and HEX.fullmatch(ident) and entry.is_file(follow_symlinks=False):
                body = _json(entry.path)
            op = body.get("op") if isinstance(body, dict) else None
            try:
                when = entry.stat(follow_symlinks=False).st_mtime_ns
            except OSError:
                when = None
            _remove(entry.path)
            if op not in tuple(Op) or when is None:
                LOG.warning("session harness: dropped request %s", entry.name[:80])
                continue
            found.append((when, ident, Op(op)))
        return [(ident, op) for _, ident, op in sorted(found)]

    def reply(
        self, ident: str, *, ok: bool, error: str | None = None, unanswered: bool = False
    ) -> None:
        os.makedirs(self._replies, 0o700, exist_ok=True)
        answer = {
            "ok": ok,
            **({"error": error} if error is not None else {}),
            **({"unanswered": True} if unanswered else {}),
        }
        save(os.path.join(self._replies, ident + ".json"), json.dumps(answer))

    def status(self) -> dict:
        found = _json(self.status_file)
        return found if isinstance(found, dict) else {}

    def write_status(self, status: Mapping) -> None:
        save(self.status_file, json.dumps(status, ensure_ascii=False))


def sweep() -> None:
    """Removes unheld HARNESS_ROOT/<32 hex> dirs whose harness.json is STALE_AGE old (none: a
    day); errors logged.
    """
    root = os.path.expanduser(HARNESS_ROOT)
    now = time.time()
    try:
        entries = list(os.scandir(root))
    except OSError as error:
        LOG.warning("session harness sweep: %s", error)
        return
    for entry in entries:
        try:
            if not HEX.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
                continue
            channel = Channel(entry.path)
            try:
                age = now - os.stat(channel.status_file, follow_symlinks=False).st_mtime
                limit = STALE_AGE
            except FileNotFoundError:
                age, limit = now - entry.stat(follow_symlinks=False).st_mtime, DAY
            if age > limit and not channel.alive():
                shutil.rmtree(entry.path)
        except OSError as error:
            LOG.warning("session harness sweep %s: %s", entry.name, error)
