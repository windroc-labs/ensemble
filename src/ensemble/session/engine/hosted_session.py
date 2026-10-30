"""The Session of a Client run in a Mode on a Host (an owned worker's view:
harness/view.py's OwnedSession). events() yields its events, Stderr and IdleTimeout (none while
Blocked) up to its Exited, recording `stderr` and each ClientBound as `client_session`.
"""

from __future__ import annotations

import builtins
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from dataclasses import fields as dataclass_fields
from typing import Any, ClassVar, NamedTuple, cast

import ensemble.utils
from ensemble.session.clients.base import Client
from ensemble.session.clients.registry import Registry, settings
from ensemble.session.engine import accounts
from ensemble.session.engine.base import Session
from ensemble.session.engine.capacity import Admission
from ensemble.session.engine.records import RunLog
from ensemble.session.engine.types import (
    DEFAULT_ACCOUNT,
    Access,
    ClientBound,
    ConfigError,
    EnvironmentSpec,
    Exited,
    HookCallback,
    Hosts,
    IdleTimeout,
    KeyPress,
    Launch,
    LimitHit,
    Liveness,
    Mode,
    NoRecordedSession,
    NotShowable,
    OwnerRefused,
    Resumable,
    SessionBusy,
    SessionError,
    SessionEvent,
    SessionInfo,
    SessionRow,
    SessionStatus,
    Spec,
    StatusState,
    Stderr,
    Unanswered,
    Usage,
)
from ensemble.session.harness.channel import SLICE, Channel, Tie
from ensemble.session.harness.channel import bound as channel_bound
from ensemble.session.harness.channel import running as channel_running
from ensemble.session.harness.dispatch import load_handlers
from ensemble.session.harness.types import Op
from ensemble.session.harness.view import OwnedSession, confirmed_end, dialog, fold
from ensemble.session.hosts.base import SIGNALS, Host, activity, host_resume, stored_record
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
    kill_group,
    load_class,
    module_roots,
    piped_popen,
    printable_abs,
    redact,
    save,
    sid_error,
    source_path,
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
START_WAIT = 60
CLOSE_WAIT = 10  # an owned headless worker's end
HARNESS = "ensemble.session.harness.process"
LOG = logging.getLogger("session")
PROBE_DIR = f"{STATE}/probe"
PROBE_TIMEOUT = 120
PROBE_RECORDS = f"{STATE}/usage/probes"
UNCONFIGURED = "session not configured: call Session.configure first"
MODES: dict[Mode, type[BaseMode]] = {
    mode.kind: mode for mode in (headless.HeadlessMode, tui.TuiMode, in_process.InProcessMode)
}

HostFactory = Callable[[type[Host], str, Mapping], Host]
ViewerFactory = Callable[[Mapping], Viewer]


def _environment_check(spec: Spec, on: Host, mode: Mode) -> None:
    """An environment runs only on a Docker-kind Host, in a mode its Host runs (FR-66)."""
    if spec.environment is None:
        return
    if not on._environments:
        raise ConfigError(f"environment: host {on.name} is not a Docker-kind Host")
    if mode == Mode.IN_PROCESS:
        raise ConfigError("environment: in_process mode runs on no Host")


def _built(cls: type[Host], name: str, settings: Mapping) -> Host:
    return cls(name, settings)


class HarnessStart(NamedTuple):
    spec: Spec
    display: Display
    host: str | None
    roots: tuple[str, ...]
    pythonpath: str | None  # the caller's, for its worker


@dataclass(frozen=True)
class _Setup:
    registry: Registry
    hosts: Hosts
    class_loader: Callable[..., Any]
    probe: Callable[..., str | subprocess.CompletedProcess[str] | None]
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


