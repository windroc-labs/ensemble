"""A session harness's dir, HARNESS_ROOT/<32 hex>, and its files. Readers treat them as
untrusted: a malformed line is skipped, a malformed file absent.
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
from ensemble.session.harness.types import SERVED, Logged, Op
from ensemble.session.hosts.base import HARNESS_ROOT
from ensemble.utils import STALE_AGE, append_line, read_regular, save

HEX = re.compile(r"[0-9a-f]{32}")
REQUEST_WAIT = 15
CLOSE_WAIT = 10  # a harness's wait for its worker's confirmed exit
DRAIN = 30  # a harness's wait for its handlers after that exit
END_WAIT = CLOSE_WAIT + DRAIN + REQUEST_WAIT
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
    """A headless start's caller pipe: the harness closes its worker at EOF on `far`, once every
    copy of the caller's end closed.
    """

    def __init__(self) -> None:
        self.far = self._near = -1  # for __del__ when os.pipe fails
        self.far, self._near = os.pipe()  # neither inheritable

    def handed(self) -> None:
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
    """EOF on non-blocking `far`; anything read is ignored."""
    try:
        return os.read(fd, 1 << 10) == b""
    except BlockingIOError:
        return False
    except OSError:
        return True


class Channel:
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
        self.asked: set[str] = set()  # requests in flight from its holder: its own handlers'

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
        """Valid records of complete lines past `cursor`, and the next cursor."""
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

    def request(self, op: Op, timeout: float = END_WAIT) -> bool:
        """True once done: from its holder's handlers, at the worker's confirmed exit; else once
        the harness answered after its handlers drained and let go of its lock (within `timeout`).
        False with no harness; Unanswered unconfirmed or unanswered; SessionError refused.
        """
        if op not in tuple(Op):
            raise ValueError(f"request {op!r}: want one of {', '.join(Op)}")
        deadline = time.monotonic() + timeout
        if self._exchange({"op": op}, timeout) is None:
            return False
        while self._held is None and self.alive() and time.monotonic() < deadline:
            time.sleep(SLICE)
        return True

    def ask(self, op: Op, data: Mapping, timeout: float = REQUEST_WAIT) -> dict:
        """Its answer's data; SessionError refused or with no harness, Unanswered unanswered."""
        if op not in SERVED:
            raise ValueError(f"ask {op!r}: want one of {', '.join(SERVED)}")
        answer = self._exchange({"op": op, "data": dict(data)}, timeout)
        if answer is None:
            raise SessionError(f"session harness {self.directory} is gone")
        found = answer.get("data")
        return found if isinstance(found, dict) else {}

    def _exchange(self, body: Mapping, timeout: float) -> dict | None:
        """The answer once ok; None with no harness."""
        if not self.alive():
            return None
        ident = uuid.uuid4().hex
        if self._held is not None:
            self.asked.add(ident)
        try:
            return self._answered(body, ident, timeout)
        finally:
            self.asked.discard(ident)

    def _answered(self, body: Mapping, ident: str, timeout: float) -> dict | None:
        op = body["op"]
        reply = os.path.join(self._replies, ident + ".json")
        os.makedirs(self._requests, 0o700, exist_ok=True)
        save(os.path.join(self._requests, ident + ".json"), json.dumps(body))
        deadline = time.monotonic() + timeout
        while True:
            answer = _json(reply)
            if isinstance(answer, dict):
                _remove(reply)
                if answer.get("ok") is True:
                    return answer
                error = answer.get("error")
                text = error if isinstance(error, str) else f"session harness: {op}"
                raise (Unanswered if answer.get("unanswered") is True else SessionError)(text)
            if not self.alive():
                _remove(os.path.join(self._requests, ident + ".json"))
                if isinstance(_json(reply), dict):  # answered just before it let go
                    continue
                return None
            if time.monotonic() >= deadline:
                _remove(os.path.join(self._requests, ident + ".json"))
                raise Unanswered(f"session harness {self.directory}: no answer to {op}")
            time.sleep(SLICE)

    def requests(self) -> list[tuple[str, Op]]:
        return [(ident, op) for ident, op, _ in self.pending()]

    def request_pending(self, ident: str) -> bool:
        return os.path.isfile(os.path.join(self._requests, ident + ".json"))

    def pending(self, *, keep: tuple[Op, ...] = ()) -> list[tuple[str, Op, dict]]:
        """Pending (id, op, data), oldest first, taken except `keep`; malformed ones deleted. Only the user's
        processes can write this 0700 dir; no worker or container can.
        """
        try:
            entries = list(os.scandir(self._requests))
        except FileNotFoundError:
            return []
        found = []
        for entry in entries:
            if entry.name.startswith(SAVING):
                continue
            ident, suffix = entry.name[:-5], entry.name[-5:]
            body = None
            if suffix == ".json" and HEX.fullmatch(ident) and entry.is_file(follow_symlinks=False):
                body = _json(entry.path)
            op = body.get("op") if isinstance(body, dict) else None
            data = body.get("data", {}) if isinstance(body, dict) else None
            try:
                when = entry.stat(follow_symlinks=False).st_mtime_ns
            except OSError:
                when = None
            if op not in tuple(Op) or not isinstance(data, dict) or when is None:
                _remove(entry.path)
                LOG.warning("session harness: dropped request %s", entry.name[:80])
                continue
            if op not in keep:
                _remove(entry.path)
            found.append((when, ident, Op(op), data))
        return [(ident, op, data) for _, ident, op, data in sorted(found, key=lambda f: f[:2])]

    def reply(
        self,
        ident: str,
        *,
        ok: bool,
        error: str | None = None,
        unanswered: bool = False,
        data: Mapping | None = None,
    ) -> None:
        _remove(os.path.join(self._requests, ident + ".json"))
        os.makedirs(self._replies, 0o700, exist_ok=True)
        answer = {
            "ok": ok,
            **({"error": error} if error is not None else {}),
            **({"unanswered": True} if unanswered else {}),
            **({"data": dict(data)} if data is not None else {}),
        }
        save(os.path.join(self._replies, ident + ".json"), json.dumps(answer))

    def status(self) -> dict:
        found = _json(self.status_file)
        return found if isinstance(found, dict) else {}

    def write_status(self, status: Mapping) -> None:
        save(self.status_file, json.dumps(status, ensure_ascii=False))


def bound(name: str, state: Mapping) -> Channel | None:
    """Its owner, once that harness.json binds it to `name`, sid and generation."""
    owner = state.get("owner")
    if owner is None:
        return None
    channel = Channel(owner)
    deadline = time.monotonic() + REQUEST_WAIT
    while channel.alive() and (found := channel.status()).get("state") in (None, "starting"):
        if time.monotonic() > deadline:
            raise Unanswered(f"session harness {owner}: still starting {name}")
        time.sleep(SLICE)
    if not channel.alive():
        return None
    if found.get("state") in ("running", "draining", "exited") and (
        found.get("name"),
        found.get("sid"),
        found.get("generation"),
    ) == (name, state.get("sid"), state.get("generation")):
        return channel
    LOG.warning("session %s: owner %s does not own it; ignored", name, owner)
    return None


def running(name: str, state: Mapping) -> Channel | None:
    channel = bound(name, state)
    return channel if channel is not None and channel.status().get("state") == "running" else None


def sweep() -> None:
    """Removes unheld harness dirs older than STALE_AGE (no harness.json: a day)."""
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
