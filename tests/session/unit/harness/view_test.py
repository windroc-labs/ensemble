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
    Liveness,
    Mode,
    Reply,
    SessionError,
    StatusState,
    Stderr,
    TurnEnd,
    TurnStarted,
    Unanswered,
)
from ensemble.session.harness.channel import Channel, encode
from ensemble.session.harness.types import Op
from ensemble.session.harness.view import OwnedSession, dialog, fold
from ensemble.utils import Record
from tests.session.case import Case
from tests.session.fakes.terminal import ScreenHost
from tests.session.hosted_support import HostedTuiBase

GEN = "g1"


class Base(Case):
    def setUp(self):
        self.channel = Channel.create()
        self.seq = 0
        self.attached = mock.Mock(name="attached")
        self.attaches = 0
        self.retire = mock.Mock(name="retire")

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

    def view(self, *, idle=None, replay=False, mode=Mode.TUI) -> OwnedSession:
        return OwnedSession(
            self.channel,
            "w1" if mode == Mode.TUI else None,
            client="claude",
            sid="s1",
            generation=GEN,
            idle_timeout=idle,
            attach=self.attach,
            retire=self.retire if mode == Mode.TUI else None,
            replay=replay,
            mode=mode,
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


class DialogFoldTest(Case):
    def test_the_first_blocked_reason_until_anything_but_stderr_reply_or_client_bound(self):
        for current, event, want in (
            (None, Blocked("a"), "a"),
            ("a", Blocked("b"), "a"),
            ("a", Stderr("x"), "a"),
            ("a", Reply("r"), "a"),
            ("a", ClientBound("c1"), "a"),
            ("a", TurnStarted(), None),
            ("a", TurnEnd("t"), None),
            (None, Reply("r"), None),
        ):
            with self.subTest(current=current, event=event):
                self.assertEqual(dialog(current, event), want)


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

    def test_end_without_owner_retires(self):
        """No owner answers: close and stop both go to retire (the record rule), never through
        the read-only attach.
        """
        for op in ("close", "stop"):
            with self.subTest(op=op):
                self.retire.reset_mock()
                getattr(self.view(), op)()
                self.retire.assert_called_once_with()
        self.assertEqual(self.attaches, 0)
        self.retire.side_effect = SessionError("busy: lock w1 is held by worker generation")
        with self.assertRaisesRegex(SessionError, "busy"):
            self.view().close()

    def test_only_a_tui_view_retires(self):
        for mode in (Mode.HEADLESS, Mode.IN_PROCESS):
            with (
                self.subTest(mode=mode),
                self.assertRaisesRegex(ValueError, "retire: a tui view's only"),
            ):
                OwnedSession(
                    self.channel,
                    None,
                    client="claude",
                    sid="s1",
                    generation=GEN,
                    idle_timeout=None,
                    attach=self.attach,
                    retire=self.retire,
                    mode=mode,
                )

    def test_a_tui_view_without_retire_is_refused(self):
        with self.assertRaisesRegex(ValueError, "retire: required for a tui view"):
            OwnedSession(
                self.channel,
                "w1",
                client="claude",
                sid="s1",
                generation=GEN,
                idle_timeout=None,
                attach=self.attach,
            )


class DelegationTest(Base):
    def test_other_members_use_one_lazy_attach(self):
        view = self.view()
        self.assertEqual(self.attaches, 0)
        self.attached.showable = True
        self.assertTrue(view.showable)
        self.assertIsNone(view.opening_pending)
        self.channel.write_status({"state": "running", "opening": "/color"})
        self.assertEqual(view.opening_pending, "/color")
        self.assertFalse(view.client_up)
        self.channel.write_status({"state": "running", "up": True})
        self.assertTrue(view.client_up)
        self.assertIsNone(view.dialog)
        self.channel.write_status({"state": "running", "dialog": "Edit"})
        self.assertEqual(view.dialog, "Edit")
        self.channel.write_status({"state": "running", "dialog": 3})
        self.assertIsNone(view.dialog)
        for name in ("alive", "status", "info", "session_status", "hide", "attach_command"):
            getattr(view, name)()
            getattr(self.attached, name).assert_called_once_with()
        view.show(display="d")
        self.attached.show.assert_called_once_with(display="d", command=None)
        self.assertEqual(self.attaches, 1)

    def test_record_is_its_worker_dirs_for_its_generation_with_no_attach(self):
        record = Record(
            v=1,
            kind="worker",
            name="w1",
            sessions=("w1",),
            sid="s1",
            workdir=None,
            ctl=("ensemble-session",),
            resume=("ensemble-session", "resume", "w1"),
            display=None,
            detail=None,
        )
        folder = os.path.join(self.ensemble, "tui", "w1")
        os.makedirs(folder)
        path = os.path.join(folder, "record")
        view = self.view()
        self.assertIsNone(view.record())
        for generation, found in ((GEN, record), ("g2", None)):
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"{generation}\t{record.to_line()}\n")
            self.assertEqual(view.record(), found)
        self.assertIsNone(self.view(mode=Mode.HEADLESS).record())
        self.assertEqual(self.attaches, 0)

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
            retire=mock.Mock(),
        )
        self.assertFalse(view.alive())


