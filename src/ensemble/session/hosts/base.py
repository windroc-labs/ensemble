"""Host: where processes live, each with its Record and Display."""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import stat
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import ClassVar

from ensemble.session.engine.types import (
    HIDDEN_SIZE,
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
HIDDEN = re.compile(r"([1-9][0-9]{0,3})x([1-9][0-9]{0,3})")
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
_PHASE: ContextVar[Callable[[str | None, float], None] | None] = ContextVar("phase", default=None)


@contextlib.contextmanager
def phase_reporter(report: Callable[[str | None, float], None]) -> Iterator[None]:
    """Threads started in this context do not inherit `report`."""
    token = _PHASE.set(report)
    try:
        yield
    finally:
        _PHASE.reset(token)


@contextlib.contextmanager
def start_phase(phase: str, wait: float) -> Iterator[None]:
    report = _PHASE.get()
    if report is None:
        yield
        return
    report(phase, wait)
    try:
        yield
    finally:
        report(None, 0)


def sent(folder: str) -> str | None:
    """Tui dir `folder`'s PROMPTED, read before the hook record it goes in."""
    try:
        data = read_regular(os.path.join(folder, PROMPTED))
    except OSError:
        return None
    return None if data is None else data.decode(errors="replace").strip()


def checked_events(path: str | None) -> str | None:
    try:
        return None if path is None else validate_events(path)
    except ValueError as error:
        raise ConfigError(str(error)) from None


def created_events(path: str | None) -> str | None:
    path = checked_events(path)
    try:
        return None if path is None else events_file(path)
    except ValueError as error:
        raise SessionError(str(error)) from None


@dataclass(frozen=True)
class WorkerState:
    sid: str | None
    generation: str | None
    client: str | None
    display: str | None
    owner: str | None
    host: str | None = None


def worker_dir(name: str) -> str:
    return os.path.join(os.path.expanduser(TUI_DIR), name)


def worker_names() -> list[str]:
    try:
        with os.scandir(os.path.expanduser(TUI_DIR)) as entries:
            return sorted(
                e.name
                for e in entries
                if SESSION_NAME.fullmatch(e.name) and e.is_dir(follow_symlinks=False)
            )
    except OSError:
        return []


def worker_row(name: str, found: WorkerState | None, state: StatusState) -> SessionRow:
    return SessionRow(
        name,
        None if found is None else found.sid,
        None if found is None else found.client,
        state,
        None if found is None else found.display,
        None,
        None,
        own=False,
    )


def worker_file(name: str, file: str, limit: int) -> bytes | None:
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
        text("sid"),
        text("generation"),
        text("client"),
        text("display"),
        text("owner"),
        text("host"),
    )


def activity(name: str, generation: str) -> StatusState | None:
    data = worker_file(name, ACTIVITY, ACTIVITY_MAX)
    fields = b"".join((data or b"").split(b"\n", 1)[:1]).decode(errors="replace").split(" ")
    if len(fields) != 2 or fields[0] != generation or fields[1] not in MARKS:
        return None
    return StatusState(fields[1])


def stored_record(name: str, generation: str) -> Record | None:
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
    """A managed worker's handle."""

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


def _hidden_size(value) -> tuple[int, int]:
    found = HIDDEN.fullmatch(value) if isinstance(value, str) else None
    if found is None:
        raise ConfigError(
            '[hosts] hidden_size: want "<columns>x<rows>", each 1 to 9999, e.g. "80x24"'
        )
    return int(found[1]), int(found[2])


def host_resume(host: Host, name: str | None, cwd: str) -> str | None:
    argv = None if name is None else host._resume_argv(name)
    return resume_command(cwd, {}, argv) if argv else None


class Host(ABC):
    """settings excludes implementation/isolated/hidden_size; Launch.env passes verbatim. Exit events:
    `exited <rc>` / `dead`; unknown attach: SessionError; list: sorted names. Viewer reads display.
    """

    attachable: ClassVar[bool] = False
    _environments: ClassVar[bool] = False  # runs Spec.environment
    _remote_tui: ClassVar[bool] = False  # its owned tui workers are reached only through a guard
    _named_headless: ClassVar[bool] = False

    def _provider_usage(self, request: ProviderProbe) -> bool | None:
        return None

    def _headless_owner(self, name: str) -> Mapping[str, str] | None:
        """The harness dir, sid and generation owning named headless run `name` (Docker)."""
        return None

    def __init__(self, name: str, settings: Mapping | None = None) -> None:
        self.name = name
        self.settings = thaw(settings or {})
        self._isolated = self.settings.pop("isolated", False)
        if not isinstance(self._isolated, bool):
            raise ConfigError(f"[hosts.{name}] isolated: want true or false")
        self._hidden_size = (
            _hidden_size(self.settings.pop("hidden_size"))
            if "hidden_size" in self.settings
            else HIDDEN_SIZE
        )

    @classmethod
    def parse_hosts(cls, table) -> Hosts:
        if not isinstance(table, Mapping):
            raise ConfigError("[hosts]: missing")
        tables = {}
        for key, value in table.items():
            if key in ("default", "hidden_size"):
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
            if "hidden_size" in value:
                raise ConfigError(f"[hosts.{key}] hidden_size: set it in [hosts], for every Host")
            tables[key] = value
        if "hidden_size" in table:  # one size for every Host: each Host's own settings carry it
            _hidden_size(table["hidden_size"])
            tables = {k: {**v, "hidden_size": table["hidden_size"]} for k, v in tables.items()}
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

    def remove_workspaces(self, cwd: str) -> list[str]:
        """cwd's workspaces, force-removed."""
        return []

    def _release(self, name: str, generation: str) -> None:
        """Removes `generation`'s leftovers once worker_liveness is ABSENT; override with worker."""
        return None

    @abstractmethod
    def list(self) -> list[str]: ...

    def rows(self) -> list[SessionRow]:
        rows = []
        for name in self.list():
            found = worker_state(name)
            word = (
                None
                if found is None or found.generation is None
                else activity(name, found.generation)
            )
            rows.append(worker_row(name, found, word or StatusState.WORKING))
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
        """None unless attachable."""
        return None

    _inherit_env = True

    def _client_context(self) -> ClientContext:
        if self._isolated:
            return replace(LOCAL_CONTEXT, permission=Permission.YOLO)
        return LOCAL_CONTEXT

    def _client_env(
        self, name: str, client: str, home: ClientHome | None, *, resume: bool
    ) -> Mapping[str, str]:
        """ConfigError: `client` cannot run on this Host."""
        return {}

    def _resume_argv(self, name: str) -> tuple[str, ...] | None:
        """None: the client's."""
        return None

    def tui_settings(self) -> TuiSettings:
        return TuiSettings(hidden_size=self._hidden_size)

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
