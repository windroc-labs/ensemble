"""HarnessDelegate owns an accepted plan, its Composer, sessions and durable recovery identity."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from enum import Enum, auto
from typing import Protocol, runtime_checkable

import ensemble.utils as utils_package
from ensemble.delegate import config
from ensemble.delegate.composer import Composer, DelegateConfig, RunConfig, Team, teams
from ensemble.delegate.engine.base import Delegate
from ensemble.delegate.engine.callback import EventCallback, sweep
from ensemble.delegate.engine.config import module_roots, packaged_environments, read_config
from ensemble.delegate.engine.drivers import AgentDriver, Driver, InstructionDriver
from ensemble.delegate.engine.environment import resolve
from ensemble.delegate.engine.handler import finish
from ensemble.delegate.engine.plan import plan
from ensemble.delegate.engine.record import CLOSED, HANDLER, RunRecord, owned
from ensemble.delegate.engine.runner import (
    Attempts,
    claim_release,
    execute,
    idle_reason,
    say,
    stopped,
)
from ensemble.delegate.engine.state import RunState
from ensemble.delegate.engine.types import (
    ConfigError,
    DelegateState,
    Event,
    Finished,
    Layout,
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
    HookCallback,
    Host,
    Launch,
    Mode,
    NoRecordedSession,
    NotShowable,
    Resumable,
    RunLog,
    Session,
    SessionBusy,
    SessionError,
    SessionStatus,
    Unanswered,
    Usage,
    Viewer,
    get_viewer,
)
from ensemble.session import ConfigError as SessionConfigError
from ensemble.session import Spec as SessionSpec
from ensemble.utils import (
    EVENTS_ENV,
    LEASE_ENV,
    SID_ENV,
    Display,
    OutcomeStatus,
    ProcessControl,
    Record,
    Status,
    StatusState,
    last_lines,
    load_class,
    remove_tree,
    source_path,
    thaw,
    under,
    validate_events,
)


@runtime_checkable
class _Publishes(Protocol):
    def publish(self, record: Record) -> None: ...


class _Ended(Enum):
    """_drive_end's compare-and-set."""

    LOST = auto()
    STORED = auto()


def _team(team: Team) -> Team:
    return teams.team(team.root, team.dirs)


def _loaded(payload: bytes) -> dict:
    try:
        data = json.loads(payload)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _live(agent: Session) -> bool:
    """Whether a close ends a running worker (unknown counts)."""
    try:
        return agent.alive() or agent.status().state == StatusState.UNKNOWN
    except Unanswered:
        return True


def _unknown(error: Unanswered) -> Unanswered:
    return Unanswered(f"delegate agent session liveness unknown: {error}")


TUI_START_TIMEOUT = 30  # seconds: a tui run's agent session to go live, off Docker
DOCKER_START_TIMEOUT = 120  # seconds, container start included
TUI_POLL = 0.1  # seconds
DRAIN_WAIT = (
    35  # seconds a worker is gone before its drive ends the attempt: past the harness's drain
)
GONE_EVERY = 1.0  # seconds between the drive's worker checks
WIND_DOWN_WAIT = 30.0  # seconds
DRIVE_STOP_WAIT = 5.0  # seconds after SIGTERM, then SIGKILL


