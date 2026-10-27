"""Host, where processes live, keeping each one's Record and Display; an attachable one names the
attach target the Viewer shows a session through.
"""

from __future__ import annotations

import json
import os
import signal
import stat
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import ClassVar

from ensemble.session.engine.types import (
    LOCAL_CONTEXT,
    MARKS,
    SESSION_NAME,
    ClientContext,
    ClientHome,
    ConfigError,
    HookCallback,
    Hosts,
    Key,
    KeyPress,
    Launch,
    Liveness,
    NotShowable,
    Paste,
    Permission,
    ProviderProbe,
    SessionError,
    SessionRow,
    StatusState,
    TuiSettings,
    WorkerMessage,
)
from ensemble.session.hosts.types import CommandView, SessionView
from ensemble.utils import (
    CLASS_PATH,
    CONFIG_NAME,
    STATE,
    Display,
    Record,
    Status,
    append_line,
    events_file,
    printable_abs,
    read_regular,
    resume_command,
    thaw,
    validate_events,
)

SIGNALS = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
TUI_DIR = f"{STATE}/tui"
HARNESS_ROOT = f"{STATE}/harness"
GUARD_LEASE_FD = "ENSEMBLE_GUARD_LEASE_FD"
GUARD_HOOK_SOCKET = "ENSEMBLE_GUARD_HOOK_SOCKET"
GUARD_WAIT = 10  # s for a guard's reply, or its exit after a close (it gives up 9 s after one)
TUI_STATE = "session.json"
PROMPTED = "prompted"  # "<generation> <begun> <submitted>" sends
ACTIVITY = "activity"  # "<generation> <word>": its harness's last mark
RECORD = "record"  # "<generation>\t<Record line>": its crew Record
STATE_MAX = 1 << 16
ACTIVITY_MAX = 256


def sent(folder: str) -> str | None:
    """Tui dir `folder`'s PROMPTED, read before the hook record it goes in."""
    try:
        data = read_regular(os.path.join(folder, PROMPTED))
    except OSError:
        return None
    return None if data is None else data.decode(errors="replace").strip()


def checked_events(path: str | None) -> str | None:
    """An events file's absolute path, checked (ConfigError); never creates it."""
    try:
        return None if path is None else validate_events(path)
    except ValueError as error:
        raise ConfigError(str(error)) from None


def created_events(path: str | None) -> str | None:
    """checked_events, then the file made 0600 when missing (SessionError when it cannot be)."""
    path = checked_events(path)
    try:
        return None if path is None else events_file(path)
    except ValueError as error:
        raise SessionError(str(error)) from None


@dataclass(frozen=True)
class WorkerState:
    """A tui worker's current generation, as its session.json says."""

    sid: str | None
    generation: str | None
    client: str | None
    display: str | None
    owner: str | None


def worker_dir(name: str) -> str:
    return os.path.join(os.path.expanduser(TUI_DIR), name)


