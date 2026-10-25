"""Record v1, one JSON line naming a started thing by its outermost starter (ctl queries it, resume
resumes it; only a LayerCommand, in the exact form that command prints, so a forged line runs
nothing else), and Status, the line `<ctl> status` prints per Record (alive: its owner's own;
state: its sessions' StatusState word, live after an ended owner left one open; hidden: hide closed what show opened; cwd, optional: where the run works). Display is opaque: only the
session layer interprets it. answer_keys is the one batch rule of every ctl's queries. json_records yields JSON objects from text or byte lines.
"""

import json
import math
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import MISSING, asdict, dataclass, fields
from enum import StrEnum
from typing import Self, TextIO, TypeVar

from ensemble.utils.text import printable

Display = str | None
T = TypeVar("T")


class LayerCommand(StrEnum):
    DELEGATE = "ensemble-delegate"
    ORCHESTRATOR = "ensemble-orchestrator"
    SESSION = "ensemble-session"


class StatusState(StrEnum):
    STARTING = "starting"  # launched, no Record yet
    WORKING = "working"  # its session is busy, not known to be done or blocked
    DONE = "done"
    BLOCKED = "blocked"
    DEAD = "dead"  # its session is listed, its client exited
    GONE = "gone"  # no session runs
    UNKNOWN = "unknown"  # its host could not tell


class OutcomeStatus(StrEnum):
    DONE = "done"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"


class RecordError(ValueError):
    pass


def _text(value) -> bool:
    return isinstance(value, str) and value != "" and value.isprintable()


def _check(*, ok: bool, message: str) -> None:
    if not ok:
        raise RecordError(message)


@dataclass(frozen=True)
class _Line:
    def to_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def parse(cls, line: str) -> Self:
        kind = f"{cls.__name__} line"
        _check(ok=isinstance(line, str), message=f"{kind}: want a str")
        try:
            data = json.loads(line.removesuffix("\n"))
        except (ValueError, RecursionError) as e:
            raise RecordError(f"{kind}: not JSON ({e})") from None
        _check(ok=isinstance(data, dict), message=f"{kind}: want a JSON object")
        names = [f.name for f in fields(cls)]
        for f in fields(cls):
            _check(ok=f.name in data or f.default is not MISSING, message=f"{f.name}: missing")
        for name in data:
            _check(ok=name in names, message=f"{name}: unknown field")
        return cls(**{k: tuple(v) if isinstance(v, list) else v for k, v in data.items()})


@dataclass(frozen=True)
class Record(_Line):
    v: int
    kind: str
    name: str
    sessions: tuple[str, ...]
    sid: str | None
    workdir: str | None
    ctl: tuple[str, ...]
    resume: tuple[str, ...]
    display: Display
    detail: str | None

    def __post_init__(self):
        _check(ok=type(self.v) is int and self.v == 1, message="v: want 1")
        for field in ("kind", "name"):
            _check(
                ok=_text(getattr(self, field)), message=f"{field}: want a non-empty printable str"
            )
        _check(
            ok=isinstance(self.sessions, tuple) and all(map(_text, self.sessions)),
            message="sessions: want a list of non-empty printable str",
        )
        for field in ("sid", "workdir", "detail"):
            value = getattr(self, field)
            _check(
                ok=value is None or _text(value),
                message=f"{field}: want null or a non-empty printable str",
            )
        for field in ("ctl", "resume"):
            argv = getattr(self, field)
            _check(
                ok=isinstance(argv, tuple) and bool(argv) and all(isinstance(a, str) for a in argv),
                message=f"{field}: want a non-empty list of str",
            )
        _check(
            ok=len(self.ctl) == 1 and self.ctl[0] in tuple(LayerCommand),
            message=f"ctl: want [C], C one of {', '.join(LayerCommand)}",
        )
        _check(
            ok=len(self.resume) == 3
            and self.resume[:2] == (self.ctl[0], "resume")
            and _text(self.resume[2])
            and not self.resume[2].startswith("-"),
            message="resume: want [ctl[0], resume, KEY], KEY printable and not starting with -",
        )
        _check(
            ok=self.display is None or isinstance(self.display, str),
            message="display: want null or a str",
        )


@dataclass(frozen=True, kw_only=True)
class Status(_Line):
    name: str
    alive: bool
    state: StatusState
    outcome: OutcomeStatus | None
    attach: str | None
    hidden: bool
    cwd: str | None = None

    def __post_init__(self):
        _check(ok=_text(self.name), message="name: want a non-empty printable str")
        _check(ok=isinstance(self.alive, bool), message="alive: want true or false")
        _check(
            ok=self.state in tuple(StatusState),
            message=f"state: want one of {', '.join(StatusState)}",
        )
        object.__setattr__(self, "state", StatusState(self.state))
        _check(ok=isinstance(self.hidden, bool), message="hidden: want true or false")
        _check(
            ok=self.outcome is None or self.outcome in tuple(OutcomeStatus),
            message=f"outcome: want null or one of {', '.join(OutcomeStatus)}",
        )
        if self.outcome is not None:
            object.__setattr__(self, "outcome", OutcomeStatus(self.outcome))
        _check(
            ok=self.attach is None or isinstance(self.attach, str),
            message="attach: want null or a str",
        )
        _check(
            ok=self.cwd is None or _text(self.cwd),
            message="cwd: want null or a non-empty printable str",
        )


def answer_keys(
    args: Sequence[str],
    stdin: TextIO,
    resolve: Callable[[str | Record], tuple[str, T]],
    reply: Callable[[T], str | None],
    *,
    single: bool = False,
    usage: tuple[type[Exception], ...],
    failures: tuple[type[Exception], ...],
    prog: str,
    out: TextIO,
    err: TextIO,
) -> int:
    """Keys are args, else one Record per stdin line (stdin unread when args). Resolves each key to
    (name, value) in order, then writes each reply(value) that is not None. A usage or failures
    exception from either step prints `<label>: <error>` and drops the key. Returns 2 on a bad stdin
    line, a key count other than one when single, a name resolved twice (nothing replied) or a
    usage error; else 1 if a key failed; else 0.
    """

    def complain(text: str) -> None:
        err.write(printable(f"{prog}: {text}") + "\n")

    def failed(key: str | Record, e: Exception) -> int:
        complain(f"{key if isinstance(key, str) else key.name}: {e}")
        return 2 if isinstance(e, usage) else 1

    keys: list[str | Record] = list(args)
    if not keys:
        for n, line in enumerate(stdin, 1):
            try:
                keys.append(Record.parse(line))
            except RecordError as e:
                complain(f"stdin line {n}: {e}")
                return 2
    if single and len(keys) != 1:
        complain(f"takes exactly one key, got {len(keys)}")
        return 2
    codes = [0]
    resolved: list[tuple[str | Record, str, T]] = []
    for key in keys:
        try:
            name, value = resolve(key)
        except usage + failures as e:
            codes.append(failed(key, e))
            continue
        resolved.append((key, name, value))
    names = set()
    for _, name, _ in resolved:
        if name in names:
            complain(f"name {name} given more than once")
            return 2
        names.add(name)
    for key, _, value in resolved:
        try:
            text = reply(value)
        except usage + failures as e:
            codes.append(failed(key, e))
            continue
        if text is not None:
            out.write(text + "\n")
    return max(codes)


def json_records(lines: Iterable[str | bytes]) -> Iterator[dict]:
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(record, dict):
            yield record


def number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None
