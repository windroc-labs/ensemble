"""TmuxHost, the Host on tmux, runs every tmux call through the TmuxCli it holds; its workers run
under the guard (hosts/tmux/managed.py), each displayed in the tmux session of its name, which the
Viewer shows as it is (SessionView).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from ensemble.session.engine.tmux_cli import (
    NAME,
    TmuxCli,
    TuiError,
    validate_start,
)
from ensemble.session.engine.types import (
    ConfigError,
    Launch,
    Liveness,
    SessionError,
    SessionRow,
    StatusState,
    TuiSettings,
)
from ensemble.session.hosts.base import (
    Host,
    InteractiveProcess,
    WorkerState,
    worker_names,
    worker_row,
    worker_state,
)
from ensemble.session.hosts.base import activity as worker_activity
from ensemble.session.hosts.tmux import managed
from ensemble.session.hosts.types import SessionView
from ensemble.utils import (
    CONFIG_NAME,
    Display,
    sid_error,
)


def _session(name) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise SessionError(f"no tmux session {name!r:.80}")
    return name


def _marked(row: dict) -> str | None:
    found = worker_state(row["name"])
    if found is not None and found.generation is not None and found.sid == row["sid"]:
        return worker_activity(row["name"], found.generation)
    return row["state"]


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
        return managed.start(
            self, name, launch, terminal=terminal, display=display, events=events, metadata=metadata
        )

    def worker_liveness(self, name: str, generation: str) -> Liveness:
        return managed.liveness(self.name, _session(name), generation)

    def _release(self, name: str, generation: str) -> None:
        """Its display, the tmux session `name`, only while tagged with `generation`."""
        try:
            if self.tmux.option(_session(name), "@generation") == generation:
                self.tmux.kill(name)
        except TuiError as error:
            raise SessionError(f"tmux session {name}: {error}") from error

    def validate(self, name: str, launch: Launch) -> None:
        super().validate(name, launch)
        try:
            validate_start(name, launch.argv, launch.env, {})
        except TuiError as error:
            raise SessionError(str(error)) from error
        self.tui_settings()

    def tui_settings(self) -> TuiSettings:
        line = self.settings.get("status_line", False)
        if not isinstance(line, bool):
            raise ConfigError("status_line: want true or false")
        return TuiSettings(line)

    def list(self) -> list[str]:
        """Its tmux sessions, and its live (or unknown) workers whose display is gone."""
        names = {row["name"] for row in self._listing()}
        return sorted(names | {name for name, _, _ in self._display_less(names)})

    def _display_less(self, displayed: set[str]) -> list[tuple[str, WorkerState, Liveness]]:
        """Its workers with no tmux session of their name, unless ABSENT: a dead one's dir is the
        tui sweep's to expire.
        """
        found = []
        for name in worker_names():
            state = worker_state(name)
            if name in displayed or state is None or state.host != self.name:
                continue
            if state.generation is None:
                continue
            if (live := self.worker_liveness(name, state.generation)) != Liveness.ABSENT:
                found.append((name, state, live))
        return found

    def _listing(self) -> list[dict]:
        try:
            return self.tmux.listing()
        except TuiError as error:
            raise SessionError(f"tmux sessions unknown: {error}") from error

    def rows(self) -> list[SessionRow]:
        """A tui worker's state from its harness's mark, any other session's from its @state; a
        worker whose display is gone from its activity (unknown liveness: unknown).
        """
        found = self._listing()
        gone = [
            worker_row(
                name,
                state,
                StatusState.UNKNOWN
                if live == Liveness.UNKNOWN
                else worker_activity(name, state.generation or "") or StatusState.WORKING,
            )
            for name, state, live in self._display_less({row["name"] for row in found})
        ]
        displayed = [
            SessionRow(
                name=row["name"],
                sid=None if sid_error(row["sid"]) else row["sid"],
                client=row["client"]
                if isinstance(row["client"], str) and CONFIG_NAME.fullmatch(row["client"])
                else None,
                state=StatusState.DEAD
                if row["dead"]
                else StatusState(word)
                if (word := _marked(row)) in (StatusState.DONE, StatusState.BLOCKED)
                else StatusState.WORKING,
                display=row["display"],
                shown=None,
                last_lines=row["last_lines"],
                own=False,
            )
            for row in found
        ]
        return displayed + gone

    def _capacity_namespace(self) -> str:
        try:
            socket = self.tmux.checked(["display-message", "-p", "#{socket_path}"]).stdout.strip()
        except TuiError as error:
            raise SessionError(f"host {self.name}: inventory namespace: {error}") from error
        if not socket or not socket.isprintable() or not os.path.isabs(socket):
            raise SessionError(f"host {self.name}: inventory namespace unavailable")
        return f"tmux:{os.path.realpath(socket)}"
