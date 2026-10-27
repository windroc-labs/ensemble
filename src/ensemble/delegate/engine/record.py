"""delegate.json's state and codec; never configures the Session, so a session harness can use it."""

from __future__ import annotations

import json
import os
import signal
from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace

from ensemble.delegate.composer import RunConfig, Team
from ensemble.delegate.engine.runner import say, stopped
from ensemble.delegate.engine.state import RunState
from ensemble.delegate.engine.types import (
    ConfigError,
    DelegateState,
    Layout,
    Limit,
    Plan,
    Result,
    Spec,
    Stopped,
)
from ensemble.delegate.event.types import Outcome
from ensemble.session import Access, EnvironmentSpec, HookCallback, SessionStatusLog
from ensemble.session import ConfigError as SessionConfigError
from ensemble.session import Spec as SessionSpec


def _encode(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


def _result(data: dict | None) -> Result | None:
    if data is None:
        return None
    outcome, limit = data.pop("outcome"), data.pop("limit")
    return Result(
        **data,
        outcome=None if outcome is None else Outcome(**outcome),
        limit=None if limit is None else Limit(**limit),
    )


HANDLER = "ensemble.delegate.engine.handler"


CLOSED = stopped(Stopped(128 + signal.SIGTERM))


def owned(plan: Plan) -> bool:
    """A run planned with the delegation handler: a session harness owns its worker."""
    return any(h.module == HANDLER for h in plan.session.handlers)


def worker(state: DelegateState) -> int | None:
    found = next(
        (h.state.get("worker") for h in state.plan.session.handlers if h.module == HANDLER), None
    )
    return found if type(found) is int else None


class RunRecord:
    def __init__(self, layout: Layout, store: RunState | None = None) -> None:
        self.layout = layout
        self.store = store or RunState(layout)

    def read(self) -> DelegateState:
        return self.parse(self.store.read())

    def save(self, state: DelegateState) -> None:
        team = state.plan.spec.team
        persisted = replace(
            state, plan=replace(state.plan, spec=replace(state.plan.spec, team=None))
        )
        values = {
            field.name: _encode(getattr(persisted, field.name)) for field in fields(persisted)
        }
        accepted = {
            "session": _encode(state.plan.session),
            "run": _encode(state.plan.run),
            "repo": state.plan.repo,
            "docker": state.plan.docker,
        }
        spec = {
            field.name: _encode(getattr(state.plan.spec, field.name))
            for field in fields(state.plan.spec)
            if field.name != "team"
        }
        spec["team"] = None if team is None else {"root": team.root, "dirs": team.dirs}
        values["plan"], values["spec"], values["version"] = accepted, spec, 1
        self.store.write(json.dumps(values))

    def parse(self, payload: bytes | None) -> DelegateState:
        try:
            if payload is None:
                raise ValueError("missing metadata")
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError("metadata must be an object")
            if data.pop("version") != 1:
                raise ValueError("unsupported metadata version")
            given = data.pop("spec")
            team = given.pop("team")
            spec = Spec(
                **given, team=None if team is None else Team(team["root"], tuple(team["dirs"]))
            )
            if os.path.realpath(spec.workdir) != os.path.realpath(self.layout.workdir):
                raise ValueError("metadata workdir differs")
            planned = data.pop("plan")
            session = planned["session"]
            access = session["access"]
            for key in ("dirs", "reads", "commands"):
                access[key] = tuple(access[key])
            session["access"] = Access(**access)
            session["flags"] = tuple(session["flags"])
            session["reply_tags"] = tuple(session["reply_tags"])
            if session.get("environment") is not None:
                session["environment"] = EnvironmentSpec.from_fields(session["environment"])
            session["handlers"] = tuple(
                HookCallback(h["module"], h["state"], tuple(h["roots"]))
                for h in session.get("handlers", ())
            )
            accepted = Plan(
                spec,
                SessionSpec(**session),
                RunConfig(**planned["run"]),
                planned.get("repo"),
                planned.get("docker", False),
            )
            result, previous = _result(data.pop("result")), _result(data.pop("previous"))
            if type(bound := data.pop("bound")) is not bool:
                raise ValueError("invalid bound")
            if type(closing := data.pop("closing")) is not bool:
                raise ValueError("invalid closing")
            callback = data.pop("callback")
            overlays = tuple(data.pop("overlays"))
            state = DelegateState(
                **data,
                plan=accepted,
                callback=None if callback is None else tuple(callback),
                result=result,
                previous=previous,
                bound=bound,
                closing=closing,
                overlays=overlays,
            )
            if type(state.attempt) is not int or state.attempt < 1:
                raise ValueError("invalid attempt")
            if state.detached and type(state.drive) is not str:
                raise ValueError("detached without drive")
        except (AttributeError, KeyError, TypeError, ValueError, SessionConfigError) as error:
            raise ConfigError(f"invalid {self.store.record}: {error}") from None
        else:
            return state

    @staticmethod
    def announce(state: DelegateState, result: Result | None) -> None:
        """Appends the run's one events line, `outcome <status>` for its harness session (`error`
        without a valid outcome); an OSError is printed, never raised.
        """
        if state.events is None:
            return
        outcome = None if result is None else result.outcome
        try:
            SessionStatusLog(state.events).write(
                state.harness_name, "outcome", "error" if outcome is None else outcome.status
            )
        except OSError as error:
            say(f"delegate: {error}")

    @staticmethod
    def exited(state: DelegateState, rc: int) -> None:
        if state.events is None:
            return
        try:
            SessionStatusLog(state.events).write(state.harness_name, "exited", rc)
        except OSError as error:
            say(f"delegate: {error}")

    def bind(self, n: int, token: int) -> bool:
        with self.store.mutex():
            state = self.read()
            if state.attempt != n or state.completed or worker(state) != token:
                return False
            if not state.bound:
                self.save(replace(state, bound=True))
            return True

    def follow_up(self, n: int, token: int) -> bool:
        """Not while a close holds its claim, nor once one closed the worker."""
        with self.store.mutex():
            state = self.read()
            if state.attempt != n or not state.completed or state.closing or worker(state) != token:
                return False
            if self.store.closing():
                return False
            self.save(
                replace(
                    state,
                    attempt=n + 1,
                    completed=False,
                    result=None,
                    previous=state.result,
                    bound=True,
                )
            )
            return True

    def stop(self, result: Result, *, sid: str) -> int | None:
        """A close: the attempt it ended, if any."""
        with self.store.mutex():
            state = self.read()
            if state.plan.sid != sid:
                return None
            active = not state.completed
            self.save(
                replace(
                    state,
                    closing=True,
                    completed=True,
                    result=result if active else state.result,
                )
            )
            return state.attempt if active else None

    def complete(
        self, n: int, result: Result, *, sid: str, unbound: bool = False, closing: bool = False
    ) -> bool:
        """Compare-and-set; unbound: only while opening; closing: no follow-up after it."""
        with self.store.mutex():
            state = self.read()
            if (
                (state.plan.sid, state.attempt) != (sid, n)
                or state.completed
                or unbound
                and state.bound
            ):
                return False
            self.save(
                replace(state, completed=True, result=result, closing=closing or state.closing)
            )
            return True
