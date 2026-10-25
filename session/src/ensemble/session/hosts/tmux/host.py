"""TmuxHost, the Host on tmux, runs every tmux call through the TmuxCli it holds; the Viewer shows
its sessions as they are (SessionView). TmuxProcess, a session it hosts, keeps its Record and display
in the tmux options @record and @display.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Mapping

from ensemble.session.engine import tmux_cli
from ensemble.session.engine.tmux_cli import (
    NAME,
    POLL,
    PROCESS_ID,
    TmuxCli,
    TmuxPane,
    TuiError,
    display_error,
    events_file,
    validate_events,
    validate_start,
)
from ensemble.session.engine.types import (
    MARKS,
    ConfigError,
    HookCallback,
    Key,
    Launch,
    Liveness,
    PaneStatus,
    Paste,
    SessionError,
    SessionRow,
    StatusState,
    TuiSettings,
)
from ensemble.session.hosts.base import Host, InteractiveProcess, Process
from ensemble.session.hosts.reclaimed import ReclaimedGroup
from ensemble.session.hosts.tmux import hook
from ensemble.session.hosts.types import SessionView
from ensemble.utils import CONFIG_NAME, Display, ProcessControl, Record, sid_error

KILL_WAIT = 5


TERMINAL_IDENTITY = ("TMUX", "TMUX_PANE", "TERM")  # a terminal program's are its pane's (FR-47)


def _session(name) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise SessionError(f"no tmux session {name!r:.80}")
    return name


class TmuxProcess(Process):
    """The captured pane (a TmuxPane) and its generation's controller. A pane can set its Record and
    display options: consumers must re-validate a Record's ctl or resume before running it.
    """

    def __init__(
        self, name: str, process: str, *, tmux: TmuxCli, control: ProcessControl | None = None
    ) -> None:
        self.name = name
        self.tmux = tmux
        self.pane = TmuxPane(name, process, tmux)
        path = self.pane.option("@control") if control is None else None
        if control is None and self.pane.option("@kind") in ("detach", "terminal") and not path:
            raise SessionError(
                f"tmux session {name}: missing @control; close sessions before upgrading"
            )
        try:
            if control is not None:
                self._control = control
            else:
                self._control = ProcessControl.attach(path) if path else None
        except OSError as error:
            raise SessionError(f"tmux session {name}: invalid @control: {error}") from error

    @property
    def identity(self) -> str:
        return self.pane.process

    @property
    def token(self) -> str | None:
        return None if self._control is None else self._control.path

    def publish(self, record: Record) -> None:
        if not isinstance(record, Record):
            raise ConfigError(f"record: want a Record, got {type(record).__name__}")
        if not self.pane.own():
            raise SessionError(f"tmux session {self.name} is gone")
        try:
            self.tmux.set_option(self.pane.id, "@record", record.to_line())
        except TuiError as error:
            raise SessionError(f"tmux session {self.name}: {error}") from error

    def state(self) -> Liveness:
        return self.pane.state()

    def returncode(self) -> int | None:
        return self.pane.returncode()

    def session_identity(self) -> tuple[str | None, str | None]:
        return self.pane.option("@sid"), self.pane.option("@generation")

    def activity(self) -> StatusState | None:
        value = self.pane.option("@state")
        return StatusState(value) if value in MARKS else None

    def screen(self, lines: int | None = None) -> str:
        if not self.pane.own():
            raise SessionError(f"tmux session {self.name} is gone")
        try:
            return self.pane.read(lines)
        except TuiError as e:
            raise SessionError(f"tmux session {self.name}: {e}") from None

    def mark(self, word: StatusState, events: str | None) -> None:
        self.pane.mark(word, events)

    def alive(self) -> bool:
        return self.pane.alive() and (self._control is None or self._control.running())

    def wait_exit(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.alive() or self._group_live():
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL)
        return True

    def kill(self) -> None:
        self._kill_group()
        if not self.wait_exit(KILL_WAIT):
            raise SessionError(f"process group {self.name}: did not exit")

    def close(self) -> None:
        """Closes the pane, then ends the process group, even when the pane fails; every failure
        is in the one SessionError raised.
        """
        failures = []
        for step in (self.pane.kill, self._end_group):
            try:
                step()
            except SessionError as error:
                failures.append(str(error))
        if failures:
            raise SessionError("; ".join(failures))

    def _end_group(self) -> None:
        if self._control is None:
            return
        try:
            self._control.terminate()
        except (ConnectionRefusedError, ConnectionResetError):
            return
        except OSError as error:
            raise SessionError(f"process group {self.name}: {error}") from error
        if not self._wait_group(KILL_WAIT):
            self._kill_group()
            if not self._wait_group(KILL_WAIT):
                raise SessionError(f"process group {self.name}: did not exit")

    def _wait_group(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self._group_live():
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL)
        return True

    def record(self) -> Record | None:
        line = self.pane.option("@record")
        if line is not None:
            with contextlib.suppress(ValueError):
                return Record.parse(line)
        return None

    def display(self) -> Display:
        return self.pane.option("@display")

    def _group_live(self) -> bool:
        try:
            return self._control is not None and self._control.live()
        except OSError as error:
            raise SessionError(f"process group {self.name}: {error}") from error

    def _kill_group(self) -> None:
        if self._control is None:
            self.pane.kill()
            return
        try:
            self._control.kill()
        except OSError as error:
            raise SessionError(f"process group {self.name}: {error}") from error


class TmuxTerminal(TmuxProcess, InteractiveProcess):
    def send(self, action: Paste | Key) -> None:
        self.pane.send(action, self.alive)

    def press(self, keys) -> None:
        self.pane.press(keys, self.alive)


class TmuxHost(Host):
    """settings: [hosts.<name>]'s status_line."""

    attachable = True

    def __init__(
        self, name: str, settings: Mapping | None = None, *, tmux: TmuxCli | None = None
    ) -> None:
        super().__init__(name, settings)
        if extra := sorted(set(self.settings) - {"status_line"}):
            raise ConfigError(f"unknown key {extra[0]!r} in [hosts.{name}]")
        self.tmux = TmuxCli() if tmux is None else tmux

    def attach_target(self, name: str) -> SessionView:
        return SessionView(_session(name))

    def detach(
        self,
        name: str,
        launch: Launch,
        *,
        display: Display = None,
        events: str | None = None,
        record: Record | None = None,
    ) -> TmuxProcess:
        return self._launch(
            name, launch, display=display, events=events, record=record, terminal=False
        )

    def terminal(
        self,
        name: str,
        launch: Launch,
        *,
        display: Display = None,
        events: str | None = None,
        record: Record | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> TmuxTerminal:
        process = self._launch(
            name,
            launch,
            display=display,
            events=events,
            record=record,
            terminal=True,
            metadata=metadata,
        )
        return TmuxTerminal(name, process.identity, tmux=self.tmux, control=process._control)

    def _launch(self, name, launch, *, display, events, record, terminal, metadata=None):
        if why := display_error(display):
            raise ConfigError(why)
        self.validate(name, launch)
        events = self.events_path(events)
        events = self.prepare_events(events)
        control = ProcessControl.prepare(
            launch.argv,
            launch.cwd,
            env=launch.env,
            inherit_env=TERMINAL_IDENTITY if terminal else (),
            blocked_signals=launch.blocked_signals,
        )
        process = None
        try:
            command = Launch(tuple(control.command()), cwd=launch.cwd, env=launch.env)
            if terminal:
                process = self.tmux.start(
                    name,
                    command,
                    events=events,
                    status_line=self.tui_settings().status_line,
                    tags={
                        "@kind": "terminal",
                        "@control": control.path,
                        **(
                            {"@sid": record.sid, "@client": record.kind}
                            if record is not None and record.sid is not None
                            else {}
                        ),
                        **{
                            f"@{key}": value
                            for key, value in (metadata or {}).items()
                            if key in ("sid", "client", "generation")
                        },
                        **({"@display": display} if display is not None else {}),
                        **({"@record": record.to_line()} if record is not None else {}),
                    },
                )
            else:
                process = self.tmux.detach(
                    name,
                    command,
                    display=display,
                    events=events,
                    record=None if record is None else record.to_line(),
                    sid=None if record is None else record.sid,
                    control=control.path,
                )
            control.wait_started()
        except (TuiError, OSError) as error:
            cleanup = self._cancel_launch(control, process)
            detail = f"; {cleanup}" if cleanup else ""
            failure = (
                f"cannot run {error.filename or launch.argv[0]}: {os.strerror(error.errno)}"
                if isinstance(error, OSError) and error.errno is not None
                else str(error)
            )
            prefix = "" if terminal else f"detach {name}: "
            raise SessionError(f"{prefix}{failure}{detail}") from error
        return TmuxProcess(name, process, tmux=self.tmux, control=control)

    def validate(self, name: str, launch: Launch) -> None:
        super().validate(name, launch)
        try:
            validate_start(name, launch.argv, launch.env, {})
        except TuiError as error:
            raise SessionError(str(error)) from error
        self.tui_settings()

    def prepare_events(self, events: str | None) -> str | None:
        try:
            return None if events is None else events_file(events)
        except TuiError as error:
            raise SessionError(str(error)) from error

    def events_path(self, events: str | None) -> str | None:
        try:
            return None if events is None else validate_events(events)
        except TuiError as error:
            raise ConfigError(str(error)) from error

    def hook_callback(self, name: str, metadata: Mapping[str, str]) -> HookCallback:
        return HookCallback(
            hook.__name__,
            {
                "executable": self._executable(),
                "target": name,
                "generation": metadata["generation"],
                "environment": {
                    key: value
                    for key, value in os.environ.items()
                    if key in ("TMUX", "TMUX_TMPDIR")
                },
            },
        )

    def _executable(self) -> str:
        path = tmux_cli.tmux_path()
        if path is None:
            raise SessionError("tmux not found")
        return path

    def tui_settings(self) -> TuiSettings:
        line = self.settings.get("status_line", False)
        if not isinstance(line, bool):
            raise ConfigError("status_line: want true or false")
        return TuiSettings(line)

    def _cancel_launch(self, control: ProcessControl, process: str | None = None) -> str:
        errors = []
        try:
            control.cancel()
        except OSError as error:
            errors.append(f"cancel controller: {error}")
        if process is not None:
            pane = process.split(" ")[2]
            try:
                if self.tmux.process(pane) == process:
                    self.tmux.kill(pane)
            except TuiError as error:
                errors.append(f"close pane: {error}")
        return "; ".join(errors)

    def attach(self, name: str) -> TmuxProcess:
        """A session it launched is the pane its launch recorded (@process), whichever pane is
        active; any other session is its active pane.
        """
        state = self.tmux.status(_session(name))
        if state is None:
            raise SessionError(f"no tmux session {name}")
        if state == PaneStatus.UNKNOWN:
            raise SessionError(f"tmux session {name}: tmux failed")
        try:
            kind = self.tmux.option(name, "@kind")
            launched = kind in ("detach", "terminal")
            process = self.tmux.option(name, "@process") if launched else self.tmux.process(name)
        except TuiError as e:
            raise SessionError(f"tmux session {name}: {e}") from None
        if launched and process is None:
            raise SessionError(
                f"tmux session {name}: missing @process; close sessions before upgrading"
            )
        if launched and not PROCESS_ID.fullmatch(process or ""):
            raise SessionError(f"tmux session {name}: bad @process {process!r:.80}")
        if process is None:
            raise SessionError(f"no tmux session {name}")
        return (TmuxTerminal if kind == "terminal" else TmuxProcess)(name, process, tmux=self.tmux)

    def list(self) -> list[str]:
        found = self.tmux.listing()
        if found is None:
            raise SessionError("tmux sessions unknown: tmux failed")
        return sorted(row["name"] for row in found)

    def reclaim(self, token: str) -> ReclaimedGroup | None:
        return ReclaimedGroup.reclaim(token)

    def rows(self) -> list[SessionRow]:
        found = self.tmux.listing()
        if found is None:
            raise SessionError("tmux sessions unknown: tmux failed")
        return [
            SessionRow(
                name=row["name"],
                sid=None if sid_error(row["sid"]) else row["sid"],
                client=row["client"]
                if isinstance(row["client"], str) and CONFIG_NAME.fullmatch(row["client"])
                else None,
                state=StatusState.DEAD
                if row["dead"]
                else StatusState(row["state"])
                if row["state"] in (StatusState.DONE, StatusState.BLOCKED)
                else StatusState.WORKING,
                display=row["display"],
                shown=None,
                last_lines=row["last_lines"],
                own=False,
            )
            for row in found
        ]
