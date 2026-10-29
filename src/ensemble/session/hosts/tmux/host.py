"""TmuxHost, the Host on tmux, runs every tmux call through the TmuxCli it holds; its workers run
under the guard (hosts/tmux/managed.py), each displayed in the tmux session of its name, which the
Viewer shows as it is (SessionView).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace

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
    worker_row,
)
from ensemble.session.hosts.base import activity as worker_activity
from ensemble.session.hosts.tmux import managed
from ensemble.session.hosts.types import SessionView
from ensemble.utils import (
    Display,
)


def _session(name) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise SessionError(f"no tmux session {name!r:.80}")
    return name


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
        return replace(super().tui_settings(), status_line=line)

    def list(self) -> list[str]:
        return [row.name for row in self.rows()]

    def rows(self) -> list[SessionRow]:
        rows = []
        for name, state in managed.records(self.name).items():
            assert state.generation is not None
            live = self.worker_liveness(name, state.generation)
            if live == Liveness.ABSENT:
                continue
            word = (
                StatusState.UNKNOWN
                if live == Liveness.UNKNOWN
                else worker_activity(name, state.generation) or StatusState.WORKING
            )
            rows.append(worker_row(name, state, word))
        return rows

    def _capacity_namespace(self) -> str:
        return f"guard:{os.path.realpath(managed.control_root())}:{self.name}"
