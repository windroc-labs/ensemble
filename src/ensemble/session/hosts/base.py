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


def stored_record(name: str, generation: str) -> Record | None:
    """Worker `name`'s crew Record while it is generation `generation`'s."""
    data = worker_file(name, RECORD, STATE_MAX)
    held, tab, line = (data or b"").decode(errors="replace").partition("\t")
    if not tab or held != generation:
        return None
    try:
        return Record.parse(line)
    except (ValueError, TypeError):
        return None


def tui_lock(name: str) -> str:
    return os.path.join(os.path.expanduser(TUI_DIR), f"{name}.lock")


class InteractiveProcess(ABC):
    """A managed worker's handle (docs/architecture.md › Session harness interfaces): send and
    press into its input (Key names: the Client's), its screen, liveness and confirmed close.
    """

    name: str
    generation: str

    @abstractmethod
    def send(self, action: Paste | Key) -> None: ...

    @abstractmethod
    def press(self, keys: Sequence[KeyPress]) -> None: ...

    def screen(self, lines: int | None = None) -> str:
        raise NotShowable(f"session {self.name}: its host cannot show")

    @abstractmethod
    def state(self) -> Liveness: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def returncode(self) -> int | None: ...

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
    def list(self) -> list[str]: ...

    def rows(self) -> list[SessionRow]:
        """One row per listed name: sid, client and display from its worker state (none: None),
        state from its activity (none: working).
        """
        rows = []
        for name in self.list():
            found = worker_state(name)
            word = (
                None
                if found is None or found.generation is None
                else activity(name, found.generation)
            )
            rows.append(
                SessionRow(
                    name,
                    None if found is None else found.sid,
                    None if found is None else found.client,
                    word or StatusState.WORKING,
                    None if found is None else found.display,
                    None,
                    None,
                    own=False,
                )
            )
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
