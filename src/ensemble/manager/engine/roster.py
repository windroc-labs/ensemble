"""A manager's durable roster, state machine, lease, and event read cursor."""

import contextlib
import datetime
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import asdict, replace
from typing import BinaryIO
from uuid import UUID, uuid4

from ensemble.manager.engine.events import Events
from ensemble.manager.engine.types import (
    Entry,
    Held,
    Lease,
    RosterError,
    Spec,
    Stale,
    StaleHolder,
    State,
)
from ensemble.utils import (
    Record,
    append_line,
    file_lock,
    one_line,
    printable,
    read_regular,
    save,
)

VERSION = 2
NOTE_MAX = 500
CLOSE_FAILURES = "close-failures.jsonl"
REASON_MAX = 500
ROSTER_MAX = 1 << 20
LOCK_TIMEOUT = 30
POLL = 0.1
RECEIPT_FILE = re.compile(r"receipt-[a-z0-9_]+\.stdout")


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _name(name: str) -> None:
    if not isinstance(name, str) or not name or printable(name) != name or "\t" in name:
        raise RosterError(f"name {name!r}: want nonempty printable text")


def _note(note: str | None) -> None:
    if note is not None and (
        not isinstance(note, str) or printable(note) != note or "\t" in note or len(note) > NOTE_MAX
    ):
        raise RosterError(f"note: want null or printable text of at most {NOTE_MAX} characters")


def _label(holder: dict | None) -> str:
    return holder["session"] or holder["id"] if holder is not None else "another holder"


