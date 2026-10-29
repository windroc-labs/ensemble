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


class TmuxAnchor(NamedTuple):
    pane: str
    socket: str
    session: str | None = None


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
