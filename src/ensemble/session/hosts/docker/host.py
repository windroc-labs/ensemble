"""DockerHost: a broker-made container per session, its main process the guard (worker.py), reached
by `docker attach`.
"""

from __future__ import annotations

import contextlib
import fcntl
import functools
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from enum import StrEnum
from typing import NamedTuple

import ensemble.utils
from ensemble.session.engine import client_state
from ensemble.session.engine.daemon import (
    CLEARED,
    RUN_TIMEOUT,
    attach_env,
    capped,
    client_home,
    container_name,
    daemon_named,
    daemon_with_id,
    describe,
    docker_call,
    load_record,
    save_record,
    ssh_agent,
    state_volume,
)
from ensemble.session.engine.status_log import SessionStatusLog
from ensemble.session.engine.tmux_cli import display_error
from ensemble.session.engine.types import (
    COMMAND,
    PLUGIN_SOURCE,
    RESUME,
    SESSION_NAME,
    CapacityRow,
    ClientContext,
    ClientHome,
    ConfigError,
    Container,
    Daemon,
    DaemonName,
    DaemonRecord,
    EnvironmentSpec,
    HomeStore,
    Launch,
    Liveness,
    SessionBusy,
    SessionError,
    SessionRow,
    StatusState,
    Target,
    TuiSettings,
    Unanswered,
)
from ensemble.session.harness import guard
from ensemble.session.hosts.base import (
    HARNESS_ROOT,
    STAGED,
    TUI_DIR,
    TUI_STATE,
    Host,
    capacity_row,
    created_events,
    start_phase,
    tui_lock,
)
from ensemble.session.hosts.base import activity as worker_activity
from ensemble.session.hosts.docker import maintenance
from ensemble.session.hosts.docker.broker import (
    TIMEOUT,
    Broker,
    BrokerError,
    BrokerMissing,
    client_keys,
    plugin_root,
    write_manifest,
)
from ensemble.session.hosts.docker.hooks import HookWorker
from ensemble.session.hosts.docker.mounts import (
    KIND_LABEL,
    WORKSPACE_LABEL,
    state_dir,
    workspace_volume,
)
from ensemble.session.hosts.docker.types import GEN, Started
from ensemble.session.hosts.docker.worker import GUARD, DockerWorker, diagnostic_file
from ensemble.session.hosts.types import CommandView
from ensemble.utils import (
    DOCKER_STATE,
    Display,
    command_path,
    inode_lock,
    kill_group,
    module_roots,
    read_regular,
    save,
    source_path,
    timestamp,
)

RECORD = "host.json"
OWNER_LOCK = "guard.lock"
BUSY = "previous worker generation (live or unknown)"
IMAGE_PYTHON = "/opt/ensemble/venv/bin/python"
STATE_ROOT = "/run/ensemble"  # Container.state_root
_ARG_STRLEN_MAX = 131072  # Linux exec's limit on one argument, its NUL included
FILES_STAGE = """import json, os, sys
for path, text in json.load(sys.stdin).items():
    os.makedirs(os.path.dirname(path), 0o700, exist_ok=True)
    temp = os.path.join(os.path.dirname(path), '.tmp-' + os.urandom(8).hex())
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f: f.write(text)
    os.replace(temp, path)"""
LABEL = "ensemble.session"
PS_FORMAT = "\t".join(
    (
        "{{.Names}}",
        '{{.Label "ensemble.session"}}',
        '{{.Label "ensemble.generation"}}',
        "{{.State}}",
    )
)
STOPPED = frozenset({"created", "exited", "dead"})  # narrower than _row_liveness's not running
LOG = logging.getLogger("session")
KEPT = f"{DOCKER_STATE}/.kept"  # a marker per kept run volume
KEPT_LOCK = ".lock"
KEPT_RETRY = 24 * 3600  # seconds until a failed check is retried
VOLUME_FORMAT = '{{.Name}}\t{{.Label "' + WORKSPACE_LABEL + '"}}'
STATE_MAX = 1 << 20
STOPS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)


class Row(NamedTuple):
    """A `docker ps` row whose labels match its container name, and its daemon."""

    name: str
    container: str
    generation: str
    state: str
    daemon: Daemon


PS_FIELDS = 4


def _record(name: str) -> dict:
    try:
        with open(os.path.join(state_dir(name), RECORD), encoding="utf-8") as f:
            found = json.load(f)
    except (OSError, ValueError, RecursionError):
        return {}
    return found if isinstance(found, dict) else {}


class Kind(StrEnum):
    TUI = "tui"
    HEADLESS = "headless"


def _damaged_record(name: str) -> bool:
    if not os.path.lexists(os.path.join(state_dir(name), RECORD)):
        return False
    found = _record(name)
    recorded = found.get("generation")
    return (
        not isinstance(recorded, str)
        or not GEN.fullmatch(recorded)
        or "kind" in found
        and found["kind"] not in tuple(Kind)
    )


def _kind(name: str, generation: str) -> Kind | None:
    found = _record(name)
    kind = found.get("kind")
    return Kind(kind) if found.get("generation") == generation and kind in tuple(Kind) else None


def _guarded(name: str, generation: str) -> bool:
    return _kind(name, generation) is not None


def _owner_path(name: str) -> str:
    return os.path.join(state_dir(name), OWNER_LOCK)