class ReattachTest(Base):
    """A held attach that fails (its display reopened since, say) is made again once."""

    def setUp(self):
        super().setUp()
        self.stale, self.fresh = mock.Mock(name="stale"), mock.Mock(name="fresh")
        self.sessions = [self.stale, self.fresh]

    def attach(self):
        self.attaches += 1
        if not self.sessions:
            raise SessionError("its worker g1 is gone")
        return self.sessions.pop(0)

    def held(self) -> OwnedSession:
        view = self.view()
        view.info()
        return view

    def test_each_delegated_call_retries_on_a_new_attach(self):
        calls = {
            "status": (),
            "info": (),
            "session_status": (),
            "hide": (),
            "attach_command": (),
        }
        for name, args in calls.items():
            with self.subTest(name=name):
                self.attaches, self.stale, self.fresh = 0, mock.Mock(), mock.Mock()
                self.sessions = [self.stale, self.fresh]
                view = self.held()
                getattr(self.stale, name).side_effect = SessionError("not running")
                found = getattr(view, name)(*args)
                if name != "hide":
                    self.assertIs(found, getattr(self.fresh, name).return_value)
                getattr(self.fresh, name).assert_called_once_with(*args)
                self.assertEqual(self.attaches, 2)

    def test_show_and_showable_retry_on_a_new_attach(self):
        view = self.held()
        self.stale.show.side_effect = SessionError("not running")
        type(self.stale).showable = mock.PropertyMock(side_effect=SessionError("gone"))
        self.fresh.showable = True
        self.assertIs(view.show(display="d"), self.fresh.show.return_value)
        self.fresh.show.assert_called_once_with(display="d", command=None)
        self.assertTrue(view.showable)
        self.assertEqual(self.attaches, 2)

    def test_the_new_attach_is_kept(self):
        view = self.held()
        self.stale.hide.side_effect = SessionError("not running")
        view.hide()
        view.hide()
        self.assertEqual(self.fresh.hide.call_args_list, [mock.call(), mock.call()])
        self.assertEqual(self.attaches, 2)

    def test_a_failure_of_the_new_attach_raises(self):
        view = self.held()
        self.stale.hide.side_effect = SessionError("not running")
        self.fresh.hide.side_effect = SessionError("still")
        with self.assertRaisesRegex(SessionError, "still"):
            view.hide()
        self.assertEqual(self.attaches, 2)
        self.fresh.hide.assert_called_once_with()

    def test_a_new_attach_that_fails_is_not_retried(self):
        view = self.view()
        self.stale.hide.side_effect = SessionError("not running")
        with self.assertRaisesRegex(SessionError, "not running"):
            view.hide()
        self.assertEqual(self.attaches, 1)
        self.fresh.hide.assert_not_called()

    def test_unanswered_is_not_retried(self):
        view = self.held()
        self.stale.hide.side_effect = Unanswered("timed out")
        with self.assertRaises(Unanswered):
            view.hide()
        self.assertEqual(self.attaches, 1)

    def test_a_failed_reattach_raises_its_error(self):
        view = self.held()
        self.stale.hide.side_effect = SessionError("not running")
        self.sessions = []
        with self.assertRaisesRegex(SessionError, "its worker g1 is gone"):
            view.hide()

    def test_alive_asks_a_new_attach_once_when_the_held_one_says_not(self):
        view = self.held()
        self.stale.alive.return_value = False
        self.fresh.alive.return_value = True
        self.assertTrue(view.alive())
        self.assertEqual(self.attaches, 2)
        self.fresh.alive.return_value = False
        third = mock.Mock(name="third")
        third.alive.return_value = False
        self.sessions.append(third)
        self.assertFalse(view.alive())
        third.alive.assert_called_once_with()

    def test_alive_of_a_fresh_attach_is_its_answer(self):
        self.stale.alive.return_value = False
        self.assertFalse(self.view().alive())
        self.assertEqual(self.attaches, 1)


