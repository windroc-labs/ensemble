"""Display functions for the session layer. FORMAT applies only on an attachable Host; on
another, a display is kept as given and matched exactly.
"""

from __future__ import annotations

import contextlib
from dataclasses import replace

from ensemble.session.engine.tmux_cli import TuiError, display_error
from ensemble.session.engine.types import NO_DISPLAY, ConfigError, SessionError, SessionRow
from ensemble.session.hosts.base import Host, worker_state
from ensemble.session.viewer import hidden as hidden_store
from ensemble.session.viewer.types import Split
from ensemble.session.viewer.viewer import (
    FORMAT,
    HIDDEN,
    NO_PANE_HINT,
    Parts,
    Viewer,
    fold,
    listed,
    parts,
    unfold,
)
from ensemble.utils import Display, Status, StatusState


def _given(value: Display, found: Parts) -> Parts:
    """found, unless a caller gave the hidden part itself: only NO_DISPLAY sets it."""
    if found[3] and value != NO_DISPLAY:
        raise ConfigError(f"display {value!r:.80}: its hidden part comes only from {NO_DISPLAY}")
    return found


def validate_display(value: Display, host: Host) -> None:
    why = display_error(value) if host.attachable else None
    if why is None and value is not None and not isinstance(value, str):
        why = f"display {value!r:.80}: want text"
    if why:
        raise ConfigError(why)


def display(viewer: Viewer, value: Display, host: Host) -> Display:
    """value completed in the caller's process: its opener part the caller's when empty."""
    validate_display(value, host)
    found = unfold(value) if host.attachable else None
    if found is None:
        return value
    opener, split, split_from, hidden = _given(value, found)
    if opener is not None:
        return value
    try:
        opener = viewer._placement.opener()
    except TuiError:
        return fold(None, split, split_from, hidden=True) if hidden else value
    return fold(opener, split, split_from, hidden=hidden)


def display_help(host: Host) -> str:
    if not host.attachable:
        return ""
    return (
        "[OPENER][/SPLIT[/ANCHOR]] or none\n"
        "    OPENER: tmux session name; empty means caller.\n"
        f"    SPLIT: {' or '.join(Split)}; ANCHOR: tmux session with a visible pane.\n"
        f"    {NO_DISPLAY}: start without a pane, the caller still its opener.\n"
        "    Omitted display uses caller context. Placement can reuse a grid or stack;\n"
        "    a grid overrides split/anchor choices. Outside tmux, show prints the\n"
        "    attach command. Start in tmux, use a visible ANCHOR or --display none.\n"
        "    No anchor: use a visible terminal/ANCHOR or start with --display none.\n"
        "    Example prerequisites: Codex configured, default Host tmux, no helper\n"
        "    session, and tmux session commander visible:\n"
        "      ensemble-session start helper --client codex --display /below/commander\n"
        "    Requests placement below commander; an existing grid can control it.\n"
        "      ensemble-session hide helper\n"
        "      ensemble-session show helper\n"
        "    Hide closes the view while work runs; show uses saved display, with no\n"
        "    new --display option. If show cannot open a view:\n"
        "      ensemble-session attach-command helper\n"
        "    Run the printed command to attach. close terminates the session.\n"
        f"    Stored metadata format: {FORMAT}\n"
        f"    {HIDDEN} is set only by {NO_DISPLAY}, never supplied as a display part.\n"
        "    Other printable values not ending\n"
        "    in ; remain opaque and are kept as given; showing needs this grammar."
    )


def layout(viewer: Viewer, host: Host, display: Display) -> str | None:
    """None on a Host that cannot show; else display, its empty opener part the caller's once the
    pane it goes beside is found. NO_DISPLAY needs no pane, started hidden.
    """
    validate_display(display, host)
    if not host.attachable:
        return None
    placement = viewer._placement
    opener, split, split_from, hidden = _given(display, parts(display))
    if hidden:
        with contextlib.suppress(TuiError):
            opener = placement.opener()
        return fold(opener, None, None, hidden=True)
    if split_from is not None and viewer._tmux.status(split_from) is None:
        raise ConfigError(f"no tmux session {split_from}")
    try:
        if opener is None or split_from is not None:
            placement.anchor(split_from)
        if opener is None:
            try:
                opener = placement.opener()
            except TuiError:
                if split_from is None:
                    raise
    except TuiError as e:
        raise SessionError(
            f"no pane to show the TUI beside ({e})" + ("" if split_from else NO_PANE_HINT)
        ) from None
    return fold(opener, split, split_from)


