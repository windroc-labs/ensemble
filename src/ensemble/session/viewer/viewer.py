"""Viewer, the one display of every Host's sessions on the host tmux, runs a Host's CommandView in
a worker pane. FORMAT has one encoder (fold), one decoder (unfold).
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Mapping

from ensemble.session.engine.tmux_cli import NAME, TmuxCli, TuiError, display_error
from ensemble.session.engine.types import (
    NO_DISPLAY,
    ConfigError,
    NotShowable,
    SessionError,
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
LOG = logging.getLogger("session")
SHOW_TRIES = 3
SHOW_RETRY_WAIT = 1  # seconds
CHAIN_LINKS = 8

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


def _known_opener(name: str) -> bool:
    return worker_state(name) is not None


def _chain(opener: str | None) -> tuple[str, ...]:
    """The display openers from opener's on, up to an unread one or a repeat."""
    chain: list[str] = []
    at = opener
    while at is not None and len(chain) < CHAIN_LINKS:
        state = worker_state(at)
        found = None if state is None else unfold(state.display)
        at = None if found is None else found[0]
        if at is None or at == opener or at in chain:
            break
        chain.append(at)
    return tuple(chain)


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
        self._grid = Grid(self._tmux, known=_known_opener)
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
        self._unclaimed(name, host)
        try:
            if command is None:
                opener, split, split_from, _ = parts(display)
                state = worker_state(name)
                why = self._placement.open_pane(
                    name,
                    target.argv,
                    target.printed,
                    sid=None if state is None else state.sid,
                    generation=None if state is None else state.generation,
                    opener=opener,
                    split=split,
                    split_from=split_from,
                    per_column=self._per_column,
                    retile=self._retile,
                    label=host.tui_settings().status_line,
                    fallbacks=_chain(opener),
                )
            else:
                why = self._placement.run_command(name, command)
        except TuiError as e:
            raise ConfigError(str(e)) from None
        placed = why is None
        if not placed and command is None:
            with contextlib.suppress(TuiError):
                placed = any(w.attach == target.printed for w in self._placement.panes(name))
        if placed:
            self._mark(name, host, hidden=False)
        if why is None:
            return None
        LOG.warning("show %s: %s", name, why)
        return target.printed

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
        """Undoes show (if_closed: only while no worker pane shows it)."""
        self._target(name, host)
        try:
            panes = self._placement.panes(name)
            if if_closed and panes:
                return
            for pane in panes:
                self._placement.close(pane)
        except TuiError as e:
            raise SessionError(f"tmux session {name}: {e}") from None
        if not panes:
            self._placement.unlabel()
        self._mark(name, host, hidden=True)
        try:
            # a show meanwhile may have written hidden=False before this mark
            shown = if_closed and bool(self._placement.panes(name))
        except TuiError as e:
            raise SessionError(f"tmux session {name}: {e}") from None
        if shown:
            self._mark(name, host, hidden=False)

    def close(self, name: str, host: Host) -> None:
        if not host.attachable:
            return
        try:
            panes = self._placement.panes(name)
            for pane in panes:
                self._placement.close(pane)
        except TuiError as error:
            raise SessionError(f"view {name}: {error}") from None
        if not panes:
            self._placement.unlabel()

    def attach_command(self, name: str, host: Host) -> str:
        return self._target(name, host).printed

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
        try:
            return set(host.list())
        except SessionError:
            return None

    def _unclaimed(self, name: str, host: Host) -> None:
        """SessionError when an attachable Host's worker `name` is a host tmux session's name."""
        if not host.attachable:
            return
        try:
            taken = self._tmux.has_session(name)
        except TuiError as error:
            raise SessionError(f"show {name}: {error}") from None
        if taken:
            raise SessionError(f"show {name}: a host tmux session {name} exists")

    def _target(self, name: str, host: Host) -> CommandView:
        target = host.attach_target(name)
        if target is None:
            raise NotShowable(f"host {host.name} cannot show")
        return target
