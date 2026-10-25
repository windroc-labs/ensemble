"""HostedSession, the session layer's one concrete Session: a Client run in a Mode on a
Host. events() yields its events, Stderr and IdleTimeout (none while Blocked) up to its Exited,
recording `stderr` and each ClientBound as `client_session`, never yielded.
"""

from __future__ import annotations

import builtins
import os
import re
import shutil
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, cast

from ensemble.session.clients.base import Client
from ensemble.session.clients.registry import Registry, settings
from ensemble.session.engine import accounts
from ensemble.session.engine.base import Session
from ensemble.session.engine.records import RunLog
from ensemble.session.engine.types import (
    Blocked,
    ClientBound,
    ConfigError,
    Exited,
    Hosts,
    IdleTimeout,
    KeyPress,
    Launch,
    Liveness,
    Mode,
    NoRecordedSession,
    NotShowable,
    Reply,
    Resumable,
    SessionError,
    SessionEvent,
    SessionInfo,
    SessionRow,
    SessionStatus,
    Spec,
    StatusState,
    Stderr,
    TurnEnd,
    Usage,
)
from ensemble.session.hosts.base import Host, host_resume
from ensemble.session.modes import headless, in_process, tui
from ensemble.session.modes.base import BaseMode, child_env
from ensemble.session.viewer import display as displays
from ensemble.session.viewer.viewer import Viewer
from ensemble.utils import (
    ACCOUNT_ENV,
    STATE,
    Display,
    Record,
    Status,
    load_class,
    module_roots,
    printable_abs,
    probe,
    sid_error,
    summary,
    thaw,
)


def _request(spec: Spec, idle_timeout: float | None) -> tuple[str, Mode]:
    _idle(idle_timeout)
    if not isinstance(spec, Spec):
        raise ConfigError(f"spec: want a Spec, got {type(spec).__name__}")
    client, mode = spec.client, spec.mode
    if client is None or mode is None:
        raise ConfigError(f"spec.{'client' if client is None else 'mode'}: required to start")
    return client, mode


POLL = 0.5
PROBE_DIR = f"{STATE}/probe"
PROBE_TIMEOUT = 120
UNCONFIGURED = "session not configured: call Session.configure first"
MODES: dict[Mode, type[BaseMode]] = {
    mode.kind: mode for mode in (headless.HeadlessMode, tui.TuiMode, in_process.InProcessMode)
}

HostFactory = Callable[[type[Host], str, Mapping], Host]
ViewerFactory = Callable[[Mapping], Viewer]


def _built(cls: type[Host], name: str, settings: Mapping) -> Host:
    return cls(name, settings)


@dataclass(frozen=True)
class _Setup:
    registry: Registry
    hosts: Hosts
    class_loader: Callable[..., Any]
    probe: Callable[..., str | None]
    clock: Callable[[], float]
    host_factory: HostFactory
    viewer: Mapping
    viewer_factory: ViewerFactory
    built: dict[str, Host] = field(default_factory=dict, compare=False)
    viewers: list[Viewer] = field(default_factory=list, compare=False)


def _idle(idle_timeout) -> None:
    if idle_timeout is not None and (
        isinstance(idle_timeout, bool)
        or not isinstance(idle_timeout, (int, float))
        or not idle_timeout > 0
    ):
        raise ConfigError(
            f"idle_timeout must be a positive number of seconds, got {idle_timeout!r}"
        )


def _session_line(workdir: str) -> Callable[[SessionInfo], None]:
    def record(info: SessionInfo) -> None:
        os.makedirs(workdir, exist_ok=True)
        RunLog(workdir).note("session", **info.persisted())

    return record


def _resumable(client: Callable[[], Client], info: SessionInfo, spec: Spec) -> SessionStatus:
    if error := sid_error(info.sid):
        raise ConfigError(error)
    if not printable_abs(info.cwd):
        raise ConfigError(f"cwd must be a printable absolute path, got {info.cwd!r:.80}")
    try:
        return client().session_status(info, env={**spec.access.env, **spec.env})
    except OSError:
        return SessionStatus(Resumable.UNKNOWN, None, "")


def _probe_dir() -> str | None:
    path = os.path.expanduser(PROBE_DIR)
    try:
        os.makedirs(path, 0o700, exist_ok=True)
    except OSError:
        return None
    return path