class ConcurrencyTest(Base):
    """Calls from two threads (a session harness's handlers share one view)."""

    def setUp(self):
        super().setUp()
        self.sessions = [mock.Mock(name="first"), mock.Mock(name="second")]

    def attach(self):
        self.attaches += 1
        if not self.sessions:
            raise SessionError("its worker g1 is gone")
        return self.sessions.pop(0)

    def test_concurrent_first_calls_make_one_attach(self):
        first, threads = self.sessions[0], []
        view = self.view()

        def attach():
            session = type(self).attach(self)
            if not threads:  # another call while this attach is being made
                threads.append(threading.Thread(target=view.status))
                threads[0].start()
                threads[0].join(0.5)
            return session

        view._attach = attach
        view.info()
        threads[0].join(5)
        self.assertEqual(self.attaches, 1)
        first.info.assert_called_once_with()
        first.status.assert_called_once_with()

    def test_concurrent_failures_on_a_stale_attach_make_one_new_attach(self):
        stale, fresh = self.sessions
        view = self.view()
        view.session_status()

        def failed():  # the other call fails on it too, and retries, before this one does
            other = threading.Thread(target=view.status)
            other.start()
            other.join(5)
            raise SessionError("not running")

        stale.info.side_effect = failed
        stale.status.side_effect = SessionError("not running")
        self.assertIs(view.info(), fresh.info.return_value)
        fresh.status.assert_called_once_with()
        self.assertEqual(self.attaches, 2)

    def test_alive_keeps_an_attach_another_call_made_meanwhile(self):
        stale, fresh = self.sessions
        third = mock.Mock(name="third")
        self.sessions.append(third)
        view = self.view()
        view.session_status()

        def dead():  # another call replaces the stale attach before this one drops it
            other = threading.Thread(target=view.status)
            other.start()
            other.join(5)
            return False

        stale.alive.side_effect = dead
        stale.status.side_effect = SessionError("not running")
        fresh.alive.return_value = True
        self.assertTrue(view.alive())
        self.assertEqual(self.attaches, 2)
        third.alive.assert_not_called()


class HeadlessViewTest(Base):
    def test_a_headless_view_has_its_mode_and_no_name(self):
        view = self.view(mode=Mode.HEADLESS)
        self.assertEqual((view.mode, view.name), (Mode.HEADLESS, None))

    def interrupted(self, mode) -> BaseException:
        """events() of a view whose harness is gone, interrupted after its first event."""
        self.attached.alive.return_value = False
        self.log(Reply("x"))
        events = self.view(mode=mode).events()
        self.assertEqual(next(events), Reply("x"))
        with self.assertRaises(KeyboardInterrupt) as caught:
            events.throw(KeyboardInterrupt())
        return caught.exception

    def test_an_interrupt_in_a_headless_views_events_stops_its_worker(self):
        self.interrupted(Mode.HEADLESS)
        self.attached.stop.assert_called_once_with()

    def test_a_failed_stop_is_noted_on_the_interrupt(self):
        self.attached.stop.side_effect = SessionError("refused")
        self.assertIn("stop: refused", self.interrupted(Mode.HEADLESS).__notes__)

    def test_an_interrupt_in_a_tui_views_events_leaves_its_worker(self):
        self.interrupted(Mode.TUI)
        self.assertEqual(self.attaches, 0)

    def test_closing_a_headless_views_iterator_leaves_its_worker(self):
        self.log(Reply("x"))
        events = self.view(mode=Mode.HEADLESS).events()
        next(events)
        events.close()
        self.assertEqual(self.attaches, 0)


