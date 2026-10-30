"""The session harness's shared types."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple

from ensemble.session.engine.base import Session
from ensemble.session.engine.types import ClientBound, SessionEvent, Stderr


@dataclass(frozen=True)
class EventCall:
    """A handler's on_event input (FR-6); on_event never owns the worker lifecycle."""

    session: Session
    event: SessionEvent
    sid: str
    generation: str
    event_id: str
    turn: str | None
    state: Mapping[str, object]


class Logged(NamedTuple):
    """One events.jsonl record: seq counts from 1 per harness, id is <generation>:<seq>."""

    seq: int
    id: str
    turn: str | None
    event: SessionEvent | Stderr | ClientBound


class Op(StrEnum):
    """What a view asks a harness to do."""

    CLOSE = "close"
    STOP = "stop"
    SEND = "send"  # these three: a remote worker's, with data; never end it
    PRESS = "press"
    SCREEN = "screen"


SERVED = (Op.SEND, Op.PRESS, Op.SCREEN)
