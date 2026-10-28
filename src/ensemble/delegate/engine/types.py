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
    """Spec.workdir: agent is cwd/only write grant; run holds harness state."""

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
        return (os.path.join(self.agent, "src"), os.path.join(self.agent, "publish"), self.tmp)

    @property
    def drive_log(self) -> str:
        return os.path.join(self.run, "drive.log")

    @property
    def out(self) -> str:
        return os.path.join(self.run, "out.md")

    def in_place(self) -> bool:
        """agent is in place, without symlinks."""
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
        """agent/src/<owner>/<name> dirs whose paths are real."""
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
    """One run: workdir=Layout; resume uses sid. None: client=role, team=built-in,
    out=<run>/out.md for sessions, else unsaved. name/events: tui in plan(), all modes in
    HarnessDelegate (agent name, harness <name>-drive, detached outcome events).
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
    credentials: str | None = None
    environment_credentials: str | None = None


@dataclass(frozen=True)
class DriverSpec:
    session: SessionSpec
    out: str


@dataclass(frozen=True)
class Plan:
    """spec: client/sid/team set, paths absolute; session: SessionSpec; docker: container,
    repo: then its Reference repo.
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
    plan: Plan
    agent: str | None
    host: str
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
    """DRAFTED: failed/needs_input PR returned to draft."""

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
    spec: EnvironmentSpec
    prefetch: bool
    credentials: str | None = None


@dataclass(frozen=True)
class DelegateState:
    """delegate.json: previous=attempt-1 Result after follow-up; bound=owned worker took attempt;
    closing=close stopped worker, no follow-up; drive=ProcessControl path.
    """

    role: str
    plan: Plan
    session_cls: str | None
    host: str
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
    closing: bool = False
    drive: str | None = None
