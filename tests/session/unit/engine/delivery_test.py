"""session/engine/delivery.py: fold's table; deliver's pre-checks, its wait for a turn start after
the send and its bound, for each client.
"""

import threading
import time
import unittest
from unittest import mock

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

CLIENTS = ("claude", "codex", "opencode")
NEVER = object()  # ends a fake's event list: its stream then waits without yielding


class FakeSession:
    """status, send and events of one attached tui session; events after the send only."""

    def __init__(self, *, client, alive=True, state=StatusState.DONE, events=(), raises=None):
        self.client = client
        self._status = Status(
            name="w1", alive=alive, state=state, outcome=None, attach=None, hidden=False
        )
        self._events, self._raises = list(events), raises
        self.sent: list[str] = []
        self._stop = threading.Event()

    def status(self):
        return self._status

    def send(self, text):
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
    def deliver(self, session, *, word=None, wait=5.0):
        """deliver against `session`, its activity word `word`; (result, seconds taken)."""
        with (
            mock.patch.object(delivery.Session, "attach", return_value=session) as attach,
            mock.patch.object(delivery, "activity", return_value=word),
            mock.patch.object(delivery.TuiMode, "snapshot", return_value={"generation": "g1"}),
        ):
            began = time.monotonic()
            try:
                found = delivery.deliver("w1", sid="s1", host=None, text="marker", wait=wait)
            finally:
                session.close_stream()
        attach.assert_called_once_with("w1", sid="s1", host=None, idle_timeout=delivery.POLL)
        return found, time.monotonic() - began

    def test_turn_start_after_send_is_delivered(self):
        for client in CLIENTS:
            for word in (None, StatusState.DONE):
                with self.subTest(client=client, word=word):
                    session = FakeSession(
                        client=client,
                        state=StatusState.WORKING if word is None else word,
                        events=[Activity(), TurnStarted()],
                    )
                    found, _ = self.deliver(session, word=word)
                    self.assertEqual(found, (Delivery.DELIVERED, None))
                    self.assertEqual(session.sent, ["marker"])

    def test_open_turn_still_open_is_queued_without_waiting(self):
        for client in CLIENTS:
            with self.subTest(client=client):
                session = FakeSession(
                    client=client, state=StatusState.WORKING, events=[Activity(), NEVER]
                )
                found, took = self.deliver(session, word=StatusState.WORKING, wait=20)
                self.assertEqual(found, (Delivery.QUEUED, None))
                self.assertLess(took, 1)
                self.assertEqual(session.sent, ["marker"])

    def test_turn_ended_during_send_then_started_is_delivered(self):
        """Codex interrupts an open turn before it sends: that turn ends, then the text's starts."""
        for ended in (TurnEnd(interrupted=True), TurnFailed("interrupted")):
            with self.subTest(ended=ended):
                session = FakeSession(
                    client="codex", state=StatusState.WORKING, events=[ended, TurnStarted()]
                )
                found, _ = self.deliver(session, word=StatusState.WORKING)
                self.assertEqual(found, (Delivery.DELIVERED, None))

    def test_no_turn_start_within_wait_is_undelivered(self):
        for client in CLIENTS:
            with self.subTest(client=client):
                session = FakeSession(
                    client=client, events=[Activity(), IdleTimeout(delivery.POLL), NEVER]
                )
                found, took = self.deliver(session, word=StatusState.DONE, wait=0.3)
                self.assertEqual(
                    found, (Delivery.UNDELIVERED, "session w1: no turn started within 0.3 s")
                )
                self.assertGreaterEqual(took, 0.3)

    def test_wait_is_bounded_with_no_events(self):
        session = FakeSession(client="claude", events=[NEVER])
        found, took = self.deliver(session, word=None, wait=0.5)
        self.assertEqual(found[0], Delivery.UNDELIVERED)
        self.assertLess(took, 0.5 + 0.5)

    def test_dead_client_is_undelivered_without_send(self):
        for client in CLIENTS:
            for state in (StatusState.GONE, StatusState.DEAD):
                with self.subTest(client=client, state=state):
                    session = FakeSession(client=client, alive=False, state=state)
                    found, _ = self.deliver(session)
                    self.assertEqual(
                        found, (Delivery.UNDELIVERED, "session w1: client not running")
                    )
                    self.assertEqual(session.sent, [])

    def test_blocked_session_is_undelivered_without_send(self):
        session = FakeSession(client="claude", state=StatusState.BLOCKED)
        found, _ = self.deliver(session, word=StatusState.BLOCKED)
        self.assertEqual(
            found,
            (Delivery.UNDELIVERED, "session w1: a dialog waits; answer it with --key"),
        )
        self.assertEqual(session.sent, [])

    def test_exit_during_wait_is_undelivered(self):
        session = FakeSession(client="opencode", events=[Exited(1)])
        found, _ = self.deliver(session, word=StatusState.DONE)
        self.assertEqual(found, (Delivery.UNDELIVERED, "session w1: client exited"))

    def test_stream_failure_during_wait_is_undelivered(self):
        session = FakeSession(
            client="claude", raises=SessionError("session w1: its harness is gone")
        )
        found, _ = self.deliver(session, word=StatusState.DONE)
        self.assertEqual(found, (Delivery.UNDELIVERED, "session w1: its harness is gone"))

    def test_send_failure_propagates(self):
        session = FakeSession(client="claude")
        session.send = mock.Mock(side_effect=SessionError("blocked by turn interruption"))
        with self.assertRaisesRegex(SessionError, "turn interruption"):
            self.deliver(session, word=StatusState.DONE)


if __name__ == "__main__":
    unittest.main()
