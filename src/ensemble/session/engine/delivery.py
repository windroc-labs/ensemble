"""Whether a tui client took a sent text, told by its session's events after the send."""

from __future__ import annotations

import queue
import threading
import time

from ensemble.session.engine.base import Session
from ensemble.session.engine.types import (
    Delivery,
    Exited,
    IdleTimeout,
    SessionError,
    SessionEvent,
    Stderr,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from ensemble.session.hosts.base import activity
from ensemble.session.modes.tui import TuiMode
from ensemble.utils import StatusState

POLL = 0.1

Event = SessionEvent | Stderr | IdleTimeout


def fold(event: Event, *, open_turn: bool) -> tuple[Delivery | None, bool]:
    """One event after the send: the delivery it decides, else None; whether a turn stays open."""
    if isinstance(event, TurnStarted):
        return Delivery.DELIVERED, open_turn
    if isinstance(event, Exited):
        return Delivery.UNDELIVERED, open_turn
    if isinstance(event, TurnEnd | TurnFailed):
        return None, False
    return None, open_turn


def deliver(
    name: str, *, sid: str | None, host: str | None, text: str, wait: float
) -> tuple[Delivery, str | None]:
    """Sends `text` unless its client is down or a dialog waits; then the delivery and, when
    undelivered, why. A turn already open when the send returns holds the text: queued at once.
    Attach and send failures raise.
    """
    session = Session.attach(name, sid=sid, host=host, idle_timeout=POLL)
    status = session.status()
    if not status.alive:
        return Delivery.UNDELIVERED, f"session {name}: client not running"
    if status.state == StatusState.BLOCKED:
        return Delivery.UNDELIVERED, f"session {name}: a dialog waits; answer it with --key"
    # status says working for a client with no turn yet; only its activity mark means one is open
    generation = TuiMode.snapshot(name).get("generation")
    open_turn = isinstance(generation, str) and activity(name, generation) == StatusState.WORKING
    session.send(text)
    deadline = time.monotonic() + wait
    found: queue.Queue[Event | SessionError] = queue.Queue()

    def drain() -> None:
        try:
            for event in session.events():
                found.put(event)
        except SessionError as error:
            found.put(error)

    # a view whose dialog shows yields nothing, not even IdleTimeout: the deadline is kept here
    threading.Thread(target=drain, daemon=True).start()
    while True:
        try:
            event = found.get(timeout=max(0.0, min(POLL, deadline - time.monotonic())))
        except queue.Empty:
            event = IdleTimeout(POLL)
        if isinstance(event, SessionError):
            return Delivery.UNDELIVERED, str(event)
        if isinstance(event, IdleTimeout):
            if open_turn:
                return Delivery.QUEUED, None
            if time.monotonic() >= deadline:
                return Delivery.UNDELIVERED, f"session {name}: no turn started within {wait:g} s"
            continue
        decided, open_turn = fold(event, open_turn=open_turn)
        if decided == Delivery.UNDELIVERED:
            return decided, f"session {name}: client exited"
        if decided is not None:
            return decided, None