def _usage_probe(
    argv, *, cwd: str, env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    try:
        proc = piped_popen(argv, cwd=cwd, env=env)
    except (OSError, ValueError) as error:
        return subprocess.CompletedProcess(argv, 127, "", str(error))
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            kill_group(proc)
            stdout, stderr = error.output, error.stderr
            stdout = (
                stdout.decode("utf-8", errors="replace")
                if isinstance(stdout, bytes)
                else stdout or ""
            )
            stderr = (
                stderr.decode("utf-8", errors="replace")
                if isinstance(stderr, bytes)
                else stderr or ""
            )
            return subprocess.CompletedProcess(argv, 124, stdout, stderr)
        assert proc.returncode is not None
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    finally:
        assert proc.stdout is not None and proc.stderr is not None
        proc.stdout.close()
        proc.stderr.close()


def _read_usage(
    setup: _Setup,
    client: Client,
    launch: Launch | None,
    *,
    container: bool = False,
    probe_accounts: tuple[str, ...] = (),
    identity: str = "",
) -> Usage | None:
    if launch is None or not launch.argv:
        return None
    cwd = launch.cwd or _probe_dir()
    if cwd is None:
        return None
    env = child_env(launch, cwd)
    if executable := client._executable(container=container):
        program = os.path.join(cwd, executable) if os.path.dirname(executable) else executable
        if shutil.which(program, path=env.get("PATH", os.defpath)) is None:
            raise ConfigError(
                f"client executable {executable!r} is missing or not executable; install or configure this client"
            )
    result = setup.probe(launch.argv, cwd=cwd, env=env, timeout=PROBE_TIMEOUT)
    stderr, returncode = "", None
    if isinstance(result, subprocess.CompletedProcess):
        raw, stderr, returncode = result.stdout, result.stderr, result.returncode
        output = raw if returncode == 0 else None
    else:
        output = raw = result
    _record_probe(
        type(client).__name__,
        probe_accounts,
        identity,
        raw,
        env,
        stderr=stderr,
        returncode=returncode,
    )
    if output is None:
        return None
    try:
        return client.usage(output)
    except ValueError:
        return None


def _record_probe(
    client: str,
    probe_accounts: tuple[str, ...],
    identity: str,
    output: str | None,
    env: Mapping[str, str],
    *,
    stderr: str = "",
    returncode: int | None = None,
) -> None:
    record = {
        "at": time.time(),
        "client": client,
        "accounts": probe_accounts,
        "identity": identity,
        "output": _probe_text(output, env),
        "stderr": _probe_text(stderr, env),
        "returncode": returncode,
    }
    folder = os.path.expanduser(PROBE_RECORDS)
    path = os.path.join(folder, f"{time.time_ns()}-{uuid.uuid4().hex}.json")
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        save(path, json.dumps(record, sort_keys=True))
    except OSError as error:
        print(f"usage probe: could not record: {error}", file=sys.stderr)


def _probe_text(text: str | None, env: Mapping[str, str]) -> str | None:
    if text is None:
        return None
    text = redact(text)
    secrets = {
        value
        for mapping in (os.environ, env)
        for key, value in mapping.items()
        if value and re.search(r"TOKEN|KEY|SECRET|PASSWORD|AUTH", key, re.I)
    }
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", "[REDACTED]", text)
    return re.sub(
        r"""(?i)(\b[\w-]*(?:token|key|secret|password|authorization)[\w-]*["']?[ \t]*[:=]\s*)"""
        r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\r\n]*)""",
        r"\1[REDACTED]",
        text,
    )


def _pooled(setup: _Setup, name: str, client: Client, host: Host) -> dict[str, Usage | None] | None:
    probes = client._usage_probes()
    if not probes or host._client_context().container is None:
        return None

    def read(launch: Launch) -> Usage | None:
        identity = next(identity for identity, probe in probes.values() if probe is launch)
        names = tuple(account for account, (source, _) in probes.items() if source == identity)
        return _read_usage(setup, client, launch, probe_accounts=names, identity=identity)

    return accounts.readings(name, probes, read)


def _pool_account(spec: Spec, client: Client, host: Host) -> str | None:
    """The account a start runs on, when its client has accounts on a container Host."""
    registered = client._accounts()
    if not registered or host._client_context().container is None:
        return None
    return spec.env.get(ACCOUNT_ENV) or DEFAULT_ACCOUNT


def _picked(setup: _Setup, spec: Spec, name: str, client: Client, host: Host) -> Spec:
    if spec.env.get(ACCOUNT_ENV):
        return spec
    if account := client._account():
        return replace(spec, env={**spec.env, ACCOUNT_ENV: account})
    if (pool := _pooled(setup, name, client, host)) is None:
        return spec
    if (account := accounts.pick(pool)) is None:
        detail = "; ".join(f"{account}: {summary(reading)}" for account, reading in pool.items())
        raise ConfigError(f"{name}: no available account for automatic selection ({detail})")
    print(f"{name} account {account}: picked ({summary(pool[account])})", file=sys.stderr)
    return replace(spec, env={**spec.env, ACCOUNT_ENV: account})


def _host_class(setup: _Setup, name: str) -> tuple[type[Host], dict]:
    settings = thaw(setup.hosts.tables[name])
    path = settings.pop("implementation", None)
    try:
        return setup.class_loader(path, Host), settings
    except ValueError as e:
        raise ConfigError(f"[hosts.{name}].implementation: {e}") from None


def _asked_to_close(channel: Channel) -> bool:
    """Its owner's confirmed close; False once it is gone unasked; a refusal is OwnerRefused."""
    try:
        return channel.request(Op.CLOSE)
    except Unanswered:
        raise
    except SessionError as error:
        raise OwnerRefused(str(error)) from error


def _host_key(setup: _Setup, name: str) -> str:
    table = json.dumps([name, thaw(setup.hosts.tables[name])], sort_keys=True)
    return hashlib.sha256(table.encode()).hexdigest()


def _class_root(kind: type, table: str) -> str:
    module = sys.modules.get(kind.__module__)
    if module is None or getattr(module, "__file__", None) is None:
        raise ConfigError(f"{table}.implementation: {kind.__module__} has no source file")
    return module_roots(module)[0]


def _reconstructible(setup: _Setup) -> bool:
    """Every callable the default, so a harness can rebuild it."""
    return (
        setup.class_loader is load_class
        and setup.probe is _usage_probe
        and setup.host_factory is _built
        and setup.viewer_factory is Viewer
    )


def _check_owned(mode: Mode, client: Client, on: Host, client_name: str) -> None:
    """Every start runs under a session harness: refuses one no harness can run (a Host without
    managed workers, a remote tui its client cannot be).
    """
    if mode == Mode.IN_PROCESS:
        return
    if type(on).worker is Host.worker:
        raise _harness_only(client_name, mode, on)
    run = client.tui() if mode == Mode.TUI and on._remote_tui else None
    if run is not None and not run._remote_owned:
        raise _harness_only(client_name, mode, on)


