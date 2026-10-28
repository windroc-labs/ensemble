"""The session layer's shared data types; times are epoch seconds."""

import re
import signal
from collections.abc import Mapping
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar, NamedTuple, Self

from ensemble.utils import (
    ACCOUNT_ENV,
    Invalid,
    StatusState,
    check_ref,
    environment_ok,
    freeze,
    printable_abs,
    sid_error,
)

__all__ = [
    "Access",
    "Activity",
    "MARKS",
    "PROCESS_EXIT",
    "Blocked",
    "ClientBound",
    "ClientHome",
    "ConfigError",
    "SessionError",
    "SessionBusy",
    "Effort",
    "EnvironmentSpec",
    "Exited",
    "HookCall",
    "HookCallback",
    "HookError",
    "HookKind",
    "Hosts",
    "IdleTimeout",
    "Key",
    "KeyPress",
    "Launch",
    "LimitHit",
    "Liveness",
    "LOCK_NAME",
    "Mode",
    "NamedKey",
    "NotShowable",
    "NoRecordedSession",
    "PaneStatus",
    "PLUGIN_SOURCE",
    "Paste",
    "Prepared",
    "ProcessOptions",
    "Reply",
    "Resumable",
    "SessionEvent",
    "SessionInfo",
    "SessionRow",
    "SessionStatus",
    "Spec",
    "Stderr",
    "Tier",
    "TurnEnd",
    "TurnFailed",
    "TurnStarted",
    "Usage",
    "Window",
    "WorkerExit",
    "WorkerHook",
    "WorkerMessage",
    "WorkerOutput",
    "StatusState",
]


def _clean_string(value) -> bool:
    return isinstance(value, str) and "\0" not in value


class ConfigError(Exception):
    pass


class Mode(StrEnum):
    HEADLESS = "headless"
    TUI = "tui"
    IN_PROCESS = "in_process"


class Permission(StrEnum):
    AUTO = "auto"
    YOLO = "yolo"


class Liveness(StrEnum):
    LIVE = "live"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class PaneStatus(StrEnum):
    RUNNING = "running"
    UNKNOWN = "unknown"  # tmux failed otherwise, or is missing


class Effort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class Resumable(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class HookKind(StrEnum):
    """TURN_STARTED: one TurnStarted per accepted turn, never a replayed record (WDRC-199 FR-10)."""

    TURN_END = "turn_end"
    TURN_FAILED = "turn_failed"
    BLOCKED = "blocked"
    ACTIVITY = "activity"
    TURN_STARTED = "turn_started"


class Tier(StrEnum):
    ONE = "1"
    TWO = "2"
    THREE = "3"
    FOUR = "4"


MARKS = (StatusState.DONE, StatusState.BLOCKED, StatusState.WORKING)  # a tui hook's activity marks


class HookError(Exception):
    """One stderr line; the hook stops."""


@dataclass(frozen=True)
class HookCallback:
    """Tui: on_hook(HookCall); Host: on_hook(word, HookCall); Spec.handlers: on_event(EventCall)."""

    module: str
    state: Mapping[str, object] = field(default_factory=dict)
    roots: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.module, str) or not all(
            part.isidentifier() for part in self.module.split(".")
        ):
            raise ConfigError(f"hook callback module {self.module!r:.80}: want a dotted name")
        if not isinstance(self.state, Mapping):
            raise ConfigError("hook callback state: want a mapping")
        if (
            not isinstance(self.roots, tuple)
            or not all(map(printable_abs, self.roots))
            or len(set(self.roots)) != len(self.roots)
        ):
            raise ConfigError("hook callback roots: want distinct printable absolute paths")

    def fields(self) -> dict:
        return {"module": self.module, "state": dict(self.state), "roots": list(self.roots)}


