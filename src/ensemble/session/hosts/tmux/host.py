"""Local guard workers; Viewer owns their tmux displays."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Mapping
from dataclasses import replace

from ensemble.session.engine.tmux_cli import (
    NAME,
    TmuxCli,
)
from ensemble.session.engine.types import (
    ConfigError,
    Launch,
    Liveness,
    SessionError,
    SessionRow,
    StatusState,
    TuiSettings,
    Unanswered,
)
from ensemble.session.harness import guard
from ensemble.session.hosts.base import (
    STATE_MAX,
    Host,
    InteractiveProcess,
    worker_row,
)
from ensemble.session.hosts.base import activity as worker_activity
from ensemble.session.hosts.tmux import managed
from ensemble.session.hosts.types import CommandView
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

    def attach_target(self, name: str) -> CommandView:
        name = _session(name)
        directory = managed.location(self.name, name)
        try:
            managed.check_directory(managed.control_root())
            managed.check_directory(directory)
            generation = managed.load_json(
                os.path.join(directory, "current.json"), limit=STATE_MAX
            ).get("generation")
        except FileNotFoundError:
            raise SessionError(f"no tmux worker {name}") from None
        except (OSError, ValueError) as error:
            raise Unanswered(f"worker {name}: guard record: {error}") from error
        if not isinstance(generation, str) or not managed.GENERATION.fullmatch(generation):
            raise Unanswered(f"worker {name}: generation unknown")
        live = self.worker_liveness(name, generation)
        if live == Liveness.ABSENT:
            raise SessionError(f"no tmux worker {name}")
        if live == Liveness.UNKNOWN:
            raise Unanswered(f"worker {name}: guard unknown")
        argv = (
            sys.executable,
            "-I",
            guard.__file__,
            "--attach",
            os.path.join(directory, generation, "display"),
        )
        return CommandView(printed=shlex.join(argv), argv=argv)

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

    def _client_exited(self, name: str, generation: str) -> bool:
        return managed.exited(self.name, _session(name), generation)

    def validate(self, name: str, launch: Launch) -> None:
        super().validate(name, launch)
        _session(name)
        if shutil.which(launch.argv[0], path=launch.env.get("PATH", os.defpath)) is None:
            raise SessionError(f"command not found: {launch.argv[0]}")
        self.tui_settings()

    def tui_settings(self) -> TuiSettings:
        line = self.settings.get("status_line", False)
        if not isinstance(line, bool):
            raise ConfigError("status_line: want true or false")
        return replace(super().tui_settings(), status_line=line)

    def list(self) -> list[str]:
        return [row.name for row in self.rows()]

    def _last_lines(self, name: str, sid: str, generation: str) -> str:
        found = managed.records(self.name).get(name)
        if found is None or (found.sid, found.generation) != (sid, generation):
            return ""
        directory = os.path.join(managed.location(self.name, name), generation)
        try:
            managed.check_directory(directory)
            text = managed.load_json(
                os.path.join(directory, "diagnostic.json"), limit=STATE_MAX
            ).get("text")
        except (OSError, ValueError):
            return ""
        return text if isinstance(text, str) else ""

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