def _terminal_less(mode: Mode) -> bool:
    return mode != Mode.TUI


def _liveness(name: str, generation: str, on: Host) -> Callable[[], Liveness]:
    """A read-only attach's liveness source for tui worker `name`'s `generation`: LIVE while its
    bound owner runs it; else (its harness gone or done) its Host's worker_liveness.
    """

    def liveness() -> Liveness:
        try:
            state = tui.TuiMode.snapshot(name)
        except SessionError:
            state = None
        try:
            if (
                state is not None
                and state.get("generation") == generation
                and channel_running(name, state) is not None
            ):
                return Liveness.LIVE
            return on.worker_liveness(name, generation)
        except SessionError:
            return Liveness.UNKNOWN

    return liveness


def _harness_only(client: str, mode: Mode, on: Host) -> ConfigError:
    return ConfigError(f"{client} {mode} on host {on.name}: runs only under a session harness")


def _refuse_running(name: str, on: Host) -> None:
    """A second start of tui session `name` while its owner runs it: refused at once, before the
    gate (a running owner is the only one asked; the gate only waits out a reaping guard).
    """
    try:
        state = tui.TuiMode.snapshot(name)
    except SessionError:
        return
    if channel_running(name, state) is not None:
        tui.TuiMode.running(name, host=on)


def _resume_check(spec: Spec, client: Client, name: str, on: Host) -> None:
    """An owned resume of a conversation its client keeps no transcript of is refused, never
    started as a new empty one (FR-18); unknown goes on. A container Host checks its own. Its run
    log's record of the conversation is checked, as inspect does, else the spec's cwd.
    """
    if not spec.resume or spec.sid is None or on._client_context().container:
        return
    try:
        recorded = RunLog(spec.workdir).session(spec.sid)
    except OSError:
        recorded = None
    info = recorded or SessionInfo(spec.sid, spec.access.cwd, spec.access.project, "", "")
    if _resumable(lambda: client, info, spec).resumable == Resumable.NO:
        raise ConfigError(f"session {name}: conversation {spec.sid} has no transcript to resume")


def _start_error(channel: Channel, status: Mapping) -> SessionError | ConfigError:
    error = status.get("error")
    if isinstance(error, Mapping) and isinstance(message := error.get("message"), str):
        kind, lock, holder = error.get("type"), error.get("lock"), error.get("holder")
        if kind == "SessionBusy" and isinstance(lock, str) and isinstance(holder, str):
            return SessionBusy(lock, holder)
        return (ConfigError if kind == "ConfigError" else SessionError)(message)
    return SessionError(f"session harness {channel.directory} did not start: {_last_line(channel)}")


def _last_line(channel: Channel) -> str:
    try:
        with open(channel.log, encoding="utf-8", errors="replace") as log:
            lines = [line.strip() for line in log if line.strip()]
    except OSError:
        lines = []
    return lines[-1][:500] if lines else "no output"