@dataclass(frozen=True)
class ClientHome:
    """`field`: its name in the Host's records; `dir`: its last path part; `env`: its variable."""

    field: str
    dir: str
    env: str
    credential: str
    files: tuple[tuple[str, str], ...] = ()
    persistent: tuple[str, ...] | None = None
    providers: tuple[str, ...] = ()


class Container(NamedTuple):
    bin_dir: str
    python: str
    pythonpath: str  # first: the image's src
    state_root: str

    @property
    def source(self) -> str:
        return self.pythonpath.split(":")[0]


@dataclass(frozen=True)
class ProviderProbe:
    provider: str
    base_url: str


@dataclass(frozen=True)
class ClientContext:
    host_address: str
    permission: Permission
    container: Container | None = None


LOCAL_CONTEXT = ClientContext("127.0.0.1", Permission.AUTO)


@dataclass(frozen=True)
class HookCall:
    """on_hook(call) returns the payload to journal, or None to journal nothing and stop; a host's
    on_hook(word, call) runs after the journal line.
    """

    label: str
    payload: dict
    sid: str
    workdir: str
    events: str | None
    state: Mapping[str, object]


def _member(enum, field: str, value):
    try:
        return enum(value)
    except ValueError:
        raise ConfigError(f"{field} must be one of {', '.join(enum)}, got {value!r}") from None


TAG = re.compile(r"[A-Za-z0-9_-]+")
NO_DISPLAY = "none"  # `--display none`: shown nowhere, so never a session name
SESSION_NAME = re.compile(rf"(?!{NO_DISPLAY}\Z){TAG.pattern}")
SESSION_NAME_RULE = f"{TAG.pattern} but not {NO_DISPLAY}"


COMMAND = "ensemble-session"
ACCOUNT_SET_BY = f"{ACCOUNT_ENV} is set by --account or [clients.<client>].account"
DEFAULT_ACCOUNT = "default"
RESUME = (COMMAND, "resume")


# The broker's rules (its protocol.schema.json)
PLUGIN_SOURCE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}@[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}#[a-f0-9]{40}"
    r"(?:::[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}#[a-f0-9]{40})?"
)
PLUGIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
BASE_IMAGE = re.compile(
    r"sha256:[a-f0-9]{64}|[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
)
GATEWAY = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
LOCK_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")
SECRET_ITEM = re.compile(r"ensemble-broker-env/[a-z0-9][a-z0-9._-]{0,63}")
SECRET_FILE = re.compile(r"[^/\x00-\x1f\x7f-\x9f]{1,255}")
SECRETS_MAX = 16
REPO = re.compile(r"[A-Za-z0-9-]+/(?!\.\.?\Z)[A-Za-z0-9._-]+")


def _string_tuple(value) -> bool:
    return isinstance(value, tuple) and all(isinstance(s, str) for s in value)


HIDDEN_SIZE = (80, 24)


@dataclass(frozen=True)
class TuiSettings:
    status_line: bool = False
    hidden_size: tuple[int, int] = HIDDEN_SIZE


@dataclass(frozen=True, kw_only=True)
class Access:
    dirs: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    cwd: str
    project: bool = False
    reads: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        for name in ("dirs", "commands", "reads"):
            if not _string_tuple(getattr(self, name)):
                raise ConfigError(f"{name} must be a tuple of strings")
        if not environment_ok(self.env):
            raise ConfigError("access env must map variable names to strings without NUL")
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


def _secret(value) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"item", "file"}
        and isinstance(value["item"], str)
        and SECRET_ITEM.fullmatch(value["item"]) is not None
        and isinstance(value["file"], str)
        and SECRET_FILE.fullmatch(value["file"]) is not None
        and value["file"] not in (".", "..")
        and len(value["file"].encode("utf-8")) <= 255
    )


def _prefetch(value) -> bool:
    if not (
        isinstance(value, Mapping)
        and "repo" in value
        and set(value) <= {"repo", "ref"}
        and isinstance(value["repo"], str)
        and REPO.fullmatch(value["repo"])
    ):
        return False
    try:
        return "ref" not in value or isinstance(value["ref"], str) and bool(check_ref(value["ref"]))
    except Invalid:
        return False


