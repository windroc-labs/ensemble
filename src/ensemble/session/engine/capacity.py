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
    ConfigError,
    Liveness,
    Mode,
    SessionError,
    StatusState,
)
from ensemble.session.hosts.base import Host
from ensemble.utils import STATE, inode_lock, sync_directory

ROOT = f"{STATE}/client-runs"
POLL = 0.1
MAX_RECORD = 16384


def _inventory(
    hosts: Mapping[str, Host], client: str, *, required: str | None = None
) -> tuple[dict, dict]:
    known, holders = {}, {}
    for key, host in hosts.items():
        try:
            rows = {row.name: row for row in host._capacity_rows()}
            try:
                backend = host._capacity_namespace() if rows else None
            except SessionError:
                if host.list():
                    raise
                rows, backend = {}, None
        except (OSError, SessionError):
            if required is None or key == required:
                raise
            continue
        namespace = ("backend", backend) if backend is not None else ("configuration", key)
        known[key] = namespace, rows
        for row in rows.values():
            if row.client == client and row.state not in (StatusState.DEAD, StatusState.GONE):
                holders[(namespace, row.name, row.sid)] = row.name
    return known, holders


def _read(fd: int) -> dict:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise ValueError("not a regular reservation")
    value = json.loads(os.read(fd, MAX_RECORD + 1))
    if (
        not isinstance(value, dict)
        or value.get("kind") not in tuple(Mode)
        or not all(isinstance(value.get(k), str) and value[k] for k in ("name", "sid"))
        or value["kind"] == Mode.TUI
        and not isinstance(value.get("host"), str)
    ):
        raise ValueError("invalid reservation")
    return value


def _retained(record: dict, known: dict) -> bool:
    if record["host"] not in known:
        return True
    row = known[record["host"]][1].get(record["name"])
    return (
        row is not None
        and row.state not in (StatusState.DEAD, StatusState.GONE)
        and (row.sid is None or row.sid == record["sid"])
    )


def _holders(directory: str, known: dict, holders: dict) -> list[str]:
    for entry in os.scandir(directory):
        if not entry.name.endswith(".run"):
            continue
        try:
            fd = os.open(entry.path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue
        try:
            record = _read(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = False
            except BlockingIOError:
                held = True
            if record["kind"] == Mode.TUI:
                if _retained(record, known):
                    host = record["host"]
                    namespace = known[host][0] if host in known else ("configuration", host)
                    holders.pop((namespace, record["name"], None), None)
                    holders[(namespace, record["name"], record["sid"])] = record["name"]
                    continue
            elif held:
                holders[(entry.name,)] = record["name"]
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
                # Waiters verify the inode again, so the lock may be removed after cleanup.
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
        name: str | None,
        sid: str,
        mode: Mode,
        hosts: Mapping[str, Host],
        host: str | None = None,
    ):
        self.client, self.limit, self.hosts = client, limit, hosts
        self.directory = os.path.join(os.path.expanduser(ROOT), client)
        self.path = os.path.join(self.directory, f"{uuid.uuid4().hex}.run")
        self.record = {"kind": mode, "name": name or sid, "sid": sid}
        if mode == Mode.TUI:
            self.record["host"] = host or next(iter(hosts))
        self._fd: int | None = None
        self._committed = False
        self._yielded = False
        self._mutex = threading.Lock()
        self.finished = threading.Event()

    def __enter__(self) -> Admission:
        self._context = self._admit()
        return self._context.__enter__()

    def __exit__(self, *args):
        return self._context.__exit__(*args)

    @contextlib.contextmanager
    def _admit(self) -> Iterator[Admission]:
        try:
            with _locked(self.directory, self.client):
                try:
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
                    self._yielded = True
                    yield self
                except ConfigError:
                    if not self._committed:
                        self.close()
                    raise
                finally:
                    if not self._committed and not self.finished.is_set():
                        self._rollback()
        except (OSError, ValueError) as error:
            if self._yielded:
                raise
            self.close()
            raise SessionError(f"client {self.client}: capacity: {error}") from error

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
        if self._yielded and self.record["kind"] == Mode.TUI:
            try:
                host = self.record["host"]
                known, _ = _inventory({host: self.hosts[host]}, self.client)
                retained = _retained(self.record, known)
            except (OSError, SessionError):
                retained = True
            if retained:
                self._detach()
                return
        self.close()

    def _detach(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

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
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.path)
            self._detach()
            self.finished.set()