def _take_owner(name: str) -> int | None:
    """guard.lock's fd, flocked for its worker's life; None while another holds it."""
    os.makedirs(state_dir(name), mode=0o700, exist_ok=True)
    fd = os.open(_owner_path(name), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _save_record(name: str, data: Mapping) -> None:
    save(os.path.join(state_dir(name), RECORD), json.dumps(dict(data)), durable=True)


def _lock_free(path: str) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


def _state_file(name: str) -> str:
    return os.path.join(os.path.expanduser(TUI_DIR), name, TUI_STATE)


def _check_history(name: str, home: str, client: str, transcript: str, image: str) -> None:
    """A resume's history, read with the image it built."""
    missing = f"docker session {name}: no native history to resume in {home}"
    try:
        path = client_state.home_path(home, os.path.join(home, transcript))
    except OSError:
        raise ConfigError(missing) from None
    try:
        store = client_state.VolumeHome(name, client, image=image)
        present = store.read(path, limit=0) is not None
    except OSError as error:
        raise ConfigError(f"{missing}: {error.strerror or error}") from None
    if not present:
        raise ConfigError(missing)


def _report_plan(name: str, plan: Mapping) -> None:
    """Saves a generation's plan beside its manifest; prints it to stderr."""
    try:
        save(
            os.path.join(state_dir(name), f"{plan['generation']}.plan.json"),
            json.dumps(plan),
            durable=True,
        )
    except OSError as error:
        raise SessionError(f"docker session {name}: plan: {error}") from None
    mounts = ", ".join(f"{mount['path']} rw ({mount['volume']})" for mount in plan["mounts"])
    workspace = plan["workspace"]
    for part in (
        f"image {plan['image']} {plan['image_digest']}",
        f"workspace {workspace['volume']} at {workspace['path']} rw",
        f"mounts {mounts}",
        f"credential {plan['credential']}",
    ):
        print(f"docker plan {name} {plan['generation']}: {part}", file=sys.stderr)


def _parsed_environment(name: str, value) -> EnvironmentSpec | None:
    """From metadata's JSON or host.json's object."""
    if value is None:
        return None
    try:
        return EnvironmentSpec.from_fields(json.loads(value) if isinstance(value, str) else value)
    except (ValueError, RecursionError, ConfigError) as error:
        raise SessionError(f"docker session {name}: bad environment: {error}") from None


def _holder_text(found: Mapping[str, str]) -> str:
    return f"{found['container']} (owner {found['owner']})"


class _Stops:
    """Defers non-ignored STOPS while a start owns its container, so its rollback runs; re-raised on
    exit. Catches, not blocks, so no child inherits them blocked; main thread only.
    """

    def __enter__(self) -> _Stops:
        self.received: list[int] = []
        main = threading.current_thread() is threading.main_thread()
        self.previous = {
            sig: signal.signal(sig, lambda signum, _: self.received.append(signum))
            for sig in STOPS
            if main and signal.getsignal(sig) is not signal.SIG_IGN
        }
        return self

    def __exit__(self, *_) -> None:
        for sig, handler in self.previous.items():
            signal.signal(sig, handler)
        if self.received:
            signal.raise_signal(self.received[0])

    def check(self, prefix: str = "") -> None:
        if self.received:
            raise SessionError(f"{prefix}stopped by {signal.Signals(self.received[0]).name}")


class _Container(NamedTuple):
    container: str
    generation: str
    hook: str
    image: str
    cwd: str
    started: Started
    began: str
    ended: str
    seeds: Mapping[str, str]  # a new start's home files, by container path
    daemon: Daemon


def _save_result(
    name: str, created: _Container, phases: Mapping[str, float], launched: str | None = None
) -> None:
    result = {
        "container": created.started.container,
        "image": created.image,
        "image_digest": created.started.image_digest,
        "client_versions": dict(created.started.clients),
        "started_at": created.began,
        "ended_at": created.ended,
        "phases": dict(phases),
        **({} if launched is None else {"launched_at": launched}),
    }
    save(
        os.path.join(state_dir(name), f"{created.generation}.result.json"),
        json.dumps(result),
        durable=True,
    )


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class _Laps:
    """Seconds per phase, since the previous one ended."""

    def __init__(self) -> None:
        self.phases: dict[str, float] = {}
        self._last = time.monotonic()

    def __call__(self, phase: str) -> None:
        now = time.monotonic()
        self.phases[phase] = round(now - self._last, 2)
        self._last = now

    def report(self, head: str) -> None:
        for phase, seconds in self.phases.items():
            print(f"{head}: {phase} {seconds:.2f} s", file=sys.stderr)


def _accept_started(name: str, generation: str, started: Started, container: str) -> None:
    """Checks the broker started `container`; saves its deadline for the credential tick."""
    if started.container != container:
        raise SessionError(f"broker: started {started.container}, want {container}")
    save(
        maintenance.deadline_file(state_dir(name), generation),
        json.dumps(started.deadline),
        durable=True,
    )


def _refusal(name: str, action: str, recorded: Daemon, target: Target) -> str:
    current = daemon_named(target, target.default) or recorded
    return (
        f"docker session {name}: cannot {action} it here: its Docker target is"
        f" {describe(recorded)} (daemon {recorded.id}), the current target is"
        f" {describe(current)} (daemon {current.id or 'unknown'}); set the Docker target back to"
        f" {recorded.endpoint} to {action} it"
    )


def _unreachable(daemon: Daemon) -> BrokerError:
    """The broker's docker_unreachable error for `daemon`, as the target command reported it."""
    message = f"Cannot reach Docker target {daemon.endpoint}: {daemon.unreachable}."
    return BrokerError(7, "docker_unreachable", message, "not_needed", None)


def _reached(target: Target, daemon_id: str) -> Daemon | None:
    """The reached daemon of `daemon_id`; while one daemon cannot be reached, a miss raises its
    docker_unreachable; None: no configured daemon has that ID.
    """
    if (hit := daemon_with_id(target, daemon_id)) is not None:
        return hit
    if (down := next((d for d in target.daemons if d.id is None), None)) is not None:
        raise _unreachable(down)
    return None


def _kept_marker(daemon: Daemon, volume: str) -> str:
    """A kept volume's marker, per daemon."""
    return f"{volume}@{daemon.id}"


def _sessions_at(anchor: str) -> list[str]:
    """The sessions whose latest generation's manifest has cwd `anchor`."""
    root = os.path.expanduser(DOCKER_STATE)
    try:
        names = sorted(n for n in os.listdir(root) if SESSION_NAME.fullmatch(n))
    except FileNotFoundError:
        return []
    found = []
    for name in names:
        generation = _record(name).get("generation")
        if not isinstance(generation, str) or not GEN.fullmatch(generation):
            continue
        try:
            with open(os.path.join(state_dir(name), f"{generation}.json"), encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(manifest, dict) and manifest.get("cwd") == anchor:
            found.append(name)
    return found


def _no_daemon(down: Iterable[Daemon]) -> str:
    return f"no Docker CLI, or Docker target {', '.join(d.endpoint for d in down)} unreachable"


def _session_rows(listed: Iterable[tuple[Daemon, list[str]]]) -> list[Row]:
    """(daemon, ps row)s as Rows."""
    return [
        Row(name, container, generation, state, daemon)
        for daemon, (container, name, generation, state) in (
            (daemon, row) for daemon, row in listed if len(row) == PS_FIELDS
        )
        if SESSION_NAME.fullmatch(name)
        and GEN.fullmatch(generation)
        and container == container_name(name, generation)
    ]


def _tui_rows(rows: Iterable[Row]) -> dict[str, Row]:
    return {row.name: row for row in rows if _kind(row.name, row.generation) == Kind.TUI}


def _held(marker: str) -> bool:
    """A failed check holds its volume for KEPT_RETRY, any other marker for good."""
    try:
        with open(marker) as f:
            failed = f.read() == "failed"
        return not failed or time.time() - os.stat(marker).st_mtime < KEPT_RETRY
    except FileNotFoundError:
        return False


class DockerHost(Host):
    """settings: docker CLI absolute path and pinned plugin sources installed in its image."""

    _inherit_env = False  # the container gets only the launch's own env
    attachable = True
    _environments = True
    _remote_tui = True  # as DockerWorker.remote
    _named_headless = True

    def __init__(
        self,
        name: str,
        settings: Mapping | None = None,
        *,
        run=capped,
        broker: Broker | None = None,
    ) -> None:
        super().__init__(name, settings)
        if extra := sorted(set(self.settings) - {"docker", "plugins"}):
            removed = "; delete it: workspaces are volumes" if extra[0] == "mount_roots" else ""
            raise ConfigError(f"unknown key {extra[0]!r} in [hosts.{name}]{removed}")
        self.docker = self.settings.get("docker")
        if not isinstance(self.docker, str) or not os.path.isabs(self.docker):
            raise ConfigError(f"[hosts.{name}] docker: want an absolute path")
        plugins = self.settings.get("plugins", [])
        if (
            not isinstance(plugins, list)
            or not all(isinstance(p, str) and PLUGIN_SOURCE.fullmatch(p) for p in plugins)
            or len(set(plugins)) != len(plugins)
        ):
            raise ConfigError(
                f"[hosts.{name}] plugins: want unique name@owner/repo#40-hex-commit sources, optionally ::owner/repo#40-hex-commit"
            )
        self.plugins: tuple[str, ...] = tuple(plugins)
        self.run = run
        self.broker = Broker() if broker is None else broker
        self._workers: dict[str, DockerWorker] = {}
        self._found: Target | None = None

    def _target(self) -> Target:
        """The broker's target, asked once per Host."""
        if self._found is None:
            self._found = self.broker.target()
        return self._found

    def _beside(self, down: tuple[Daemon, ...]) -> tuple[Daemon, ...]:
        """The unreachable daemons of two: with one, it stays silent as before."""
        return down if down and len(self._target().daemons) > 1 else ()

    def tui_settings(self) -> TuiSettings:
        return replace(super().tui_settings(), status_line=True, badge="🐳")

    def _inventory(
        self, *, required: bool = False
    ) -> tuple[list[tuple[Daemon, list[str]]], tuple[Daemon, ...]]:
        """(daemon, ps row)s, and the daemons that did not answer: those the target could not
        reach are not asked. Admission needs one answer: a worker on an unreachable daemon loses
        its heartbeat and ends.
        """
        try:
            daemons = self._target().daemons
        except BrokerMissing as error:
            if required:
                raise SessionError(f"host {self.name}: inventory unavailable ({error})") from None
            return [], ()
        listed: list[tuple[Daemon, list[str]]] = []
        down = []
        for daemon in daemons:
            try:
                rows = None
                if daemon.id is not None:
                    rows = self._listed(
                        *("ps", "-a", "--filter", f"label={LABEL}", "--format", PS_FORMAT),
                        daemon=daemon,
                    )
            except Unanswered as error:
                if len(daemons) == 1:
                    raise
                LOG.warning("docker %s: %s", daemon.endpoint, error)
                rows = None
            if rows is None:
                down.append(daemon)
            else:
                listed.extend((daemon, row) for row in rows)
        if required and len(down) == len(daemons):
            raise SessionError(f"host {self.name}: inventory unavailable ({_no_daemon(down)})")
        return listed, tuple(down)

    def _ps(self, *, required: bool = False) -> list[tuple[Daemon, list[str]]]:
        return self._inventory(required=required)[0]

    def _listed(self, *args: str, daemon: Daemon) -> list[list[str]] | None:
        """Tab-separated rows; None without a CLI; Unanswered on failure."""
        argv, env = docker_call(self.docker, *args, daemon=daemon)
        try:
            res = self.run(argv, env, timeout=RUN_TIMEOUT)
        except FileNotFoundError:
            return None
        except (OSError, subprocess.TimeoutExpired) as error:
            raise Unanswered(f"docker {args[0]}: {error}") from None
        if res.returncode:
            raise Unanswered(f"docker {args[0]}: {res.stderr.strip()}")
        return [line.split("\t") for line in res.stdout.splitlines()]

    def _rows(self, *, required: bool = False) -> list[Row]:
        return _session_rows(self._ps(required=required))

    def _containers(self, *, required: bool = False) -> dict[str, Row]:
        return _tui_rows(self._rows(required=required))

    def sweep(self, own: str) -> None:
        """Closes rows whose owner lock is free, and `own`'s debris; others only under their tui
        lock, untaken if busy (`own`'s caller holds its).
        """
        try:
            listed, down = self._inventory()
        except SessionError as error:
            LOG.warning("docker sweep: %s", error)
            return
        for daemon in self._beside(down):
            LOG.warning("docker sweep: %s unreachable; its containers stay", describe(daemon))
        for row in _session_rows(listed):
            try:
                if row.name == own:
                    self._sweep_row(row, own=True)
                    continue
                lock = tui_lock(row.name)
                with inode_lock(lock, wait=False) as held:
                    if held:
                        try:
                            self._sweep_row(row, own=False)
                        finally:
                            if not os.path.lexists(_state_file(row.name)):
                                os.unlink(lock)
            except (SessionError, OSError) as error:
                LOG.warning("docker sweep %s: %s", row.name, error)

    def _spawn_reclaim(self) -> None:
        """Detached, so broker checks outlive the caller without delaying it."""
        try:
            state = os.path.expanduser(DOCKER_STATE)
            os.makedirs(state, mode=0o700, exist_ok=True)
            fd = os.open(
                os.path.join(state, "reclaim.log"),
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(fd, "a") as log:
                roots = module_roots(sys.modules[__name__], ensemble.utils)
                proc = subprocess.Popen(
                    [os.path.realpath(sys.executable), "-P", "-s", "-m", __name__, self.docker],
                    env={
                        "PATH": os.defpath,
                        "HOME": os.path.expanduser("~"),
                        **source_path(roots),
                        **ssh_agent(),
                    },
                    cwd="/",
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    start_new_session=True,
                )
            threading.Thread(target=proc.wait, daemon=True).start()
        except OSError as error:
            LOG.warning("docker reclaim: %s", error)

    def reclaim_volumes(self) -> None:
        """Removes run workspace volumes whose workdir is gone, after the broker's check; a kept
        one is marked and warned about once. Lists before locking, so no orphan writes no file.
        """
        try:
            self._reclaim()
        except (SessionError, OSError) as error:
            LOG.warning("docker reclaim: %s", error)

    def _workspaces(self, daemon: Daemon) -> list[tuple[str, str]] | None:
        """Run workspace volumes with their cwd label; None without a reachable daemon or a CLI."""
        if daemon.id is None:
            return None
        rows = self._listed(
            "volume",
            "ls",
            "--filter",
            f"label={KIND_LABEL}",
            "--format",
            VOLUME_FORMAT,
            daemon=daemon,
        )
        return None if rows is None else [(row[0], row[1]) for row in rows if len(row) == 2]

    def remove_workspaces(self, cwd: str) -> list[str]:
        """Every volume labelled realpath(cwd), on its session's daemon, unsaved work included;
        at a refusal, the rest kept.
        """
        anchor, target = os.path.realpath(cwd), self._target()
        found: list[tuple[Daemon, str]] = []
        for daemon in target.daemons:
            rows = self._workspaces(daemon)
            if rows is None:
                raise SessionError(
                    f"host {self.name}: workspaces unknown ({_no_daemon((daemon,))})"
                )
            found.extend(
                (daemon, volume)
                for volume, at in rows
                if at == anchor
                and self._session_daemon(volume.removeprefix(workspace_volume(""))).id == daemon.id
            )
        for name in _sessions_at(anchor):
            recorded = self._session_daemon(name)
            if daemon_with_id(target, recorded.id or "") is None:
                found.append((recorded, workspace_volume(name)))
        removed = []
        for daemon, volume in sorted(found, key=lambda pair: (pair[0].endpoint, pair[1])):
            try:
                if self.broker.workspace_rm(volume, force=True, daemon=daemon.id or ""):
                    removed.append(volume)
            except SessionError as error:
                raise SessionError(f"docker workspace {volume} kept: {error}") from None
        return removed

    def _reclaim(self) -> None:
        """Each daemon that answers; an unreachable one's volumes and markers stay."""
        daemons = self._target().daemons
        listed = {}
        for daemon in daemons:
            try:
                rows = self._workspaces(daemon)
            except Unanswered as error:
                LOG.warning("docker reclaim: %s", error)
                continue
            if rows is not None:
                listed[daemon] = rows
        gone = [
            (daemon, volume)
            for daemon, rows in listed.items()
            for volume, anchor in rows
            if os.path.isabs(anchor) and not os.path.lexists(anchor)
        ]
        kept = os.path.expanduser(KEPT)
        if not gone and not os.path.isdir(kept):
            return
        os.makedirs(kept, mode=0o700, exist_ok=True)
        with inode_lock(os.path.join(kept, KEPT_LOCK), wait=False) as held:
            if not held:
                return
            if len(listed) == len(daemons):
                markers = {_kept_marker(d, row[0]) for d, rows in listed.items() for row in rows}
                for marker in set(os.listdir(kept)) - markers - {KEPT_LOCK}:
                    os.unlink(os.path.join(kept, marker))
            for daemon, volume in gone:
                marker = os.path.join(kept, _kept_marker(daemon, volume))
                if _held(marker):
                    continue
                try:
                    self.broker.workspace_rm(volume, daemon=daemon.id or "")
                except BrokerError as error:
                    save(marker, "dirty" if error.code == "workspace_dirty" else "failed")
                    LOG.warning("docker workspace %s kept: %s", volume, error.message)
                except SessionError as error:
                    save(marker, "failed")
                    LOG.warning("docker workspace %s kept: %s", volume, error)
                else:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(marker)

    def _sweep_row(self, row: Row, *, own: bool) -> None:
        if _damaged_record(row.name):
            return
        if _lock_free(_owner_path(row.name)) or own and not _guarded(row.name, row.generation):
            self.broker.close(row.container, daemon=row.daemon.id or "")

    def _client_context(self) -> ClientContext:
        return replace(
            super()._client_context(),
            host_address="host.docker.internal",
            container=Container(
                "/opt/ensemble/bin",
                IMAGE_PYTHON,
                "/opt/ensemble/src",
                STATE_ROOT,
            ),
        )

    def _client_env(
        self, name: str, client: str, home: ClientHome | None, *, resume: bool
    ) -> Mapping[str, str]:
        if home is None:
            raise ConfigError(
                f"client {client or '(unnamed)'}: not supported on the Docker Host yet"
            )
        return {home.env: client_home(home.dir)}

    def _home_store(self, name: str, home: ClientHome | None) -> HomeStore:
        if home is None:
            raise ConfigError("client home: not supported on the Docker Host yet")
        return client_state.VolumeHome(name, home.dir)

    def _resume_argv(self, name: str) -> tuple[str, ...]:
        return (*RESUME, name)

    def _preflight(
        self, name: str, launch: Launch, metadata: Mapping[str, str] | None = None
    ) -> None:
        """With an environment: cwd and handshake; start does lock and credential checks."""
        resumed = self._check_resume(name, metadata)
        self._credentials(name, metadata or {})
        environment = self._environment(name, metadata)
        if environment is None:
            self._prebuild(name, launch, resumed or self._choose(None), missing_ok=True)
            return
        laps = _Laps()
        self._check_cwd(name, launch, missing_ok=True)
        self.broker.version()
        laps("preflight")
        laps.report(f"docker timing {name}")

    def _validate(
        self, name: str, launch: Launch, metadata: Mapping[str, str] | None = None
    ) -> None:
        """With an environment: cwd and handshake; the start builds and locks."""
        resumed = self._check_resume(name, metadata)
        self._credentials(name, metadata or {})
        if self._environment(name, metadata) is None:
            self._prebuild(name, launch, resumed or self._choose(None))
            return
        self._check_cwd(name, launch)
        self.broker.version()

    def _credentials(self, name: str, metadata: Mapping[str, str]) -> dict[str, str]:
        record = _record(name)
        resumed = metadata.get("resume") == "1" and record.get("sid") == metadata.get("sid")
        values = {}
        for key in ("credentials", "environment_credentials"):
            given = metadata.get(key)
            value = record.get(key) if resumed else given
            if resumed and given is not None and given != value:
                raise ConfigError(
                    f"docker session {name}: resume: {key} differs from the one it was started with"
                )
            if value is not None:
                values[key] = value
        return values

    def _environment(self, name: str, metadata: Mapping[str, str] | None) -> EnvironmentSpec | None:
        """A resume's is its stored one: taken if none is given, refused if the given one
        differs but in prefetch.
        """
        given = _parsed_environment(name, (metadata or {}).get("environment"))
        record = _record(name)
        if (metadata or {}).get("resume") != "1" or record.get("sid") != (metadata or {}).get(
            "sid"
        ):
            return given
        stored = _parsed_environment(name, record.get("environment"))
        if given is None:
            return stored
        if stored is None or replace(given, prefetch=None) != replace(stored, prefetch=None):
            raise ConfigError(
                f"docker session {name}: resume: environment differs from the one it was started with"
            )
        return stored

    def validate(self, name: str, launch: Launch, *, missing_ok: bool = False) -> None:
        """The preflight, before any container: cwd (`missing_ok`: or none yet) and build."""
        self._prebuild(name, launch, self._choose(None), missing_ok=missing_ok)

    def _prebuild(
        self, name: str, launch: Launch, daemon: Daemon, *, missing_ok: bool = False
    ) -> None:
        """validate, built on `daemon`."""
        laps = _Laps()
        self._check_cwd(name, launch, missing_ok=missing_ok)
        self._image(name, daemon=daemon)
        laps("preflight")
        laps.report(f"docker timing {name}")

    def _check_cwd(self, name: str, launch: Launch, *, missing_ok: bool = False) -> None:
        super().validate(name, launch)
        for i, arg in enumerate(launch.argv):
            if (size := len(arg.encode("utf-8"))) + 1 > _ARG_STRLEN_MAX:
                raise ConfigError(
                    f"docker session {name}: argument {i} is {size} bytes, over the "
                    f"{_ARG_STRLEN_MAX}-byte limit Linux exec allows one argument"
                )
        cwd = os.path.realpath(launch.cwd)
        if not os.path.isdir(cwd) and not (missing_ok and not os.path.lexists(cwd)):
            raise ConfigError(f"Docker cwd {cwd}: no such directory")

    def _image(
        self,
        name: str,
        environment: EnvironmentSpec | None = None,
        *,
        daemon: Daemon,
    ) -> tuple[str, str, tuple[str, str]]:
        """(hook path, plugin root, (image, digest)); an environment's image and plugins added."""
        hook = command_path(COMMAND)
        if hook is None:
            raise ConfigError(f"docker session {name}: {COMMAND} is not on PATH (its hooks run it)")
        self.broker.version()
        root = plugin_root(hook)
        with start_phase("building the worker image", TIMEOUT):
            if environment is None:
                return (
                    hook,
                    root,
                    self.broker.build(root, daemon=daemon.id or "", plugins=self.plugins),
                )
            sources = [p for p in environment.plugins if PLUGIN_SOURCE.fullmatch(p)]
            built = self.broker.build(
                root,
                plugins=(*self.plugins, *(p for p in sources if p not in self.plugins)),
                base_image=None
                if environment.image == EnvironmentSpec.WORKER
                else environment.image,
                image_plugins=tuple(p for p in environment.plugins if p not in sources),
                daemon=daemon.id or "",
            )
        return hook, root, built

    def _choose(self, environment: EnvironmentSpec | None) -> Daemon:
        """A new session's daemon: its environment's, else the target's default."""
        target = self._target()
        name = environment.daemon if environment and environment.daemon else target.default
        daemon = daemon_named(target, name)
        if daemon is None:
            raise SessionError(f"host {self.name}: no {name} Docker daemon is configured")
        if daemon.id is None:
            raise _unreachable(daemon)
        return daemon

    def _recorded(self, name: str) -> DaemonRecord | None:
        try:
            return load_record(name)
        except OSError as error:
            raise SessionError(f"docker session {name}: daemon record: {error}") from None

    def _session_daemon(self, name: str) -> Daemon:
        """Session `name`'s daemon as recorded; with no record, the local Docker Desktop."""
        recorded = self._recorded(name)
        if recorded is not None:
            return recorded.daemon
        local = daemon_named(self._target(), DaemonName.LOCAL)
        if local is None:
            raise SessionError(f"host {self.name}: no local Docker daemon is configured")
        return local

    def _resumed(self, name: str) -> Daemon:
        """A resume's daemon, its record's (with none, the local Docker Desktop's), refused when
        no configured daemon has its ID.
        """
        recorded = self._session_daemon(name)
        if recorded.id is None:
            raise _unreachable(recorded)
        target = self._target()
        found = _reached(target, recorded.id)
        if found is None:
            raise ConfigError(_refusal(name, "resume", recorded, target))
        return found

    def _check_resume(self, name: str, metadata: Mapping[str, str] | None) -> Daemon | None:
        """A resume's daemon, before any build; None for a new start."""
        if (metadata or {}).get("resume") == "1":
            return self._resumed(name)
        return None

    def _move_workspace(self, name: str, volume: str, cwd: str, daemon: Daemon) -> None:
        """Deletes a new start's workspace volume made at another cwd, unless it has unsaved work."""
        anchor = next((a for v, a in self._workspaces(daemon) or () if v == volume), cwd)
        if anchor == cwd:
            return
        try:
            self.broker.workspace_rm(volume, daemon=daemon.id or "")
        except BrokerError as error:
            if error.code == "workspace_dirty":
                raise ConfigError(
                    f"docker session {name}: {error.message}; or start it in {anchor} again"
                ) from error
            raise

    def _container(
        self,
        name: str,
        launch: Launch,
        stops: _Stops,
        laps: _Laps,
        metadata: Mapping[str, str],
        *,
        environment: EnvironmentSpec | None,
    ) -> _Container:
        """Checks, image, workspace, home and plan, then the start; rolled back on transport failure."""
        generation, field, home_dir, credential = (
            metadata.get(key) for key in ("generation", "home_field", "home_dir", "credential")
        )
        if not (generation and field and home_dir and credential):
            raise SessionError(f"docker session {name}: no client home to mount")
        resume = metadata.get("resume")
        if resume not in ("0", "1"):
            raise SessionError(f"docker session {name}: bad resume flag {resume!r:.20}")
        self.sweep(name)
        for _, row in self._ps():
            if row[1:2] == [name]:
                if len(row) == PS_FIELDS and _guarded(name, row[2]):
                    raise SessionError(
                        f"docker session {name}: a container for it exists; close {name} first"
                    )
                if (
                    len(row) == PS_FIELDS
                    and GEN.fullmatch(row[2])
                    and row[0] == container_name(name, row[2])
                ):
                    raise SessionError(
                        f"docker session {name}: container {row[0]} of an earlier start is still"
                        " up (its removal failed; see the log); the next start retries it"
                    )
                raise SessionError(
                    f"docker session {name}: container {row[0]} is labelled for it but not named"
                    f" as this Host names one; remove it: docker rm -f {row[0]}"
                )
        home = client_home(home_dir)
        recorded = None if resume == "0" else self._recorded(name)
        daemon = self._resumed(name) if resume == "1" else self._choose(environment)
        if recorded is None:
            try:
                save_record(name, DaemonRecord(daemon, self.docker))
            except OSError as error:
                raise SessionError(f"docker session {name}: daemon record: {error}") from None
        laps("checks")
        hook, root, (image, digest) = self._image(name, environment, daemon=daemon)
        if resume == "1":
            _check_history(name, home, home_dir, launch.transcript, digest)
        volume = workspace_volume(name)
        cwd = os.path.realpath(launch.cwd)
        persistent = json.loads(metadata.get("home_persistent") or "null")
        files = json.loads(metadata.get("home_files") or "{}")
        for file in files if persistent is not None else ():
            if file.split("/", 1)[0] not in persistent or "/" not in file:
                raise SessionError(
                    f"docker session {name}: home file {file} is outside its kept dirs"
                )
        if resume == "0":
            self._move_workspace(name, volume, cwd, daemon)
        seeds = {os.path.join(home, f): text for f, text in files.items()} if resume == "0" else {}
        clients = client_keys(root)
        manifest = os.path.join(state_dir(name), f"{generation}.json")
        write_manifest(
            manifest,
            name=name,
            generation=generation,
            daemon=daemon.id or "",
            image=image,
            cwd=cwd,
            workspace=volume,
            state="new" if resume == "0" else "keep",
            home=(field, home),
            hook_path=hook,
            providers=tuple(json.loads(metadata.get("home_providers") or "[]")),
            account=metadata.get("home_account", ""),
            credentials=metadata.get("credentials"),
            environment_credentials=metadata.get("environment_credentials"),
            **(
                {
                    "gateway": environment.gateway,
                    "lock": environment.lock,
                    "lock_owner": name,
                    "prefetch": environment.prefetch,
                }
                if environment is not None
                else {}
            ),
        )
        _report_plan(
            name,
            {
                "name": name,
                "generation": generation,
                "image": image,
                "image_digest": digest,
                "workspace": {"volume": volume, "path": cwd},
                "mounts": [
                    {
                        "path": os.path.join(home, directory),
                        "rw": True,
                        "volume": state_volume(name),
                    }
                    for directory in persistent
                ]
                if persistent is not None
                else [{"path": home, "rw": True, "volume": state_volume(name)}],
                "credential": credential,
            },
        )
        laps("image")
        container = container_name(name, generation)
        stops.check(f"docker session {name}: ")
        began = timestamp()
        started = None
        try:
            started = self.broker.start(manifest, clients=clients)
            _accept_started(name, generation, started, container)
            ended = timestamp()
            laps("start")
            created = _Container(
                container, generation, hook, image, cwd, started, began, ended, seeds, daemon
            )
            _save_result(name, created, laps.phases)
        except BrokerError as error:
            if error.code == "busy" and error.holder and environment and environment.lock:
                raise SessionBusy(environment.lock, _holder_text(error.holder)) from error
            if error.cleanup == "failed" and error.container:
                with contextlib.suppress(SessionError):
                    self.broker.close(error.container, daemon=daemon.id or "")
            raise
        except (SessionError, OSError) as error:
            cleanup = self._roll_back(container, daemon)
            raise SessionError(f"docker session {name}: {error}{cleanup}") from error
        return created

    def worker(
        self,
        name: str,
        launch: Launch,
        *,
        terminal: bool,
        display: Display = None,
        events: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> DockerWorker:
        """A direct (headless) start holds the tui lock, its file removed unless a session.json
        names it; an owned one's (metadata `owner`) TuiStore holds it.
        """
        if (metadata or {}).get("owner"):
            return self._worker(
                name, launch, terminal=terminal, display=display, events=events, metadata=metadata
            )
        lock = tui_lock(name)
        os.makedirs(os.path.dirname(lock), mode=0o700, exist_ok=True)
        with inode_lock(lock, wait=False) as held:
            if not held:
                raise SessionError(f"docker session {name}: busy")
            try:
                return self._worker(
                    name,
                    launch,
                    terminal=terminal,
                    display=display,
                    events=events,
                    metadata=metadata,
                )
            finally:
                if not os.path.lexists(_state_file(name)):
                    os.unlink(lock)

    def _worker(
        self,
        name: str,
        launch: Launch,
        *,
        terminal: bool,
        display: Display,
        events: str | None,
        metadata: Mapping[str, str] | None,
    ) -> DockerWorker:
        """Holds the owner lock; a live or unknown earlier guarded generation refuses (SessionBusy),
        an ended one is removed first.
        """
        if why := display_error(display):
            raise ConfigError(why)
        for key in launch.env:
            if key in CLEARED or key.startswith("DOCKER_"):
                raise ConfigError(
                    f"docker session {name}: env {key} would reconfigure the docker CLI"
                )
        self._check_cwd(name, launch)
        events = created_events(events)
        metadata = metadata or {}
        metadata = {**metadata, **self._credentials(name, metadata)}
        environment = self._environment(name, metadata)
        owner = _take_owner(name)
        if owner is None:
            raise SessionBusy(name, BUSY)
        made: list[DockerWorker] = []  # once built, the handle alone owns `owner`
        try:
            if _damaged_record(name):
                raise SessionBusy(name, f"damaged record: {os.path.join(state_dir(name), RECORD)}")
            for row in self._rows():
                if row.name == name and _guarded(name, row.generation):
                    if self._row_liveness(name, row.generation) != Liveness.ABSENT:
                        raise SessionBusy(name, BUSY)
                    self.broker.close(row.container, daemon=row.daemon.id or "")
            with _Stops() as stops:
                handle = self._guard(
                    name,
                    launch,
                    stops,
                    owner=owner,
                    made=made,
                    terminal=terminal,
                    display=display,
                    events=events,
                    metadata=metadata,
                    environment=environment,
                )
        except BaseException:
            if not made:
                os.close(owner)
            raise
        return handle

    def _guard(
        self,
        name: str,
        launch: Launch,
        stops: _Stops,
        *,
        owner: int,
        made: list[DockerWorker],
        terminal: bool,
        display: Display,
        events: str | None,
        metadata: Mapping[str, str],
        environment: EnvironmentSpec | None,
    ) -> DockerWorker:
        laps = _Laps()
        created = self._container(name, launch, stops, laps, metadata, environment=environment)
        container, generation, hook, _, _, _, _, _, seeds, daemon = created
        handle = proc = None
        try:
            stops.check()
            self._stage_files(container, metadata, daemon)
            self._stage_staged_files(container, metadata, seeds, daemon)
            _save_record(
                name,
                {
                    "generation": generation,
                    "sid": metadata.get("sid"),
                    "client": metadata.get("client"),
                    "display": display,
                    "record": None,
                    "events": events,
                    "kind": Kind.TUI if terminal else Kind.HEADLESS,
                    "environment": None if environment is None else environment.persisted(),
                    **{
                        key: metadata[key]
                        for key in ("credentials", "environment_credentials")
                        if key in metadata
                    },
                    **({} if terminal else {"owner": metadata.get("harness"), "host": self.name}),
                },
            )
            handoff = {
                "argv": [
                    *("with-secrets", "--client", metadata["home_field"].removesuffix("_dir")),
                    *launch.argv,
                ],
                "env": {k: v for k, v in launch.env.items() if k != "PATH"}
                | dict(created.started.env),
                "terminal": terminal,
                "generation": generation,
                "size": "{}x{}".format(*self.tui_settings().hidden_size),
            }
            argv, docker_env = docker_call(
                self.docker, "attach", "--sig-proxy=false", container, daemon=daemon
            )
            proc = subprocess.Popen(
                argv,
                env=docker_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                bufsize=0,
            )
            assert proc.stdin is not None
            os.set_blocking(proc.stdin.fileno(), False)
            tui_dir = os.path.join(os.path.expanduser(TUI_DIR), name)
            handle = DockerWorker(
                name,
                generation,
                proc,
                state=state_dir(name),
                events=events,
                hooks=HookWorker([hook, "hook", "--serve", tui_dir])
                if metadata.get("owner")
                else None,
                remove=lambda: self._remove(container, daemon),
                owner=owner,
                maintenance=functools.partial(
                    maintenance.Maintenance,
                    name,
                    state_dir(name),
                    generation,
                    self.broker,
                ),
                report=self._reporter(name, events),
                terminal=terminal,
            )
            made.append(handle)
            handle.launch(handoff)
            handle.await_start()
            handle.begin()
            laps("client")
            _save_result(name, created, laps.phases, _now())
            stops.check()
        except BaseException as error:
            cleanup = ""
            if handle is not None:
                try:
                    handle.close()
                except SessionError as failed:
                    cleanup = f"; {failed}"
            else:
                if proc is not None:
                    kill_group(proc)
                cleanup = self._roll_back(container, daemon)
            if not isinstance(error, (SessionError, OSError, ValueError)):
                raise
            raise SessionError(f"docker session {name}: {error}{cleanup}") from error
        laps.report(f"docker timing {name} {generation}")
        self._workers[name] = handle
        return handle

    def _remove(self, container: str, daemon: Daemon) -> None:
        self.broker.close(container, daemon=daemon.id or "")
        self._spawn_reclaim()

    def _reporter(self, name: str, events: str | None):
        def report(word: str, text: str) -> None:
            if events:
                SessionStatusLog(events).write(name, word, text)
            else:
                LOG.warning("docker session %s: %s %s", name, word, text)

        return report

    def worker_liveness(self, name: str, generation: str) -> Liveness:
        """LIVE while another process holds the owner lock (no docker call); else the row's."""
        if not GEN.fullmatch(generation) or not SESSION_NAME.fullmatch(name):
            return Liveness.UNKNOWN
        if not _lock_free(_owner_path(name)):
            return Liveness.LIVE
        return self._row_liveness(name, generation)

    def _headless_owner(self, name: str) -> Mapping[str, str] | None:
        """While its owner lock is held, by its record alone: no docker call."""
        found = _record(name)
        owner = found.get("owner")
        if (
            found.get("kind") != Kind.HEADLESS
            or found.get("host") != self.name
            or not isinstance(owner, str)
            or not GEN.fullmatch(os.path.basename(owner))
            or os.path.dirname(owner) != os.path.expanduser(HARNESS_ROOT)
            or _lock_free(_owner_path(name))
        ):
            return None
        return {"owner": owner, "sid": found.get("sid") or "", "generation": found["generation"]}

    def _release(self, name: str, generation: str) -> None:
        """Once its owner lock is free, on the session's own daemon: one no configured daemon has
        is closed by its ID, which the broker refuses.
        """
        recorded = self._session_daemon(name)
        target = self._target()
        if daemon_with_id(target, recorded.id or "") is None and all(d.id for d in target.daemons):
            if not _lock_free(_owner_path(name)):
                raise SessionError(f"docker session {name}: owned by its harness")
            self._remove(container_name(name, generation), recorded)
            return
        row = next(
            (
                r
                for r in self._rows()
                if (r.name, r.generation, r.daemon.id) == (name, generation, recorded.id)
            ),
            None,
        )
        if row is None:
            return
        if not _lock_free(_owner_path(name)):
            raise SessionError(f"docker session {name}: owned by its harness")
        self._remove(row.container, row.daemon)

    def _row_liveness(self, name: str, generation: str) -> Liveness:
        """By docker, on the session's own daemon; ABSENT permits only that container's removal."""
        try:
            listed, down = self._inventory()
            recorded = self._session_daemon(name)
        except SessionError:
            return Liveness.UNKNOWN
        daemon = daemon_with_id(self._target(), recorded.id or "")
        if daemon is None or daemon in down:
            return Liveness.UNKNOWN
        rows = _session_rows(listed)
        row = next(
            (
                r
                for r in rows
                if (r.name, r.generation, r.daemon.id) == (name, generation, daemon.id)
            ),
            None,
        )
        if row is None or row.state != "running":
            return Liveness.ABSENT
        return self._guard_liveness(row)

    def _guard_liveness(self, row: Row) -> Liveness:
        if not _guarded(row.name, row.generation):
            return Liveness.UNKNOWN
        argv, env = docker_call(
            self.docker,
            *("exec", "-i", row.container, IMAGE_PYTHON, "-I", GUARD),
            *("--probe", guard.run_paths(row.generation)["dir"]),
            daemon=row.daemon,
        )
        try:
            res = self.run(argv, env, timeout=RUN_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            return Liveness.UNKNOWN
        word = res.stdout.strip() if res.returncode == 0 else ""
        if word in ("exited", "absent"):
            return Liveness.ABSENT
        return Liveness.LIVE if word == "live" else Liveness.UNKNOWN

    def _stage_files(self, container: str, metadata: Mapping[str, str], daemon: Daemon) -> None:
        files = metadata.get("runtime_files")
        if not files or files == "{}":
            return
        argv, env = docker_call(
            self.docker,
            "exec",
            "-i",
            "--user",
            "agent",
            container,
            "/opt/ensemble/venv/bin/python",
            "-I",
            "/opt/ensemble/lib/bootstrap.py",
            "runtime-files",
            daemon=daemon,
        )
        try:
            result = self.run(argv, env, input=files, timeout=RUN_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SessionError(f"staging client runtime: {error}") from None
        if result.returncode:
            raise SessionError("staging client runtime failed")

    def _stage_staged_files(
        self, container: str, metadata: Mapping[str, str], seeds: Mapping[str, str], daemon: Daemon
    ) -> None:
        """As the container's agent user, with a new start's home files."""
        staged = metadata.get(STAGED)
        if staged is None and not seeds:
            return
        files = json.dumps({**json.loads(staged or "{}"), **seeds})
        argv, env = docker_call(
            self.docker,
            *("exec", "-i", "--user", "agent", container),
            *(IMAGE_PYTHON, "-I", "-c", FILES_STAGE),
            daemon=daemon,
        )
        try:
            result = self.run(argv, env, input=files, timeout=RUN_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SessionError(f"staging files: {error}") from None
        if result.returncode:
            why = (result.stderr or "").strip().splitlines()
            raise SessionError(
                f"staging files: {why[-1] if why else f'exit status {result.returncode}'}"
            )

    def _roll_back(self, container: str, daemon: Daemon) -> str:
        try:
            self.broker.close(container, daemon=daemon.id or "")
        except SessionError as error:
            return f"; close {container}: {error}"
        return ""

    def list(self) -> list[str]:
        return sorted(self._containers())

    def _capacity_rows(self) -> list[CapacityRow]:
        containers = self._containers(required=True)
        return [
            capacity_row(self._row(row, probe=True), row.generation)
            for _, row in sorted(containers.items())
        ]

    def last_lines(self, name: str, sid: str, generation: str | None = None) -> str:
        found = _record(name)
        recorded = found.get("generation")
        if (
            found.get("sid") != sid
            or not isinstance(recorded, str)
            or not GEN.fullmatch(recorded)
            or generation not in (None, recorded)
        ):
            return ""
        generation = recorded
        try:
            text = json.loads(
                read_regular(diagnostic_file(state_dir(name), generation), limit=STATE_MAX)
                or b"null"
            )["text"]
        except (ValueError, TypeError, KeyError):
            return ""
        return text if isinstance(text, str) else ""

    def rows(self) -> list[SessionRow]:
        """[] when docker fails, so other Hosts' rows list; an unreachable daemon named on stderr."""
        try:
            listed, down = self._inventory()
        except SessionError as error:
            LOG.warning("docker rows: %s", error)
            return []
        for daemon in self._beside(down):
            print(
                f"ensemble-session: docker: {describe(daemon)} unreachable; its workers are not"
                " listed",
                file=sys.stderr,
            )
        containers = _tui_rows(_session_rows(listed))
        return [self._row(containers[name]) for name in sorted(containers)]

    def _row(self, row: Row, *, probe: bool = False) -> SessionRow:
        name, generation = row.name, row.generation
        record = _record(name)
        record = record if record.get("generation") == generation else {}
        word = worker_activity(name, generation)
        state = StatusState.WORKING
        if _lock_free(_owner_path(name)):
            exited = (
                probe and row.state == "running" and self._guard_liveness(row) == Liveness.ABSENT
            )
            state = StatusState.DEAD if row.state in STOPPED or exited else StatusState.UNKNOWN
        elif word is not None and word != StatusState.WORKING:
            state = word
        return SessionRow(
            name=name,
            sid=record.get("sid"),
            client=record.get("client"),
            state=state,
            display=record.get("display"),
            shown=None,
            last_lines=None,
            own=False,
        )

    def attach_target(self, name: str) -> CommandView:
        found = self._containers().get(name)
        if found is None:
            raise SessionError(f"no docker session {name}")
        display = guard.run_paths(found.generation)["display"]
        argv = docker_call(
            self.docker,
            *("exec", "-it", found.container, IMAGE_PYTHON, "-I", GUARD),
            *("--attach", display),
            daemon=found.daemon,
        )[0]
        return CommandView(
            printed=shlex.join(argv),
            argv=(
                "/usr/bin/env",
                *(arg for key in CLEARED for arg in ("-u", key)),
                *attach_env(found.daemon),
                *argv,
            ),
            run=display,
        )


if __name__ == "__main__":
    DockerHost("docker", {"docker": sys.argv[1]}).reclaim_volumes()