@dataclass(frozen=True, kw_only=True)
class EnvironmentSpec:
    """WDRC-109 FR-56; github_repo: the repo its token writes."""

    WORKER: ClassVar[str] = "@worker"
    image: str
    gateway: str
    secrets: tuple[Mapping[str, str], ...] = ()
    plugins: tuple[str, ...] = ()
    lock: str | None = None
    prefetch: Mapping[str, str] | None = None
    github_repo: str | None = None

    def __post_init__(self):
        secrets, files = self.secrets, ()
        if isinstance(secrets, tuple) and all(map(_secret, secrets)):
            files = [secret["file"] for secret in secrets]
        plugins = self.plugins
        checks = {
            "image": isinstance(self.image, str)
            and (self.image == self.WORKER or bool(BASE_IMAGE.fullmatch(self.image)))
            and len(self.image) <= 255,
            "gateway": isinstance(self.gateway, str) and bool(GATEWAY.fullmatch(self.gateway)),
            "secrets": isinstance(secrets, tuple)
            and len(files) == len(secrets) <= SECRETS_MAX
            and len(set(files)) == len(files),
            "plugins": isinstance(plugins, tuple)
            and all(
                isinstance(p, str) and (PLUGIN_SOURCE.fullmatch(p) or PLUGIN_NAME.fullmatch(p))
                for p in plugins
            )
            and len(set(plugins)) == len(plugins),
            "lock": self.lock is None
            or isinstance(self.lock, str)
            and bool(LOCK_NAME.fullmatch(self.lock)),
            "prefetch": self.prefetch is None or _prefetch(self.prefetch),
            "github_repo": self.github_repo is None
            or isinstance(self.github_repo, str)
            and bool(REPO.fullmatch(self.github_repo)),
        }
        if bad := next((name for name, ok in checks.items() if not ok), None):
            raise ConfigError(f"environment {bad}: {getattr(self, bad)!r:.120} is not valid")
        object.__setattr__(
            self, "secrets", tuple(MappingProxyType(dict(secret)) for secret in secrets)
        )
        if self.prefetch is not None:
            object.__setattr__(self, "prefetch", MappingProxyType(dict(self.prefetch)))

    def persisted(self) -> dict:
        return {
            "image": self.image,
            "gateway": self.gateway,
            "secrets": [dict(secret) for secret in self.secrets],
            "plugins": list(self.plugins),
            "lock": self.lock,
            "prefetch": None if self.prefetch is None else dict(self.prefetch),
            "github_repo": self.github_repo,
        }

    @classmethod
    def from_fields(cls, fields: Mapping) -> Self:
        if not isinstance(fields, Mapping):
            raise ConfigError(f"environment: want a table, got {type(fields).__name__}")
        names = [f.name for f in dataclass_fields(cls)]
        if unknown := sorted(str(key) for key in fields if key not in names):
            raise ConfigError(f"environment: unknown field {unknown[0]!r:.80}")
        if missing := [name for name in ("image", "gateway") if name not in fields]:
            raise ConfigError(f"environment: missing field {missing[0]!r}")
        found = dict(fields)
        for name in ("secrets", "plugins"):
            if isinstance(found.get(name), list):
                found[name] = tuple(found[name])
        return cls(**found)


@dataclass(frozen=True, kw_only=True)
class ProcessOptions:
    flags: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not (isinstance(self.flags, tuple) and all(map(_clean_string, self.flags))):
            raise ConfigError("flags must be a tuple of strings without NUL")
        if not environment_ok(self.env):
            raise ConfigError("env must map variable names to strings without NUL")
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


