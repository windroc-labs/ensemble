"""The normalized event-handler call, with its session control view."""

from collections.abc import Mapping
from dataclasses import dataclass

from ensemble.session.engine.base import Session
from ensemble.session.engine.types import SessionEvent


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
