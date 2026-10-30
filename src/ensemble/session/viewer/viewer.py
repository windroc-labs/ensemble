"""Viewer, the one display of every Host's sessions on the host tmux, places a CommandView in a
view session it makes (tagged VIEW = VIEW_TAG). FORMAT has one encoder (fold), one decoder (unfold).
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Mapping

from ensemble.session.engine import tmux_cli
from ensemble.session.engine.tmux_cli import NAME, VIEW, TmuxCli, TuiError, display_error
from ensemble.session.engine.types import (
    NO_DISPLAY,
    ConfigError,
    NotShowable,
    PaneStatus,
    SessionError,
    TuiSettings,
    Unanswered,
)
from ensemble.session.hosts.base import Host, worker_state
from ensemble.session.hosts.types import CommandView
from ensemble.session.viewer import hidden as hidden_store
from ensemble.session.viewer.grid import Grid
from ensemble.session.viewer.placement import Placement
from ensemble.session.viewer.types import Retile, Split
from ensemble.utils import Display, thaw

FORMAT = "<opener>[/<split>[/<split_from>[/hidden]]]"
HIDDEN = "hidden"  # started hidden; only NO_DISPLAY sets it
NO_PANE_HINT = ": run from tmux, or pass split_from"
VIEW_TAG = "viewer"
LOG = logging.getLogger("session")
SHOW_TRIES = 3
SHOW_RETRY_WAIT = 1  # seconds

Parts = tuple[str | None, str | None, str | None, bool]


def fold(opener, split, split_from, *, hidden=False) -> Display:
    """FORMAT's one encoder; None when every part is. ConfigError for a bad part."""
    if opener is not None and not (isinstance(opener, str) and NAME.fullmatch(opener)):
        raise ConfigError(f"opener must be a tmux session name, got {opener!r:.80}")
    if split is not None and split not in tuple(Split):
        raise ConfigError(f"split must be {' or '.join(Split)}, got {split!r:.80}")
    if split_from is not None and not (isinstance(split_from, str) and NAME.fullmatch(split_from)):
        raise ConfigError(f"split_from must match [A-Za-z0-9_-]+, got {split_from!r:.80}")
    parts = (opener or "", split or "", split_from or "", HIDDEN if hidden else "")
    return "/".join(parts).rstrip("/") or None


def unfold(display) -> Parts | None:
    """Decodes and validates FORMAT; empty parts become None, NO_DISPLAY becomes hidden."""
    if display is None:
        return None, None, None, False
    if display == NO_DISPLAY:
        return None, None, None, True
    if display_error(display):
        return None
    given = display.split("/")
    if given[3:] not in ([], [HIDDEN]):
        return None
    opener, split, split_from = (part or None for part in (*given, "", "")[:3])
    try:
        fold(opener, split, split_from)
    except ConfigError:
        return None
    return opener, split, split_from, given[3:] == [HIDDEN]


def parts(display) -> Parts:
    found = unfold(display)
    if found is None:
        raise ConfigError(f"display {display!r:.80}: not {FORMAT}")
    return found


def listed(display: Display, value: Display) -> bool:
    found, other = unfold(display), unfold(value)
    return value == display or (
        found is not None and other is not None and found[0] is not None and found[0] == other[0]
    )