@dataclass(frozen=True, kw_only=True)
class Spec(ProcessOptions):
    workdir: str
    access: Access
    tier: int | None = None
    effort: Effort | None = None
    sid: str | None = None
    name: str | None = None
    reply_tags: tuple[str, str] | None = None  # caller's (progress, outcome) tags for in_process
    client: str | None = None
    mode: Mode | None = None
    prompt: str = ""
    resume: bool = False  # resume session sid
    events: str | None = None
    environment: EnvironmentSpec | None = None
    handlers: tuple[HookCallback, ...] = ()  # ordered on_event modules (FR-5)

    def __post_init__(self):
        if not printable_abs(self.workdir):
            raise ConfigError(
                f"workdir must be a printable absolute path, got {self.workdir!r:.80}"
            )
        if self.tier is not None and (
            isinstance(self.tier, bool)
            or not isinstance(self.tier, int)
            or str(self.tier) not in tuple(Tier)
        ):
            raise ConfigError(f"tier must be an integer 1–4, got {self.tier!r}")
        if self.effort is not None:
            object.__setattr__(self, "effort", _member(Effort, "effort", self.effort))
        if not printable_abs(self.access.cwd):
            raise ConfigError(f"cwd must be a printable absolute path, got {self.access.cwd!r:.80}")
        if not all(map(printable_abs, self.access.dirs)):
            raise ConfigError("dirs must contain only printable absolute paths")
        if self.sid is not None and (error := sid_error(self.sid)):
            raise ConfigError(error)
        super().__post_init__()
        if self.name is not None and not isinstance(self.name, str):
            raise ConfigError("name must be None or a string")
        if self.reply_tags is not None and not (
            isinstance(self.reply_tags, tuple)
            and len(self.reply_tags) == 2
            and all(isinstance(t, str) and TAG.fullmatch(t) for t in self.reply_tags)
        ):
            raise ConfigError(
                f"reply_tags must be None or (progress tag, outcome tag), each {TAG.pattern}"
            )
        if self.client is not None and not isinstance(self.client, str):
            raise ConfigError("client must be None or a string")
        if self.mode is not None:
            object.__setattr__(self, "mode", _member(Mode, "mode", self.mode))
        if not isinstance(self.prompt, str):
            raise ConfigError("prompt must be a string")
        if not isinstance(self.resume, bool):
            raise ConfigError(f"resume must be a bool, got {self.resume!r:.80}")
        if self.resume and self.sid is None:
            raise ConfigError("resume: needs spec.sid, the session to resume")
        if self.events is not None and not (
            isinstance(self.events, str) and self.events.isprintable()
        ):
            raise ConfigError(f"events must be a printable path, got {self.events!r:.80}")
        if self.environment is not None and not isinstance(self.environment, EnvironmentSpec):
            raise ConfigError("environment must be None or an EnvironmentSpec")
        if not isinstance(self.handlers, tuple) or not all(
            isinstance(handler, HookCallback) for handler in self.handlers
        ):
            raise ConfigError("handlers must be a tuple of HookCallback")


@dataclass(frozen=True)
class Launch:
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    unset: tuple[str, ...] = ()
    cwd: str = ""
    transcript: str = ""
    resume: str = ""  # a human's shell command, each interpolated field shlex.quoted

    project: bool | None = None
    blocked_signals: tuple[int, ...] = ()

    def __post_init__(self):
        if not isinstance(self.blocked_signals, tuple) or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value not in signal.valid_signals()
            or value in (signal.SIGKILL, signal.SIGSTOP)
            for value in self.blocked_signals
        ):
            raise ConfigError("launch: blocked_signals must be a tuple of catchable signal numbers")
        if self.project is not None and not isinstance(self.project, bool):
            raise ConfigError("launch: project must be a bool or None")
        env = self.env
        for name, ok in (
            ("argv", isinstance(self.argv, tuple) and all(map(_clean_string, self.argv))),
            (
                "env",
                environment_ok(env),
            ),
            ("unset", isinstance(self.unset, tuple) and all(map(_clean_string, self.unset))),
        ):
            if not ok:
                raise ConfigError(f"launch: {name} must be strings without NUL")
        object.__setattr__(self, "env", MappingProxyType(dict(env)))


