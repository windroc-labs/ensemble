"""harness/view.py: OwnedSession, the control view of a worker its session harness owns."""

import fcntl
import os
import threading
import time
from collections.abc import Generator
from typing import cast
from unittest import mock

from ensemble.session.engine.types import (
    Blocked,
    ClientBound,
    Exited,
    IdleTimeout,
    KeyPress,
    Mode,
    NotShowable,
    Reply,
    Resumable,
    SessionError,
    SessionInfo,
    StatusState,
    Stderr,
    TurnEnd,
    TurnStarted,
    Unanswered,
)
from ensemble.session.harness.channel import Channel, encode
from ensemble.session.harness.types import Op
from ensemble.session.harness.view import OwnedInProcess, OwnedSession, fold
from tests.session.case import Case

GEN = "g1"


class Base(Case):
    def setUp(self):
        self.channel = Channel.create()
        self.seq = 0
        self.attached = mock.Mock(name="attached")
        self.attaches = 0

    def attach(self):
        self.attaches += 1
        return self.attached

    def hold(self) -> int:
        fd = os.open(self.channel.lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        return fd

    def log(self, *events, generation=GEN, seq=None, turn=None):
        for event in events:
            self.seq = self.seq + 1 if seq is None else seq
            self.channel.append(
                {"seq": self.seq, "id": f"{generation}:{self.seq}", "turn": turn, **encode(event)}
            )

    def view(self, *, idle=None, replay=False) -> OwnedSession:
        return OwnedSession(
            self.channel,
            "w1",
            client="claude",
            sid="s1",
            generation=GEN,
            idle_timeout=idle,
            attach=self.attach,
            replay=replay,
        )


class FoldTest(Case):
    def test_reply_and_blocked(self):
        self.assertEqual(fold(None, Reply("a"), blocked=False, replayed=False), ("a", False))
        self.assertEqual(fold("a", TurnEnd("", 0), blocked=False, replayed=False), ("a", False))
        self.assertEqual(fold("a", TurnEnd("", 0), blocked=False, replayed=True), ("", False))
        self.assertEqual(fold("a", TurnEnd("b", 0), blocked=False, replayed=False), ("b", False))
        self.assertEqual(fold("a", Blocked("Bash"), blocked=False, replayed=False), ("a", True))
        self.assertEqual(fold("a", Stderr("x"), blocked=True, replayed=False), ("a", True))
        self.assertEqual(fold("a", Reply("c"), blocked=True, replayed=False), ("c", True))
        self.assertEqual(fold("a", TurnStarted(), blocked=True, replayed=False), ("a", False))
        self.assertEqual(fold("a", ClientBound("c"), blocked=True, replayed=False), ("a", True))


class EventsTest(Base):
    def test_attributes(self):
        view = self.view()
        self.assertEqual(
            (view.sid, view.client, view.name, view.mode), ("s1", "claude", "w1", Mode.TUI)
        )

    def test_yields_logged_events_up_to_exited_without_client_bound(self):
        self.hold()
        self.log(
            TurnStarted(), ClientBound("c1"), Stderr("e"), TurnEnd("full\nreply", 0), Exited(0)
        )
        self.assertEqual(
            list(self.view().events()),
            [TurnStarted(), Stderr("e"), TurnEnd("full\nreply", 0), Exited(0)],
        )

    def test_events_logged_later_are_yielded(self):
        self.hold()
        view = self.view()
        events = view.events()
        self.log(TurnStarted())
        self.assertEqual(next(events), TurnStarted())
        threading.Timer(0.1, self.log, (Exited(1),)).start()
        self.assertEqual(next(events), Exited(1))
        self.assertEqual(list(events), [])

    def test_another_generation_or_no_seq_increase_is_skipped(self):
        self.hold()
        self.log(Reply("a"))
        self.log(Exited(9), generation="g0", seq=2)
        self.log(Exited(8), seq=1)
        self.log(Reply("b"), seq=2)
        self.log(Exited(0), seq=3)
        self.assertEqual(list(self.view().events()), [Reply("a"), Reply("b"), Exited(0)])

    def test_an_owner_ending_without_exited_raises(self):
        self.log(TurnStarted())
        events = self.view().events()
        self.assertEqual(next(events), TurnStarted())
        with self.assertRaisesRegex(SessionError, "ended without the worker's exit"):
            next(events)

    def test_idle_timeout_while_not_blocked_only(self):
        self.hold()
        events = self.view(idle=0.05).events()
        self.assertEqual(next(events), IdleTimeout(0.05))
        self.log(Blocked("Bash"))
        self.assertEqual(next(events), Blocked("Bash"))
        result = []
        thread = threading.Thread(target=lambda: result.append(next(events)), daemon=True)
        thread.start()
        thread.join(0.3)
        self.assertTrue(thread.is_alive())
        self.log(Reply("x"), Exited(0))
        thread.join(5)
        self.assertEqual(result, [Reply("x")])

    def test_closing_the_iterator_asks_nothing(self):
        self.hold()
        self.log(TurnStarted())
        events = cast(Generator, self.view().events())
        next(events)
        events.close()
        self.assertFalse(os.path.exists(os.path.join(self.channel.directory, "requests")))

    def test_replay_starts_past_the_log_and_folds_through_the_filter(self):
        self.hold()
        self.log(Reply("a"), TurnEnd("", 0))
        self.log(Reply("stale"), generation="g0", seq=3)
        view = self.view(replay=True)
        self.assertEqual(view.reply, "")
        self.log(TurnEnd("b", 0), Exited(0))
        self.assertEqual(list(view.events()), [TurnEnd("b", 0), Exited(0)])
        self.assertEqual(view.reply, "b")

    def test_reply_follows_the_log_without_iterating(self):
        self.hold()
        view = self.view()
        self.assertIsNone(view.reply)
        self.log(Reply("a"), TurnEnd("", 0))
        self.assertEqual(view.reply, "a")
        self.log(TurnEnd("done", 0))
        self.assertEqual(view.reply, "done")


class CloseTest(Base):
    def serve(self, *, ok=True, error=None, unanswered=False, let_go=None):
        """let_go: the lock fd it then unlocks, as a harness does once it answered an end."""
        self.served = []

        def answer():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                for ident, op in self.channel.requests():
                    self.served.append(op)
                    self.channel.reply(ident, ok=ok, error=error, unanswered=unanswered)
                    if let_go is not None:
                        fcntl.flock(let_go, fcntl.LOCK_UN)
                    return
                time.sleep(0.01)

        thread = threading.Thread(target=answer)
        thread.start()
        self.addCleanup(thread.join)

    def test_close_and_stop_ask_the_owner(self):
        for op, call in ((Op.CLOSE, OwnedSession.close), (Op.STOP, OwnedSession.stop)):
            with self.subTest(op=op):
                self.serve(let_go=self.hold())
                view = self.view()
                call(view)
                self.assertEqual(self.served, [op])
        self.assertEqual(self.attaches, 0)

    def test_a_refusal_raises_its_error(self):
        self.hold()
        self.serve(ok=False, error="w1: cannot close")
        with self.assertRaisesRegex(SessionError, "cannot close") as caught:
            self.view().close()
        self.assertNotIsInstance(caught.exception, Unanswered)

    def test_unconfirmed_is_unanswered(self):
        self.hold()
        self.serve(ok=False, error="w1: worker exit not confirmed", unanswered=True)
        with self.assertRaises(Unanswered):
            self.view().close()

    def test_no_owner_closes_directly_and_confirms(self):
        self.attached.alive.return_value = False
        for op in ("close", "stop"):
            with self.subTest(op=op):
                getattr(self.view(), op)()
                getattr(self.attached, op).assert_called_once_with()
        self.assertEqual(self.attaches, 2)

    def test_no_owner_and_no_session_is_done(self):
        def gone():
            raise SessionError("no tui session w1")

        view = OwnedSession(
            self.channel,
            "w1",
            client="claude",
            sid="s1",
            generation=GEN,
            idle_timeout=None,
            attach=gone,
        )
        view.close()
        view.close()

    def test_a_direct_close_left_alive_is_unanswered(self):
        self.attached.alive.return_value = True
        with self.assertRaises(Unanswered):
            self.view().close()


class DelegationTest(Base):
    def test_other_members_use_one_lazy_attach(self):
        view = self.view()
        self.assertEqual(self.attaches, 0)
        self.attached.screen.return_value = "screen"
        self.attached.showable = True
        view.send("hi")
        view.press(())
        self.assertEqual(view.screen(3), "screen")
        self.assertTrue(view.showable)
        self.assertIsNone(view.opening_pending)
        self.channel.write_status({"state": "running", "opening": "/color"})
        self.assertEqual(view.opening_pending, "/color")
        for name in ("alive", "status", "content", "info", "session_status", "record", "hide"):
            getattr(view, name)()
            getattr(self.attached, name).assert_called_once_with()
        view.show(display="d")
        self.attached.show.assert_called_once_with(display="d", command=None)
        view.attach_command()
        self.attached.send.assert_called_once_with("hi")
        self.attached.screen.assert_called_once_with(3)
        self.assertEqual(self.attaches, 1)

    def test_alive_is_false_once_the_session_is_gone(self):
        def gone():
            raise SessionError("no tui session w1")

        view = OwnedSession(
            self.channel,
            "w1",
            client="claude",
            sid="s1",
            generation=GEN,
            idle_timeout=None,
            attach=gone,
        )
        self.assertFalse(view.alive())


class OwnedInProcessTest(Base):
    """An owned in_process worker's view: events and requests through the channel, no terminal."""

    def view(self, *, idle=None, replay=False) -> OwnedInProcess:
        self.info = SessionInfo("s1", "/w", None, "", "")
        return OwnedInProcess(
            self.channel, client="fake", sid="s1", generation=GEN, idle_timeout=idle, info=self.info
        )

    def test_it_has_no_terminal_or_display(self):
        view = self.view()
        self.assertEqual((view.mode, view.name, view.showable), (Mode.IN_PROCESS, None, False))
        for call, run in (
            ("send", lambda: view.send("x")),
            ("press", lambda: view.press(())),
            ("screen", view.screen),
        ):
            with (
                self.subTest(call=call),
                self.assertRaisesRegex(SessionError, f"^{call}: tui mode only$"),
            ):
                run()
        for call, run in (
            ("show", view.show),
            ("hide", view.hide),
            ("attach_command", view.attach_command),
        ):
            with (
                self.subTest(call=call),
                self.assertRaisesRegex(NotShowable, f"^{call}: tui mode only$"),
            ):
                run()
        self.assertIsNone(view.record())
        self.assertEqual(view.info(), self.info)
        self.assertEqual(view.session_status().resumable, Resumable.NO)

    def test_it_is_alive_while_its_harness_holds_on_before_the_exit(self):
        view = self.view()
        self.assertFalse(view.alive())
        self.hold()
        self.log(Reply("hi"))
        self.assertTrue(view.alive())
        status = view.status()
        self.assertEqual((status.name, status.alive, status.state), ("s1", True, "working"))
        self.assertEqual(view.content(), "hi")
        self.log(Exited(0))
        self.assertFalse(view.alive())
        self.assertEqual((view.status().alive, view.status().state), (False, "dead"))
        self.assertEqual(list(view.events()), [Reply("hi"), Exited(0)])

    def test_stop_and_close_without_an_owner_find_the_worker_gone(self):
        view = self.view()
        view.stop()
        view.close()
        self.assertEqual(self.attaches, 0)


class RemoteTest(Base):
    def serve(self, answers):
        self.served = []

        def run():
            deadline = time.monotonic() + 5
            left = len(answers)
            while time.monotonic() < deadline and left:
                for ident, op, data in self.channel.pending():
                    self.served.append((op, data))
                    ok, found = answers[op]
                    if ok:
                        self.channel.reply(ident, ok=True, data=found)
                    else:
                        self.channel.reply(ident, ok=False, error=found)
                    left -= 1
                time.sleep(0.01)

        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(thread.join)

    def test_a_remote_workers_input_and_screen_go_through_its_harness(self):
        self.hold()
        self.channel.write_status({"state": "running", "remote": True})
        self.serve({Op.SEND: (True, {}), Op.PRESS: (True, {}), Op.SCREEN: (True, {"text": "now"})})
        view = self.view()
        view.send("hi")
        view.press([KeyPress("Enter"), KeyPress("c", ctrl=True)])
        self.assertEqual(view.screen(3), "now")
        self.assertEqual(
            self.served,
            [
                (Op.SEND, {"text": "hi"}),
                (Op.PRESS, {"keys": ["Enter", str(KeyPress("c", ctrl=True))]}),
                (Op.SCREEN, {"lines": 3}),
            ],
        )
        self.attached.send.assert_not_called()
        self.attached.screen.assert_not_called()

    def test_remote_content_is_the_done_reply_else_the_remote_screen(self):
        self.hold()
        self.channel.write_status({"state": "running", "remote": True})
        self.attached.status.return_value = mock.Mock(state=StatusState.DONE)
        self.log(TurnEnd("final", 0))
        self.assertEqual(self.view().content(), "final")
        self.attached.status.return_value = mock.Mock(state=StatusState.WORKING)
        self.serve({Op.SCREEN: (True, {"text": "busy screen"})})
        self.assertEqual(self.view().content(), "busy screen")
        self.attached.content.assert_not_called()

    def test_a_refusal_raises_and_a_gone_harness_is_named(self):
        self.channel.write_status({"state": "running", "remote": True})
        with self.assertRaisesRegex(SessionError, "session w1: its harness is gone"):
            self.view().send("hi")
        self.hold()
        self.serve({Op.SEND: (False, "tui session w1 is not running")})
        with self.assertRaisesRegex(SessionError, "not running"):
            self.view().send("hi")
