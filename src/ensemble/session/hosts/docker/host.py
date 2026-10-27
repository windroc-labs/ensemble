"""DockerHost runs each session in its own broker-made container (docs/docker-host.md): its
worker's guard (worker.py), tui or headless, through `docker exec -i`.
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
from ensemble.session.engine.status_log import SessionStatusLog
from ensemble.session.engine.tmux_cli import display_error
from ensemble.session.engine.types import (
    COMMAND,
    PLUGIN_SOURCE,
    RESUME,
    SESSION_NAME,
    ClientContext,
    ClientHome,
    ConfigError,
    Container,
    EnvironmentSpec,
    Launch,
    Liveness,
    SessionBusy,
    SessionError,
    SessionRow,
    StatusState,
    Unanswered,
)
from ensemble.session.hosts.base import (
    GUARD_HOOK_SOCKET,
    TUI_DIR,
    TUI_STATE,
    Host,
    InteractiveProcess,
    Process,
    created_events,
    tui_lock,
)
from ensemble.session.hosts.base import activity as worker_activity
from ensemble.session.hosts.docker import maintenance
from ensemble.session.hosts.docker.broker import (
    Broker,
    BrokerError,
    client_keys,
    hook_root,
    plugin_root,
    write_manifest,
)
from ensemble.session.hosts.docker.daemon import (
    CLEARED,
    RUN_TIMEOUT,
    capped,
    container_name,
    docker_call,
    endpoint,
)
from ensemble.session.hosts.docker.hooks import HookWorker
from ensemble.session.hosts.docker.mounts import (
    KIND_LABEL,
    WORKSPACE_LABEL,
    state_dir,
    workspace_volume,
)
from ensemble.session.hosts.docker.types import GEN, Started
from ensemble.session.hosts.docker.worker import GUARD, RUN, DockerWorker, GuardedView
from ensemble.session.hosts.types import CommandView
from ensemble.utils import (
    DOCKER_STATE,
    Display,
    Record,
    command_path,
    docker_home,
    inode_lock,
    kill_group,
    module_roots,
    read_beneath,
    remove_tree,
    save,
    source_path,
    timestamp,
)

RECORD = "host.json"
OWNER_LOCK = "guard.lock"
BUSY = "previous worker generation (live or unknown)"
IMAGE_PYTHON = "/opt/ensemble/venv/bin/python"
STATE_ROOT = "/run/ensemble"  # the client context's Container.state_root
_ARG_STRLEN_MAX = 131072  # Linux exec's limit on one argument, its NUL included
# modes/base.py BaseMode._write_prompt in the container: keep the two alike
PROMPT_STAGE = """import os, sys
os.makedirs(os.path.dirname(sys.argv[1]), 0o700, exist_ok=True)
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'wb') as f: f.write(sys.stdin.buffer.read())"""
LABEL = "ensemble.session"
PS_FORMAT = "\t".join(
    (
        "{{.Names}}",
        '{{.Label "ensemble.session"}}',
        '{{.Label "ensemble.generation"}}',
        "{{.State}}",
    )
)
LOG = logging.getLogger("session")
KEPT = f"{DOCKER_STATE}/.kept"  # a marker per run volume kept: unsaved work, or a failed check
KEPT_LOCK = ".lock"
KEPT_RETRY = 24 * 3600  # seconds before a volume whose check failed is checked again
VOLUME_FORMAT = '{{.Name}}\t{{.Label "' + WORKSPACE_LABEL + '"}}'
STATE_MAX = 1 << 20
STOPS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)


class Row(NamedTuple):
    """A `docker ps` row whose labels name a session and generation and match its name."""

    name: str
    container: str
    generation: str
    state: str


def _record(name: str) -> dict:
    try:
        with open(os.path.join(state_dir(name), RECORD), encoding="utf-8") as f:
            found = json.load(f)
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


class Kind(StrEnum):
    TUI = "tui"
    HEADLESS = "headless"


def _kind(name: str, generation: str) -> Kind | None:
    found = _record(name)
    kind = found.get("kind")
    return Kind(kind) if found.get("generation") == generation and kind in set(Kind) else None


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


def _reset_home(name: str, home: str, kept: Iterable[str]) -> None:
    """Empties a new start's client home in place, never following a link: Docker Desktop can
    report a bind source recreated at a path it saw before as missing, so `home` and each `kept`
    dir under it stay the same dir, mode 0700; a link or file there is removed.
    """
    keep = {os.path.join(home, directory) for directory in kept}

    def empty(path: str) -> None:
        os.chmod(path, 0o700)
        with os.scandir(path) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    os.unlink(entry.path)
                elif entry.path in keep:
                    empty(entry.path)
                else:
                    remove_tree(entry.path)

    try:
        if os.path.lexists(home) and (os.path.islink(home) or not os.path.isdir(home)):
            os.unlink(home)
        os.makedirs(home, mode=0o700, exist_ok=True)
        empty(home)
    except OSError as error:
        raise SessionError(
            f"docker session {name}: cannot reset {home}: {error.strerror or error}"
        ) from None


def _report_plan(name: str, plan: Mapping) -> None:
    """Freeze a generation's plan beside its manifest, then print it to stderr, a line a part."""
    try:
        save(
            os.path.join(state_dir(name), f"{plan['generation']}.plan.json"),
            json.dumps(plan),
            durable=True,
        )
    except OSError as error:
        raise SessionError(f"docker session {name}: plan: {error}") from None
    mounts = ", ".join(f"{mount['path']} rw" for mount in plan["mounts"])
    workspace = plan["workspace"]
    for part in (
        f"image {plan['image']} {plan['image_digest']}",
        f"workspace {workspace['volume']} at {workspace['path']} rw",
        f"mounts {mounts}",
        f"credential {plan['credential']}",
    ):
        print(f"docker plan {name} {plan['generation']}: {part}", file=sys.stderr)


