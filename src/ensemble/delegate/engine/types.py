"""The Delegate's shared types."""

import contextlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict

from ensemble.delegate.composer import ConfigError, RunConfig, RunParams, Team
from ensemble.delegate.composer.types import DELIVERABLE, TMP
from ensemble.delegate.event.types import Outcome
from ensemble.session import EnvironmentSpec
from ensemble.session import Spec as SessionSpec
from ensemble.utils import Display
from ensemble.utils import Stopped as Stopped

__all__ = [
    "ConfigError",
    "Layout",
    "Spec",
    "Plan",
    "Limit",
    "Result",
    "Instruction",
    "Begun",
    "Progress",
    "Finished",
    "Event",
    "GateStatus",
    "Gated",
]


class Outer(TypedDict, total=False):
    kind: str
    name: str
    ctl: Sequence[str]
    resume: Sequence[str]


@dataclass(frozen=True)
class Layout:
    """A run's workdir (Spec.workdir): `agent`, the agent's cwd and only write grant; `run`, the
    harness's state and records.
    """

    workdir: str

    def __post_init__(self):
        object.__setattr__(self, "workdir", os.path.abspath(self.workdir))

    @property
    def agent(self) -> str:
        return os.path.join(self.workdir, "agent")

    @property
    def run(self) -> str:
        return os.path.join(self.workdir, "run")

    @property
    def tmp(self) -> str:
        return os.path.join(self.agent, TMP)

    @property
    def scratch(self) -> tuple[str, ...]:
        """`agent`'s checkouts (`src`, the report repo's clone `publish`) and temp files."""
        return (os.path.join(self.agent, "src"), os.path.join(self.agent, "publish"), self.tmp)

    @property
    def out(self) -> str:
        """A session run's default `out`."""
        return os.path.join(self.run, "out.md")

    def in_place(self) -> bool:
        """Whether `agent` is a real directory where it belongs, reached through no symlink."""
        real = os.path.join(
            os.path.realpath(os.path.dirname(self.workdir)),
            os.path.relpath(self.agent, os.path.dirname(self.workdir)),
        )
        return (
            not os.path.islink(self.agent)
            and os.path.isdir(self.agent)
            and os.path.realpath(self.agent) == real
        )

    def checkouts(self) -> tuple[str, ...] | None:
        """Delegate.checkouts: the dirs at `agent`/src/<owner>/<name> that are their own real path."""
        if not os.path.lexists(self.agent):
            return None
        if not self.in_place():
            return ()
        src, found = os.path.join(os.path.realpath(self.agent), "src"), []
        with contextlib.suppress(OSError):
            for owner in sorted(os.listdir(src)):
                with contextlib.suppress(OSError):
                    names = sorted(os.listdir(os.path.join(src, owner)))
                    found += [os.path.join(src, owner, name) for name in names]
        return tuple(p for p in found if os.path.realpath(p) == p and os.path.isdir(p))


@dataclass(frozen=True)
class Spec:
    """One run to start: `workdir` is the run's workdir (Layout); `resume` resumes session `sid`;
    client None: the role's; `out` None: `<run>/out.md` for a session run, else nothing is saved;
    `team` None: the built-in one. name and events: tui only for plan(); HarnessDelegate takes them
    in every mode (`name`: its agent session, `<name>-drive` its harness's; `events`: also the
    detached harness's outcome line).
    """

    input: str
    workdir: str
    task: str | None = None
    mode: str = "headless"
    client: str | None = None
    sid: str | None = None
    resume: bool = False
    name: str | None = None
    events: str | None = None
    out: str | None = None
    repo: str | None = None
    cwd: str | None = None
    team: Team | None = None


@dataclass(frozen=True)
class DriverSpec:
    session: SessionSpec
    out: str


@dataclass(frozen=True)
class Plan:
    """`spec` resolved: its client, sid and team set, its paths absolute; `session` the session's
    Spec; `docker` whether the agent runs in a container, then `repo` its Reference repo.
    """

    spec: Spec
    session: SessionSpec
    run: RunConfig
    repo: str | None = None
    docker: bool = False

    @property
    def prompt(self) -> str:
        return self.session.prompt

    @property
    def params(self) -> RunParams:
        return RunParams(
            input=self.spec.input,
            out=self.driver_spec.out,
            workdir=Layout(self.spec.workdir).agent,
            resume=self.spec.resume,
            docker=self.docker,
        )

    @property
    def driver_spec(self) -> DriverSpec:
        return DriverSpec(
            self.session,
            self.spec.out or os.path.join(Layout(self.spec.workdir).agent, DELIVERABLE),
        )

    @property
    def client(self) -> str:
        return self.spec.client or self.run.client

    @property
    def sid(self) -> str:
        sid = self.spec.sid
        if sid is None:
            raise ConfigError("plan has no session id")
        return sid


@dataclass(frozen=True)
class Preview:
    """A construction's plan and placement; agent None: unnamed."""

    plan: Plan
    agent: str | None
    harness: str
    host: str
    harness_host: str
    callback: tuple[str, ...] | None


@dataclass(frozen=True)
class Limit:
    """resets_at: epoch seconds or None."""

    resets_at: float | None


@dataclass(frozen=True)
class Result:
    returncode: int
    outcome: Outcome | None
    error: str = ""
    limit: Limit | None = None
    instruction: str | None = None


@dataclass(frozen=True)
class Instruction:
    path: str


@dataclass(frozen=True)
class Begun:
    sid: str


@dataclass(frozen=True)
class Progress:
    name: str
    text: str


@dataclass(frozen=True)
class Finished:
    result: Result


Event = Begun | Progress | Finished


class GateStatus(StrEnum):
    """The ready gate's act; drafted: a failed or needs_input run's PR put back in draft."""

    SKIPPED = "skipped"
    PASSED = "passed"
    FAILED = "failed"
    DRAFTED = "drafted"
    KEPT = "kept"


@dataclass(frozen=True)
class Gated:
    outcome: Outcome
    status: GateStatus
    reason: str


@dataclass(frozen=True)
class Resolved:
    """A run's environment from the config layers (engine/environment.py)."""

    spec: EnvironmentSpec
    prefetch: bool


@dataclass(frozen=True)
class DelegateState:
    """delegate.json's run state (engine/record.py); previous: attempt - 1's Result once a
    follow-up opened attempt; bound: an owned attempt's worker took it (its owner phase).
    """

    role: str
    plan: Plan
    session_cls: str | None
    host: str
    harness_host: str
    display: Display
    callback: tuple[str, ...] | None
    agent_name: str
    harness_name: str
    attempt: int
    outer: Outer
    overlays: tuple[Mapping, ...]
    detached: bool = False
    completed: bool = False
    result: Result | None = None
    events: str | None = None
    start_failure: str | None = None
    previous: Result | None = None
    bound: bool = False