def _await_start(
    channel: Channel,
    ended: Callable[[], bool],
    *,
    clock: Callable[[], float] | None = None,
    pause: Callable[[float], None] | None = None,
) -> dict:
    """harness.json once its harness runs the worker; a phase it names adds its `phase_wait`."""
    clock, pause = clock or time.monotonic, pause or time.sleep
    began = clock()
    phase, deadline = (None, None), began + START_WAIT
    while True:
        gone = ended()  # first: a dead harness's status is final
        status = channel.status()
        if status.get("state") in ("running", "draining", "exited"):  # its end at once
            return status
        if status.get("state") == "failed" or gone:
            raise _start_error(channel, status)
        now = clock()
        if (status.get("phase"), status.get("phase_wait")) != phase:
            phase = (status.get("phase"), wait := status.get("phase_wait"))
            deadline = (
                now + START_WAIT + (wait if isinstance(wait, int | float) and wait > 0 else 0)
            )
        if now > deadline:
            doing = phase[0] if isinstance(phase[0], str) else "starting"
            raise SessionError(
                f"session harness {channel.directory} did not start: timed out after "
                f"{now - began:.0f} s while {doing[:200]}; its log's last line: {_last_line(channel)}"
            )
        pause(SLICE)


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
        self._dialog: str | None = None
        self._bound = False
        self._capacity: Admission | None = None
        self._capacity_hosts: Mapping[str, Host] = {}
        self._read_only = False  # a view's attach: no run records
        self._account: str | None = None  # its pool account, held over at a usage limit

    @classmethod
    def configure(
        cls,
        *,
        clients: Mapping,
        hosts: Hosts,
        class_loader=load_class,
        probe=_usage_probe,
        clock: Callable[[], float] = time.monotonic,
        host_factory: HostFactory | None = None,
        viewer: Mapping | None = None,
        viewer_factory: ViewerFactory = Viewer,
    ) -> None:
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
        _environment_check(spec, on, mode)
        _check_owned(mode, client, on, client_name)
        cls.viewer()
        displays.validate_display(display, on)
        if mode == Mode.TUI:
            name = tui.TuiMode.resolve_name(spec)
            _refuse_running(name, on)
            _resume_check(spec, client, name, on)
        MODES[mode].preflight(
            spec,
            client,
            registry=setup.registry,
            host=on,
            display=display,
        )
        load_handlers(spec.handlers, cls._roots(client_name), load=False)

    @classmethod
    def start(
        cls,
        spec: Spec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> Session:
        """display: kept by its Host. Every start runs under its own session harness and returns
        its control view.
        """
        setup = cls._config()
        client_name, mode = _request(spec, idle_timeout)
        on, client = cls.host(host), setup.registry.client(client_name)
        _environment_check(spec, on, mode)
        _check_owned(mode, client, on, client_name)
        spec = replace(spec, sid=spec.sid or str(uuid.uuid4()))
        if mode == Mode.TUI:
            spec = replace(spec, name=tui.TuiMode.resolve_name(spec))
            assert spec.name is not None
            _refuse_running(spec.name, on)
            _resume_check(spec, client, spec.name, on)
        else:  # its refusals before any harness
            cls.preflight(spec, display=display, host=host, idle_timeout=idle_timeout)
        return cls._spawn(spec, mode, display=display, host=host, idle_timeout=idle_timeout)

    @classmethod
    def _spawn(
        cls,
        spec: Spec,
        mode: Mode,
        *,
        display: Display,
        host: str | None,
        idle_timeout: float | None,
    ) -> Session:
        """Its view once a new harness runs it; else the start error or stop, nothing left. The
        harness process builds its session class from configuration: injected callables are refused.
        """
        assert spec.client is not None and spec.sid is not None
        if not _reconstructible(cls._config()):
            raise ConfigError(
                f"{spec.client} {mode}: runs only under a session harness, which builds its "
                "session class from configuration, not injected callables"
            )
        roots = cls._roots(spec.client)
        request = cls.harness_request(spec, display=display, host=host, roots=roots)
        channel = Channel.create()
        tie, held = None, signal.pthread_sigmask(signal.SIG_BLOCK, ())
        try:
            signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
            tie = None if mode in (Mode.TUI, Mode.IN_PROCESS) else Tie()  # these outlive it
            caller = () if tie is None else ("--caller", str(tie.far))
            channel.write_request(request)
            with open(channel.log, "ab") as log:
                harness = subprocess.Popen(
                    [
                        os.path.realpath(sys.executable),
                        "-P",
                        "-s",
                        "-m",
                        HARNESS,
                        *caller,
                        channel.directory,
                    ],
                    env={**os.environ, **source_path(roots)},
                    cwd=os.getcwd(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    start_new_session=True,
                    pass_fds=() if tie is None else (tie.far,),
                )
        except BaseException as error:
            try:
                if tie is not None:
                    tie.close()
                shutil.rmtree(channel.directory, ignore_errors=True)  # request.json holds spec.env
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, held)
            if isinstance(error, TypeError | ValueError):
                raise ConfigError(f"session harness request: not JSON: {error}") from None
            if isinstance(error, OSError):
                raise SessionError(f"session harness: {error.strerror or error}") from None
            raise
        try:  # an interrupt too: no view would close it
            signal.pthread_sigmask(signal.SIG_SETMASK, held)  # a stop held over Popen lands here
            if tie is not None:
                tie.handed()
            status = _await_start(channel, lambda: harness.poll() is not None)
            generation = status.get("generation")
            if (status.get("name"), status.get("sid")) != (spec.name, spec.sid) or not isinstance(
                generation, str
            ):
                raise SessionError(f"session harness {channel.directory}: started another session")
            view = cls._view(
                channel,
                name=spec.name,
                client=spec.client,
                sid=spec.sid,
                generation=generation,
                host=host,
                idle_timeout=idle_timeout,
                mode=mode,
                spec=spec,
                tie=tie,
            )
        except BaseException:
            cls._unspawn(channel, harness, spec.name, host, mode)
            if tie is not None:
                tie.close()
            raise
        return view

    @classmethod
    def _roots(cls, client: str) -> tuple[str, ...]:
        return (*module_roots(sys.modules[__name__], ensemble.utils), *cls.class_roots(client))

    @classmethod
    def _unspawn(
        cls,
        channel: Channel,
        harness: subprocess.Popen,
        name: str | None,
        host: str | None,
        mode: Mode,
    ) -> None:
        """SIGTERM: its harness closes a headless worker, its child, first."""
        if _terminal_less(mode) and harness.poll() is None:
            harness.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                harness.wait(CLOSE_WAIT)
        if harness.poll() is None:
            harness.kill()
        harness.wait()
        if name is None or _terminal_less(mode):
            shutil.rmtree(channel.directory, ignore_errors=True)
            return
        try:
            owned = tui.TuiMode.snapshot(name).get("owner") == channel.directory
        except SessionError:
            owned = False
        if owned:
            try:
                cls.retire(name, host=host)
            except SessionError as error:
                LOG.warning("session harness %s: close %s: %s", channel.directory, name, error)
        shutil.rmtree(channel.directory, ignore_errors=True)

    @classmethod
    def harness_request(
        cls, spec: Spec, *, display: Display, host: str | None, roots: Sequence[str]
    ) -> dict:
        """A harness's JSON start request, secret-bearing (spec.env); pythonpath: the caller's,
        its worker's (None: unset).
        """
        setup = cls._config()
        fields = {f.name: getattr(spec, f.name) for f in dataclass_fields(spec)}
        fields.update(
            access={
                f.name: thaw(getattr(spec.access, f.name)) for f in dataclass_fields(spec.access)
            },
            env=dict(spec.env),
            handlers=[
                {"module": h.module, "state": thaw(h.state), "roots": list(h.roots)}
                for h in spec.handlers
            ],
            environment=None if spec.environment is None else spec.environment.persisted(),
        )
        return {
            "spec": thaw(fields),
            "display": display,
            "host": host,
            "config": {
                "client_tables": thaw(setup.registry.settings.clients),
                "hosts": {"default": setup.hosts.default, "tables": thaw(setup.hosts.tables)},
                "viewer": thaw(setup.viewer),
            },
            "roots": list(roots),
            "pythonpath": os.environ.get("PYTHONPATH"),
        }

    @classmethod
    def from_harness_request(cls, request: Mapping) -> HarnessStart:
        """harness_request's inverse, configuring this class; ConfigError when malformed."""
        try:
            config, fields = request["config"], dict(request["spec"])
            cls.configure(
                clients=config["client_tables"],
                hosts=Hosts(config["hosts"]["default"], config["hosts"]["tables"]),
                viewer=config["viewer"],
            )
            access: dict[str, Any] = {
                k: tuple(v) if isinstance(v, list) else v for k, v in fields["access"].items()
            }
            fields.update(
                access=Access(**access),
                flags=tuple(fields["flags"]),
                reply_tags=None if fields["reply_tags"] is None else tuple(fields["reply_tags"]),
                handlers=tuple(
                    HookCallback(h["module"], h["state"], tuple(h["roots"]))
                    for h in fields["handlers"]
                ),
                environment=None
                if fields["environment"] is None
                else EnvironmentSpec.from_fields(fields["environment"]),
            )
            spec = Spec(**fields)
            display, host, roots = request["display"], request["host"], request["roots"]
            pythonpath = request["pythonpath"]
        except (KeyError, TypeError, AttributeError) as error:
            raise ConfigError(f"session harness request: {error!r:.200}") from None
        if not (display is None or isinstance(display, str)) or not (
            host is None or isinstance(host, str)
        ):
            raise ConfigError("session harness request: bad display or host")
        if not isinstance(roots, list) or not all(map(printable_abs, roots)):
            raise ConfigError("session harness request: bad roots")
        if not (pythonpath is None or isinstance(pythonpath, str) and pythonpath.isprintable()):
            raise ConfigError("session harness request: bad pythonpath")
        return HarnessStart(spec, display, host, tuple(roots), pythonpath)

    @classmethod
    def own(
        cls, spec: Spec, *, display: Display = None, host: str | None = None, owner: str
    ) -> Owned:
        return Owned(cls._begin(spec, display=display, host=host, owner=owner), host)

    @classmethod
    def _begin(
        cls,
        spec: Spec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
        owner: str,
    ) -> HostedSession:
        setup = cls._config()
        client_name, mode = _request(spec, idle_timeout)
        client = setup.registry.client(client_name)
        spec = replace(spec, sid=spec.sid or str(uuid.uuid4()))
        on = cls.host(host)
        _environment_check(spec, on, mode)
        viewer = cls.viewer()
        displays.validate_display(display, on)
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
        limit = setup.registry.max_runs(client_name)
        selected_host = host or setup.hosts.default
        hosts = {
            _host_key(setup, configured): cls.host(configured)
            for configured in setup.hosts.tables
            if limit or configured == selected_host
        }
        assert spec.sid is not None
        with Admission(
            client_name,
            limit,
            name=name,
            sid=spec.sid,
            mode=mode,
            hosts=hosts,
            host=_host_key(setup, selected_host),
        ) as capacity:
            if (
                not (spec.env.get(ACCOUNT_ENV) or client._account())
                and client._accounts()
                and on._client_context().container is not None
            ):
                MODES[mode].preflight(
                    spec, client, registry=setup.registry, host=on, display=display
                )
            spec = _picked(setup, spec, client_name, client, on)
            running = MODES[mode].start(
                spec,
                client,
                registry=setup.registry,
                record=_session_line(spec.workdir),
                host=on,
                display=display,
                check_name=check_name,
                owner=owner,
            )
            try:
                capacity.started(running.state)
                if mode == Mode.TUI and name is not None:
                    cls._started(viewer, name, running, on, display)
                session = cls(
                    client_name,
                    mode,
                    running.prepared.spec,
                    info=running.prepared.info,
                    running=running,
                    host=on,
                    idle_timeout=idle_timeout,
                    client=client,
                )
            except BaseException:
                try:
                    running.close()
                finally:
                    capacity.release_if_absent(running.state)
                raise
            session._capacity = capacity
            session._capacity_hosts = {_host_key(setup, selected_host): on}
            session._account = _pool_account(spec, client, on)
            return session

    @classmethod
    def _started(
        cls, viewer: Viewer, name: str, running: BaseMode, on: Host, display: Display
    ) -> None:
        try:
            viewer.close(name, on)
            if on.attachable:
                viewer._prepare(name, on)
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
    ) -> Session:
        """Tui session `name`, from any process; its client record and hook log replayed into its
        reply and blocked, blocked too while its activity says so. A worker its session harness
        runs: that owner's control view; else the read-only attach of its worker state.
        """
        cls._config()
        tui.checked_name(name)
        _idle(idle_timeout)
        on = cls.host(host)
        state = tui.TuiMode.snapshot(name)
        if state.get("host") != on.name and name not in on.list():  # another table's, no alias
            raise SessionError(f"no tui session {name} on host {on.name}")
        if sid is not None and state.get("sid") != sid:
            raise SessionError(
                f"session {name}: expected sid {sid!r}, found {state.get('sid')!r} in session.json"
            )
        channel = channel_running(name, state)
        if channel is None:
            return cls._attached(name, sid=sid, host=host, idle_timeout=idle_timeout)
        attached = cls._attached(name, sid=sid, host=host, generation=state["generation"])
        return cls._view(
            channel,
            name=name,
            client=state["client"],
            sid=state["sid"],
            generation=state["generation"],
            host=host,
            idle_timeout=idle_timeout,
            replay=True,
            attached=attached,
        )

    @classmethod
    def _attached(
        cls,
        name: str,
        *,
        sid: str | None = None,
        host: str | None = None,
        idle_timeout: float | None = None,
        generation: str | None = None,
    ) -> HostedSession:
        """Holds no handle of its worker: its liveness from its owner, else its Host's
        worker_liveness. With `generation` (a view's): read-only, that generation only.
        """
        setup = cls._config()
        tui.checked_name(name)
        _idle(idle_timeout)
        on = cls.host(host)
        state = tui.TuiMode.snapshot(name)
        if generation is not None and state.get("generation") != generation:
            raise SessionError(f"session {name}: its worker {generation} is gone")
        try:
            client = setup.registry.client(state["client"])
        except ConfigError as e:
            raise SessionError(f"no tui session {name}: {e}") from None
        running = tui.TuiMode.attach(
            name,
            state,
            client,
            host=on,
            liveness=_liveness(name, state["generation"], on),
            sid=sid,
        )
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
        session._read_only = generation is not None
        for event in running.replayed:
            session._fold(event, replayed=True)
        session._capacity_hosts = {_host_key(setup, host or setup.hosts.default): on}
        if not session._blocked:
            session._blocked = running.marked_blocked()
        session.info()
        return session

    @classmethod
    def _view(
        cls,
        channel: Channel,
        *,
        name: str | None,
        client: str,
        sid: str,
        generation: str,
        host: str | None = None,
        idle_timeout: float | None = None,
        replay: bool = False,
        attached: Session | None = None,
        mode: Mode = Mode.TUI,
        spec: Spec | None = None,
        tie: Tie | None = None,
    ) -> OwnedSession:
        """The only way a view is built; a headless one's (`spec` its run's) holds its start's Tie."""
        retiring: Callable[[], None] | None = None
        if _terminal_less(mode):
            assert spec is not None
            unhosted = _Unhosted(cls, channel, replace(spec, sid=sid), mode)

            def attach() -> Session:
                return unhosted

            if (on := cls.host(host))._named_headless and name is not None:
                retiring = confirmed_end(on, name, generation)

        else:
            assert name is not None

            def attach() -> Session:
                return cls._attached(name, sid=sid, host=host, generation=generation)

            def retire() -> None:
                cls._retire(name, sid=sid, host=host, generation=generation)

            retiring = retire

        return OwnedSession(
            channel,
            name,
            client=client,
            sid=sid,
            generation=generation,
            idle_timeout=idle_timeout,
            attach=attach,
            attached=attached,
            retire=retiring,
            replay=replay,
            mode=mode,
            tie=tie,
            clock=cls._config().clock,
        )

    @classmethod
    def retire(cls, name: str, *, sid: str | None = None, host: str | None = None) -> None:
        """A bound owner's confirmed close (OwnerRefused, Unanswered), asked before any Host
        probe, else closed here.
        """
        cls._retire(name, sid=sid, host=host)

    @classmethod
    def _retire(
        cls,
        name: str,
        *,
        sid: str | None = None,
        host: str | None = None,
        generation: str | None = None,
    ) -> None:
        """generation: the caller's (a view's); while session.json is another's, or gone, nothing
        is asked or closed. Without an owner, an owned record goes by the record rule.
        """
        setup = cls._config()
        on = cls.host(host)
        store = tui.TuiStore.attach(name, host=on, sid=sid)
        try:
            state = store.read()
        except SessionError:
            state = None
        if generation is not None and (state is None or state.get("generation") != generation):
            return
        if state is not None and sid not in (None, state.get("sid")):
            raise SessionError(
                f"session {name}: expected sid {sid!r}, found {state.get('sid')!r} in session.json"
            )
        if state is not None and (channel := channel_bound(name, state)) is not None:
            store.check()
            if _asked_to_close(channel):
                return
        if state is None and generation is None and (found := on._headless_owner(name)):
            if sid not in (None, found["sid"]):
                raise SessionError(f"session {name}: expected sid {sid!r}, found {found['sid']!r}")
            channel = channel_bound(name, found)
            if channel is None or not _asked_to_close(channel):
                confirmed_end(on, name, found["generation"])()
            return
        try:
            clients = [store.read()["client"]]
        except SessionError:
            clients = setup.registry.configured()
        removed = store.close(generation=generation)
        try:
            if removed:
                cls.viewer().close(name, on)
        finally:
            for client in clients:
                cls._reconcile_capacity(client, {_host_key(setup, host or setup.hosts.default): on})

    @classmethod
    def _reconcile_capacity(cls, client: str, selected: Mapping[str, Host]) -> None:
        if not selected:
            return
        setup = cls._config()
        hosts = dict(selected)
        for name in setup.hosts.tables:
            key = _host_key(setup, name)
            if key not in hosts:
                try:
                    hosts[key] = cls.host(name)
                except (ConfigError, OSError, SessionError):
                    continue
        Admission.reconcile(client, hosts, required=next(iter(selected)))

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
    def resume_argv(cls, client: str, native: str) -> tuple[str, ...]:
        return cls._config().registry.client(client).resume_argv(native)

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
        if account := found._account():
            return pool[account]
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
        """Built once per class."""
        setup = cls._config()
        if not setup.viewers:
            setup.viewers.append(setup.viewer_factory(setup.viewer))
        return setup.viewers[0]

    @classmethod
    def host(cls, name: str | None = None) -> Host:
        """[hosts.<name>], built once per class."""
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
                    self._queue.extend(self._step(wait)[0])
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

    def _step(self, wait: float) -> tuple[list[SessionEvent | Stderr | ClientBound], bool]:
        if self._closed:
            return [], True
        found, self._closed = self._running.poll(wait)
        return [e for e in found if not isinstance(e, Exited)], self._closed

    def _record(self, event: SessionEvent | Stderr | ClientBound) -> None:
        if self._read_only:
            self._fold(event)
            return
        if isinstance(event, ClientBound):
            self._log.note(
                "client_session",
                sid=self.sid,
                client_id=event.client_id,
                durable=True,
            )
        elif isinstance(event, Stderr):
            self._log.note("stderr", best_effort=True, text=event.text)
        elif isinstance(event, LimitHit) and self._account is not None:
            accounts.hold(self.client, self._account, event.resets_at)
        self._fold(event)
        if isinstance(event, ClientBound):
            self._refresh_metadata()

    def _fold(self, event: SessionEvent | Stderr | ClientBound, *, replayed: bool = False) -> None:
        """What an event, live or replayed on attach, changes."""
        if isinstance(event, ClientBound):
            self._bound = True
            return
        self._reply, self._blocked = fold(
            self._reply, event, blocked=self._blocked, replayed=replayed
        )
        self._dialog = dialog(self._dialog, event)

    @property
    def opening_pending(self) -> str | None:
        self._running.state()
        return self._running.opening_pending

    @property
    def dialog(self) -> str | None:
        """By its yielded, then queued events."""
        found = self._dialog
        for event in self._queue:
            found = dialog(found, event)
        return found

    def send(self, text: str) -> None:
        self._running.send(text)

    def press(self, keys: Sequence[KeyPress]) -> None:
        self._running.press(tuple(keys))

    def alive(self) -> bool:
        return self._running.state() == Liveness.LIVE

    def client_alive(self) -> bool:
        return self._running.client_alive()

    def status(self) -> Status:
        """Its run's; a live tui one's state from its worker dir's activity, attach and hidden
        from its Viewer.
        """
        liveness, name = self._running.state(), self._tui()
        if liveness == Liveness.LIVE and name is not None:
            generation = self._running.generation
            word = None if generation is None else activity(name, generation)
            found = displays.status(self._viewer, self._host, name, self.sid, word)
            return replace(found, cwd=self._info.cwd) if found.alive else found
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
        self._release_capacity()

    def _release_capacity(self) -> None:
        if self._capacity is not None:
            self._capacity.release_if_absent(self._running.state)
        elif self.mode == Mode.TUI:
            type(self)._reconcile_capacity(self.client, self._capacity_hosts)

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
            if self._read_only:
                return
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
        try:
            try:
                close_view = None if name is None else self._viewer._capture_close(name, self._host)
            finally:
                kept = self._running.close() is False
            if close_view is not None and not kept:
                close_view()
        finally:
            self._release_capacity()

    def session_status(self) -> SessionStatus:
        info = self.info()
        found = _resumable(lambda: self._client, info, self._running.prepared.spec)
        resume = self._host_resume(info.cwd)
        return found if resume is None else replace(found, resume_command=resume)

    def _host_resume(self, cwd: str) -> str | None:
        return host_resume(self._host, self.name, cwd)

    def record(self) -> Record | None:
        self._running.state()
        name, generation = self._tui(), self._running.generation
        return None if name is None or generation is None else stored_record(name, generation)

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