def _uuid4(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return str(parsed) == value and parsed.version == 4


def _entry(data: dict) -> Entry:
    if set(data) != {"record", "state", "note", "started", "argv", "sid", "receipt"}:
        raise ValueError("entry: invalid fields")
    record = None if data["record"] is None else Record.parse(json.dumps(data["record"]))
    state = State(data["state"])
    _note(data["note"])
    if (
        not isinstance(data["started"], str)
        or datetime.datetime.fromisoformat(data["started"]).utcoffset() is None
    ):
        raise ValueError("started: want an ISO 8601 time with a UTC offset")
    argv = data["argv"]
    if not isinstance(argv, list | tuple) or not all(isinstance(word, str) for word in argv):
        raise ValueError("argv: want a list of strings")
    if state in {State.STARTING, State.LOST}:
        if record is not None or not argv:
            raise ValueError("starting/lost: want an intent without a Record")
    elif record is None:
        raise ValueError(f"{state}: missing Record")
    sid, receipt = data["sid"], data["receipt"]
    if receipt is not None and (
        not isinstance(receipt, str) or RECEIPT_FILE.fullmatch(receipt) is None
    ):
        raise ValueError("receipt: want an allocated receipt filename")
    if record is not None and record.sid != sid:
        raise ValueError("sid: differs from Record.sid")
    if not _uuid4(sid):
        raise ValueError("sid: want a canonical uuid4")
    return Entry(record, state, data["note"], data["started"], tuple(argv), sid, receipt)


class Roster:
    def __init__(self, directory: str):
        self.directory = os.path.abspath(directory)
        self.path = os.path.join(self.directory, "roster.json")
        self._events = Events(self.directory)
        self._lease_token: Lease | None = None

    @property
    def events_path(self) -> str:
        """Its events file's path, without a lease check or a write."""
        return self._events.path

    @property
    def events(self) -> str:
        with self._locked(create=True) as data:
            self._lease(data)
            self._events.ensure()
        return self._events.path

    def bind(self, lease: Lease | None) -> None:
        if lease is not None and (
            not _uuid4(lease.id) or type(lease.generation) is not int or lease.generation <= 0
        ):
            raise RosterError("lease: want a holder uuid4 and positive generation")
        self._lease_token = lease

    def _ensure(self) -> None:
        parent = os.path.dirname(self.directory)
        os.makedirs(os.path.dirname(parent), mode=0o700, exist_ok=True)
        for path in (parent, self.directory):
            with contextlib.suppress(FileExistsError):
                os.mkdir(path, 0o700)

    @contextlib.contextmanager
    def _locked(self, *, create: bool = False) -> Iterator[dict]:
        try:
            if not create and not os.path.lexists(self.directory):
                yield self._read()
                return
            self._ensure()
            lock = os.path.join(self.directory, "roster.lock")
            fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise RosterError(f"{lock}: not a regular file")
                deadline = time.monotonic() + LOCK_TIMEOUT
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise RosterError(f"{lock}: busy") from None
                        time.sleep(POLL)
                data = self._read()
                with file_lock(self._events.path):
                    self._events.recover(data["gen"])
                    before = json.dumps(data, sort_keys=True)
                    try:
                        yield data
                        maintain = self._maintenance_allowed(data)
                        if maintain:
                            self._settle_receipts(data)
                        if json.dumps(data, sort_keys=True) != before:
                            save(
                                self.path,
                                json.dumps(data, indent=2, sort_keys=True) + "\n",
                                durable=True,
                            )
                    except BaseException:
                        if os.path.lexists(self._events.journal):
                            self._events.recover(self._read()["gen"])
                        raise
                    else:
                        self._events.recover(data["gen"])
                        if maintain:
                            self._cleanup_receipts(data)
            finally:
                os.close(fd)
        except OSError as error:
            raise RosterError(str(error)) from error

    def _read(self) -> dict:
        raw = read_regular(self.path, limit=ROSTER_MAX + 1)
        if raw is None:
            if os.path.lexists(self.path):
                raise RosterError(f"{self.path}: not a readable regular file")
            return {
                "version": VERSION,
                "holder": None,
                "lease_gen": 0,
                "cursor": 0,
                "gen": 0,
                "entries": {},
            }
        if len(raw) > ROSTER_MAX:
            raise RosterError(f"{self.path}: over {ROSTER_MAX} bytes")
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {
                "version",
                "holder",
                "lease_gen",
                "cursor",
                "gen",
                "entries",
            }:
                raise ValueError("want roster fields")
            if type(data["version"]) is not int or data["version"] != VERSION:
                raise ValueError(f"version: want {VERSION}")
            for field in ("cursor", "gen", "lease_gen"):
                if type(data[field]) is not int or data[field] < 0:
                    raise ValueError(f"{field}: want a nonnegative integer")
            holder = data["holder"]
            if holder is not None:
                if not isinstance(holder, dict) or set(holder) != {"id", "session"}:
                    raise ValueError("holder: want id and display session")
                if not _uuid4(holder["id"]) or data["lease_gen"] <= 0:
                    raise ValueError("holder: want uuid4 and positive generation")
                if holder["session"] is not None:
                    _name(holder["session"])
            if not isinstance(data["entries"], dict):
                raise ValueError("entries: want an object")
        except (ValueError, TypeError, RecursionError) as error:
            raise RosterError(f"{self.path}: {error}") from error
        entries = {}
        for name, value in data["entries"].items():
            try:
                _name(name)
                if not isinstance(value, dict):
                    raise ValueError("entry: want an object")
                _entry(value)
            except (ValueError, TypeError, RecursionError, RosterError) as error:
                print(f"manager: roster.json: entry {name!r}: {error}; skipped", file=sys.stderr)
            else:
                entries[name] = value
        return {**data, "entries": entries}

    def _open_holder(self) -> int:
        path = os.path.join(self.directory, "holder.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RosterError(f"{path}: not a regular holder lock")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _replace_holder(self) -> int:
        fd, path = tempfile.mkstemp(prefix=".holder-", dir=self.directory)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.replace(path, os.path.join(self.directory, "holder.lock"))
        except BaseException:
            os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
            raise
        return fd

    def _active_holder(self) -> bool:
        if not os.path.lexists(self.directory):
            return False
        fd = self._open_holder()
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)

    def _lease(self, data: dict) -> None:
        holder, lease = data["holder"], self._lease_token
        if lease is None:
            if self._active_holder():
                raise Held(f"{self.directory}: held by {_label(holder)}")
            return
        if holder is None or holder["id"] != lease.id or data["lease_gen"] != lease.generation:
            raise StaleHolder(
                f"{self.directory}: stale holder generation; start a new manager holder"
            )
        if not self._active_holder():
            raise StaleHolder(f"{self.directory}: holder no longer alive")

    def _maintenance_allowed(self, data: dict) -> bool:
        if not any(entry["receipt"] is not None for entry in data["entries"].values()):
            with os.scandir(self.directory) as files:
                if not any(RECEIPT_FILE.fullmatch(item.name) for item in files):
                    return False
        try:
            self._lease(data)
        except (Held, Stale):
            return False
        return True

    @contextlib.contextmanager
    def hold(self, session: str | None, *, force: bool = False) -> Iterator[Lease]:
        if session is not None:
            _name(session)
        previous = self._lease_token
        fd = None
        try:
            with self._locked(create=True) as data:
                fd = self._open_holder()
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    if not force:
                        raise Held(f"{self.directory}: held by {_label(data['holder'])}") from None
                    os.close(fd)
                    fd = None
                    fd = self._replace_holder()
                lease = Lease(str(uuid4()), data["lease_gen"] + 1)
                data["lease_gen"] = lease.generation
                data["holder"] = {"id": lease.id, "session": session}
            self.bind(lease)
            try:
                yield lease
            finally:
                with self._locked() as data:
                    holder = data["holder"]
                    if (
                        holder is not None
                        and holder["id"] == lease.id
                        and data["lease_gen"] == lease.generation
                    ):
                        data["holder"] = None
        finally:
            if fd is not None:
                os.close(fd)
            self.bind(previous)

    def lease_state(self) -> tuple[int, int, str | None]:
        """Handled cursor, generation and the active holder's label, read without a lease."""
        with self._locked() as data:
            holder = _label(data["holder"]) if self._active_holder() else None
            return data["cursor"], data["gen"], holder

    def check_lease(self) -> None:
        with self._locked() as data:
            self._lease(data)

    def release(self) -> None:
        with self._locked() as data:
            self._lease(data)
            fd = self._replace_holder()
            try:
                data["holder"] = None
            finally:
                os.close(fd)

    def entries(self) -> dict[str, Entry]:
        with self._locked() as data:
            return {name: _entry(value) for name, value in data["entries"].items()}

    def _new_receipt(self) -> str:
        fd, path = tempfile.mkstemp(prefix="receipt-", suffix=".stdout", dir=self.directory)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return os.path.basename(path)

    def _cleanup_receipts(self, data: dict) -> None:
        referenced = {entry["receipt"] for entry in data["entries"].values()}
        with os.scandir(self.directory) as items:
            for item in items:
                if RECEIPT_FILE.fullmatch(item.name) is None or item.name in referenced:
                    continue
                try:
                    fd = os.open(item.path, os.O_RDONLY | os.O_NONBLOCK)
                    try:
                        info = os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode):
                            raise RosterError(f"{item.path}: not a regular receipt")
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            continue
                        current = item.stat()
                        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                            raise RosterError(f"{item.path}: receipt replaced during cleanup")
                        os.unlink(item.path)
                    finally:
                        os.close(fd)
                except FileNotFoundError:
                    continue
                except (OSError, RosterError) as error:
                    print(f"manager: receipt cleanup: {error}; left in place", file=sys.stderr)

    def begin(self, intent: Spec, *, sid: str) -> Entry:
        _name(intent.name)
        _note(intent.note)
        if not _uuid4(sid):
            raise RosterError("sid: want a canonical uuid4")
        if (
            not intent.argv
            or not all(isinstance(word, str) for word in intent.argv)
            or not intent.argv[0]
        ):
            raise RosterError("argv: want a nonempty command")
        with self._locked(create=True) as data:
            self._lease(data)
            if intent.name in data["entries"]:
                raise RosterError(f"{intent.name}: already in roster")
            if any(value["sid"] == sid for value in data["entries"].values()):
                raise RosterError(f"sid {sid}: already owned")
            entry = Entry(
                None,
                State.STARTING,
                intent.note or None,
                _now(),
                intent.argv,
                sid,
                self._new_receipt(),
            )
            data["entries"][intent.name] = asdict(entry)
        return entry

    @contextlib.contextmanager
    def open_receipt(self, name: str) -> Iterator[BinaryIO]:
        with self._locked() as data:
            self._lease(data)
            entry = self._get(data, name)
            if entry.receipt is None:
                raise RosterError(f"{name}: no launch receipt")
            path = os.path.join(self.directory, entry.receipt)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise RosterError(f"{path}: not a regular receipt")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                os.close(fd)
                raise
        with os.fdopen(fd, "wb") as stream:
            yield stream

    def _receipt_output(self, entry: Entry) -> bytes:
        if entry.receipt is None:
            raise RosterError("no launch receipt")
        path = os.path.join(self.directory, entry.receipt)
        raw = read_regular(path, limit=ROSTER_MAX, tail=True)
        if raw is None:
            raise RosterError(f"{path}: not a readable receipt")
        return raw

    def receipt_output(self, name: str) -> bytes:
        with self._locked() as data:
            self._lease(data)
            return self._receipt_output(self._get(data, name))

    def _receipt_record(self, entry: Entry) -> Record:
        raw = self._receipt_output(entry)
        lines = [line for line in raw.splitlines() if line.decode(errors="replace").strip()]
        if not lines:
            raise RosterError("no Record received")
        try:
            return Record.parse(lines[-1].decode("utf-8"))
        except ValueError as error:
            raise RosterError(f"invalid final Record: {error}") from error

    def receipt_record(self, name: str) -> Record:
        with self._locked() as data:
            self._lease(data)
            return self._receipt_record(self._get(data, name))

    def _settle_receipts(self, data: dict) -> None:
        for name, value in data["entries"].items():
            entry = _entry(value)
            if entry.receipt is None:
                continue
            path = os.path.join(self.directory, entry.receipt)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except FileNotFoundError:
                data["entries"][name] = asdict(replace(entry, receipt=None))
                print(
                    f"manager: {name}: missing receipt {path}; reference cleared", file=sys.stderr
                )
                continue
            except OSError as error:
                print(f"manager: receipt: {error}; left in place", file=sys.stderr)
                continue
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    print(
                        f"manager: receipt {path}: not a regular file; left in place",
                        file=sys.stderr,
                    )
                    continue
                if entry.state != State.LIVE:
                    continue
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                data["entries"][name] = asdict(replace(entry, receipt=None))
            finally:
                os.close(fd)

    def _control_available(self, data: dict, record: Record, name: str | None) -> None:
        for owner, value in data["entries"].items():
            if owner == name:
                continue
            owned = _entry(value).record
            if owned is not None and (owned.ctl, owned.name) == (record.ctl, record.name):
                raise RosterError(
                    f"control {record.ctl!r} name {record.name!r}: already owned by {owner}"
                )

    def _commit(self, data: dict, name: str, record: Record) -> None:
        entry = self._get(data, name)
        if record.sid is None or record.sid != entry.sid:
            raise RosterError(f"{name}: Record.sid does not match launch sid {entry.sid}")
        if any(
            other != name and value["sid"] == record.sid for other, value in data["entries"].items()
        ):
            raise RosterError(f"sid {record.sid}: already owned")
        self._control_available(data, record, name)
        if entry.state == State.LIVE and entry.record == record:
            data["entries"][name] = asdict(replace(entry, receipt=None))
            return
        if entry.state not in {State.STARTING, State.RESUMING}:
            raise RosterError(f"{name}: cannot commit {entry.state}")
        data["entries"][name] = asdict(
            replace(entry, record=record, state=State.LIVE, argv=(), receipt=None)
        )

    def commit(self, name: str, record: Record) -> None:
        with self._locked() as data:
            self._lease(data)
            self._commit(data, name, record)

    def _get(self, data: dict, name: str) -> Entry:
        if name not in data["entries"]:
            raise RosterError(f"{name}: not in roster")
        return _entry(data["entries"][name])

    def _mark(self, data: dict, name: str, state: State) -> Entry:
        entry = self._get(data, name)
        allowed = {
            State.STARTING: {State.LOST},
            State.LIVE: {State.DONE, State.WAITING, State.GONE},
            State.GONE: {State.LIVE, State.DONE, State.WAITING, State.RESUMING},
            State.RESUMING: {State.GONE, State.WAITING},
            State.DONE: set(),
            State.WAITING: {State.RESUMING},
            State.LOST: set(),
        }
        if state != entry.state and state not in allowed[entry.state]:
            raise RosterError(f"{name}: cannot change {entry.state} to {state}")
        started = _now() if state == State.RESUMING and entry.state != state else entry.started
        receipt = (
            self._new_receipt()
            if state == State.RESUMING and entry.state != state
            else entry.receipt
        )
        updated = replace(entry, state=state, started=started, receipt=receipt)
        data["entries"][name] = asdict(updated)
        return updated

    def mark(self, name: str, state: State) -> None:
        with self._locked() as data:
            self._lease(data)
            try:
                target = State(state)
            except ValueError as error:
                raise RosterError(f"{name}: unknown state {state!r}") from error
            self._mark(data, name, target)

    def mark_if(self, name: str, expected: Entry, state: State) -> bool:
        with self._locked() as data:
            self._lease(data)
            value = data["entries"].get(name)
            if value is None or _entry(value) != expected:
                return False
            self._mark(data, name, state)
            return True

    def claim_resume(self, name: str, *, state: State = State.GONE) -> Entry | None:
        with self._locked() as data:
            self._lease(data)
            entry = self._get(data, name)
            if entry.state in {State.RESUMING, State.LIVE}:
                return None
            if entry.state != state:
                raise RosterError(f"{name}: cannot resume {entry.state}")
            return self._mark(data, name, State.RESUMING)

    def remove(self, name: str) -> None:
        with self._locked() as data:
            self._lease(data)
            self._get(data, name)
            del data["entries"][name]

    def remove_if(self, name: str, expected: Entry) -> bool:
        with self._locked() as data:
            self._lease(data)
            value = data["entries"].get(name)
            if value is None or _entry(value) != expected:
                return False
            del data["entries"][name]
            return True

    def close_failed(self, name: str, *, sid: str | None, ctl: tuple[str, ...], reason: str) -> str:
        """One close-failures.jsonl line for a failed close of entry `name`; its path."""
        path = os.path.join(self.directory, CLOSE_FAILURES)
        line = {
            "at": int(time.time()),
            "name": name,
            "sid": sid,
            "ctl": list(ctl),
            "reason": printable(one_line(reason))[:REASON_MAX],
        }
        with self._locked() as data:
            self._lease(data)
            append_line(path, json.dumps(line, ensure_ascii=False) + "\n")
        return path

    def note(self, name: str, text: str | None) -> None:
        _note(text)
        with self._locked() as data:
            self._lease(data)
            data["entries"][name] = asdict(replace(self._get(data, name), note=text or None))

    def adopt(self, record: Record, *, resuming_before: str | None = None) -> str | None:
        """The entry this roster launched with record.sid takes record; None when none did, so a
        Record anyone else published is never adopted.
        """
        _name(record.name)
        if not _uuid4(record.sid):
            raise RosterError("sid: want a canonical uuid4")
        with self._locked() as data:
            self._lease(data)
            matches = [
                name for name, value in data["entries"].items() if value["sid"] == record.sid
            ]
            if len(matches) > 1:
                raise RosterError(f"sid {record.sid}: more than one roster entry matches")
            if not matches:
                return None
            name = matches[0]
            self._control_available(data, record, name)
            current = self._get(data, name)
            if (
                current.state == State.RESUMING
                and resuming_before is not None
                and datetime.datetime.fromisoformat(current.started)
                > datetime.datetime.fromisoformat(resuming_before)
            ):
                return name
            if current.state in {State.LOST, State.RESUMING}:
                data["entries"][name] = asdict(
                    replace(current, record=record, state=State.LIVE, argv=())
                )
            elif current.state != State.STARTING and current.record != record:
                raise RosterError(f"sid {record.sid}: already owned by {name}")
            return name

    def backlog(self, *, require_lease: bool = False) -> tuple[int, int, list[str]]:
        with self._locked() as data:
            if require_lease:
                self._lease(data)
            return data["cursor"], data["gen"], self._events.following(data["cursor"])

    def _cursor(self, data: dict, after: int | None, gen: int) -> int:
        if gen != data["gen"]:
            raise Stale(f"stale line numbers (gen {gen}, now {data['gen']}): run crew attach")
        count = self._events.count()
        value = count if after is None else after
        if type(value) is not int or value < 0 or value > count:
            raise Stale(f"line {value} is outside events ({count} lines): run crew attach")
        return value

    def advance(self, after: int | None, gen: int) -> int:
        return self.watch_events(after, gen)[0]

    def watch_events(
        self, after: int | None, gen: int, *, acknowledge: bool = True
    ) -> tuple[int, tuple[int, int] | None]:
        with self._locked() as data:
            self._lease(data)
            value = self._cursor(data, after, gen)
            if acknowledge:
                data["cursor"] = max(data["cursor"], value)
            return value, self._events.identity()

    def event_identity(self) -> tuple[int, int] | None:
        with self._locked():
            return self._events.identity()

    def next_event(
        self, after: int | None, gen: int, *, identity: tuple[int, int] | None = None
    ) -> tuple[int, str | None]:
        with self._locked() as data:
            self._lease(data)
            if identity is not None and identity != self._events.identity():
                raise Stale("events file replaced: run crew attach")
            value = self._cursor(data, after, gen)
            if self._events.due(data["cursor"]):
                raise Stale("events rotation due: run crew attach")
            lines = self._events.following(value)
            if identity is not None and identity != self._events.identity():
                raise Stale("events file replaced: run crew attach")
            return (value + 1, lines[0]) if lines else (value, None)

    def rotate_events(self) -> bool:
        with self._locked() as data:
            self._lease(data)
            if not self._events.rotate(data["cursor"], data["gen"]):
                return False
            data["cursor"], data["gen"] = 0, data["gen"] + 1
            return True
