"""session/engine/delivery.py: fold's table; deliver's pre-checks, its wait for a turn start after
the send and its bound.
"""

import threading
import time
import unittest

from ensemble.session.engine import delivery
from ensemble.session.engine.types import (
    Activity,
    Blocked,
    Delivery,
    Exited,
    IdleTimeout,
    Reply,
    SessionError,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from ensemble.utils import Status, StatusState
from tests.session.case import Case

NEVER = object()  # ends a fake's event list: its stream then waits without yielding


class FakeSession:
    """status, send and events of one attached tui session; events after the send only."""

    def __init__(self, *, alive=True, state=StatusState.DONE, events=(), raises=None):
        self.name, self.sid = "w1", "s1"
        self._status = Status(
            name="w1", alive=alive, state=state, outcome=None, attach=None, hidden=False
        )
        self._events, self._raises = list(events), raises
        self.sent: list[str] = []
        self.failure: SessionError | None = None
        self._stop = threading.Event()

    def status(self):
        return self._status

    def send(self, text):
        if self.failure is not None:
            raise self.failure
        self.sent.append(text)

    def events(self):
        for event in self._events:
            if event is NEVER:
                break
            yield event
        if self._raises is not None:
            raise self._raises
        self._stop.wait(60)

    def close_stream(self):
        self._stop.set()


class Fold(Case):
    def test_each_event_kind(self):
        for open_turn in (False, True):
            with self.subTest(open_turn=open_turn):
                self.assertEqual(
                    delivery.fold(TurnStarted(), open_turn=open_turn),
                    (Delivery.DELIVERED, open_turn),
                )
                self.assertEqual(
                    delivery.fold(Exited(0), open_turn=open_turn), (Delivery.UNDELIVERED, open_turn)
                )
                self.assertEqual(delivery.fold(TurnEnd(), open_turn=open_turn), (None, False))
                self.assertEqual(delivery.fold(TurnFailed("x"), open_turn=open_turn), (None, False))
                for other in (Activity(), Reply("r"), Blocked("b"), IdleTimeout(1)):
                    self.assertEqual(delivery.fold(other, open_turn=open_turn), (None, open_turn))


class Deliver(Case):
    """deliver reads no client: claude, codex and opencode sessions give these same events."""

    def deliver(self, session, *, open_turn=False, wait=5.0):
        """deliver to `session`; (result, seconds taken)."""
        began = time.monotonic()
        try:
            found = delivery.deliver(session, "marker", open_turn=open_turn, wait=wait)
        finally:
            session.close_stream()
        return found, time.monotonic() - began

    def test_turn_start_after_send_is_delivered(self):
        for state in (StatusState.DONE, StatusState.WORKING):
            with self.subTest(state=state):
                session = FakeSession(state=state, events=[Activity(), TurnStarted()])
                found, _ = self.deliver(session)
                self.assertEqual(found, (Delivery.DELIVERED, None))
                self.assertEqual(session.sent, ["marker"])

    def test_open_turn_still_open_is_queued_without_waiting_for_it(self):
        session = FakeSession(state=StatusState.WORKING, events=[Activity(), NEVER])
        found, took = self.deliver(session, open_turn=True, wait=20)
        self.assertEqual(found, (Delivery.QUEUED, None))
        self.assertGreaterEqual(took, delivery.GRACE)
        self.assertLess(took, delivery.GRACE + 2)
        self.assertEqual(session.sent, ["marker"])

    def test_turn_start_within_grace_of_a_stale_open_mark_is_delivered(self):
        session = FakeSession(state=StatusState.WORKING, events=[IdleTimeout(1), TurnStarted()])
        found, took = self.deliver(session, open_turn=True, wait=20)
        self.assertEqual(found, (Delivery.DELIVERED, None))
        self.assertLess(took, delivery.GRACE)

    def test_turn_ended_during_send_then_started_is_delivered(self):
        """Codex interrupts an open turn before it sends: that turn ends, then the text's starts."""
        for ended in (TurnEnd(interrupted=True), TurnFailed("interrupted")):
            with self.subTest(ended=ended):
                session = FakeSession(state=StatusState.WORKING, events=[ended, TurnStarted()])
                found, _ = self.deliver(session, open_turn=True)
                self.assertEqual(found, (Delivery.DELIVERED, None))

    def test_turn_ended_and_none_started_is_undelivered(self):
        session = FakeSession(state=StatusState.WORKING, events=[TurnEnd(), NEVER])
        found, _ = self.deliver(session, open_turn=True, wait=0.5)
        self.assertEqual(found[0], Delivery.UNDELIVERED)

    def test_no_turn_start_within_wait_is_undelivered(self):
        session = FakeSession(events=[Activity(), IdleTimeout(delivery.POLL), NEVER])
        found, took = self.deliver(session, wait=0.3)
        self.assertEqual(found, (Delivery.UNDELIVERED, "session w1: no turn started within 0.3 s"))
        self.assertGreaterEqual(took, 0.3)

    def test_wait_is_bounded_with_no_events(self):
        session = FakeSession(events=[NEVER])
        found, took = self.deliver(session, wait=0.5)
        self.assertEqual(found[0], Delivery.UNDELIVERED)
        self.assertLess(took, 0.5 + 2)

    def test_dead_client_is_undelivered_without_send(self):
        for state in (StatusState.GONE, StatusState.DEAD):
            with self.subTest(state=state):
                session = FakeSession(alive=False, state=state)
                found, _ = self.deliver(session)
                self.assertEqual(found, (Delivery.UNDELIVERED, "session w1: client not running"))
                self.assertEqual(session.sent, [])

    def test_blocked_session_is_undelivered_without_send(self):
        session = FakeSession(state=StatusState.BLOCKED)
        found, _ = self.deliver(session, open_turn=True)
        self.assertEqual(
            found,
            (Delivery.UNDELIVERED, "session w1: a dialog waits; answer it with --key"),
        )
        self.assertEqual(session.sent, [])

    def test_exit_during_wait_is_undelivered(self):
        session = FakeSession(events=[Exited(1)])
        found, _ = self.deliver(session)
        self.assertEqual(found, (Delivery.UNDELIVERED, "session w1: client exited"))

    def test_stream_failure_during_wait_is_undelivered(self):
        session = FakeSession(raises=SessionError("session w1: its harness is gone"))
        found, _ = self.deliver(session)
        self.assertEqual(found, (Delivery.UNDELIVERED, "session w1: its harness is gone"))

    def test_send_failure_propagates(self):
        session = FakeSession()
        session.failure = SessionError("blocked by turn interruption")
        with self.assertRaisesRegex(SessionError, "turn interruption"):
            self.deliver(session)


if __name__ == "__main__":
    unittest.main()