class Owned:
    """The owner handle a session harness drives its worker through."""

    def __init__(self, session: HostedSession, host: str | None) -> None:
        self._session, self._host = session, host

    @property
    def name(self) -> str | None:
        return self._session.name

    @property
    def sid(self) -> str:
        return self._session.sid

    @property
    def generation(self) -> str:
        generation = self._session._running.generation
        assert generation is not None
        return generation

    @property
    def opening_pending(self) -> str | None:
        return self._session._running.opening_pending

    def pump(self, wait: float) -> tuple[list[SessionEvent | Stderr | ClientBound], bool]:
        return self._session._step(wait)

    def record(self, event: SessionEvent | Stderr | ClientBound) -> None:
        self._session._record(event)

    def stop(self, timeout: float = CLOSE_WAIT) -> list[SessionEvent | Stderr | ClientBound]:
        final = self._final()
        self._session.stop()
        return final + self._ending(timeout)

    def close(self, timeout: float = CLOSE_WAIT) -> list[SessionEvent | Stderr | ClientBound]:
        final = self._final()
        self._session.close()
        return final + self._ending(timeout)

    def _ending(self, timeout: float) -> list[SessionEvent | Stderr | ClientBound]:
        found: list[SessionEvent | Stderr | ClientBound] = []
        deadline = time.monotonic() + timeout
        while _terminal_less(self._session.mode) and (left := deadline - time.monotonic()) > 0:
            batch, ended = self._session._step(min(POLL, left))
            found += batch
            if ended:
                break
        return found

    def _final(self) -> list[SessionEvent | Stderr | ClientBound]:
        """Its last read; none once its channel failed, so the close still runs."""
        try:
            return self._session._step(0)[0]
        except SessionError:
            return []

    def returncode(self) -> int | None:
        return self._session._running.returncode(0)

    def confirm_end(self, timeout: float) -> int | None:
        """The exit code once its owner handle says its worker is gone (headless: once known)."""
        running = self._session._running
        deadline = time.monotonic() + timeout
        if self._session.mode == Mode.IN_PROCESS:
            return running.returncode(timeout)
        try:
            code = running.returncode(timeout)
            if _terminal_less(self._session.mode):
                return code
            while code is not None:
                if running.state() == Liveness.ABSENT:
                    return code
                if (left := deadline - time.monotonic()) <= 0:
                    return None
                time.sleep(min(POLL, left))
        except SessionError:
            pass
        return None

    @property
    def remote(self) -> bool:
        """Its worker is reachable only through this harness."""
        return self._session._running.remote

    @property
    def up(self) -> bool:
        return self._session._running.client_up

    def try_send(self, text: str, pending: Callable[[], bool]) -> bool | str:
        return self._session._running._try_send(text, pending)

    def press(self, keys: Sequence[KeyPress], pending: Callable[[], bool]) -> None:
        self._session._running._try_press(keys, pending)

    def screen(self, lines: int | None = None) -> str:
        return self._session.screen(lines)

    def redisplay(self) -> bool:
        """Its worker's display opened again where gone; False: none to open."""
        session = self._session
        if not isinstance(session._running, tui.TuiMode) or not session._host.attachable:
            return False
        viewer, name = session._shown("redisplay")
        return viewer._prepare(name, session._host)

    def view(self, channel: Channel, idle_timeout: float | None = None) -> OwnedSession:
        return type(self._session)._view(
            channel,
            name=self.name,
            client=self._session.client,
            sid=self.sid,
            generation=self.generation,
            host=self._host,
            idle_timeout=idle_timeout,
            mode=self._session.mode,
            spec=self._session._running.prepared.spec,
        )