_IDENTITY = {
    "sid": lambda v: sid_error(v) is None,
    "cwd": printable_abs,
    "transcript": lambda v: v == "" or printable_abs(v),
    "project": lambda v: isinstance(v, bool),
    "resume": lambda v: isinstance(v, str),
}


@dataclass(frozen=True)
class SessionInfo:
    """persisted() and from_fields are its one codec. Read back (agent-writable), it is a status hint,
    never an authorization, a path to open or argv to run without re-validation.
    """

    sid: str
    cwd: str
    project: bool
    transcript: str
    resume: str

    def persisted(self) -> dict:
        return {
            "sid": self.sid,
            "cwd": self.cwd,
            "project": self.project,
            "transcript": self.transcript,
            "resume": self.resume,
        }

    @classmethod
    def from_fields(cls, fields: Mapping, *, strict: bool = False) -> "SessionInfo | None":
        """persisted()'s inverse; invalid: None, or with strict a ValueError naming its key."""
        found = fields if isinstance(fields, Mapping) else {}
        get = found.get
        for key, ok in _IDENTITY.items():
            if not ok(get(key, "" if key in ("transcript", "resume") else None)):
                if strict:
                    raise ValueError(key)
                return None
        return cls(
            found["sid"], found["cwd"], found["project"], get("transcript", ""), get("resume", "")
        )


@dataclass(frozen=True)
class Prepared:
    spec: Spec
    info: SessionInfo
    argv: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


@dataclass(frozen=True)
class ClientBound:
    """A run.jsonl client_session line, never a SessionEvent."""

    client_id: str


@dataclass(frozen=True)
class TurnStarted:
    """Never for a replayed record."""


@dataclass(frozen=True)
class Activity:
    detail: str = ""


@dataclass(frozen=True)
class Reply:
    text: str


@dataclass(frozen=True)
class TurnEnd:
    reply: str = ""
    pending: int = 0


@dataclass(frozen=True)
class TurnFailed:
    error: str
    detail: str = ""


PROCESS_EXIT = "process_exit"  # TurnFailed.error


@dataclass(frozen=True)
class Blocked:
    reason: str = ""


@dataclass(frozen=True)
class LimitHit:
    resets_at: float | None = None


@dataclass(frozen=True)
class Exited:
    returncode: int


SessionEvent = TurnStarted | Activity | Reply | TurnEnd | TurnFailed | Blocked | LimitHit | Exited


@dataclass(frozen=True)
class WorkerHook:
    """Relayed by a worker's guard (WDRC-199 FR-15); untrusted (FR-36): check size and shape."""

    label: str
    payload: Mapping[str, object]
    delivery_id: str


@dataclass(frozen=True)
class WorkerOutput:
    """FR-12, FR-15: its reader parses complete lines only."""

    data: bytes
    stream: str = "stdout"


@dataclass(frozen=True)
class WorkerExit:
    """returncode -N: signal N ended it (FR-16)."""

    returncode: int


WorkerMessage = WorkerHook | WorkerOutput | WorkerExit


@dataclass(frozen=True)
class Window:
    used_percent: float  # may exceed 100
    resets_at: float | None  # None: not given


@dataclass(frozen=True)
class Usage:
    five_hour: Window | None  # None: not reported
    weekly: Window | None


@dataclass(frozen=True)
class SessionStatus:
    resumable: Resumable  # given its plain value too
    last_active: float | None
    resume_command: str

    def __post_init__(self):
        object.__setattr__(self, "resumable", _member(Resumable, "resumable", self.resumable))


@dataclass(frozen=True)
class Stderr:
    text: str


@dataclass(frozen=True)
class Paste:
    text: str


@dataclass(frozen=True)
class Key:
    name: str


