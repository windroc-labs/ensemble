"""HarnessDelegate owns an accepted plan, its Composer, sessions and durable recovery identity."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace

import ensemble.utils as utils_package
from ensemble.delegate import config
from ensemble.delegate.composer import Composer, DelegateConfig, RunConfig, Team, teams
from ensemble.delegate.engine.base import Delegate
from ensemble.delegate.engine.callback import EventCallback, sweep
from ensemble.delegate.engine.config import module_roots, read_config
from ensemble.delegate.engine.container import on_docker
from ensemble.delegate.engine.drivers import AgentDriver, Driver, InstructionDriver
from ensemble.delegate.engine.plan import plan
from ensemble.delegate.engine.runner import execute, say, stopped
from ensemble.delegate.engine.state import RunState
from ensemble.delegate.engine.types import (
    ConfigError,
    Event,
    Finished,
    Layout,
    Limit,
    Outer,
    Plan,
    Preview,
    Result,
    Spec,
    Stopped,
)
from ensemble.delegate.event.reply import outcome_only
from ensemble.delegate.event.types import Outcome
from ensemble.session import (
    SIGNALS,
    Access,
    Host,
    Launch,
    Mode,
    NoRecordedSession,
    NotShowable,
    Process,
    Resumable,
    RunLog,
    Session,
    SessionBusy,
    SessionError,
    SessionStatus,
    SessionStatusLog,
    Unanswered,
    Usage,
    Viewer,
    get_viewer,
)
from ensemble.session import ConfigError as SessionConfigError
from ensemble.session import (
    Spec as SessionSpec,
)
from ensemble.utils import (
    EVENTS_ENV,
    LEASE_ENV,
    SID_ENV,
    Display,
    OutcomeStatus,
    Record,
    Status,
    StatusState,
    last_lines,
    load_class,
    remove_tree,
    source_path,
    thaw,
    under,
)


def _team(team: Team) -> Team:
    return teams.team(team.root, team.dirs)


def _loaded(payload: bytes) -> dict:
    try:
        data = json.loads(payload)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _unknown(error: Unanswered) -> Unanswered:
    return Unanswered(f"delegate agent session liveness unknown: {error}")


def _encode(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


@dataclass(frozen=True)
class _State:
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


TUI_START_TIMEOUT = 30  # seconds: a tui run's agent session to go live, off Docker
DOCKER_START_TIMEOUT = 120  # seconds, container start included
TUI_POLL = 0.1  # seconds


class HarnessDelegate(Delegate):
    def __init__(self, role: str, spec: Spec, **placement) -> None:
        self._accept(role, spec, **placement)
        self._persist_new()

    @classmethod
    def create(
        cls,
        role: str,
        spec: Spec,
        *,
        display: Display = None,
        callback: Sequence[str] | None = None,
        overlays: Sequence[Mapping] = (),
    ) -> HarnessDelegate:
        return cls(role, spec, display=display, callback=callback, overlays=overlays)

    @classmethod
    def preview(cls, role: str, spec: Spec, **placement) -> Preview:
        """What the constructor would accept, without the session preflight; writes nothing."""
        view = cls.__new__(cls)
        view._accept(role, spec, **placement)
        state = view._state
        return Preview(
            state.plan,
            state.agent_name if view._named(state.plan.spec.mode) else None,
            state.harness_name,
            state.host,
            state.harness_host,
            state.callback,
        )

    @classmethod
    def instruct(cls, role: str, spec: Spec) -> Result:
        """Delivers the role's prompt to spec.out: no Session, no delegate.json."""
        try:
            composer = Composer(config=DelegateConfig(read_config()), team=spec.team)
        except (OSError, ValueError) as error:
            raise ConfigError(str(error)) from None
        driver = InstructionDriver()
        accepted = plan(
            composer.resolve(role, spec.task),
            replace(spec, team=composer.team),
            driver=driver,
            composition=composer,
        )
        return execute(accepted, driver=driver)

    def _accept(
        self,
        role: str,
        spec: Spec,
        *,
        host: str | None = None,
        display: Display = None,
        harness_host: str | Host | None = None,
        callback: Sequence[str] | None = None,
        overlays: Sequence[Mapping] = (),
        session_cls: type[Session] = Session,
        viewer: Viewer | None = None,
        composer: Composer | None = None,
        driver: Driver | None = None,
    ):
        self._viewer = viewer
        try:
            self.composer = composer or Composer(
                config=DelegateConfig(read_config()), team=spec.team
            )
            run = self.composer.resolve(role, spec.task, overlays=overlays)
            accounts = self.composer.accounts(role, overlays=overlays)
            self._settings = thaw(self.composer.config.values)
            self.session_cls = session_cls
            self._configure()
            hosts = Host.parse_hosts(self._settings.get("hosts")).tables
            agent_host = session_cls.host(host)
            self._harness_override = harness_host if isinstance(harness_host, Host) else None
            harness = (
                harness_host
                if isinstance(harness_host, Host)
                else session_cls.host(
                    harness_host or config.harness_host(self._settings, hosts) or agent_host.name
                )
            )
            display = session_cls.display(
                "none" if run.show == "none" else display, host=agent_host.name
            )
            events = harness.events_path(spec.events)
        except (OSError, ValueError, SessionConfigError, SessionError) as error:
            raise ConfigError(str(error)) from None
        if harness.name not in hosts:
            raise ConfigError(
                f"harness_host {harness.name}: want one of {', '.join(sorted(hosts))}"
            )
        self.driver = driver or AgentDriver(
            session_cls, settings=self._settings, display=display, host=agent_host.name
        )
        if self.driver.docker and on_docker(self._settings, harness.name):
            other = next((n for n in hosts if not on_docker(self._settings, n)), "<a host Host>")
            raise ConfigError(
                f"harness_host: an agent on the Docker Host needs its harness on another Host; "
                f'set [delegate.delegates] harness_host = "{other}"'
            )
        tui = spec.mode == Mode.TUI
        accepted = plan(
            run,
            replace(
                spec,
                team=self.composer.team,
                name=spec.name if tui else None,
                events=events if tui else None,
            ),
            driver=self.driver,
            composition=self.composer,
            accounts=accounts,
        )
        name = spec.name or f"{role}-{accepted.sid[:8]}"
        named = name if self._named(spec.mode) else None
        accepted = replace(
            accepted,
            spec=replace(accepted.spec, name=named),
            session=replace(accepted.session, name=named),
        )
        self.workdir = accepted.spec.workdir
        self.layout = Layout(self.workdir)
        self.store = RunState(self.layout)
        self._state = _State(
            role,
            accepted,
            None
            if session_cls is Session
            else f"{session_cls.__module__}:{session_cls.__qualname__}",
            agent_host.name,
            harness.name,
            display,
            None if callback is None else tuple(callback),
            name,
            f"{name}-drive",
            1,
            {},
            tuple(thaw(overlay) for overlay in overlays),
            events=events,
        )
        try:
            harness.validate(self._state.harness_name, self._harness_launch(self._state))
        except (SessionConfigError, SessionError) as error:
            raise ConfigError(str(error)) from None

    def _persist_new(self) -> None:
        """Every refusal precedes every write: a fresh workdir's session preflight runs before its
        run dir is made; otherwise it follows the replaced run's refusal and cleanup, under the
        mutex. The agent dir is made last, just before the run record.
        """
        fresh = not os.path.lexists(self.layout.run)
        try:
            if fresh:
                self._preflight()
            os.makedirs(self.layout.run, exist_ok=True)
            with self.store.mutex():
                payload = self.store.read()
                if payload is not None:
                    self._state = replace(self._state, attempt=self._replacing(payload))
                    self._sweep()
                if not fresh or payload is not None:
                    self._preflight()
                os.makedirs(self.layout.agent, exist_ok=True)
                self._save(self._state)
        except OSError as error:
            raise ConfigError(str(error)) from None

    def _preflight(self) -> None:
        try:
            self.driver.preflight(self._state.plan.session)
        except (SessionConfigError, SessionError) as error:
            raise ConfigError(str(error)) from None

    def _replacing(self, payload: bytes) -> int:
        """The attempt replacing the run `payload` records (its session's next, else 1; a corrupt
        record's readable attempt + 1), refused while that run is live; the agent session it left
        open (a tui run's, once it ended) is closed.
        """
        if self._live(payload):
            raise ConfigError(f"{self.workdir}: delegate is still alive")
        try:
            previous = self._parse(payload)
        except ConfigError:
            attempt = _loaded(payload).get("attempt")
            return attempt + 1 if type(attempt) is int and attempt >= 1 else 1
        agent = self._known_agent(previous)
        if agent is not None:
            try:
                agent.close()
            except SessionError as error:
                raise ConfigError(str(error)) from None
        return previous.attempt + 1 if previous.plan.sid == self._state.plan.sid else 1

    def _sweep(self) -> None:
        try:
            sweep(self.store)
        except OSError as error:
            raise ConfigError(str(error)) from None

    def _live(self, payload: bytes | None) -> bool:
        """Whether the run `payload` records is live: a readable record's per _alive; a corrupt
        one's while its run lock is held or the harness it names lives; none: while the run lock
        is held.
        """
        if payload is None:
            return self.store.running()
        try:
            return self._alive(self._parse(payload))
        except ConfigError:
            data = _loaded(payload)
        host, name = data.get("harness_host"), data.get("harness_name")
        return self.store.running() or (
            isinstance(host, str)
            and isinstance(name, str)
            and (process := self._process(host, name)) is not None
            and process.alive()
        )

    @property
    def _harness_host(self) -> Host:
        return self._harness_override or self.session_cls.host(self._state.harness_host)

    def _configure(self) -> None:
        try:
            self.session_cls.configure_from(self._settings)
        except SessionConfigError as error:
            raise ConfigError(str(error)) from None

    def _save(self, state: _State) -> None:
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

    def _read(self) -> _State:
        return self._parse(self.store.read())

    def _parse(self, payload: bytes | None) -> _State:
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
            if os.path.realpath(spec.workdir) != os.path.realpath(self.workdir):
                raise ValueError("metadata workdir differs")
            planned = data.pop("plan")
            session = planned["session"]
            access = session["access"]
            for key in ("dirs", "reads", "commands"):
                access[key] = tuple(access[key])
            session["access"] = Access(**access)
            session["flags"] = tuple(session["flags"])
            session["reply_tags"] = tuple(session["reply_tags"])
            accepted = Plan(
                spec,
                SessionSpec(**session),
                RunConfig(**planned["run"]),
                planned.get("repo"),
                planned.get("docker", False),
            )
            result = data.pop("result")
            if result is not None:
                outcome = result.pop("outcome")
                if outcome is not None:
                    result["outcome"] = Outcome(**outcome)
                else:
                    result["outcome"] = None
                from_limit = result.pop("limit")
                if from_limit is not None:
                    result["limit"] = Limit(**from_limit)
                result = Result(**result)
            callback = data.pop("callback")
            overlays = tuple(data.pop("overlays"))
            state = _State(
                **data,
                plan=accepted,
                callback=None if callback is None else tuple(callback),
                result=result,
                overlays=overlays,
            )
            if type(state.attempt) is not int or state.attempt < 1:
                raise ValueError("invalid attempt")
        except (AttributeError, KeyError, TypeError, ValueError, SessionConfigError) as error:
            raise ConfigError(f"invalid {self.store.record}: {error}") from None
        else:
            return state

    @classmethod
    def attach(
        cls,
        record_or_workdir: Record | str,
        *,
        sid: str | None = None,
        session_cls: type[Session] | None = None,
        viewer: Viewer | None = None,
        composer: Composer | None = None,
    ) -> HarnessDelegate:
        workdir = (
            record_or_workdir.workdir
            if isinstance(record_or_workdir, Record)
            else record_or_workdir
        )
        if workdir is None:
            raise ConfigError("record has no workdir")
        view = cls._bare(Layout(workdir))
        view._viewer = viewer
        view._state = view._read()
        if sid is not None and sid != view._state.plan.sid:
            raise ConfigError(f"sid {sid} is not the run's (its sid: {view._state.plan.sid})")
        try:
            view.composer = composer or Composer(
                config=DelegateConfig(read_config()),
                team=None
                if view._state.plan.spec.team is None
                else _team(view._state.plan.spec.team),
            )
            view._settings = thaw(view.composer.config.values)
            view.session_cls = session_cls or (
                Session
                if view._state.session_cls in (None, f"{Session.__module__}:{Session.__qualname__}")
                else load_class(view._state.session_cls, Session)
            )
            view._configure()
            view.session_cls.host(view._state.host)
            view.session_cls.host(view._state.harness_host)
            view._harness_override = None
        except (OSError, ValueError, SessionConfigError, SessionError) as error:
            raise ConfigError(str(error)) from None
        view.driver = AgentDriver(
            view.session_cls,
            settings=view._settings,
            display=view._state.display,
            host=view._state.host,
        )
        return view

    @classmethod
    def roles(cls) -> dict[str, dict[str, str]]:
        return Composer().index()

    @classmethod
    def resolve(
        cls, role: str, task: str | None = None, *, overlays: Sequence[Mapping] = ()
    ) -> RunConfig:
        return Composer().resolve(role, task, overlays=overlays)

    @classmethod
    def trusted_dirs(cls) -> frozenset[str]:
        return Composer().trusted_dirs()

    @classmethod
    def _bare(cls, layout: Layout) -> HarnessDelegate:
        """A view of layout's run with no state loaded: enough for _live."""
        view = cls.__new__(cls)
        view.layout, view.workdir, view.store = layout, layout.workdir, RunState(layout)
        view.session_cls, view._harness_override, view._viewer = Session, None, None
        return view

    @classmethod
    def checkouts(cls, workdir: str) -> tuple[str, ...] | None:
        return Layout(workdir).checkouts()

    @classmethod
    def clean(cls, workdir: str, *, dry: bool = False, scratch: bool = False) -> bool:
        layout = Layout(workdir)
        agent = os.path.lexists(layout.agent)
        if agent and not layout.in_place():
            raise ConfigError(f"not a real directory: {layout.agent}, refusing to touch")
        store = RunState(layout)
        with store.mutex() if os.path.isdir(layout.run) else contextlib.nullcontext():
            payload = store.read()
            try:
                view = cls.attach(workdir)
            except ConfigError:
                if scratch and payload is not None:
                    raise ConfigError(f"{layout.workdir}: delegate liveness unknown") from None
                view = cls._bare(layout)
            if view._live(payload):
                raise ConfigError(f"{layout.workdir}: delegate is still alive")
            if scratch and payload is not None:
                session = view._known_agent(view._read())
                if session is not None and session.alive():
                    raise ConfigError(f"{layout.workdir}: its agent session is still open")
            found = [
                p for p in (layout.scratch if scratch else (layout.agent,)) if os.path.lexists(p)
            ]
            if not dry:
                for path in found:
                    _remove(path)
            return bool(found)

    @classmethod
    def check_tui(
        cls,
        *,
        display: Display = None,
        events: str | None = None,
        session_cls: type[Session] = Session,
    ) -> None:
        """The display on the agent's Host, the events file on the harness's."""
        settings = thaw(Composer().config.values)
        try:
            session_cls.configure_from(settings)
            hosts = Host.parse_hosts(settings.get("hosts")).tables
            agent = session_cls.host()
            session_cls.layout(display=display)
            harness = session_cls.host(config.harness_host(settings, hosts) or agent.name)
            harness.events_path(events)
        except (OSError, ValueError, SessionConfigError, SessionError) as error:
            raise ConfigError(str(error)) from None

    @classmethod
    def usage(
        cls,
        role: str,
        *,
        overlays: Sequence[Mapping] = (),
        session_cls: type[Session] = Session,
    ) -> Usage | None:
        composition = Composer()
        try:
            session_cls.configure_from(thaw(composition.config.values))
            run = composition.resolve(role, overlays=overlays)
            return session_cls.usage(run.client)
        except SessionConfigError as error:
            raise ConfigError(str(error)) from None

    def _own(self, state: _State) -> Session | None:
        """The agent session this process started for `state`'s attempt."""
        agent = self.driver.session
        if (
            agent is not None
            and state.attempt == self._state.attempt
            and agent.sid == state.plan.sid
        ):
            return agent
        return None

    def _agent(self, state: _State) -> Session | None:
        """None when no such session; Unanswered when its Host could not tell."""
        if (agent := self._own(state)) is not None and agent.mode != Mode.TUI:
            return agent
        try:
            return self.session_cls.attach(state.agent_name, sid=state.plan.sid, host=state.host)
        except Unanswered:
            raise
        except SessionError:
            return None
        except SessionConfigError as error:
            raise ConfigError(str(error)) from None

    def _answered_agent(self, state: _State) -> Session | None:
        try:
            return self._agent(state)
        except Unanswered:
            return None

    def _known_agent(self, state: _State) -> Session | None:
        try:
            return self._agent(state)
        except Unanswered as error:
            raise ConfigError(f"{self.workdir}: agent session liveness unknown: {error}") from None

    def _harness(self, state: _State) -> Process | None:
        return (
            self._process(state.harness_host, state.harness_name, sid=state.plan.sid)
            if state.detached
            else None
        )

    def _process(self, host_name: str, name: str, *, sid: str | None = None) -> Process | None:
        try:
            host = (
                self._harness_override
                if self._harness_override is not None and self._harness_override.name == host_name
                else self.session_cls.host(host_name)
            )
            process = host.attach(name)
            return process if sid is None or process.session_identity()[0] == sid else None
        except SessionError:
            return None
        except SessionConfigError as error:
            raise ConfigError(str(error)) from None

    def _alive(self, state: _State) -> bool:
        if state.detached:
            process = self._harness(state)
            return process is not None and process.alive()
        return not state.completed and self.store.running()

    def alive(self) -> bool:
        return self._alive(self._read())

    def outcome(self) -> Outcome | None:
        result = self._read().result
        return None if result is None else result.outcome

    def _named(self, mode: str) -> bool:
        return mode == Mode.TUI or (self.driver.docker and mode == Mode.HEADLESS)

    def _record(self, state: _State) -> Record:
        sessions = (state.harness_name,) if state.detached else ()
        if state.plan.spec.mode == "tui":
            sessions += (state.agent_name,)
        return Record(
            1,
            state.outer.get("kind", "role"),
            state.outer.get("name", state.agent_name),
            sessions,
            state.plan.sid,
            self.workdir,
            tuple(state.outer.get("ctl", ("ensemble-delegate",))),
            tuple(state.outer.get("resume", ("ensemble-delegate", "resume", self.workdir))),
            state.display,
            state.role,
        )

    def content(self) -> str:
        state = self._read()
        result = state.result
        if (
            state.plan.spec.mode == Mode.TUI
            and (result is None or result.outcome is None and not result.error)
            and (agent := self._answered_agent(state)) is not None
        ):
            return outcome_only(agent.content())
        return self.status().to_line()

    def status(self) -> Status:
        state = self._read()
        try:
            agent, unanswered = self._agent(state), False
        except Unanswered:
            agent, unanswered = None, True
        found = None if agent is None else agent.status()
        alive = self._alive(state) and (
            state.plan.spec.mode != Mode.TUI or unanswered or found is not None
        )
        if unanswered or found is not None and found.state == StatusState.UNKNOWN:
            word = StatusState.UNKNOWN
        elif alive:
            word = StatusState.WORKING
        elif state.plan.spec.mode == Mode.TUI and found is not None:
            word = found.state
        else:
            word = StatusState.GONE
        views, _ = self._views(state)
        outcome = None if state.result is None else state.result.outcome
        return Status(
            name=state.agent_name,
            alive=alive,
            state=word,
            outcome=None if outcome is None else outcome.status,
            attach=self._shown_by().attach_command(*views[0]) if views else None,
            hidden=found is not None and found.hidden,
            cwd=state.plan.session.access.cwd,
        )

    def attach_command(self) -> str | None:
        views, unanswered = self._views(self._read())
        if views:
            return self._shown_by().attach_command(*views[0])
        if unanswered is not None:
            raise _unknown(unanswered)
        return None

    def _shown_agent(self, state: _State) -> Session | None:
        agent = self._agent(state)
        return agent if agent is not None and agent.alive() and agent.showable else None

    def _views(self, state: _State) -> tuple[list[tuple[str, Host]], Unanswered | None]:
        """Its live showable sessions and their Hosts, agent first; why its agent's Host could not
        tell, if so.
        """
        views, unanswered = [], None
        try:
            if self._shown_agent(state) is not None:
                views.append((state.agent_name, self._session_host(state.host)))
        except Unanswered as error:
            unanswered = error
        process = self._harness(state)
        if process is not None and process.alive() and self._harness_host.attachable:
            views.append((state.harness_name, self._harness_host))
        return views, unanswered

    def _shown_by(self) -> Viewer:
        """The Viewer given, else ensemble.session's."""
        return self._viewer or get_viewer()

    def _session_host(self, name: str | None) -> Host:
        try:
            return self.session_cls.host(name)
        except SessionConfigError as error:
            raise ConfigError(str(error)) from None

    def show(self) -> str | None:
        state = self._read()
        try:
            shown = self._shown_agent(state)
        except Unanswered as error:
            raise _unknown(error) from None
        if shown is None:
            raise NotShowable("delegate has no live showable agent session")
        return self._shown_by().show(
            state.agent_name, self._session_host(state.host), display=state.display
        )

    def hide(self) -> None:
        views, unanswered = self._views(self._read())
        if not views and unanswered is None:
            raise NotShowable("delegate has no live showable session")
        try:
            for view in views:
                self._shown_by().hide(*view)
        except SessionError as error:
            if unanswered is None:
                raise
            raise SessionError(f"{error}; {_unknown(unanswered)}") from error
        if unanswered is not None:
            raise _unknown(unanswered)

    def close(self) -> None:
        """Unanswered after closing the harness when the agent's Host could not tell."""
        state = self._read()
        try:
            agent, unanswered = self._agent(state), None
        except Unanswered as error:
            agent, unanswered = None, error
        if agent is not None:
            agent.close()
        unchecked = (
            None
            if unanswered is None
            else f"close {state.agent_name}: agent session not checked: {unanswered}"
        )
        process = self._harness(state)
        if process is not None:
            try:
                process.close()
                if self._harness_host.attachable:
                    self._shown_by().close(state.harness_name, self._harness_host)
            except SessionError as error:
                also = "" if unchecked is None else f"; {unchecked}"
                raise SessionError(f"close {state.harness_name}: {error}{also}") from error
        if unchecked is not None:
            raise Unanswered(unchecked)

    def session_status(self) -> SessionStatus:
        """Its own live agent session's status, else the plan's: an attached one lacks its env;
        never recorded (its launch failed): not resumable.
        """
        state = self._read()
        agent = None if state.completed else self._own(state)
        if agent is not None:
            return agent.session_status()
        try:
            return self.session_cls.inspect(state.plan.session)
        except NoRecordedSession:
            return SessionStatus(Resumable.NO, None, "")

    def run(self, on_event: Callable[[Event], None] | None = None) -> Result:
        """Each event goes to the callback, then `on_event`; a Finished is stored first, a failed
        store printed and the Finished still sent. A run that raises before its Finished stores and
        calls back a stopped one (not to `on_event`), then re-raises; a crash (an Exception or
        SystemExit, not a signal's Stopped or an interrupt) is also a run.jsonl run_error row with
        its traceback.
        """
        state = self._state
        if state.events is not None and not state.detached and state.plan.spec.mode != Mode.TUI:
            raise ConfigError("events: a headless run writes them only when detached")
        with self.store.run_lock():
            with self.store.mutex():
                if self._current().completed:
                    raise ConfigError(f"{self.workdir}: delegate run already completed")
            run_dir = self.layout.run
            callback = (
                None
                if self._state.callback is None
                else EventCallback(
                    self._state.callback,
                    self.store,
                    sid=self._state.plan.sid,
                    attempt=self._state.attempt,
                )
            )

            finished = False

            def emit(event: Event) -> None:
                nonlocal finished
                if isinstance(event, Finished):
                    finished = True
                    try:
                        self._complete(event.result)
                    except (ConfigError, OSError) as failure:
                        say(f"delegate: {failure}")
                if callback is not None:
                    callback(event)
                if on_event is not None:
                    on_event(event)

            try:
                return execute(
                    self._state.plan,
                    driver=self.driver,
                    on_event=emit,
                    ending=self._start_failure_end,
                )
            except BaseException as error:
                if not finished:
                    ended = self._start_failure_end(error)
                    if isinstance(error, (Exception, SystemExit)):
                        RunLog(run_dir).note(
                            "run_error",
                            error=ended.error,
                            traceback="".join(traceback.format_exception(error)),
                            best_effort=True,
                        )
                    try:
                        self._complete(ended)
                    except (ConfigError, OSError) as failure:
                        say(f"delegate: {failure}")
                    if callback is not None:
                        callback(Finished(ended))
                raise
            finally:
                self._clear_tmp()

    def _start_failure_end(self, error: BaseException) -> Result:
        """A signal's stop ends as this attempt's start_failure when detach saved one."""
        ended = stopped(error)
        if not isinstance(error, Stopped):
            return ended
        try:
            state = self._current()
        except (ConfigError, OSError):
            return ended
        if state.start_failure is None:
            return ended
        return Result(1, None, f"stopped: {state.start_failure}")

    def _clear_tmp(self) -> None:
        """agent/tmp at the run's end, never followed, keeping a deliverable file `out` directly in
        it; kept whole while its agent session is open; an error printed.
        """
        tmp, out = self.layout.tmp, os.path.abspath(self._state.plan.driver_spec.out)
        try:
            agent = self._agent(self._read())
            if agent is not None and agent.alive():
                return
            if not (os.path.dirname(out) == tmp and _regular(out) and _regular(tmp, dir=True)):
                _remove(tmp)
                return
            fd = os.open(tmp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for entry in os.scandir(fd):
                    if entry.name == os.path.basename(out):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        remove_tree(entry.name, dir_fd=fd)
                    else:
                        os.unlink(entry.name, dir_fd=fd)
            finally:
                os.close(fd)
        except (ConfigError, SessionError, OSError) as error:
            say(f"delegate: {error}")

    def ctl(self) -> tuple[str, ...]:
        return self._record(self._read()).ctl

    def _complete(self, result: Result) -> None:
        with self.store.mutex():
            state = self._current()
            self._state = replace(state, result=result, completed=True)
            self._save(self._state)

    def announce(self, result: Result | None) -> None:
        """Appends the run's one events line, `outcome <status>` for its harness session (`error`
        without a valid outcome); an OSError is printed, never raised.
        """
        events = self._state.events
        if events is None:
            return
        outcome = None if result is None else result.outcome
        try:
            SessionStatusLog(events).write(
                self._state.harness_name, "outcome", "error" if outcome is None else outcome.status
            )
        except OSError as error:
            say(f"delegate: {error}")

    def _current(self) -> _State:
        state = self._read()
        if (state.plan.sid, state.attempt) != (self._state.plan.sid, self._state.attempt):
            raise ConfigError(f"{self.workdir}: delegate attempt was replaced")
        return state

    def _harness_launch(self, state: _State) -> Launch:
        """The harness imports from the delegate, session and utils roots, then from its session
        class's and the classes that session loads by name for the run's client.
        """
        try:
            roots = (
                *module_roots(),
                *utils_package.module_roots(sys.modules[self.session_cls.__module__]),
                *self.session_cls.class_roots(state.plan.client),
            )
        except (ValueError, SessionConfigError) as error:
            raise ConfigError(str(error)) from None
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in ("PYTHONPATH", SID_ENV, EVENTS_ENV, *LEASE_ENV)
        }
        environment.update(source_path(roots))
        return Launch(
            (sys.executable, "-P", "-s", "-m", "ensemble.delegate.engine.harness", self.workdir),
            cwd=self.layout.run,
            env=environment,
            blocked_signals=SIGNALS,
        )

    def _launch(self, previous: _State, state: _State) -> Record:
        """`state` saved detached before its harness starts; `previous` again when it cannot."""
        state = replace(state, detached=True, start_failure=None)
        record = self._record(state)
        launch = self._harness_launch(state)
        self._save(state)
        try:
            self._harness_host.detach(
                state.harness_name,
                launch,
                display=state.display,
                events=state.events,
                record=record,
            )
        except BaseException as error:
            self._save(previous)
            if isinstance(error, SessionConfigError):
                raise ConfigError(str(error)) from None
            raise
        self._state = state
        return record

    def detach(self, *, outer: Outer | None = None) -> Record:
        mode = str(self._state.plan.spec.mode)
        if mode not in (Mode.HEADLESS, Mode.TUI):
            raise ConfigError(f"detach: mode must be headless or tui, got {mode!r}")
        if not self.driver.session_based:
            raise ConfigError("instruction cannot detach")
        given: Outer = {} if outer is None else outer.copy()
        if unknown := next((key for key in given if key not in Outer.__annotations__), None):
            raise ConfigError(f"outer: unknown key {unknown}")
        try:
            self._record(replace(self._state, outer=given))
        except ValueError as error:
            raise ConfigError(f"outer: {error}") from None
        with self.store.mutex():
            previous = self._current()
            if previous.detached and self._alive(previous):
                return self._record(previous)
            if previous.completed:
                raise ConfigError(f"{self.workdir}: delegate run already completed")
            if self.store.running():
                raise ConfigError(f"{self.workdir}: delegate is still alive")
            self._sweep()
            record = self._launch(previous, replace(previous, outer=given))
        self._started(self._state)
        return record

    def _started(self, state: _State) -> None:
        """Tui: waits, outside the mutex, until this run's agent session has its client running;
        SessionError naming why, its harness and agent session closed, when the harness ends first,
        a Host fails or its start limit passes; the last two first saved as start_failure, the
        harness's end. A same-named session of another sid is not this run's.
        """
        if state.plan.spec.mode != Mode.TUI:
            return
        limit = DOCKER_START_TIMEOUT if self.driver.docker else TUI_START_TIMEOUT
        deadline = time.monotonic() + limit
        harness = agent = unanswered = None
        ended = False
        try:
            while True:
                agent, unanswered = self._probed_agent(state)
                try:
                    if agent is not None and agent.alive() and agent.status().alive:
                        return
                except Unanswered as error:
                    unanswered = error
                harness = self._harness(state)
                ended = harness is None or not harness.alive()
                if ended:
                    why = f": its harness ended{self._ended()}"
                    break
                if time.monotonic() >= deadline:
                    why = f" in {limit} s"
                    break
                time.sleep(TUI_POLL)
        except SessionError as error:
            why = f": {error}"
        if agent is None and unanswered is not None:
            agent, unanswered = self._probed_agent(state)
        message = f"tui session {state.agent_name} did not start{why}{self._tail(state, agent)}"
        if unanswered is not None:
            message += f"; agent session not checked: {unanswered}"
        message = " ".join(message.split())
        if not ended:
            self._start_failed(message)
        if harness is not None:
            try:
                harness.close()
            except SessionError:
                if not ended:
                    self._start_failed(None)
        if agent is not None:
            with contextlib.suppress(SessionError):
                agent.close()
        if busy := self._busy(state):
            raise busy
        raise SessionError(message)

    def _probed_agent(self, state: _State) -> tuple[Session | None, Unanswered | None]:
        try:
            agent = self._agent(state)
        except Unanswered as error:
            return None, error
        return (agent if agent is not None and agent.sid == state.plan.sid else None), None

    def _ended(self) -> str:
        try:
            result = self._current().result
        except ConfigError:
            return ""
        if result is None:
            return ""
        return f": exit {result.returncode}" + (f": {result.error}" if result.error else "")

    def _tail(self, state: _State, agent: Session | None) -> str:
        """`; last lines: …` of the agent's client: its screen, else its Host row's once exited."""
        try:
            text = "" if agent is None else agent.screen()
        except SessionError:
            text = ""
        if not text:
            try:
                rows = self.session_cls.host(state.host).rows()
            except (SessionError, SessionConfigError):
                rows = []
            text = next(
                (
                    r.last_lines or ""
                    for r in rows
                    if (r.name, r.sid) == (state.agent_name, state.plan.sid)
                ),
                "",
            )
        tail = last_lines(text)
        return f"; last lines: {tail}" if tail else ""

    def _start_failed(self, cause: str | None) -> None:
        with contextlib.suppress(ConfigError, OSError), self.store.mutex():
            state = self._current()
            if not state.completed:
                self._save(replace(state, start_failure=cause))

    def _busy(self, state: _State) -> SessionBusy | None:
        environment = state.plan.session.environment
        try:
            result = self._read().result
        except (ConfigError, SessionError, OSError):
            return None
        found = None if result is None else SessionBusy.PATTERN.search(result.error)
        if (
            found is None
            or environment is None
            or found[1] != environment.lock
            or not found[2].isprintable()
        ):
            return None
        return SessionBusy(found[1], found[2])

    def resume(self, *, mode: str | None = None, input: str | None = None) -> Record:
        """Continues in `mode` (headless or tui), else the recorded one; the new mode is recorded."""
        if mode not in (None, Mode.HEADLESS, Mode.TUI):
            raise ConfigError(f"resume: mode must be headless or tui, got {mode!r}")
        with self.store.mutex():
            state = self._read()
            if state.plan.sid != self._state.plan.sid:
                raise ConfigError(
                    f"{self.workdir}: run {self._state.plan.sid} was replaced by {state.plan.sid}"
                )
            previous = self._state = state
            alive = self._alive(state)
            if input is not None:
                if alive:
                    raise ConfigError(f"{self.workdir}: delegate is still alive")
                outcome = state.result.outcome if state.result is not None else None
                if outcome is None or outcome.status != OutcomeStatus.NEEDS_INPUT:
                    raise ConfigError(f"{self.workdir}: --input requires a needs_input outcome")
            elif alive or state.result is not None and state.result.outcome is not None:
                return self._record(state)
            self._sweep()
            original = state.plan.session.access
            if (
                original.project
                and under(os.path.realpath(original.cwd), self.composer.trusted_dirs()) is None
            ):
                raise ConfigError(f"cwd {original.cwd} is no longer in trusted_dirs")
            mode = mode or state.plan.spec.mode
            tui = mode == Mode.TUI
            spec = replace(
                state.plan.spec,
                resume=True,
                input=state.plan.spec.input if input is None else input,
                cwd=original.cwd,
                team=self.composer.team,
                mode=mode,
                name=state.agent_name if tui else None,
                events=state.events if tui else None,
            )
            accepted = plan(
                self.composer.resolve(state.role, spec.task, overlays=state.overlays),
                spec,
                driver=self.driver,
                composition=self.composer,
                accounts=self.composer.accounts(state.role, overlays=state.overlays),
            )
            named = state.agent_name if self._named(mode) else None
            accepted = replace(
                accepted,
                spec=replace(accepted.spec, name=named),
                session=replace(
                    accepted.session,
                    name=named,
                    access=replace(
                        accepted.session.access, cwd=original.cwd, project=original.project
                    ),
                ),
            )
            agent = self._known_agent(state)
            probe = accepted.session
            if agent is not None and probe.name == state.agent_name:
                # the tui preflight refuses a live same-named session: the one this closes
                probe = replace(probe, name=f"{probe.name}-preflight")
            try:
                self.driver.preflight(probe)
            except SessionConfigError as error:
                raise ConfigError(str(error)) from None
            if agent is not None:
                agent.close()
            state = replace(
                state, plan=accepted, completed=False, result=None, attempt=state.attempt + 1
            )
            try:
                os.makedirs(self.layout.agent, exist_ok=True)
            except OSError as error:
                raise ConfigError(str(error)) from None
            record = self._launch(previous, state)
        self._started(self._state)
        return record


def _remove(path: str) -> None:
    """`path`, never followed: a directory's tree (remove_tree resists a symlink swap), else the
    entry itself.
    """
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(found.st_mode):
        remove_tree(path)
    else:
        os.unlink(path)


def _regular(path: str, *, dir: bool = False) -> bool:
    """`path`, not followed, is a regular file (dir: a directory)."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(mode) if dir else stat.S_ISREG(mode)
