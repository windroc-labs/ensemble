"""Whether a tui client took a sent text."""

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
from ensemble.utils import StatusState

POLL = 0.1
# lets a stale working mark (interrupt, closed dialog) still see a turn start
GRACE = 2.0

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
    session: Session, text: str, *, open_turn: bool, wait: float
) -> tuple[Delivery, str | None]:
    """Sends `text` unless its client is down or a dialog waits: the delivery and why not. A turn
    still open GRACE s after the send holds it: queued. Send failures raise.
    """
    name = session.name or session.sid
    status = session.status()
    if not status.alive:
        return Delivery.UNDELIVERED, f"session {name}: client not running"
    if status.state == StatusState.BLOCKED:
        return Delivery.UNDELIVERED, f"session {name}: a dialog waits; answer it with --key"
    session.send(text)
    sent = time.monotonic()
    deadline, queued = sent + wait, sent + min(GRACE, wait)
    found: queue.Queue[Event | SessionError] = queue.Queue()

    def drain() -> None:
        try:
            for event in session.events():
                found.put(event)
        except SessionError as error:
            found.put(error)

    # a blocked view yields not even IdleTimeout
    threading.Thread(target=drain, daemon=True).start()
    while True:
        try:
            event = found.get(timeout=max(0.0, min(POLL, deadline - time.monotonic())))
        except queue.Empty:
            event = IdleTimeout(POLL)
        if isinstance(event, SessionError):
            return Delivery.UNDELIVERED, str(event)
        if isinstance(event, IdleTimeout):
            if open_turn and time.monotonic() >= queued:
                return Delivery.QUEUED, None
            if time.monotonic() >= deadline:
                return Delivery.UNDELIVERED, f"session {name}: no turn started within {wait:g} s"
            continue
        decided, open_turn = fold(event, open_turn=open_turn)
        if decided == Delivery.UNDELIVERED:
            return decided, f"session {name}: client exited"
        if decided is not None:
            return decided, None
