"""Worker, a plain tui session started by name (FR-20): WorkerState in
~/.ensemble/workers/<sid>/worker.json (by-name/<name>: its latest sid); its Record on its Host once
its early watch passes. restart and resume close the old session and wait for its group (FR-36).
Callers hold locked() around start, resume and restart.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from ensemble.session.engine.base import Session
from ensemble.session.engine.types import (
    COMMAND,
    RESUME,
    SESSION_NAME,
    SESSION_NAME_RULE,
    Access,
    Blocked,
    ConfigError,
    EnvironmentSpec,
    Exited,
    Mode,
    SessionError,
    Spec,
)
from ensemble.session.hosts.base import Process
from ensemble.utils import (
    CONFIG_NAME,
    STALE_AGE,
    STATE,
    Display,
    Record,
    file_lock,
    printable_abs,
    read_regular,
    save,
)

SID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
WORKERS = f"{STATE}/workers"
BY_NAME = "by-name"
STATE_FILE = "worker.json"
STATE_MAX = 64 << 10
VERSION = 1
EARLY = 5
TICK = 1
COLOR_CAP = 15
WAIT = 10
REPORT_LINES = 20
SCREEN_LINES = 2000
KIND = "worker"


@runtime_checkable
class _Opens(Protocol):
    @property
    def opening_pending(self) -> str | None: ...


def _say(text: str) -> None:
    print(text, file=sys.stderr)


def _text(value) -> bool:
    return isinstance(value, str) and value != "" and value.isprintable()


@dataclass(frozen=True)
class WorkerState:
    """What a worker's resume replays; token: its last launch's Process.token; environment: its
    start's, absent from a pre-upgrade worker.json.
    """

    name: str
    sid: str
    client: str
    cwd: str
    host: str
    flags: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    events: str | None = None
    display: Display = None
    token: str | None = None
    environment: EnvironmentSpec | None = None

    def __post_init__(self):
        checks = {
            "name": isinstance(self.name, str) and SESSION_NAME.fullmatch(self.name) is not None,
            "sid": isinstance(self.sid, str) and SID.fullmatch(self.sid) is not None,
            "client": isinstance(self.client, str) and CONFIG_NAME.fullmatch(self.client),
            "cwd": printable_abs(self.cwd),
            "host": isinstance(self.host, str) and CONFIG_NAME.fullmatch(self.host),
            "flags": isinstance(self.flags, tuple)
            and all(isinstance(flag, str) and "\0" not in flag for flag in self.flags),
            "env": isinstance(self.env, Mapping)
            and all(
                isinstance(key, str)
                and key
                and "=" not in key
                and "\0" not in key
                and isinstance(value, str)
                and value.isprintable()
                for key, value in self.env.items()
            ),
            "events": self.events is None or printable_abs(self.events),
            "display": self.display is None or _text(self.display),
            "token": self.token is None or _text(self.token),
            "environment": self.environment is None
            or isinstance(self.environment, EnvironmentSpec),
        }
        if bad := [name for name, ok in checks.items() if not ok]:
            raise ConfigError(f"worker {bad[0]}: {getattr(self, bad[0])!r:.80} is not valid")
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))

    @classmethod
    def from_json(cls, data) -> WorkerState:
        names = {"version", *(name for name in cls.__dataclass_fields__)}
        if (
            not isinstance(data, dict)
            or not names - {"environment"} <= set(data) <= names
            or data["version"] != VERSION
        ):
            raise ConfigError(
                f"want version {VERSION} with exactly {', '.join(sorted(names))}"
                " (environment optional)"
            )
        if not isinstance(data["flags"], list) or not isinstance(data["env"], dict):
            raise ConfigError("flags: want a list; env: want an object")
        fields = {k: v for k, v in data.items() if k != "version"}
        environment = fields.get("environment")
        if environment is not None:
            environment = EnvironmentSpec.from_fields(environment)
        return cls(**{**fields, "flags": tuple(data["flags"]), "environment": environment})

    def to_json(self) -> str:
        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        data.update(
            version=VERSION,
            flags=list(self.flags),
            env=dict(self.env),
            environment=None if self.environment is None else self.environment.persisted(),
        )
        return json.dumps(data, indent=2, sort_keys=True) + "\n"

    def workdir(self) -> str:
        return os.path.join(os.path.abspath(os.path.expanduser(WORKERS)), self.sid)

    def record(self) -> Record:
        return Record(
            v=1,
            kind=KIND,
            name=self.name,
            sessions=(self.name,),
            sid=self.sid,
            workdir=self.workdir(),
            ctl=(COMMAND,),
            resume=(*RESUME, self.name),
            display=self.display,
            detail=self.client,
        )

    def spec(self, *, prompt: str, resume: bool) -> Spec:
        return Spec(
            workdir=self.workdir(),
            access=Access(cwd=self.cwd, project=True),
            sid=self.sid,
            name=self.name,
            client=self.client,
            mode=Mode.TUI,
            prompt=prompt,
            resume=resume,
            events=self.events,
            flags=self.flags,
            env=self.env,
            environment=self.environment,
        )


def _pending(session: Session) -> str | None:
    return session.opening_pending if isinstance(session, _Opens) else None


def _quoted(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def _detail(error: SessionError | OSError) -> object:
    return error.strerror if isinstance(error, OSError) else error


def _private_dir(path: str) -> None:
    os.makedirs(path, 0o700, exist_ok=True)


def _read(path: str, what: str) -> bytes:
    data = read_regular(path, limit=STATE_MAX + 1)
    if data is None or len(data) > STATE_MAX:
        raise SessionError(f"{what}: {path}: missing, not a regular file, or too large")
    return data


class Worker:
    """One worker's state and its runs on session_cls (configured by the caller)."""

    def __init__(
        self,
        state: WorkerState,
        *,
        session_cls: type[Session] = Session,
        say: Callable[[str], None] = _say,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state, self.session_cls, self.say, self.clock = state, session_cls, say, clock

    @classmethod
    def new(cls, **fields) -> Worker:
        return cls(WorkerState(**fields))

    @classmethod
    def load(cls, name: str, *, sid: str | None = None, **kw) -> Worker:
        """Worker `name`'s state: sid's (given, e.g. by ENSEMBLE_SID), else its latest start's."""
        if not isinstance(name, str) or not SESSION_NAME.fullmatch(name):
            raise ConfigError(f"name must match {SESSION_NAME_RULE}, got {name!r:.80}")
        root = os.path.abspath(os.path.expanduser(WORKERS))
        given = sid is not None
        if given and (not isinstance(sid, str) or not SID.fullmatch(sid)):
            raise ConfigError(f"sid {sid!r:.80}: want a session id")
        latest = read_regular(os.path.join(root, BY_NAME, name), limit=STATE_MAX)
        latest = None if latest is None else latest.decode(errors="replace").strip()
        if sid is None:
            if latest is None:
                raise SessionError(f"{name}: not a worker")
            if not SID.fullmatch(latest):
                raise SessionError(f"{name}: not a worker: {BY_NAME}/{name} holds no session id")
            sid = latest
        if given and latest not in (None, sid):
            raise ConfigError(f"sid {sid} is not worker {name}'s (its latest: {latest})")
        data = _read(os.path.join(root, sid, STATE_FILE), f"{name}: no worker {sid}")
        try:
            state = WorkerState.from_json(json.loads(data))
        except (ValueError, RecursionError, TypeError, ConfigError) as error:
            raise SessionError(f"{name}: worker {sid}: bad {STATE_FILE}: {error}") from None
        if (state.name, state.sid) != (name, sid):
            mismatch = f"sid {sid} is worker {state.name}'s, not {name}'s"
            raise ConfigError(mismatch) if given else SessionError(f"{name}: {mismatch}")
        return cls(state, **kw)

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        workdir = self.state.workdir()
        try:
            _private_dir(os.path.dirname(workdir))
            _private_dir(workdir)
            with file_lock(os.path.join(workdir, "worker")):
                yield
        except OSError as error:
            raise SessionError(f"{self.state.name}: {workdir}: {error.strerror or error}") from None

    def check(self, *, prompt: str, resume: bool) -> None:
        """No effects."""
        self.session_cls.preflight(
            self.state.spec(prompt=prompt, resume=resume),
            display=self.state.display,
            host=self.state.host,
        )

    def start(self, *, prompt: str, resume: bool, hidden: bool | None = None) -> Record:
        """hidden: True hides, False shows, None keeps its Host's. Watches EARLY s (up to COLOR_CAP s
        while its opening is pending), then publishes and returns its Record. A failed save or start
        puts its files back; its client exiting in the watch: SessionError with its last lines, its
        terminal kept, no Record.
        """
        name = self.state.name
        self._drop_stale()
        self._claim()
        before = self._saved()
        self.state = replace(self.state, token=None)
        try:
            self._save()
        except (SessionError, OSError) as error:
            self._put_back(before)
            raise SessionError(f"{name}: {_detail(error)}; nothing started") from None
        try:
            session = self.session_cls.start(
                self.state.spec(prompt=prompt, resume=resume),
                display=self.state.display,
                host=self.state.host,
                idle_timeout=TICK,
            )
        except (SessionError, ConfigError):
            self._put_back(before)
            raise
        host = self.session_cls.host(self.state.host)
        try:
            handle = host.attach(name)
        except SessionError:  # it exited already: the watch reports it
            handle = None
        self.state = replace(self.state, token=None if handle is None else handle.token)
        try:
            self._save()
        except (SessionError, OSError) as error:
            with contextlib.suppress(SessionError, ConfigError):
                session.close()
            self._put_back(before)
            raise SessionError(f"{name}: {_detail(error)}; start undone") from None
        if session.showable:
            self._show(session, hidden=hidden)
        self._watch(session)
        record = self.state.record()
        (host.attach(name) if handle is None else handle).publish(record)
        return record

    def resume(self) -> Record:
        """Its Record, starting nothing, while its session lives; else a resumed start."""
        session = self._session()
        if session is not None and session.alive():
            record = session.record()
            if record is None or record.sid != self.state.sid:
                record = self.state.record()
                session.publish(record)
            return record
        self.check(prompt="", resume=True)
        self.replace(session or self._group())
        return self._start_again()

    def restart(self) -> Record:
        session = self._session()
        if session is None:
            raise SessionError(f"{self.state.name}: no session to restart; resume starts it")
        self.replace(session)
        return self._start_again()

    def _start_again(self) -> Record:
        try:
            return self.start(prompt="", resume=True)
        except ConfigError as error:
            raise SessionError(
                f"{self.state.name}: old session closed, none started: {error}"
            ) from None

    def replace(self, handle: Process | None) -> None:
        """Close its session by name, close `handle`, wait WAIT s, else kill and wait again."""
        name = self.state.name
        self.session_cls.retire(name, host=self.state.host)
        if handle is None:
            return
        handle.close()
        if handle.wait_exit(WAIT):
            return
        handle.kill()
        if not handle.wait_exit(WAIT):
            raise SessionError(f"{name}: its old client did not exit; nothing started")

    def _session(self) -> Process | None:
        """Its Host's session of its name, refused if another's; None when absent."""
        host = self.session_cls.host(self.state.host)
        name = self.state.name
        if name not in host.list():
            return None
        handle = host.attach(name)
        sid, _ = handle.session_identity()
        if sid != self.state.sid:
            raise SessionError(f"session {name} is not worker {self.state.sid} (its sid: {sid})")
        return handle

    def _group(self) -> Process | None:
        """Its last launch's process group, while it outlives its session."""
        if self.state.token is None:
            return None
        return self.session_cls.host(self.state.host).reclaim(self.state.token)

    def _drop_stale(self) -> None:
        """Removes each workers/<sid>/ no by-name/ entry names, whose worker.json is over STALE_AGE
        old and whose lock nobody holds; an error is said, never raised.
        """
        root = os.path.dirname(self.state.workdir())
        pointers = os.path.join(root, BY_NAME)
        try:
            names = os.listdir(pointers)
        except FileNotFoundError:
            names = []
        except OSError as error:
            self.say(f"worker cleanup skipped: {error}")
            return
        latest = set()
        for name in names:
            data = read_regular(os.path.join(pointers, name), limit=STATE_MAX)
            if data is None:
                self.say(f"worker cleanup skipped: {pointers}/{name}: unreadable")
                return
            latest.add(data.decode(errors="replace").strip())
        try:
            with os.scandir(root) as entries:
                old = [
                    entry.path
                    for entry in entries
                    if SID.fullmatch(entry.name)
                    and entry.name not in latest
                    and entry.is_dir(follow_symlinks=False)
                ]
        except FileNotFoundError:
            return
        except OSError as error:
            self.say(f"worker cleanup: {root}: {error}")
            return
        cutoff = time.time() - STALE_AGE
        for path in old:
            try:
                found = os.stat(os.path.join(path, STATE_FILE), follow_symlinks=False)
                if (
                    not stat.S_ISREG(found.st_mode)
                    or found.st_mtime >= cutoff
                    or os.path.islink(os.path.join(path, "worker.lock"))
                ):
                    continue
                with file_lock(os.path.join(path, "worker"), wait=False):
                    shutil.rmtree(path)
            except (FileNotFoundError, BlockingIOError):
                continue
            except OSError as error:
                self.say(f"worker cleanup: {path}: {error}")

    def _claim(self) -> None:
        data = read_regular(os.path.join(self.state.workdir(), STATE_FILE), limit=STATE_MAX)
        try:
            saved = json.loads(data) if data else None
        except (ValueError, RecursionError):
            saved = None
        owner = saved.get("name") if isinstance(saved, dict) else None
        if owner not in (None, self.state.name):
            raise ConfigError(f"sid {self.state.sid} is worker {owner}'s")

    def _files(self) -> tuple[str, str]:
        workdir = self.state.workdir()
        latest = os.path.join(os.path.dirname(workdir), BY_NAME, self.state.name)
        return os.path.join(workdir, STATE_FILE), latest

    def _saved(self) -> tuple[bytes | None, bytes | None]:
        state, latest = self._files()
        limit = STATE_MAX + 1
        return read_regular(state, limit=limit), read_regular(latest, limit=limit)

    def _save(self) -> None:
        state, latest = self._files()
        save(state, self.state.to_json())
        _private_dir(os.path.dirname(latest))
        save(latest, self.state.sid + "\n")

    def _put_back(self, before: tuple[bytes | None, bytes | None]) -> None:
        """Its own files as `before` (None: removed); the pointer only while it names its sid."""
        state, latest = self._files()
        mine = read_regular(latest, limit=STATE_MAX) == f"{self.state.sid}\n".encode()
        for path, data in [(state, before[0]), *([(latest, before[1])] if mine else [])]:
            if read_regular(path, limit=0) is None:
                continue
            with contextlib.suppress(OSError):
                if data is None:
                    os.unlink(path)
                else:
                    save(path, data)

    def _show(self, session: Session, *, hidden: bool | None) -> None:
        name = self.state.name
        try:
            self.say(f"session {name}: {session.attach_command()}")
            if hidden:
                session.hide()
            elif hidden is False or not session.status().hidden:
                session.show(display=self.state.display)
        except (SessionError, ConfigError) as error:
            self.say(f"session {name}: not {'hidden' if hidden else 'shown'}: {error}")

    def _watch(self, session: Session) -> None:
        began = self.clock()
        for event in session.events():
            if isinstance(event, Exited):
                raise SessionError(self._died(event.returncode))
            if isinstance(event, Blocked):
                self.say(f"session {self.state.name}: waits on a dialog: {event.reason}")
                self._uncolored(session, "waits on a dialog")
                return
            elapsed = self.clock() - began
            if elapsed >= COLOR_CAP:
                self._uncolored(session, f"not ready within {COLOR_CAP} s")
                return
            if elapsed >= EARLY and _pending(session) is None:
                return

    def _uncolored(self, session: Session, reason: str) -> None:
        text = _pending(session)
        if text is not None:
            name = self.state.name
            self.say(
                f"session {name}: color not set: {reason}; {COMMAND} send {name} {_quoted(text)}"
            )

    def _died(self, code: int) -> str:
        head = f"{self.state.name}: {self.state.client} exited {code} at once"
        host = self.session_cls.host(self.state.host)
        try:
            screen = host.attach(self.state.name).screen(SCREEN_LINES)
        except SessionError:
            return head
        lines = [line for line in screen.splitlines() if line.strip()][-REPORT_LINES:]
        return "\n".join([f"{head}; its last lines:", *lines]) if lines else head