def worker_file(name: str, file: str, limit: int) -> bytes | None:
    """Worker `name`'s `file`, at most `limit` bytes, only when a regular file of this user's and no
    link; else None.
    """
    if not isinstance(name, str) or not SESSION_NAME.fullmatch(name):
        return None
    try:
        fd = os.open(
            os.path.join(worker_dir(name), file), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > limit:
            return None
        data = os.read(fd, limit + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return None if len(data) > limit else data


def worker_state(name: str) -> WorkerState | None:
    data = worker_file(name, TUI_STATE, STATE_MAX)
    try:
        found = None if data is None else json.loads(data)
    except (ValueError, RecursionError):
        return None
    if not isinstance(found, dict):
        return None

    def text(key: str) -> str | None:
        value = found.get(key)
        return value if isinstance(value, str) else None

    return WorkerState(
        text("sid"), text("generation"), text("client"), text("display"), text("owner")
    )


def activity(name: str, generation: str) -> StatusState | None:
    """Worker `name`'s last mark while it is generation `generation`'s."""
    data = worker_file(name, ACTIVITY, ACTIVITY_MAX)
    fields = b"".join((data or b"").split(b"\n", 1)[:1]).decode(errors="replace").split(" ")
    if len(fields) != 2 or fields[0] != generation or fields[1] not in MARKS:
        return None
    return StatusState(fields[1])


def tui_lock(name: str) -> str:
    return os.path.join(os.path.expanduser(TUI_DIR), f"{name}.lock")


class Process(ABC):
    """close: end/forget, absent is fine; kill: process group; wait_exit: await the whole group."""

    name: str

    @abstractmethod
    def alive(self) -> bool: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def wait_exit(self, timeout: float) -> bool: ...

    @abstractmethod
    def kill(self) -> None: ...

    @property
    def identity(self) -> str:
        return self.name

    @property
    def token(self) -> str | None:
        return None

    def publish(self, record: Record) -> None:
        raise SessionError(f"session {self.name}: its host cannot store a Record after launch")

    def session_identity(self) -> tuple[str | None, str | None]:
        """Private sid and generation, independent of Record; None never matches a supplied sid."""
        return None, None

    def state(self) -> Liveness:
        return Liveness.LIVE if self.alive() else Liveness.ABSENT

    def returncode(self) -> int | None:
        return None if self.alive() else -signal.SIGHUP

    def activity(self) -> StatusState | None:
        return None

    def screen(self, lines: int | None = None) -> str:
        raise NotShowable(f"session {self.name}: its host cannot show")

    def status(self) -> Status:
        """unknown, too, when its host fails after telling its liveness."""
        unknown = Status(
            name=self.name,
            alive=False,
            state=StatusState.UNKNOWN,
            outcome=None,
            attach=None,
            hidden=False,
        )
        liveness = self.state()
        if liveness == Liveness.UNKNOWN:
            return unknown
        try:
            alive = liveness == Liveness.LIVE and self.alive()
            state = (self.activity() or StatusState.WORKING) if alive else StatusState.DEAD
        except SessionError:
            return unknown
        return Status(
            name=self.name, alive=alive, state=state, outcome=None, attach=None, hidden=False
        )

    def mark(self, word: StatusState, events: str | None) -> None:
        """Optional activity metadata for a terminal's status display."""
        return None

    def row(self) -> SessionRow:
        receipt = self.record()
        return SessionRow(
            self.name,
            None if receipt is None else receipt.sid,
            None if receipt is None else receipt.kind,
            StatusState.WORKING if self.alive() else StatusState.DEAD,
            self.display(),
            None,
            None,
            own=False,
        )

    @abstractmethod
    def record(self) -> Record | None: ...

    @abstractmethod
    def display(self) -> Display: ...


class Interactive(ABC):
    """send: into its input (Key names: the Client's)."""

    @abstractmethod
    def send(self, action: Paste | Key) -> None: ...

    @abstractmethod
    def press(self, keys: Sequence[KeyPress]) -> None: ...


class InteractiveProcess(Process, Interactive):
    """Managed contract: docs/architecture.md › Session harness interfaces; no Channel."""

    generation: str

    def read(self, timeout: float) -> list[WorkerMessage]:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        """b"": headless EOF; refused after exit."""
        raise NotImplementedError

    def tick(self, now: float) -> float | None:
        return None


def host_resume(host: Host, name: str | None, cwd: str) -> str | None:
    argv = None if name is None else host._resume_argv(name)
    return resume_command(cwd, {}, argv) if argv else None


def check_sid(name: str, process: Process | None, sid: str | None) -> None:
    if sid is not None:
        actual = None if process is None else process.session_identity()[0]
        if actual != sid:
            raise SessionError(f"session {name}: expected sid {sid!r}, found {actual!r}")


class Host(ABC):
    """settings excludes implementation/isolated; Launch.env passes verbatim. Exit events:
    `exited <rc>` / `dead`; unknown attach: SessionError; list: sorted names. Viewer reads display.
    """

    attachable: ClassVar[bool] = False
    _environments: ClassVar[bool] = False  # runs Spec.environment
    _remote_tui: ClassVar[bool] = False  # its owned tui workers are reached only through a guard
    _named_headless: ClassVar[bool] = False

    def _provider_usage(self, request: ProviderProbe) -> bool | None:
        """Probe with this Host's credentials, when supported."""
        return None

    def __init__(self, name: str, settings: Mapping | None = None) -> None:
        self.name = name
        self.settings = thaw(settings or {})
        self._isolated = self.settings.pop("isolated", False)
        if not isinstance(self._isolated, bool):
            raise ConfigError(f"[hosts.{name}] isolated: want true or false")

    @classmethod
    def parse_hosts(cls, table) -> Hosts:
        """The [hosts] parser's only home: an already-read [hosts] table as Hosts(default, tables),
        else ConfigError. An implementation is checked only for the module.path:Class form, never
        imported.
        """
        if not isinstance(table, Mapping):
            raise ConfigError("[hosts]: missing")
        tables = {}
        for key, value in table.items():
            if key == "default":
                continue
            if not isinstance(key, str):
                raise ConfigError(f"[hosts] {key!r}: want a string key")
            if not isinstance(value, Mapping):
                raise ConfigError(f"[hosts] {key}: want default or a [hosts.<name>] table")
            if not CONFIG_NAME.match(key):
                raise ConfigError(f"[hosts.{key}]: want a name matching [a-z][a-z0-9_]*")
            implementation = value.get("implementation")
            if not isinstance(implementation, str) or not CLASS_PATH.fullmatch(implementation):
                raise ConfigError(f"[hosts.{key}].implementation: want module.path:Class")
            tables[key] = value
        default = table.get("default")
        names = ", ".join(sorted(tables))
        if isinstance(default, str) and default and default not in tables:
            raise ConfigError(
                f"[hosts] default: {default!r} is not a Host here; want one of {names}"
            )
        if not isinstance(default, str) or default not in tables:
            raise ConfigError(f"[hosts] default: want one of {names}")
        return Hosts(default, tables)

    @abstractmethod
    def detach(
        self,
        name: str,
        launch: Launch,
        *,
        display: Display = None,
        events: str | None = None,
        record: Record | None = None,
    ) -> Process: ...

    @abstractmethod
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
        """metadata: private sid/client/generation, apart from the published Record; a container
        Host writes prompt to prompt_file."""
        ...

    def worker(
        self,
        name: str,
        launch: Launch,
        *,
        terminal: bool,
        display: Display = None,
        events: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> InteractiveProcess:
        """Managed worker `name` under the guard (WDRC-199 FR-13)."""
        raise SessionError(f"host {self.name} runs no managed worker")

    def worker_liveness(self, name: str, generation: str) -> Liveness:
        raise SessionError(f"host {self.name} runs no managed worker")

    def _release(self, name: str, generation: str) -> None:
        """Worker `name`'s generation `generation`'s leftovers removed, once its worker_liveness
        is ABSENT; a Host that overrides worker overrides this too.
        """
        return None

    @abstractmethod
    def attach(self, name: str) -> Process: ...

    @abstractmethod
    def list(self) -> list[str]: ...

    def rows(self) -> list[SessionRow]:
        rows = []
        for name in self.list():
            try:
                rows.append(self.attach(name).row())
            except SessionError:
                if name in self.list():
                    raise
        return rows

    def _capacity_rows(self) -> list[SessionRow]:
        names = set(self.list())
        rows = self.rows()
        if (missing := names - {row.name for row in rows}) and missing & set(self.list()):
            raise SessionError(f"host {self.name}: inventory unavailable")
        return rows

    def _capacity_namespace(self) -> str | None:
        """Shared backend identity, if known."""
        return None

    def attach_target(self, name: str) -> SessionView | CommandView | None:
        """What the Viewer shows session `name` through; None unless attachable."""
        return None

    def reclaim(self, token: str) -> Process | None:
        """An untrusted token's live process group, else None."""
        return None

    def events_path(self, events: str | None) -> str | None:
        return None if events is None else os.path.abspath(events)

    def prepare_events(self, events: str | None) -> str | None:
        if events is not None:
            try:
                append_line(events, "")
            except OSError as error:
                raise SessionError(f"events file {events}: {error.strerror}") from error
        return events

    def hook_callback(self, name: str, metadata: Mapping[str, str]) -> HookCallback | None:
        return None

    _inherit_env = True

    def _client_context(self) -> ClientContext:
        if self._isolated:
            return replace(LOCAL_CONTEXT, permission=Permission.YOLO)
        return LOCAL_CONTEXT

    def _client_env(
        self, name: str, client: str, home: ClientHome | None, *, resume: bool
    ) -> Mapping[str, str]:
        """Env for `client`'s launch as `name`; ConfigError: not on this Host."""
        return {}

    def _resume_argv(self, name: str) -> tuple[str, ...] | None:
        """None: the client's."""
        return None

    def tui_settings(self) -> TuiSettings:
        return TuiSettings()

    def _preflight(
        self, name: str, launch: Launch, metadata: Mapping[str, str] | None = None
    ) -> None:
        """validate, before its caller makes the cwd."""
        self.validate(name, launch)

    def _validate(
        self, name: str, launch: Launch, metadata: Mapping[str, str] | None = None
    ) -> None:
        self.validate(name, launch)

    def validate(self, name: str, launch: Launch) -> None:
        if not launch.argv:
            raise ConfigError("no command")
        if not printable_abs(launch.cwd):
            raise ConfigError(f"cwd must be a printable absolute path, got {launch.cwd!r:.80}")