def _read_usage(
    setup: _Setup, client: Client, launch: Launch | None, *, container: bool = False
) -> Usage | None:
    if launch is None or not launch.argv:
        return None
    cwd = launch.cwd or _probe_dir()
    if cwd is None:
        return None
    env = child_env(launch, cwd)
    if executable := client._usage_executable(container=container):
        program = os.path.join(cwd, executable) if os.path.dirname(executable) else executable
        if shutil.which(program, path=env.get("PATH", os.defpath)) is None:
            raise ConfigError(
                f"client executable {executable!r} is missing or not executable; install or configure this client"
            )
    output = setup.probe(launch.argv, cwd=cwd, env=env, timeout=PROBE_TIMEOUT)
    if output is None:
        return None
    try:
        return client.usage(output)
    except ValueError:
        return None


def _pooled(setup: _Setup, name: str, client: Client, host: Host) -> dict[str, Usage | None] | None:
    """Each account's reading when `client` has two or more and `host` runs containers."""
    probes = client._usage_probes()
    if probes is None or len(probes) < 2 or host._client_context().container is None:
        return None
    return accounts.readings(name, probes, lambda launch: _read_usage(setup, client, launch))


def _picked(setup: _Setup, spec: Spec, name: str, client: Client, host: Host) -> Spec:
    """spec, selecting the pool's best account when it selects none."""
    if spec.env.get(ACCOUNT_ENV) or (pool := _pooled(setup, name, client, host)) is None:
        return spec
    if (account := accounts.best(pool)) is None:
        return spec
    print(f"{name} account {account}: picked ({summary(pool[account])})", file=sys.stderr)
    return replace(spec, env={**spec.env, ACCOUNT_ENV: account})


def _host_class(setup: _Setup, name: str) -> tuple[type[Host], dict]:
    settings = thaw(setup.hosts.tables[name])
    path = settings.pop("implementation", None)
    try:
        return setup.class_loader(path, Host), settings
    except ValueError as e:
        raise ConfigError(f"[hosts.{name}].implementation: {e}") from None


def _class_root(kind: type, table: str) -> str:
    module = sys.modules.get(kind.__module__)
    if module is None or getattr(module, "__file__", None) is None:
        raise ConfigError(f"{table}.implementation: {kind.__module__} has no source file")
    return module_roots(module)[0]


