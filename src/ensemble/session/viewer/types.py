"""Tmux host shared data types."""

from __future__ import annotations

from enum import StrEnum
from typing import NamedTuple


class Split(StrEnum):
    RIGHT = "right"
    BELOW = "below"


class Retile(StrEnum):
    ALL = "all"
    OPEN_CLOSE = "open-close"
    OFF = "off"


class WorkerPane(NamedTuple):
    """A pane showing worker `name`; attach (@worker-attach) tells its run from an older run."""

    pane: str
    name: str
    opener: str
    window: str
    session: str
    attach: str = ""


class CloseEntry(NamedTuple):
    """after-kill-pane[index], a close hook ensemble set for worker pane `pane`."""

    index: int
    pane: str


class Spot(NamedTuple):
    """grid: (window, manager)."""

    pane: str
    side: Split
    grid: tuple[str, str] | None


class Cell(NamedTuple):
    """A layout cell: w x h at x, y; a LEAF shows pane, a container holds children."""

    w: int
    h: int
    x: int
    y: int
    pane: str | None = None
    kind: str = ""
    children: tuple[Cell, ...] = ()


class Slot(NamedTuple):
    """A grid cell: a pane and the slots stacked right of it, or (pane None) an orphan group."""

    pane: str | None
    children: tuple[Slot, ...] = ()