def tile(viewer: Viewer, window: str, *, wait: bool = True, if_panes_changed: bool = False) -> None:
    try:
        viewer._grid.tile(window, wait=wait, if_panes_changed=if_panes_changed)
    except TuiError as e:
        raise SessionError(f"tile {window}: {e}") from None


def _matching(host: Host, display: Display) -> list[SessionRow]:
    if display == "":
        return []
    return [
        row
        for row in host.rows()
        if display is None
        or (listed(display, row.display) if host.attachable else row.display == display)
    ]


def _placed(viewer: Viewer) -> dict[str, dict]:
    try:
        found = viewer._tmux.listing()
    except TuiError as error:
        raise SessionError(f"tmux sessions unknown: {error}") from error
    return {row["name"]: row for row in found}


def rows(viewer: Viewer, host: Host, display: Display) -> list[SessionRow]:
    """host's rows display lists, with shown and own."""
    found = _matching(host, display)
    if not (found and host.attachable):
        return found
    placed = _placed(viewer)
    try:
        own = viewer._placement.own_session() if placed else None
    except TuiError as error:
        raise SessionError(f"tmux sessions unknown: {error}") from error
    return [
        replace(
            row,
            shown=placed[row.name]["shown"] if row.name in placed else None,
            own=row.name == own,
        )
        for row in found
    ]


def names(viewer: Viewer, host: Host, display: Display, *, descendants: bool = False) -> list[str]:
    """display's sessions, then (descendants) each session one of those opened, transitively."""
    listed = host.list()
    if display is None and not descendants:
        return sorted(listed)
    found = {row.name for row in _matching(host, display)}
    if not (descendants and found and host.attachable):
        return sorted(found)
    every = host.rows()
    placed = _placed(viewer)
    openers = {row.name: _opener(row, placed.get(row.name)) for row in every}
    while grown := {n for n, opener in openers.items() if n not in found and opener in found}:
        found |= grown
    return sorted(found)


def _opener(row: SessionRow, live: dict | None) -> str | None:
    """Its placed session's live @opener, else its display's opener part."""
    if live is not None and live["opener"] is not None:
        return live["opener"]
    found = unfold(row.display)
    return None if found is None else found[0]


def started(viewer: Viewer, name: str, host: Host, display: Display) -> None:
    """A session started with NO_DISPLAY is hidden, unless its sid's entry exists (a restart or
    resume keeps its view); one without a sid in its worker state keeps none.
    """
    found = unfold(display) if host.attachable else None
    if found is None or not found[3]:
        return
    state = worker_state(name)
    sid = None if state is None else state.sid
    if sid is None:
        return
    try:
        hidden_store.start(name, sid, live=viewer._live(host))
    except OSError as error:
        raise SessionError(
            f"tui session {name}: hidden state: {error.strerror or error}"
        ) from error


def status(
    viewer: Viewer, host: Host, name: str, sid: str | None, word: StatusState | None
) -> Status:
    """Live session `name`'s Status: its activity `word` (None: working), hidden by its sid's
    entry, attach from the Viewer; unknown when its Host fails.
    """
    try:
        hidden = sid is not None and hidden_store.read(name, sid)
        attach = viewer.attach_command(name, host) if host.attachable else None
    except SessionError:
        return Status(
            name=name,
            alive=False,
            state=StatusState.UNKNOWN,
            outcome=None,
            attach=None,
            hidden=False,
        )
    return Status(
        name=name,
        alive=True,
        state=word or StatusState.WORKING,
        outcome=None,
        attach=attach,
        hidden=hidden,
    )