class HarnessDelegate(Delegate):
    _controls: type[ProcessControl] = ProcessControl  # the drive's
    _stop_wait = DRIVE_STOP_WAIT

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
            state.host,
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
            config.delegates(self._settings)
            agent_host = session_cls.host(host)
            environment = resolve(
                self._settings, role=role, host=agent_host.name, packaged=packaged_environments()
            )
            display = session_cls.display(
                "none" if run.show == "none" else display, host=agent_host.name
            )
            events = None if spec.events is None else validate_events(spec.events)
        except (OSError, ValueError, SessionConfigError, SessionError) as error:
            raise ConfigError(str(error)) from None
        self.driver = driver or AgentDriver(
            session_cls, settings=self._settings, display=display, host=agent_host.name
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
            environment=environment,
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
        self._state = DelegateState(
            role,
            accepted,
            None
            if session_cls is Session
            else f"{session_cls.__module__}:{session_cls.__qualname__}",
            agent_host.name,
            display,
            None if callback is None else tuple(callback),
            name,
            f"{name}-drive",
            1,
            {},
            tuple(thaw(overlay) for overlay in overlays),
            events=events,
        )
        self._harness_launch(self._state)

    def _persist_new(self) -> None:
        """Every refusal precedes every write: a fresh workdir's session preflight runs before its
        run dir is made; otherwise it follows the replaced run's refusal and cleanup, under the
        mutex. The agent dir is made last, just before the run record.
        """
        fresh = not os.path.lexists(self.layout.run)
        if not fresh:
            self._settle()
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
        state = self._state
        self._state = replace(state, plan=self._probed(state.plan, state.attempt))

    def _probed(self, accepted: Plan, token: int, probe: SessionSpec | None = None) -> Plan:
        """With the handler of worker `token` if its preflight (of `probe`) takes it (D3)."""
        plain = replace(accepted, session=replace(accepted.session, handlers=()))
        probe = replace(probe or plain.session, handlers=())
        if plain.spec.mode == Mode.TUI:
            handler = HookCallback(
                HANDLER, {"workdir": self.workdir, "worker": token}, module_roots()
            )
            try:
                self._checked(replace(probe, handlers=(handler,)))
            except ConfigError:
                pass
            else:
                return replace(plain, session=replace(plain.session, handlers=(handler,)))
        self._checked(probe)
        return plain

    def _checked(self, spec: SessionSpec) -> None:
        try:
            self.driver.preflight(spec)
        except SessionBusy as error:
            raise SessionError(str(error)) from None
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
            live = _live(agent)
            try:
                agent.close()
            except SessionError as error:
                raise ConfigError(str(error)) from None
            if live:
                claim_release(previous.plan, previous.agent_name)
        return previous.attempt + 1 if previous.plan.sid == self._state.plan.sid else 1

    def _settle(self) -> None:
        """A gone owned worker retired outside the mutex, after its handler drained. Only under
        the run lock, which every start takes, and only the attempt it checked there.
        """
        try:
            state = self._read()
            if not owned(state.plan) or not (state.completed or state.bound):
                return
            with self.store.run_lock(wait=False):
                if self._read() != state:
                    return
                agent = self._agent(state)
                if agent is None or not agent.alive():
                    self.session_cls.retire(state.agent_name, sid=state.plan.sid, host=state.host)
        except (BlockingIOError, ConfigError, OSError, SessionError, SessionConfigError):
            return

    def _input_agent(self, state: DelegateState) -> Session | None:
        """Its live owned agent, for resume's input."""
        agent = self._answered_agent(state) if owned(state.plan) else None
        return agent if agent is not None and agent.alive() else None

    def _wound_down(self) -> None:
        """Up to WIND_DOWN_WAIT s for a completed run's drive to let the run lock go."""
        deadline = time.monotonic() + WIND_DOWN_WAIT
        while self.store.running() and time.monotonic() < deadline:
            try:
                if not self._read().completed:
                    return
            except ConfigError:
                return
            time.sleep(TUI_POLL)

    def _sweep(self) -> None:
        try:
            sweep(self.store)
        except OSError as error:
            raise ConfigError(str(error)) from None

    def _live(self, payload: bytes | None) -> bool:
        """Whether the run `payload` records is live: a readable record's per _alive; else while
        its run lock is held or a corrupt one's drive runs.
        """
        if payload is None:
            return self.store.running()
        try:
            return self._alive(self._parse(payload))
        except ConfigError:
            drive = _loaded(payload).get("drive")
        return self.store.running() or self._running(drive if isinstance(drive, str) else None)

    def _configure(self) -> None:
        try:
            self.session_cls.configure_from(self._settings)
        except SessionConfigError as error:
            raise ConfigError(str(error)) from None

    def _save(self, state: DelegateState) -> None:
        RunRecord(self.layout, self.store).save(state)

    def _read(self) -> DelegateState:
        return self._parse(self.store.read())

    def _parse(self, payload: bytes | None) -> DelegateState:
        return RunRecord(self.layout, self.store).parse(payload)

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
        view.session_cls, view._viewer = Session, None
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
        """The display on the agent's Host; the events file."""
        settings = thaw(Composer().config.values)
        try:
            session_cls.configure_from(settings)
            config.delegates(settings)
            session_cls.host()
            session_cls.layout(display=display)
            if events is not None:
                validate_events(events)
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

    def _own(self, state: DelegateState) -> Session | None:
        """The agent session this process started for `state`'s attempt."""
        agent = self.driver.session
        if (
            agent is not None
            and state.attempt == self._state.attempt
            and agent.sid == state.plan.sid
        ):
            return agent
        return None

    def _agent(self, state: DelegateState) -> Session | None:
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

    def _answered_agent(self, state: DelegateState) -> Session | None:
        try:
            return self._agent(state)
        except Unanswered:
            return None

    def _known_agent(self, state: DelegateState) -> Session | None:
        try:
            return self._agent(state)
        except Unanswered as error:
            raise ConfigError(f"{self.workdir}: agent session liveness unknown: {error}") from None

    def _alive(self, state: DelegateState) -> bool:
        if owned(state.plan):
            if state.completed:
                return False
            if self.store.running():
                return True
            agent = self._answered_agent(state) if state.bound else None
            return agent is not None and agent.alive()
        if state.detached:
            return self._running(state.drive) or self.store.running()
        return not state.completed and self.store.running()

    def alive(self) -> bool:
        return self._alive(self._read())

    def outcome(self) -> Outcome | None:
        result = self._read().result
        return None if result is None else result.outcome

    def _named(self, mode: str) -> bool:
        return mode == Mode.TUI or (self.driver.docker and mode == Mode.HEADLESS)

    def _record(self, state: DelegateState) -> Record:
        sessions = (state.agent_name,) if state.plan.spec.mode == "tui" else ()
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
        try:
            found = None if agent is None else agent.status()
        except Unanswered:
            found, unanswered = None, True
        except SessionError:  # its terminal gone
            found = None
        alive = (
            self.store.running()
            or self._alive(state)
            and (state.plan.spec.mode != Mode.TUI or unanswered or found is not None)
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

    def _shown_agent(self, state: DelegateState) -> Session | None:
        agent = self._agent(state)
        return agent if agent is not None and agent.alive() and agent.showable else None

    def _views(self, state: DelegateState) -> tuple[list[tuple[str, Host]], Unanswered | None]:
        """Its live showable agent session and Host; why its Host could not tell, if so."""
        views, unanswered = [], None
        try:
            if self._shown_agent(state) is not None:
                views.append((state.agent_name, self._session_host(state.host)))
        except Unanswered as error:
            unanswered = error
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
        """Unanswered, once the drive stops, if the agent's Host could not tell."""
        state = self._read()
        claim = self.store.closing_claim() if owned(state.plan) else contextlib.nullcontext()
        with claim:
            try:
                agent, unanswered = self._agent(state), None
            except Unanswered as error:
                agent, unanswered = None, error
            live = agent is not None and _live(agent)
            if agent is not None:
                agent.close()
        if unanswered is None:
            self._stop_owned(closed=live)
        unchecked = (
            None
            if unanswered is None
            else f"close {state.agent_name}: agent session not checked: {unanswered}"
        )
        try:
            self._stop_drive(state)
        except SessionError as error:
            also = "" if unchecked is None else f"; {unchecked}"
            raise SessionError(f"{error}{also}") from error
        if unchecked is not None:
            raise Unanswered(unchecked)

    def _control(self, path: str | None) -> ProcessControl | None:
        if path is None:
            return None
        try:
            return self._controls.attach(path)
        except OSError:
            return None

    def _running(self, path: str | None) -> bool:
        control = self._control(path)
        try:
            return control is not None and control.running()
        except OSError:
            return False

    @staticmethod
    def _spawn(control: ProcessControl, launch: Launch, log: str) -> None:
        """`control`'s command in a new session, SIGNALS blocked, appending to regular file `log`."""
        fd = os.open(
            log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "not a regular file", log)
            mask = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
            try:
                process = subprocess.Popen(
                    control.command(),
                    cwd=launch.cwd,
                    env=dict(launch.env),
                    stdin=subprocess.DEVNULL,
                    stdout=fd,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        finally:
            os.close(fd)
        threading.Thread(target=process.wait, daemon=True).start()

    def _stop_drive(self, state: DelegateState) -> None:
        """SIGTERM to its drive's group, then SIGKILL; SessionError if it outlives both."""
        control = self._control(state.drive) if state.detached else None
        if control is None:
            return
        try:
            for stop in (control.terminate, control.kill):
                stop()
                deadline = time.monotonic() + self._stop_wait
                while control.live():
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(TUI_POLL)
                else:
                    return
        except OSError as error:
            raise SessionError(f"close {state.harness_name}: {error.strerror or error}") from error
        raise SessionError(f"close {state.harness_name}: did not exit")

    def _stop_owned(self, *, closed: bool) -> None:
        """Once an owned run's worker is closed or gone: delegate.json `closing`, and its still
        active attempt ended once as CLOSED, with one Finished; closed: a lock's release claimed."""
        try:
            state = self._read()
            if not owned(state.plan):
                return
            n = RunRecord(self.layout, self.store).stop(CLOSED, sid=state.plan.sid)
        except (ConfigError, OSError) as error:
            say(f"delegate: {error}")
            return
        if n is not None:
            state = replace(state, attempt=n)
            finish(state, CLOSED, None)
        if closed:
            claim_release(state.plan, state.agent_name)
        if n is not None and state.callback is not None:
            EventCallback(state.callback, self.store, sid=state.plan.sid, attempt=n)(
                Finished(CLOSED)
            )

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

            if owned(self._state.plan):
                try:
                    return self._drive(callback, on_event)
                finally:
                    self._clear_tmp()
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
                    self._crashed(error, callback)
                raise
            finally:
                self._clear_tmp()

    def _crashed(
        self,
        error: BaseException,
        callback: EventCallback | None,
        *,
        refused: bool | None = None,
        handle: Session | None = None,
    ) -> Result:
        ended = self._start_failure_end(error)
        if isinstance(error, (Exception, SystemExit)):
            RunLog(self.layout.run).note(
                "run_error",
                error=ended.error,
                traceback="".join(traceback.format_exception(error)),
                best_effort=True,
            )
        if refused is None:
            self._finished(ended, callback, None)
        else:
            self._drive_end(ended, handle, callback, None, notes=not refused, unbound=False)
            if handle is not None:
                live = _live(handle)
                try:
                    handle.close()
                except SessionError as failure:
                    say(f"delegate: tui: close: {failure}")
                else:
                    if live:
                        claim_release(self._state.plan, self._state.agent_name)
        return ended

    def _finished(
        self,
        result: Result,
        callback: EventCallback | None,
        on_event: Callable[[Event], None] | None,
        *,
        store: bool = True,
    ) -> None:
        if store:
            try:
                self._complete(result)
            except (ConfigError, OSError) as failure:
                say(f"delegate: {failure}")
        for each in (callback, on_event):
            if each is not None:
                each(Finished(result))

    def _drive(
        self, callback: EventCallback | None, on_event: Callable[[Event], None] | None
    ) -> Result:
        """An owned run: an exception after Begun leaves the attempt as it is."""

        def begun(event: Event) -> None:
            for each in (callback, on_event):
                if each is not None:
                    each(event)

        attempts = Attempts(self._state.plan, begun, self.driver)
        try:
            opened = attempts.begin()
        except BaseException as error:
            if not attempts.begun:
                handle = attempts.handle or self._own(self._state)
                self._crashed(error, callback, refused=attempts.refused, handle=handle)
            raise
        if not isinstance(opened, Result):
            return self._await(opened, callback, on_event)
        if attempts.handle is None:
            if self._drive_end(opened, None, callback, on_event, unbound=False) is _Ended.LOST:
                return self._stored(self._read(), on_event)
            return opened
        if attempts.stale:  # stored before the stop
            handle = attempts.handle
            ended = self._drive_end(
                opened,
                handle,
                callback,
                on_event,
                unbound=False,
                closing=True,
                then=lambda: attempts.stop(handle),
            )
            return self._stored(self._read(), on_event) if ended is _Ended.LOST else opened
        if self._drive_end(opened, attempts.handle, callback, on_event) is not _Ended.LOST:
            return opened
        return self._await(attempts.handle, callback, on_event)

    def _drive_end(
        self,
        result: Result,
        handle: Session | None,
        callback: EventCallback | None,
        on_event: Callable[[Event], None] | None,
        *,
        unbound: bool = True,
        give_up: str | None = None,
        notes: bool = True,
        closing: bool = False,
        then: Callable[[], object] | None = None,
    ) -> _Ended:
        """Compare-and-set, then `then`."""
        n, sid = self._state.attempt, self._state.plan.sid
        try:
            won = RunRecord(self.layout, self.store).complete(
                n, result, sid=sid, unbound=unbound, closing=closing
            )
        finally:
            if then is not None:
                then()
        if not won:
            return _Ended.LOST
        finish(self._read(), result, handle, give_up=give_up, notes=notes)
        self._finished(result, callback, on_event, store=False)
        return _Ended.STORED

    def _stored(self, state: DelegateState, on_event: Callable[[Event], None] | None) -> Result:
        """Attempt n's stored Result (a follow-up's `previous`), sent to on_event."""
        p, n = self._state.plan, self._state.attempt
        if state.plan.sid != p.sid or state.attempt not in (n, n + 1):
            raise ConfigError(f"{self.workdir}: delegate attempt was replaced")
        result = state.previous if state.attempt == n + 1 else state.result
        if result is None:
            raise ConfigError(f"{self.workdir}: delegate attempt {n} has no Result")
        self._finished(result, None, on_event, store=False)
        return result

    def _await(
        self,
        handle: Session,
        callback: EventCallback | None,
        on_event: Callable[[Event], None] | None,
    ) -> Result:
        """Attempt n's Result (a follow-up's `previous`); ends it itself only while opening past
        run.idle_timeout or once its worker was gone DRAIN_WAIT s (AC18).
        """
        p, n = self._state.plan, self._state.attempt
        idle, start = p.run.idle_timeout, time.monotonic()
        checked = gone = None
        while True:
            state = self._read()
            if state.attempt != n or state.completed or state.plan.sid != p.sid:
                return self._stored(state, on_event)
            now = time.monotonic()
            if checked is None or now - checked >= GONE_EVERY:
                checked = now
                with contextlib.suppress(Unanswered):
                    gone = None if handle.alive() else gone or now
            if not state.bound and idle and now - start >= idle:
                ending = Result(0, None, "the agent run returned no outcome")
                if (
                    self._drive_end(ending, handle, callback, on_event, give_up=idle_reason(idle))
                    is not _Ended.LOST
                ):
                    return ending
            elif gone is not None and now - gone >= DRAIN_WAIT:
                ending = Result(1, None, "stopped: session harness ended without a Result")
                if (
                    self._drive_end(ending, handle, callback, on_event, unbound=False)
                    is not _Ended.LOST
                ):
                    return ending
            time.sleep(TUI_POLL)

    def _start_failure_end(self, error: BaseException) -> Result:
        """A signal's stop ends as the saved start_failure, if any."""
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
        if not owned(self._state.plan):
            RunRecord.announce(self._state, result)

    def _exited(self, rc: int) -> None:
        RunRecord.exited(self._state, rc)

    def _current(self) -> DelegateState:
        state = self._read()
        if (state.plan.sid, state.attempt) != (self._state.plan.sid, self._state.attempt):
            raise ConfigError(f"{self.workdir}: delegate attempt was replaced")
        return state

    def _harness_launch(self, state: DelegateState) -> Launch:
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

    def _launch(self, previous: DelegateState, state: DelegateState) -> Record:
        """`state` saved with its drive before it starts; `previous` if it cannot."""
        launch = self._harness_launch(state)
        try:
            control = self._controls.prepare(
                launch.argv, launch.cwd, env=launch.env, blocked_signals=SIGNALS
            )
        except OSError as error:
            raise SessionError(f"detach {state.harness_name}: {_why(error)}") from None
        state = replace(state, detached=True, start_failure=None, drive=control.path)
        record = self._record(state)
        try:
            self._save(state)
            self._spawn(control, launch, self.layout.drive_log)
            control.wait_started()
        except BaseException as error:
            with contextlib.suppress(OSError):
                control.cancel()
            self._save(previous)
            if isinstance(error, OSError):
                raise SessionError(f"detach {state.harness_name}: {_why(error)}") from None
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

    def _started(self, state: DelegateState) -> None:
        """Tui: waits, outside the mutex, until this run's agent session has its client running;
        else SessionError naming why, its drive and agent closed; a Host failure or the start
        limit is first saved as start_failure, unless the drive ended.
        """
        if state.plan.spec.mode != Mode.TUI:
            return
        limit = DOCKER_START_TIMEOUT if self.driver.docker else TUI_START_TIMEOUT
        deadline = time.monotonic() + limit
        agent = unanswered = None
        ended = False
        try:
            while True:
                agent, unanswered = self._probed_agent(state)
                try:
                    if agent is not None and agent.alive() and agent.status().alive:
                        self._publish_record(state)
                        return
                except Unanswered as error:
                    unanswered = error
                ended = not self._running(state.drive)
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
            try:
                self._stop_drive(state)
            except SessionError:
                self._start_failed(None)
        if agent is not None:
            with contextlib.suppress(SessionError):
                agent.close()
        if busy := self._busy(state):
            raise SessionError(str(busy))
        raise SessionError(message)

    def _publish_record(self, state: DelegateState) -> None:
        """Its Record in its agent session's worker dir, through that session's view."""
        try:
            agent = self.session_cls.attach(state.agent_name, sid=state.plan.sid, host=state.host)
            if not isinstance(agent, _Publishes):
                raise SessionError(f"session {state.agent_name}: stores no Record")
            agent.publish(self._record(state))
        except SessionError as error:
            say(f"delegate: Record not published: {error}")

    def _probed_agent(self, state: DelegateState) -> tuple[Session | None, Unanswered | None]:
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

    def _tail(self, state: DelegateState, agent: Session | None) -> str:
        """`; last lines: …` of the agent's client: its screen, else its Host row's."""
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

    def _busy(self, state: DelegateState) -> SessionBusy | None:
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
        self._settle()
        try:
            takes = input is not None and self._input_agent(self._read()) is not None
        except ConfigError:
            takes = False
        if not takes:
            self._wound_down()
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
                if (agent := self._input_agent(state)) is not None:
                    # its worker's TurnStarted opens the follow-up attempt (D9)
                    if state.closing:
                        RunRecord(self.layout, self.store).save(replace(state, closing=False))
                    RunLog(state.plan.session.workdir).note("input", text=input)
                    agent.send(input)
                    return self._record(state)
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
                    # stored, never re-resolved (FR-62)
                    environment=state.plan.session.environment,
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
            accepted = self._probed(accepted, state.attempt + 1, probe)
            if agent is not None:
                live = _live(agent)
                agent.close()
                if live:
                    claim_release(state.plan, state.agent_name)
            state = replace(
                state,
                plan=accepted,
                completed=False,
                result=None,
                attempt=state.attempt + 1,
                previous=None,
                bound=False,
                closing=False,
            )
            try:
                os.makedirs(self.layout.agent, exist_ok=True)
            except OSError as error:
                raise ConfigError(str(error)) from None
            record = self._launch(previous, state)
        self._started(self._state)
        return record


def _why(error: OSError) -> str:
    return error.strerror or str(error)


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