class NamedKey(StrEnum):
    ENTER = "Enter"
    ESCAPE = "Escape"
    TAB = "Tab"
    BTAB = "BTab"
    BSPACE = "BSpace"
    UP = "Up"
    DOWN = "Down"
    LEFT = "Left"
    RIGHT = "Right"
    HOME = "Home"
    END = "End"
    PAGE_UP = "PageUp"
    PAGE_DOWN = "PageDown"
    F1 = "F1"
    F2 = "F2"
    F3 = "F3"
    F4 = "F4"
    F5 = "F5"
    F6 = "F6"
    F7 = "F7"
    F8 = "F8"
    F9 = "F9"
    F10 = "F10"
    F11 = "F11"
    F12 = "F12"


MODIFIERS = {"C-": "ctrl", "M-": "meta", "S-": "shift"}
KEY_RULE = (
    f"want one printable character or one of {', '.join(NamedKey)}, after any of "
    f"{', '.join(MODIFIERS)}"
)


@dataclass(frozen=True)
class KeyPress:
    """`send --key`'s notation, e.g. `C-c`, `S-Up`; M- is Alt."""

    key: str
    ctrl: bool = False
    meta: bool = False
    shift: bool = False

    def __post_init__(self):
        if not (
            isinstance(self.key, str)
            and (self.key in tuple(NamedKey) or len(self.key) == 1 and self.key.isprintable())
        ):
            raise ConfigError(f"key {self.key!r:.80}: {KEY_RULE}")
        if not all(isinstance(getattr(self, flag), bool) for flag in MODIFIERS.values()):
            raise ConfigError("key modifiers: want booleans")

    @classmethod
    def parse(cls, text) -> Self:
        if not isinstance(text, str):
            raise ConfigError(f"key {text!r:.80}: want text")
        flags: dict[str, bool] = {}
        rest = text
        while len(rest) > 2 and rest[:2] in MODIFIERS:
            flag = MODIFIERS[rest[:2]]
            if flag in flags:
                raise ConfigError(f"key {text!r:.80}: {rest[:2]} twice")
            flags[flag] = True
            rest = rest[2:]
        try:
            return cls(rest, **flags)
        except ConfigError:
            raise ConfigError(f"key {text!r:.80}: {KEY_RULE}") from None

    def __str__(self) -> str:
        on = (prefix for prefix, flag in MODIFIERS.items() if getattr(self, flag))
        return "".join(on) + self.key


class SessionError(Exception):
    """A failed start, or a call its mode lacks."""


class SessionBusy(SessionError):
    """holder: "<container> (owner <owner>)"; PATTERN: (lock, holder) of its message in a text."""

    PATTERN: ClassVar[re.Pattern] = re.compile(r"(?:^|: )busy: lock (\S+) is held by (.+)\Z", re.S)

    def __init__(self, lock: str, holder: str) -> None:
        self.lock, self.holder = lock, holder
        super().__init__(f"busy: lock {lock} is held by {holder}")


class Unanswered(SessionError):
    """A Host probe that timed out or failed: no proof the session ended."""


class OwnerRefused(SessionError):
    """A live session harness refused a stop or close."""


class NoRecordedSession(SessionError):
    """The requested sid has no durable session record in its workdir."""


class NotShowable(SessionError):
    """`host <name> cannot show`, or the Delegate's `no showable session`."""


@dataclass(frozen=True)
class IdleTimeout:
    seconds: float


@dataclass(frozen=True)
class SessionRow:
    """display: its Host's, opaque; shown: the session showing it, "" unnamed, None none."""

    name: str
    sid: str | None
    client: str | None
    state: StatusState
    display: str | None
    shown: str | None
    last_lines: str | None
    own: bool

    def __post_init__(self):
        object.__setattr__(self, "state", _member(StatusState, "state", self.state))


@dataclass(frozen=True)
class Hosts:
    default: str
    tables: Mapping[str, Mapping]

    def __post_init__(self):
        object.__setattr__(self, "tables", freeze(self.tables))