class _Unhosted(Session):
    """A headless or in_process view's harness state and RunLog record; no terminal. stop, close:
    nothing.
    """

    def __init__(self, cls: type[HostedSession], channel: Channel, spec: Spec, mode: Mode):
        assert spec.client is not None and spec.sid is not None
        self.sid, self.client, self.mode, self.name = spec.sid, spec.client, mode, spec.name
        self._cls, self._channel, self._spec = cls, channel, spec

    def events(self) -> Iterator[SessionEvent | Stderr | IdleTimeout]:
        return iter(())

    def send(self, text: str) -> None:
        raise SessionError("send: tui mode only")

    def press(self, keys: Sequence[KeyPress]) -> None:
        raise SessionError("press: tui mode only")

    def alive(self) -> bool:
        return self._channel.alive() and self._channel.status().get("state") == "running"

    def status(self) -> Status:
        alive = self.alive()
        return Status(
            name=self.name or self.sid,
            alive=alive,
            state=StatusState.WORKING if alive else StatusState.DEAD,
            outcome=None,
            attach=None,
            hidden=False,
            cwd=self.info().cwd if alive else None,
        )

    def content(self) -> str:
        return self.screen()

    def stop(self) -> None:
        return None

    def info(self) -> SessionInfo:
        try:
            info = RunLog(self._spec.workdir).session(self.sid)
        except OSError as error:
            raise SessionError(f"session {self.sid}: {error}") from error
        if info is None:
            raise NoRecordedSession(f"no recorded session {self.sid} in {self._spec.workdir}")
        return info

    @property
    def reply(self) -> str | None:
        return None

    def close(self) -> None:
        return None

    def session_status(self) -> SessionStatus:
        return self._cls.inspect(self._spec)

    def record(self) -> Record | None:
        return None

    @property
    def showable(self) -> bool:
        return False

    def show(self, *, display: Display = None, command: str | None = None) -> str | None:
        raise NotShowable("show: tui mode only")

    def hide(self) -> None:
        raise NotShowable("hide: tui mode only")

    def attach_command(self) -> str:
        raise NotShowable("attach_command: tui mode only")

    def screen(self, lines: int | None = None) -> str:
        raise NotShowable("screen: tui mode only")