class RequestTest(Base):
    """A tui view's send, press and screen are its harness's requests, whatever its worker."""

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

    def test_input_and_screen_go_through_its_harness(self):
        self.hold()
        self.channel.write_status({"state": "running"})
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
        self.assertEqual(self.attaches, 0)

    def test_content_is_the_done_reply_else_its_screen(self):
        self.hold()
        self.channel.write_status({"state": "running"})
        self.attached.status.return_value = mock.Mock(state=StatusState.DONE)
        self.log(TurnEnd("final", 0))
        self.assertEqual(self.view().content(), "final")
        self.attached.status.return_value = mock.Mock(state=StatusState.WORKING)
        self.serve({Op.SCREEN: (True, {"text": "busy screen"})})
        self.assertEqual(self.view().content(), "busy screen")
        self.attached.content.assert_not_called()

    def test_a_refusal_raises_and_a_gone_harness_is_named(self):
        self.channel.write_status({"state": "running"})
        with self.assertRaisesRegex(SessionError, "session w1: its harness is gone"):
            self.view().send("hi")
        self.hold()
        self.serve({Op.SEND: (False, "tui session w1 is not running")})
        with self.assertRaisesRegex(SessionError, "not running"):
            self.view().send("hi")

    def test_show_asks_its_running_harness_to_reopen_the_display_first(self):
        self.hold()
        self.serve({Op.DISPLAY: (True, {"reopened": True})})
        self.view().show(display="d")
        self.assertEqual(self.served, [(Op.DISPLAY, {})])
        self.attached.show.assert_called_once_with(display="d", command=None)

    def test_a_headless_views_input_and_screen_are_refused_by_its_attach(self):
        self.attached.send.side_effect = SessionError("send: tui mode only")
        view = self.view(mode=Mode.HEADLESS)
        with self.assertRaisesRegex(SessionError, "tui mode only"):
            view.send("hi")
        view.press(())
        view.screen(3)
        self.attached.press.assert_called_once_with(())
        self.attached.screen.assert_called_once_with(3)
        self.assertFalse(os.path.exists(os.path.join(self.channel.directory, "requests")))


class NoProcessHost(ScreenHost):
    """A ScreenHost whose worker a view could reach only through its harness: any attach or
    terminal of its fails the test.
    """

    def attach(self, name):
        raise AssertionError(f"Host.attach({name!r})")

    def terminal(self, name, launch, **_options):
        raise AssertionError(f"Host.terminal({name!r})")


class Started(HostedTuiBase):
    """A tui worker started on the Host named tmux, its harness on a thread; self.view its view."""

    def setUp(self):
        super().setUp()
        self.host = NoProcessHost("tmux")
        self.configure(tui=True)
        self.view = self.tui(name="w1")
        assert isinstance(self.view, OwnedSession)
        self.worker = self.host.processes["w1"]
        self.dir = os.path.join(self.ensemble, "tui", "w1")


class Requests(Started):
    def test_send_press_screen_are_requests_on_tmux(self):
        """Its harness answers them (the read-only attach has no terminal: it would refuse)."""
        self.host.calls.screen.return_value = "its screen"
        self.view.send("hi")
        self.view.press([KeyPress.parse("Enter")])
        self.assertEqual(self.view.screen(3), "its screen")
        self.worker.calls.send.assert_called()
        self.worker.calls.press.assert_called_once_with((KeyPress.parse("Enter"),))
        self.host.calls.screen.assert_any_call(self.worker, 3)
        self.assertTrue(os.path.isdir(os.path.join(self.view._channel.directory, "replies")))

    def test_reads_use_no_host_process(self):
        """alive, status, info, reply, record, show, hide and attach_command reach the worker's
        Host only through list, rows, attach_target, worker_liveness and attachable.
        """
        self.assertTrue(self.view.alive())
        self.assertTrue(self.view.status().alive)
        self.assertEqual(self.view.info().sid, self.view.sid)
        self.assertIsNone(self.view.reply)
        self.assertIsNone(self.view.record())
        self.view.show()
        self.view.hide()
        self.view.attach_command()
        self.view.session_status()
        self.host.calls.worker_liveness.return_value = Liveness.LIVE
        attached = self.session.attach("w1")
        self.assertTrue(attached.alive())
        self.view.close()
        self.host.calls.worker_liveness.return_value = Liveness.ABSENT
        self.assertFalse(self.view.alive())


class Status(Started):
    def test_state_from_activity(self):
        """Its worker dir's activity of its generation; none, or another generation's: working."""
        path = os.path.join(self.dir, "activity")
        for line, state in (
            (None, StatusState.WORKING),
            (f"{self.view.generation} done\n", StatusState.DONE),
            (f"{self.view.generation} blocked\n", StatusState.BLOCKED),
            ("another done\n", StatusState.WORKING),
        ):
            with self.subTest(line=line):
                if line is not None:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(line)
                found = self.view.status()
                self.assertEqual((found.alive, found.state), (True, state))
                self.assertEqual(found.cwd, self.workdir)