class HostedSession(Session):
    """Each configured class holds its own Setup; a subclass starts unconfigured."""

    _setup: ClassVar[_Setup | None] = None

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        cls._setup = None

    def __init__(
        self,
        client_name: str,
        mode: Mode,
        spec: Spec,
        *,
        info: SessionInfo,
        running: BaseMode,
        host: Host,
        idle_timeout: float | None = None,
        client: Client,
    ):
        self.sid, self.client, self.mode, self.name = info.sid, client_name, mode, spec.name
        self._running, self._host, self._info, self._idle = running, host, info, idle_timeout
        self._client = client
        self._clock = type(self)._config().clock
        self._viewer = type(self).viewer()
        self._log = RunLog(spec.workdir)
        self._reply: str | None = None
        self._queue: deque = deque()
        self._closed = False
        self._ended = False
        self._blocked = False
        self._bound = False

    @classmethod
    def configure(
        cls,
        *,
        clients: Mapping,
        hosts: Hosts,
        class_loader=load_class,
        probe=probe,
        clock: Callable[[], float] = time.monotonic,
        host_factory: HostFactory | None = None,
        viewer: Mapping | None = None,
        viewer_factory: ViewerFactory = Viewer,
    ) -> None:
        """host_factory(cls, name, settings) builds a configured Host."""
        if not isinstance(hosts, Hosts):
            raise ConfigError("hosts: want parsed Hosts (Host.parse_hosts)")
        if viewer is not None and not isinstance(viewer, Mapping):
            raise ConfigError("[viewer]: want a table")
        registry = Registry(clients=clients, hosts=hosts, class_loader=class_loader)
        cls._setup = _Setup(
            registry,
            hosts,
            class_loader,
            probe,
            clock,
            host_factory or _built,
            viewer or {},
            viewer_factory,
        )

    @classmethod
    def configure_from(cls, config: Mapping, *, hosts: Hosts | None = None) -> None:
        cls.configure(**settings(config, hosts=hosts))

    @classmethod
    def configured(cls, **setup) -> type[HostedSession]:
        own = cast("type[HostedSession]", type(cls.__name__, (cls,), {"__module__": __name__}))
        own.configure(**setup)
        return own

    @classmethod
    def _config(cls) -> _Setup:
        if cls._setup is None:
            raise ConfigError(UNCONFIGURED)
        return cls._setup

    @classmethod
    def preflight(
        cls,
        spec: Spec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> None:
        setup = cls._config()
        client_name, mode = _request(spec, idle_timeout)
        client = setup.registry.client(client_name)
        spec = replace(spec, sid=spec.sid or str(uuid.uuid4()))
        on = cls.host(host)
        cls.viewer()
        displays.validate_display(display, on)
        MODES[mode].preflight(
            spec,
            client,
            registry=setup.registry,
            host=on,
            display=display,
        )

    @classmethod
    def start(
        cls,
        spec: Spec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> HostedSession:
        """spec.sid: its id, else a new one; display: kept by its Host."""
        setup = cls._config()
        client_name, mode = _request(spec, idle_timeout)
        client = setup.registry.client(client_name)
        spec = replace(spec, sid=spec.sid or str(uuid.uuid4()))
        on = cls.host(host)
        viewer = cls.viewer()
        displays.validate_display(display, on)
        spec = _picked(setup, spec, client_name, client, on)
        name = tui.TuiMode.resolve_name(spec) if mode == Mode.TUI else spec.name

        def check_name(name: str) -> None:
            if any(
                name in cls.host(configured).list()
                for configured in setup.hosts.tables
                if cls.host(configured) is not on
            ):
                raise SessionError(f"session {name} exists")

        if name is not None:
            check_name(name)
        running = MODES[mode].start(
            spec,
            client,
            registry=setup.registry,
            record=_session_line(spec.workdir),
            host=on,
            display=display,
            check_name=check_name,
        )
        if mode == Mode.TUI and name is not None:
            cls._started(viewer, name, running, on, display)
        return cls(
            client_name,
            mode,
            running.prepared.spec,
            info=running.prepared.info,
            running=running,
            host=on,
            idle_timeout=idle_timeout,
            client=client,
        )

    @classmethod
    def _started(
        cls, viewer: Viewer, name: str, running: BaseMode, on: Host, display: Display
    ) -> None:
        try:
            viewer.close(name, on)
            displays.started(viewer, name, on, display)
        except SessionError:
            running.close()
            raise

    @classmethod
    def attach(
        cls,
        name: str,
        *,
        sid: str | None = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> HostedSession:
        """Tui session `name`, from any process; its client record and hook log replayed into its
        reply and blocked, blocked too while its terminal is marked so.
        """
        setup = cls._config()
        tui.checked_name(name)
        _idle(idle_timeout)
        on = cls.host(host)
        state = tui.TuiMode.snapshot(name)
        try:
            client = setup.registry.client(state["client"])
        except ConfigError as e:
            raise SessionError(f"no tui session {name}: {e}") from None
        running = tui.TuiMode.attach(name, state, client, host=on, sid=sid)
        session = cls(
            state["client"],
            Mode.TUI,
            running.prepared.spec,
            info=running.prepared.info,
            running=running,
            host=on,
            idle_timeout=idle_timeout,
            client=client,
        )
        for event in running.replayed:
            session._fold(event, replayed=True)
        if not session._blocked:
            session._blocked = running.marked_blocked()
        session.info()
        return session

    @classmethod
    def retire(cls, name: str, *, sid: str | None = None, host: str | None = None) -> None:
        on = cls.host(host)
        tui.TuiStore.attach(name, host=on, sid=sid).close()
        cls.viewer().close(name, on)

    @classmethod
    def list(
        cls, match: str | None = None, *, display: Display = None
    ) -> builtins.list[SessionRow]:
        """All configured Hosts' sessions fullmatching `match`, filtered by their displays."""
        if match is not None and not isinstance(match, str):
            raise ConfigError(f"match must be a regular expression, got {match!r:.80}")
        try:
            pattern = None if match is None else re.compile(match)
        except re.error as error:
            raise ConfigError(f"match {match!r:.80}: {error}") from None
        setup = cls._config()
        if display == "":
            return []
        return sorted(
            (
                row
                for name in setup.hosts.tables
                for row in displays.rows(cls.viewer(), cls.host(name), display)
                if pattern is None or pattern.fullmatch(row.name)
            ),
            key=lambda row: row.name,
        )

    @classmethod
    def client_resume(cls, client: str, argv: Sequence[str]) -> int:
        return cls._config().registry.client(client).resume_main(argv)

    @classmethod
    def usage(cls, client: str) -> Usage | None:
        setup = cls._config()
        found = setup.registry.client(client)
        host = cls.host()
        if (
            request := found._provider_probe()
        ) is not None and host._client_context().container is not None:
            return Usage(None, None) if host._provider_usage(request) else None
        if (pool := _pooled(setup, client, found, host)) is None:
            return _read_usage(
                setup,
                found,
                found.usage_probe(),
                container=host._client_context().container is not None,
            )
        return None if (name := accounts.best(pool)) is None else pool[name]

    @classmethod
    def layout(cls, *, display: Display = None) -> str | None:
        on = cls.host()
        displays.validate_display(display, on)
        return displays.layout(cls.viewer(), on, display) if on.attachable else None

    @classmethod
    def writable_dirs(cls, spec: Spec) -> tuple[str, ...]:
        """An OSError is a ConfigError, so a caller fails closed."""
        setup = cls._config()
        if not isinstance(spec, Spec):
            raise ConfigError(f"spec: want a Spec, got {type(spec).__name__}")
        if spec.client is None:
            raise ConfigError("spec.client: required")
        client = setup.registry.client(spec.client)
        try:
            return tuple(client.writable_dirs(spec))
        except OSError as e:
            raise ConfigError(f"{spec.client}'s writable dirs: {e}") from e

    @classmethod
    def display(cls, value: Display = None, *, host: str | None = None) -> Display:
        on = cls.host(host)
        displays.validate_display(value, on)
        return displays.display(cls.viewer(), value, on) if on.attachable else value

    @classmethod
    def viewer(cls) -> Viewer:
        """The Viewer [viewer] configures, built once per class."""
        setup = cls._config()
        if not setup.viewers:
            setup.viewers.append(setup.viewer_factory(setup.viewer))
        return setup.viewers[0]

    @classmethod
    def host(cls, name: str | None = None) -> Host:
        """[hosts.<name>] (None: the default), built once per class."""
        setup = cls._config()
        name = setup.hosts.default if name is None else name
        if not isinstance(name, str) or name not in setup.hosts.tables:
            raise ConfigError(f"unknown host {name}")
        found = setup.built.get(name)
        if found is None:
            kind, settings = _host_class(setup, name)
            found = setup.built.setdefault(name, setup.host_factory(kind, name, settings))
        return found

    @classmethod
    def class_roots(cls, client: str) -> tuple[str, ...]:
        """Loads each class without building it; an unloadable one is a ConfigError."""
        setup = cls._config()
        loaded: list[tuple[str, type]] = [
            (f"[clients.{client}]", setup.registry.client_class(client))
        ]
        loaded += [(f"[hosts.{name}]", _host_class(setup, name)[0]) for name in setup.hosts.tables]
        return tuple(dict.fromkeys(_class_root(kind, table) for table, kind in loaded))

    @classmethod
    def inspect(cls, spec: Spec) -> SessionStatus:
        setup = cls._config()
        if not isinstance(spec, Spec):
            raise ConfigError(f"spec: want a Spec, got {type(spec).__name__}")
        client, sid = spec.client, spec.sid
        if client is None or sid is None:
            field = "client" if client is None else "sid"
            raise ConfigError(f"spec.{field}: required to inspect")
        try:
            info = RunLog(spec.workdir).session(sid)
        except OSError as error:
            raise SessionError(f"inspect session {sid}: {error}") from error
        if info is None:
            raise NoRecordedSession(f"no recorded session {sid} in {spec.workdir}")
        return _resumable(lambda: setup.registry.client(client), info, spec)

    def events(self) -> Iterator[SessionEvent | Stderr | IdleTimeout]:
        last = self._clock()
        try:
            while not self._ended:
                if self._queue:
                    event = self._queue.popleft()
                    self._record(event)
                    if not isinstance(event, ClientBound):
                        last = self._clock()
                        yield event
                    continue
                wait = POLL
                if self._idle is not None and not self._blocked:
                    wait = min(POLL, last + self._idle - self._clock())
                if self._closed:
                    code = self._running.returncode(max(wait, 0))
                    if code is not None:
                        self._ended = True
                        yield Exited(code)
                        continue
                elif wait > 0:
                    found, self._closed = self._running.poll(wait)
                    self._queue.extend(e for e in found if not isinstance(e, Exited))
                    continue
                if wait <= 0 and self._idle is not None:
                    last = self._clock()
                    yield IdleTimeout(self._idle)
        except GeneratorExit:  # the caller closed this iterator; the session goes on
            raise
        except BaseException as error:
            self._ended = True
            try:
                self._running.stop()
            except SessionError as failed:
                error.add_note(f"stop: {failed}")
            raise

    def _record(self, event: SessionEvent | Stderr | ClientBound) -> None:
        if isinstance(event, ClientBound):
            self._log.note(
                "client_session",
                sid=self.sid,
                client_id=event.client_id,
                durable=True,
            )
        elif isinstance(event, Stderr):
            self._log.note("stderr", best_effort=True, text=event.text)
        self._fold(event)
        if isinstance(event, ClientBound):
            self._refresh_metadata()

    def _fold(self, event: SessionEvent | Stderr | ClientBound, *, replayed: bool = False) -> None:
        """What an event, live or replayed on attach, changes: bound, reply, blocked."""
        if isinstance(event, ClientBound):
            self._bound = True
            return
        if isinstance(event, Reply):
            self._reply = event.text
        elif isinstance(event, TurnEnd) and (event.reply or replayed):
            self._reply = event.reply
        # a client record's Reply can land after the block it preceded
        self._blocked = (
            isinstance(event, Blocked) or self._blocked and isinstance(event, (Stderr, Reply))
        )

    @property
    def opening_pending(self) -> str | None:
        self._running.state()
        return self._running.opening_pending

    def send(self, text: str) -> None:
        self._running.send(text)

    def press(self, keys: Sequence[KeyPress]) -> None:
        self._running.press(tuple(keys))

    def alive(self) -> bool:
        return self._running.state() == Liveness.LIVE

    def status(self) -> Status:
        """Its run's; a live tui one's activity, attach and hidden from its terminal."""
        liveness, name = self._running.state(), self._tui()
        if liveness == Liveness.LIVE and name is not None:
            try:
                found = displays.status(self._viewer, self._host, self._host.attach(name))
                return replace(found, cwd=self._info.cwd)
            except SessionError:
                liveness = Liveness.ABSENT
        return Status(
            name=self.name or self.sid,
            alive=liveness == Liveness.LIVE,
            state={
                Liveness.LIVE: StatusState.WORKING,
                Liveness.UNKNOWN: StatusState.UNKNOWN,
            }.get(liveness, StatusState.GONE if name else StatusState.DEAD),
            outcome=None,
            attach=None,
            hidden=False,
            cwd=self._info.cwd if liveness == Liveness.LIVE else None,
        )

    def stop(self) -> None:
        self._running.stop()

    def _refresh_metadata(self) -> None:
        if not self._bound:
            return
        try:
            resolved = self._client.session_metadata(self._info)
            resume = self._host_resume(self._info.cwd) or resolved.resume
            info = replace(self._info, transcript=resolved.transcript, resume=resume)
        except OSError:
            return
        if info != self._info:
            self._info = info
            self._log.note(
                "session_metadata",
                best_effort=True,
                sid=self.sid,
                transcript=info.transcript,
                resume=info.resume,
            )

    def info(self) -> SessionInfo:
        self._running.state()
        self._refresh_metadata()
        return self._info

    @property
    def reply(self) -> str | None:
        """The latest Reply or non-empty TurnEnd text, None before; attached, its hook log's last
        TurnEnd reply, even "", comes first, else its client record's newest text.
        """
        self._running.state()
        return self._reply

    def close(self) -> None:
        name = self._tui()
        self._running.close()
        if name is not None:
            self._viewer.close(name, self._host)

    def session_status(self) -> SessionStatus:
        info = self.info()
        found = _resumable(lambda: self._client, info, self._running.prepared.spec)
        resume = self._host_resume(info.cwd)
        return found if resume is None else replace(found, resume_command=resume)

    def _host_resume(self, cwd: str) -> str | None:
        return host_resume(self._host, self.name, cwd)

    def record(self) -> Record | None:
        self._running.state()
        name = self._tui()
        return None if name is None else self._host.attach(name).record()

    @property
    def showable(self) -> bool:
        self._running.state()
        return self._tui() is not None and self._host.attachable

    def show(self, *, display: Display = None, command: str | None = None) -> str | None:
        viewer, name = self._shown("show")
        return viewer.show(name, self._host, display=display, command=command)

    def hide(self) -> None:
        viewer, name = self._shown("hide")
        viewer.hide(name, self._host)

    def attach_command(self) -> str:
        viewer, name = self._shown("attach_command")
        return viewer.attach_command(name, self._host)

    def content(self) -> str:
        if self.status().state == StatusState.DONE and self.reply:
            return self.reply
        return self.screen()

    def screen(self, lines: int | None = None) -> str:
        """Its mode's read, which refuses a terminal no longer running."""
        self._shown("screen")
        return self._running.screen(lines)

    def _tui(self) -> str | None:
        return self.name if self.mode == Mode.TUI else None

    def _shown(self, call: str) -> tuple[Viewer, str]:
        self._running.state()
        if not self._host.attachable:
            raise NotShowable(f"host {self._host.name} cannot show")
        name = self._tui()
        if name is None:
            raise NotShowable(f"{call}: tui mode only")
        return self._viewer, name
