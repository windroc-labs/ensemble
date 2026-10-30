"""Per-client admission: durable TUI ownership and process-held transient leases."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager

from ensemble.session.engine.types import (
    CapacityRow,
    ConfigError,
    Liveness,
    Mode,
    SessionError,
)
from ensemble.session.hosts.base import Host
from ensemble.utils import STATE, inode_lock, sync_directory

ROOT = f"{STATE}/client-runs"
POLL = 0.1
MAX_RECORD = 16384


Known = dict[str, list[CapacityRow]]
Holders = dict[str | tuple[str, str], str]  # a run's generation (else host and row name): label


def _inventory(
    hosts: Mapping[str, Host], client: str, *, required: str | None = None
) -> tuple[Known, Holders]:
    known: Known = {}
    holders: Holders = {}
    for key, host in hosts.items():
        try:
            known[key] = host._capacity_rows()
        except (OSError, SessionError):
            if required is None or key == required:
                raise
            continue
        for row in known[key]:
            if row.client == client and row.live:
                holders.setdefault(row.generation or (key, row.name), row.name)
    return known, holders


def _read(fd: int) -> dict:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise ValueError("not a regular reservation")
    value = json.loads(os.read(fd, MAX_RECORD + 1))
    if (
        not isinstance(value, dict)
        or value.get("kind") not in tuple(Mode)
        or not isinstance(value.get("name"), str)
        or not value["name"]
        or value["kind"] == Mode.TUI
        and not isinstance(value.get("host"), str)
    ):
        raise ValueError("invalid reservation")
    return value


def _retained(record: dict, generation: str, known: Known) -> bool:
    """A tui record stays while its Host is unknown or lists its generation live."""
    rows = known.get(record["host"])
    return rows is None or any(row.live and row.generation == generation for row in rows)


def _holders(directory: str, known: Known, holders: Holders) -> list[str]:
    for entry in os.scandir(directory):
        if not entry.name.endswith(".run"):
            continue
        try:
            fd = os.open(entry.path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue
        try:
            record = _read(fd)
            generation = entry.name.removesuffix(".run")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = False
            except BlockingIOError:
                held = True
            if held or record["kind"] == Mode.TUI and _retained(record, generation, known):
                holders[generation] = record["name"]
                continue
            with contextlib.suppress(FileNotFoundError):
                os.unlink(entry.path)
        finally:
            os.close(fd)
    return sorted(holders.values())


@contextlib.contextmanager
def _locked(directory: str, client: str) -> Iterator[None]:
    parent = os.path.dirname(os.path.dirname(directory))
    gate = os.path.join(parent, f"client-runs-{client}.lock")
    existed = os.path.isdir(parent)
    try:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        with inode_lock(gate):
            try:
                os.makedirs(directory, mode=0o700, exist_ok=True)
                yield
            finally:
                for path in (directory, os.path.dirname(directory)):
                    with contextlib.suppress(OSError):
                        os.rmdir(path)
                os.unlink(gate)
    finally:
        if not existed:
            with contextlib.suppress(OSError):
                os.rmdir(parent)


class Admission(AbstractContextManager):
    def __init__(
        self,
        client: str,
        limit: int,
        *,
        label: str,
        mode: Mode,
        hosts: Mapping[str, Host],
        host: str | None = None,
    ):
        self.client, self.limit, self.hosts = client, limit, hosts
        self.generation = uuid.uuid4().hex  # the run's identity: its reservation's and its worker's
        self.directory = os.path.join(os.path.expanduser(ROOT), client)
        self.path = os.path.join(self.directory, f"{self.generation}.run")
        self.record = {"kind": mode, "name": label}
        if mode == Mode.TUI:
            self.record["host"] = host or next(iter(hosts))
        self._fd: int | None = None
        self._committed = False
        self._mutex = threading.Lock()
        self.finished = threading.Event()

    def __enter__(self) -> Admission:
        self._context = self._admit()
        try:
            return self._context.__enter__()
        except BaseException:
            self._tidy()
            raise

    def __exit__(self, *args):
        try:
            return self._context.__exit__(*args)
        finally:
            self._tidy()

    def _tidy(self) -> None:
        """The gate's exit removes a client directory this start left empty."""
        with contextlib.suppress(OSError), _locked(self.directory, self.client):
            pass

    @contextlib.contextmanager
    def _admit(self) -> Iterator[Admission]:
        try:
            with _locked(self.directory, self.client):
                if self.limit:
                    known, holders = _inventory(self.hosts, self.client)
                    active = _holders(self.directory, known, holders)
                    if len(active) >= self.limit:
                        raise SessionError(
                            f"client {self.client} reached max_runs limit {self.limit}; "
                            f"held by: {', '.join(active)}"
                        )
                self._fd = os.open(
                    self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                fcntl.flock(self._fd, fcntl.LOCK_EX)
                with os.fdopen(os.dup(self._fd), "w", encoding="utf-8") as stream:
                    json.dump(self.record, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                sync_directory(self.directory)
        except (OSError, ValueError) as error:
            self.close()
            raise SessionError(f"client {self.client}: capacity: {error}") from error
        except BaseException:
            self.close()
            raise
        try:
            yield self
        except ConfigError:
            if not self._committed:
                self.close()
            raise
        finally:
            if not self._committed and not self.finished.is_set():
                self._rollback()

    @classmethod
    def reconcile(
        cls, client: str, hosts: Mapping[str, Host], *, required: str | None = None
    ) -> None:
        directory = os.path.join(os.path.expanduser(ROOT), client)
        if not os.path.isdir(directory):
            return
        try:
            with _locked(directory, client):
                known, holders = _inventory(hosts, client, required=required)
                _holders(directory, known, holders)
        except (OSError, ValueError) as error:
            raise SessionError(f"client {client}: capacity: {error}") from error

    def started(self, state: Callable[[], Liveness]) -> None:
        self._committed = True
        if self.record["kind"] == Mode.TUI:
            self._detach()
        else:
            threading.Thread(target=self._watch, args=(state,), daemon=True).start()

    def _rollback(self) -> None:
        if self.record["kind"] == Mode.TUI:
            try:
                host = self.record["host"]
                known, _ = _inventory({host: self.hosts[host]}, self.client)
                retained = _retained(self.record, self.generation, known)
            except (OSError, SessionError):
                retained = True
            if retained:
                self._detach()
                return
        self.close()

    def _detach(self) -> None:
        """Under the gate: a count between its inventory and its scan would reap the record."""
        try:
            with _locked(self.directory, self.client):
                self._unhold()
        except OSError:
            self._unhold()

    def _unhold(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def _watch(self, state: Callable[[], Liveness]) -> None:
        while not self.finished.wait(POLL):
            self.release_if_absent(state)

    def release_if_absent(self, state: Callable[[], Liveness]) -> None:
        try:
            if not self.finished.is_set() and state() == Liveness.ABSENT:
                self.close()
        except (OSError, SessionError):
            pass  # Unknown liveness retains capacity.

    def close(self) -> None:
        with self._mutex:
            if self.finished.is_set():
                return
            # No gate: unlinked before unheld, the record is never half released, and a
            # watcher ending after its home is gone recreates none of it.
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.path)
            self._unhold()
            self.finished.set()