def _parsed_environment(name: str, value) -> EnvironmentSpec | None:
    """An environment from metadata's JSON or host.json's object; None for none."""
    if value is None:
        return None
    try:
        return EnvironmentSpec.from_fields(json.loads(value) if isinstance(value, str) else value)
    except (ValueError, RecursionError, ConfigError) as error:
        raise SessionError(f"docker session {name}: bad environment: {error}") from None


def _holder_text(found: Mapping[str, str]) -> str:
    return f"{found['container']} (owner {found['owner']})"


class _Stops:
    """Holds STOPS but ignored ones while a start owns its container, so its rollback still runs:
    check() raises for one received, which is raised again on exit. Catches, not blocks, so no child
    inherits them blocked; only the main thread can set handlers.
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
    """Seconds per phase, since the previous one ended (docs/docker-host.md › Start timing)."""

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


def _accept_started(
    name: str, generation: str, started: Started, container: str, github: str
) -> None:
    """The broker started `container` here, with a bot user ID exactly when `github` names an App;
    its deadline saved for the credential tick.
    """
    if (started.endpoint, started.container) != (endpoint(), container):
        raise SessionError(
            f"broker: started {started.container} at {started.endpoint},"
            f" want {container} at {endpoint()}"
        )
    if (started.github_user_id is None) == bool(github):
        raise SessionError("broker: bad response to start: github_user_id")
    save(
        maintenance.deadline_file(state_dir(name), generation),
        json.dumps(started.deadline),
        durable=True,
    )


def _held(marker: str) -> bool:
    """Whether a kept marker still holds its volume back: a failed check for KEPT_RETRY, anything
    else for good.
    """
    try:
        with open(marker) as f:
            failed = f.read() == "failed"
        return not failed or time.time() - os.stat(marker).st_mtime < KEPT_RETRY
    except FileNotFoundError:
        return False


class DockerHost(Host):
    """settings: [hosts.<name>]'s docker, the CLI's absolute path; plugins, repository sources
    installed in its image (none when absent); and github.app, the GitHub App whose tokens every
    session gets ("" or no table: none).
    """

    _inherit_env = False  # the container gets only the launch's own env (D10)
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
        if extra := sorted(set(self.settings) - {"docker", "github", "plugins"}):
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
        github = self.settings.get("github", {"app": ""})
        if not isinstance(github, Mapping):
            raise ConfigError(f"[hosts.{name}] github: want a table")
        if extra := sorted(set(github) - {"app", "permissions"}):
            raise ConfigError(f"unknown key {extra[0]!r} in [hosts.{name}.github]")
        app = github.get("app")
        if not isinstance(app, str):
            raise ConfigError(f"[hosts.{name}.github] app: want a string")
        permissions = github.get("permissions", {})
        if not isinstance(permissions, Mapping) or not all(
            isinstance(level, str) for level in permissions.values()
        ):
            raise ConfigError(
                f"[hosts.{name}.github] permissions: want a table of permission = level strings"
            )
        if app and not permissions:
            raise ConfigError(f"[hosts.{name}.github] permissions: want at least one with app set")
        self.github = app
        # The broker validates the names and levels.
        self.github_permissions: dict[str, str] = dict(permissions)
        self.run = run
        self.broker = Broker() if broker is None else broker
        self._workers: dict[str, DockerWorker] = {}

    def _ps(self, *, required: bool = False) -> list[list[str]]:
        """Admission requires an answer; other callers list no sessions without a daemon/CLI."""
        rows = self._listed("ps", "-a", "--filter", f"label={LABEL}", "--format", PS_FORMAT)
        if rows is None and required:
            raise SessionError(f"host {self.name}: inventory unavailable (no Docker CLI or socket)")
        return rows or []

    def _listed(self, *args: str) -> list[list[str]] | None:
        """docker `args`' tab-separated rows; None with no daemon socket or docker CLI; Unanswered
        when docker fails or times out.
        """
        if not os.path.exists(endpoint().removeprefix("unix://")):
            return None
        argv, env = docker_call(self.docker, *args)
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
        return [
            Row(name, container, generation, state)
            for container, name, generation, state in (
                row for row in self._ps(required=required) if len(row) == len(Row._fields)
            )
            if SESSION_NAME.fullmatch(name)
            and GEN.fullmatch(generation)
            and container == container_name(name, generation)
        ]

    def _containers(self, *, required: bool = False) -> dict[str, Row]:
        """Name → its guarded tui row."""
        return {
            row.name: row
            for row in self._rows(required=required)
            if _kind(row.name, row.generation) == Kind.TUI
        }

    def sweep(self, own: str) -> None:
        """Closes debris and workers whose owner lock is free; other names only under
        their tui lock taken without waiting (`own`'s caller holds its).
        """
        try:
            rows = self._rows()
        except SessionError as error:
            LOG.warning("docker sweep: %s", error)
            return
        for row in rows:
            try:
                if row.name == own:
                    self._sweep_row(row)
                    continue
                lock = tui_lock(row.name)
                with inode_lock(lock, wait=False) as held:
                    if held:
                        try:
                            self._sweep_row(row)
                        finally:
                            if not os.path.lexists(_state_file(row.name)):
                                os.unlink(lock)
            except (SessionError, OSError) as error:
                LOG.warning("docker sweep %s: %s", row.name, error)

    def _spawn_reclaim(self) -> None:
        """Detach the close's cleanup, so broker checks outlive the caller without delaying it."""
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
                    env={"PATH": os.defpath, "HOME": os.path.expanduser("~"), **source_path(roots)},
                    cwd="/",
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    start_new_session=True,
                )
            threading.Thread(target=proc.wait, daemon=True).start()
        except OSError as error:
            LOG.warning("docker reclaim: %s", error)

    def reclaim(self) -> None:
        """Background close pass: removes all run workspace volumes whose workdir is gone, after
        the broker's check. One with unsaved work is kept for good, one whose check
        failed for KEPT_RETRY; each is warned about once and marked. Listed before the lock, so no
        orphan writes no file.
        """
        try:
            self._reclaim()
        except (SessionError, OSError) as error:
            LOG.warning("docker reclaim: %s", error)

    def _reclaim(self) -> None:
        rows = self._listed(
            "volume", "ls", "--filter", f"label={KIND_LABEL}", "--format", VOLUME_FORMAT
        )
        if rows is None:
            return
        gone = [
            volume
            for volume, anchor in (row for row in rows if len(row) == 2)
            if os.path.isabs(anchor) and not os.path.lexists(anchor)
        ]
        kept = os.path.expanduser(KEPT)
        if not gone and not os.path.isdir(kept):
            return
        os.makedirs(kept, mode=0o700, exist_ok=True)
        with inode_lock(os.path.join(kept, KEPT_LOCK), wait=False) as held:
            if not held:
                return
            for marker in set(os.listdir(kept)) - {row[0] for row in rows} - {KEPT_LOCK}:
                os.unlink(os.path.join(kept, marker))
            for volume in gone:
                marker = os.path.join(kept, volume)
                if _held(marker):
                    continue
                try:
                    self.broker.workspace_rm(volume)
                except BrokerError as error:
                    save(marker, "dirty" if error.code == "workspace_dirty" else "failed")
                    LOG.warning("docker workspace %s kept: %s", volume, error.message)
                except SessionError as error:
                    save(marker, "failed")
                    LOG.warning("docker workspace %s kept: %s", volume, error)
                else:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(marker)

    def _sweep_row(self, row: Row) -> None:
        if not _guarded(row.name, row.generation) or _lock_free(_owner_path(row.name)):
            self.broker.close(row.container)

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

    def _provider_usage(self, request):
        self.broker.version()
        try:
            return self.broker.probe_provider(request.provider, request.base_url)
        except BrokerError as error:
            if error.code == "login_required":
                raise ConfigError(str(error)) from None
            raise

    def _client_env(
        self, name: str, client: str, home: ClientHome | None, *, resume: bool
    ) -> Mapping[str, str]:
        if home is None:
            raise ConfigError(
                f"client {client or '(unnamed)'}: not supported on the Docker Host yet"
            )
        path = docker_home(name, home.dir)
        if os.path.realpath(path) != path:
            raise ConfigError(f"client home {path}: symlink component")
        return {home.env: path}

    def _resume_argv(self, name: str) -> tuple[str, ...]:
        return (*RESUME, name)

    def _preflight(
        self, name: str, launch: Launch, metadata: Mapping[str, str] | None = None
    ) -> None:
        """With an environment: cwd and handshake; start owns the lock and credential checks."""
        environment = self._environment(name, metadata)
        if environment is None:
            self.validate(name, launch, missing_ok=True)
            return
        laps = _Laps()
        self._check_cwd(name, launch, missing_ok=True)
        self.broker.version()
        laps("preflight")
        laps.report(f"docker timing {name}")

    def _validate(
        self, name: str, launch: Launch, metadata: Mapping[str, str] | None = None
    ) -> None:
        """With an environment: the cwd and the broker handshake; the start builds and locks."""
        if self._environment(name, metadata) is None:
            self.validate(name, launch)
            return
        self._check_cwd(name, launch)
        self.broker.version()

    def _environment(self, name: str, metadata: Mapping[str, str] | None) -> EnvironmentSpec | None:
        """The given environment; a resume's is its stored one (FR-62): taken when none is given,
        refused when the given one differs but in prefetch.
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

    def _app(self, environment: EnvironmentSpec | None) -> str:
        """The GitHub App a start's container gets: none with a prefetch (FR-52)."""
        return "" if environment is not None and environment.prefetch else self.github

    def validate(self, name: str, launch: Launch, *, missing_ok: bool = False) -> None:
        """The preflight, before any container: the cwd (`missing_ok`: or none yet), the broker
        handshake and the build.
        """
        laps = _Laps()
        self._check_cwd(name, launch, missing_ok=missing_ok)
        self._image(name)
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
        self, name: str, environment: EnvironmentSpec | None = None
    ) -> tuple[str, str, tuple[str, str]]:
        """(the hook path, the plugin root, the built image and digest); an environment's base
        image and plugins over the Host's.
        """
        hook = command_path(COMMAND)
        if hook is None:
            raise ConfigError(f"docker session {name}: {COMMAND} is not on PATH (its hooks run it)")
        self.broker.version()
        root = plugin_root(hook)
        if environment is None:
            return hook, root, self.broker.build(root, plugins=self.plugins)
        sources = [p for p in environment.plugins if PLUGIN_SOURCE.fullmatch(p)]
        built = self.broker.build(
            root,
            plugins=(*self.plugins, *(p for p in sources if p not in self.plugins)),
            base_image=None if environment.image == EnvironmentSpec.WORKER else environment.image,
            image_plugins=tuple(p for p in environment.plugins if p not in sources),
        )
        return hook, root, built

    def _move_workspace(self, name: str, volume: str, cwd: str) -> None:
        """Deletes a new start's workspace volume mounted at another cwd, unless the broker's check
        finds unsaved work there; one at `cwd`, or none, is kept for this start.
        """
        rows = self._listed(
            "volume", "ls", "--filter", f"label={KIND_LABEL}", "--format", VOLUME_FORMAT
        )
        anchor = next((row[1] for row in rows or () if len(row) == 2 and row[0] == volume), cwd)
        if anchor == cwd:
            return
        try:
            self.broker.workspace_rm(volume)
        except BrokerError as error:
            if error.code == "workspace_dirty":
                raise ConfigError(
                    f"docker session {name}: {error.message}; or start it in {anchor} again"
                ) from error
            raise

    def detach(self, name, launch, *, display=None, events=None, record=None) -> Process:
        raise SessionError(f"detach {name}: runs only under a session harness")

    def terminal(
        self,
        name: str,
        launch: Launch,
        *,
        display: Display = None,
        events: str | None = None,
        record: Record | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> InteractiveProcess:
        raise SessionError(f"docker session {name}: terminal: runs only under a session harness")

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
        """The checks, image, workspace, home and plan, then the broker's start, rolled back on a
        transport failure.
        """
        generation, field, home_dir, credential = (
            metadata.get(key) for key in ("generation", "home_field", "home_dir", "credential")
        )
        if not (generation and field and home_dir and credential):
            raise SessionError(f"docker session {name}: no client home to mount")
        resume = metadata.get("resume")
        if resume not in ("0", "1"):
            raise SessionError(f"docker session {name}: bad resume flag {resume!r:.20}")
        self.sweep(name)
        for row in self._ps():
            if row[1:2] == [name]:
                if len(row) == len(Row._fields) and _guarded(name, row[2]):
                    raise SessionError(
                        f"docker session {name}: a container for it exists; close {name} first"
                    )
                if (
                    len(row) == len(Row._fields)
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
        home = docker_home(name, home_dir)
        if resume == "1" and read_beneath(home, launch.transcript, limit=0) is None:
            raise ConfigError(f"docker session {name}: no native history to resume in {home}")
        laps("checks")
        hook, root, (image, digest) = self._image(name, environment)
        volume = workspace_volume(name)
        cwd = os.path.realpath(launch.cwd)
        persistent = json.loads(metadata.get("home_persistent") or "null")
        if resume == "0":
            self._move_workspace(name, volume, cwd)
            _reset_home(name, home, persistent or ())
            for file, text in json.loads(metadata.get("home_files") or "{}").items():
                save(os.path.join(home, file), text)
            for directory in persistent or ():
                os.makedirs(os.path.join(home, directory), mode=0o700, exist_ok=True)
        clients = client_keys(root)
        manifest = os.path.join(state_dir(name), f"{generation}.json")
        write_manifest(
            manifest,
            name=name,
            generation=generation,
            image=image,
            cwd=cwd,
            workspace=volume,
            home=(field, home),
            hook_path=hook,
            github=self._app(environment),
            permissions=self.github_permissions,
            providers=tuple(json.loads(metadata.get("home_providers") or "[]")),
            account=metadata.get("home_account", ""),
            **(
                {
                    "gateway": environment.gateway,
                    "secrets": environment.secrets,
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
                    {"path": os.path.join(home, directory), "rw": True} for directory in persistent
                ]
                if persistent is not None
                else [{"path": home, "rw": True}],
                "credential": credential,
            },
        )
        laps("image")
        container = container_name(name, generation)
        stops.check(f"docker session {name}: ")
        began = timestamp()
        try:
            started = self.broker.start(manifest, clients=clients)
            _accept_started(name, generation, started, container, self._app(environment))
            ended = timestamp()
            laps("start")
            created = _Container(container, generation, hook, image, cwd, started, began, ended)
            _save_result(name, created, laps.phases)
        except BrokerError as error:
            if error.code == "busy" and error.holder and environment and environment.lock:
                raise SessionBusy(environment.lock, _holder_text(error.holder)) from error
            if error.cleanup == "failed" and error.container:
                with contextlib.suppress(SessionError):
                    self.broker.close(error.container)
            raise
        except (SessionError, OSError) as error:
            cleanup = self._roll_back(container)
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
        """A direct caller's (a headless run's) start: under the tui lock, whose file goes after
        unless a session.json names it; an owned one's (metadata `owner`) TuiStore holds it.
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
        """The guard in a new container over one `docker exec -i`, holding the owner lock; a live or
        unknown earlier guarded generation refuses (SessionBusy), an ended one is removed first.
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
        environment = self._environment(name, metadata)
        owner = _take_owner(name)
        if owner is None:
            raise SessionBusy(name, BUSY)
        made: list[DockerWorker] = []  # once built, the handle alone owns `owner`
        try:
            for row in self._rows():
                if row.name == name and _guarded(name, row.generation):
                    if self._row_liveness(name, row.generation) != Liveness.ABSENT:
                        raise SessionBusy(name, BUSY)
                    self.broker.close(row.container)
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
        container, generation, hook, _, cwd, _, _, _ = created
        run = f"{RUN}/{generation}"
        handle = proc = None
        try:
            stops.check()
            self._stage_files(container, metadata)
            self._stage_prompt(container, metadata)
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
                },
            )
            env = {
                **self._exec_env(launch, created.started, self._app(environment)),
                GUARD_HOOK_SOCKET: f"{run}/hook",
            }
            shown = ("--terminal", "--display-socket", f"{run}/display") if terminal else ()
            argv, docker_env = docker_call(
                self.docker,
                *("exec", "-i", "--workdir", cwd),
                *(arg for key in env for arg in ("--env", key)),
                *(container, IMAGE_PYTHON, "-I", GUARD, *shown),
                *("--hook-socket", f"{run}/hook", "--lease", f"{run}/lease", "--"),
                *("with-secrets", "--client", metadata["home_field"].removesuffix("_dir")),
                *launch.argv,
            )
            proc = subprocess.Popen(
                argv,
                env={**docker_env, **env},
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
                remove=lambda: self._remove(container),
                owner=owner,
                maintenance=functools.partial(
                    maintenance.Maintenance,
                    name,
                    state_dir(name),
                    generation,
                    self.broker,
                    root=hook_root(hook),
                ),
                report=self._reporter(name, events),
                load=lambda: _record(name),
                store=lambda data: _save_record(name, data),
                terminal=terminal,
            )
            made.append(handle)
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
                cleanup = self._roll_back(container)
            if not isinstance(error, (SessionError, OSError, ValueError)):
                raise
            raise SessionError(f"docker session {name}: {error}{cleanup}") from error
        laps.report(f"docker timing {name} {generation}")
        self._workers[name] = handle
        return handle

    def _remove(self, container: str) -> None:
        self.broker.close(container)
        self._spawn_reclaim()

    def _reporter(self, name: str, events: str | None):
        def report(word: str, text: str) -> None:
            if events:
                SessionStatusLog(events).write(name, word, text)
            else:
                LOG.warning("docker session %s: %s %s", name, word, text)

        return report

    def worker_liveness(self, name: str, generation: str) -> Liveness:
        """LIVE while another process holds its owner lock, with no docker call; else the row's."""
        if not GEN.fullmatch(generation) or not SESSION_NAME.fullmatch(name):
            return Liveness.UNKNOWN
        if not _lock_free(_owner_path(name)):
            return Liveness.LIVE
        return self._row_liveness(name, generation)

    def _row_liveness(self, name: str, generation: str) -> Liveness:
        """The container of `generation` and its guard's lease, by docker; ABSENT permits only that
        container's removal.
        """
        try:
            rows = self._rows()
        except SessionError:
            return Liveness.UNKNOWN
        row = next((r for r in rows if r.name == name and r.generation == generation), None)
        if row is None or row.state != "running":
            return Liveness.ABSENT
        if not _guarded(name, generation):
            return Liveness.UNKNOWN
        argv, env = docker_call(
            self.docker,
            *("exec", "-i", row.container, IMAGE_PYTHON, "-I", GUARD),
            *("--probe", f"{RUN}/{generation}"),
        )
        try:
            res = self.run(argv, env, timeout=RUN_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            return Liveness.UNKNOWN
        word = res.stdout.strip() if res.returncode == 0 else ""
        if word in ("exited", "absent"):
            return Liveness.ABSENT
        return Liveness.LIVE if word == "live" else Liveness.UNKNOWN

    def _stage_files(self, container: str, metadata: Mapping[str, str]) -> None:
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
        )
        try:
            result = self.run(argv, env, input=files, timeout=RUN_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SessionError(f"staging client runtime: {error}") from None
        if result.returncode:
            raise SessionError("staging client runtime failed")

    def _stage_prompt(self, container: str, metadata: Mapping[str, str]) -> None:
        """metadata's prompt written at its prompt_file in the container, as its agent user."""
        path = metadata.get("prompt_file")
        if path is None:
            return
        argv, env = docker_call(
            self.docker,
            *("exec", "-i", "--user", "agent", container),
            *(IMAGE_PYTHON, "-I", "-c", PROMPT_STAGE, path),
        )
        try:
            result = self.run(argv, env, input=metadata["prompt"], timeout=RUN_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SessionError(f"staging prompt file: {error}") from None
        if result.returncode:
            why = (result.stderr or "").strip().splitlines()
            raise SessionError(
                f"staging prompt file: {why[-1] if why else f'exit status {result.returncode}'}"
            )

    def _exec_env(self, launch: Launch, started: Started, app: str) -> dict[str, str]:
        """launch's env, plus with a GitHub App its bot as PR author and git identity."""
        env = {k: v for k, v in launch.env.items() if k != "PATH"}
        if app:
            bot = f"{app}[bot]"
            email = f"{started.github_user_id}+{bot}@users.noreply.github.com"
            env |= {
                "ENSEMBLE_PR_AUTHOR": bot,
                "GIT_AUTHOR_NAME": bot,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": bot,
                "GIT_COMMITTER_EMAIL": email,
            }
        return env

    def _roll_back(self, container: str) -> str:
        try:
            self.broker.close(container)
        except SessionError as error:
            return f"; close {container}: {error}"
        return ""

    def attach(self, name: str) -> InteractiveProcess:
        found = self._containers().get(name)
        if found is None:
            raise SessionError(f"no docker session {name}")
        return self._guarded_process(found)

    def _guarded_process(self, row: Row) -> InteractiveProcess:
        """The handle this process holds, else a view of the worker its owner holds."""
        handle = self._workers.get(row.name)
        if handle is not None and handle.removed:
            del self._workers[row.name]
        elif handle is not None and handle.generation == row.generation:
            return handle
        name, generation = row.name, row.generation

        def remove() -> None:
            if not _lock_free(_owner_path(name)):
                raise SessionError(f"docker session {name}: owned by its harness")
            self._remove(row.container)

        return GuardedView(
            name,
            generation,
            state=state_dir(name),
            load=lambda: _record(name),
            liveness=lambda: self.worker_liveness(name, generation),
            remove=remove,
        )

    def list(self) -> list[str]:
        return sorted(self._containers())

    def _capacity_rows(self) -> list[SessionRow]:
        containers = self._containers(required=True)
        return [self._row(name, row.generation) for name, row in sorted(containers.items())]

    def _capacity_namespace(self) -> str:
        return f"docker:{endpoint()}"

    def rows(self) -> list[SessionRow]:
        """[] with a warning when docker fails, so the other Hosts' rows still list; list() raises,
        as its callers act on absence.
        """
        try:
            containers = self._containers()
        except SessionError as error:
            LOG.warning("docker rows: %s", error)
            return []
        return [self._row(name, containers[name].generation) for name in sorted(containers)]

    def _row(self, name: str, generation: str) -> SessionRow:
        record = _record(name)
        record = record if record.get("generation") == generation else {}
        word = worker_activity(name, generation)
        state = StatusState.WORKING
        if _lock_free(_owner_path(name)):
            state = StatusState.UNKNOWN
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
        argv = docker_call(
            self.docker,
            *("exec", "-it", found.container, IMAGE_PYTHON, "-I", GUARD),
            *("--attach", f"{RUN}/{found.generation}/display"),
        )[0]
        return CommandView(
            printed=shlex.join(argv),
            argv=("/usr/bin/env", *(arg for key in CLEARED for arg in ("-u", key)), *argv),
        )


if __name__ == "__main__":
    DockerHost("docker", {"docker": sys.argv[1]}).reclaim()