class Viewer:
    """settings: [viewer]'s workers_per_column and grid_retile."""

    def __init__(self, settings: Mapping | None = None, *, tmux: TmuxCli | None = None) -> None:
        table = thaw(settings or {})
        if extra := sorted(set(table) - {"workers_per_column", "grid_retile"}):
            raise ConfigError(f"unknown key {extra[0]!r} in [viewer]")
        per = table.get("workers_per_column")
        retile = table.get("grid_retile")
        if per is not None and (
            isinstance(per, bool) or not isinstance(per, int) or not 1 <= per <= 9999
        ):
            raise ConfigError("workers_per_column: want an integer from 1 to 9999")
        if retile is not None and retile not in tuple(Retile):
            raise ConfigError(f"grid_retile: want one of {', '.join(Retile)}")
        self._per_column, self._retile = per, retile
        self._tmux = TmuxCli() if tmux is None else tmux
        self._grid = Grid(self._tmux)
        self._placement = Placement(tmux=self._tmux, grid=self._grid)

    def show(
        self, name: str, host: Host, *, display: Display = None, command: str | None = None
    ) -> str | None:
        """None once shown, else an attach command; display None: the session's stored one;
        command: a shell command run instead, `{{session}}` the session name.
        """
        target, display = self._viewed(name, host, display)
        if command is None and not os.environ.get("TMUX"):
            return target.printed
        self._view(name, target, host.tui_settings())
        try:
            if command is None:
                opener, split, split_from, _ = parts(display)
                why = self._placement.open_pane(
                    name,
                    opener=opener,
                    split=split,
                    split_from=split_from,
                    per_column=self._per_column,
                    retile=self._retile,
                )
            else:
                why = self._placement.run_command(name, command)
        except TuiError as e:
            raise ConfigError(str(e)) from None
        placed = why is None
        if not placed and command is None:
            with contextlib.suppress(TuiError):
                placed = self._placement.shown(name) is not None
        if placed:
            if command is None:
                self._placement.watch(name)
            self._mark(name, host, hidden=False)
        if why is None:
            return None
        LOG.warning("show %s: %s", name, why)
        return target.printed

    def _prepare(self, name: str, host: Host) -> bool:
        target, _ = self._viewed(name, host, None)
        return self._view(name, target, host.tui_settings())

    def _viewed(self, name: str, host: Host, display: Display) -> tuple[CommandView, Display]:
        """Retries preparation on Unanswered."""
        for _ in range(SHOW_TRIES - 1):
            with contextlib.suppress(Unanswered):
                return self._prepared(name, host, display)
            time.sleep(SHOW_RETRY_WAIT)
        return self._prepared(name, host, display)

    def _prepared(self, name: str, host: Host, display: Display) -> tuple[CommandView, Display]:
        target = self._target(name, host)
        if display is None and (state := worker_state(name)) is not None:
            display = state.display
        return target, display

    def hide(self, name: str, host: Host, *, if_closed: bool = False) -> None:
        """Undoes show: hidden until a show (if_closed: only once its pane was closed by hand)."""
        self._target(name, host)
        size = host.tui_settings().hidden_size
        if self._tmux.status(name) is None:
            if not if_closed:
                self._mark(name, host, hidden=True)
            return
        try:
            if if_closed:
                closed = self._placement.closed(name)
                if closed is None or not self._placement.forget(name, closed):
                    return
            else:
                pane = self._placement.shown(name)
                if pane is not None:
                    self._tmux.kill(pane)
                self._placement.forget(name)
        except TuiError as e:
            raise SessionError(f"tmux session {name}: {e}") from None
        try:
            self._tmux.unview(name, size)
        except TuiError as e:
            LOG.warning("hide %s: hidden size: %s", name, e)
        self._mark(name, host, hidden=True)
        try:
            # a show meanwhile may have written hidden=False before this mark
            shown = if_closed and self._placement.shown(name) is not None
        except TuiError as e:
            raise SessionError(f"tmux session {name}: {e}") from None
        if shown:
            self._mark(name, host, hidden=False)

    def attach_command(self, name: str, host: Host) -> str:
        return self._target(name, host).printed

    def close(self, name: str, host: Host) -> None:
        """Kills only the view session it made for `name`."""
        if not host.attachable:
            return
        try:
            if self._tmux.option(name, VIEW) == VIEW_TAG:
                self._tmux.kill(name)
        except TuiError as error:
            raise SessionError(f"view {name}: {error}") from None

    def _mark(self, name: str, host: Host, *, hidden: bool) -> None:
        """Only a session whose worker state names a sid keeps it."""
        state = worker_state(name)
        if state is not None and state.sid is not None:
            try:
                hidden_store.write(name, state.sid, hidden=hidden, live=self._live(host))
            except OSError as error:
                raise SessionError(
                    f"tmux session {name}: hidden state: {error.strerror or error}"
                ) from error

    def _live(self, host: Host) -> set[str] | None:
        """Whose hidden entries a write keeps (None: all)."""
        found = self._tmux.sessions()
        if found is None:
            return None
        try:
            return {*found, *host.list()}
        except SessionError:
            return None

    def _target(self, name: str, host: Host) -> CommandView:
        target = host.attach_target(name)
        if target is None:
            raise NotShowable(f"host {host.name} cannot show")
        return target

    def _view(self, name: str, target: CommandView, settings: TuiSettings) -> bool:
        size = tmux_cli.sized(settings.hidden_size)
        try:
            state = self._tmux.status(name)
            if state == PaneStatus.UNKNOWN:
                raise Unanswered(f"show {name}: display unknown")
            if state is not None:
                if self._tmux.option(name, VIEW) != VIEW_TAG:
                    raise SessionError(f"show {name}: a host tmux session {name} exists")
                if state == PaneStatus.RUNNING:
                    return False
                self._tmux.kill(name)
            self._tmux.checked(
                [
                    *("new-session", "-d", "-s", name, *size, "--", *target.argv),
                    *(";", "set-option", "-t", f"={name}:", VIEW, VIEW_TAG),
                    *(";", "set-option", "-w", "-t", f"={name}:", "remain-on-exit", "on"),
                    *(";", *tmux_cli.status(name, on=settings.status_line)),
                ]
            )
        except TuiError as error:
            raise SessionError(f"show {name}: {error}") from None
        return True
