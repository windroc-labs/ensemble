"""The session harness's shared types: the normalized event-handler call, with its session
control view; a logged event; a request to its owner.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple

from ensemble.session.engine.base import Session
from ensemble.session.engine.types import ClientBound, SessionEvent, Stderr


@dataclass(frozen=True)
class EventCall:
    """A handler's on_event input (FR-6): session, the control view to send, stop or close through;
    event, the normalized event; event_id and turn, stable ids within the worker generation;
    state, its HookCallback.state. on_event returns None and never owns the worker lifecycle.
    """

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
