"""hosts/docker/worker.py: a guarded Docker worker's hook checks, journal dedupe, maintenance thread
and end, with a fake channel, broker, hook process and removal.
"""

import argparse
import json
import os
import select
import threading
from types import SimpleNamespace
from unittest import mock

from ensemble.session.engine.types import (
    HookCall,
    HookError,
    Liveness,
    SessionError,
    StatusState,
    WorkerHook,
)
from ensemble.session.harness import guard
from ensemble.session.hosts.docker import guard_hook, worker
from ensemble.session.hosts.docker.maintenance import Maintenance
from ensemble.session.hosts.docker.mounts import state_dir
from tests.session.case import Case

GEN = "0123456789abcdef0123456789abcdef"
NOW = 1_792_238_400.0
WAIT = 5  # seconds for a background thread's step


def hook(label="Stop", payload: object = None, delivery="a" * 32) -> bytes:
    body = {"sid": "s"} if payload is None else payload
    return json.dumps({"label": label, "payload": body, "delivery_id": delivery}).encode()


class Hooks:
    """The resident hook process over `folder`: each call answers from `answers` (None: ok), else
    ok; each call's send count in `sents`.
    """

    def __init__(self, *answers):
        self.answers, self.calls, self.sents, self.stopped = list(answers), [], [], False
        self.folder = None

    def stop(self):
        self.stopped = True

    def call(self, label, body, event, sent=None):
        self.calls.append((label, body, event))
        self.sents.append(sent)
        return self.answers.pop(0) if self.answers else None


class Broker:
    def __init__(self, maintain=None):
        self.calls, self._maintain = [], maintain

    def maintain(self, container, manifest, delivered, *, root):
        self.calls.append("maintain")
        if self._maintain is not None:
            return self._maintain()
        return SimpleNamespace(next_check=None, deadline=None, results=[], notices=[])


class Base(Case):
    def setUp(self):
        super().setUp()
        self.state = state_dir("worker_1")
        os.makedirs(self.state, exist_ok=True)
        self.events = os.path.join(self.home, "events")
        open(self.events, "w").close()
        self.removed, self.remove_errors = [], []
        self.record = {"generation": GEN, "sid": "sid-1", "display": None}
        self.broker = Broker()
        self.reports = []

    def remove(self):
        self.removed.append(GEN)
        if self.remove_errors:
            raise self.remove_errors.pop(0)

    def make(self, *, hooks=None, clock=None):
        def factory(close, report):
            return Maintenance(
                "worker_1",
                self.state,
                GEN,
                self.broker,
                report=report,
                root="/repo",
                close=close,
                clock=clock or (lambda: NOW),
            )

        if hooks is not None:
            hooks.folder = self.state
        self.owner = os.open(os.path.join(self.state, "guard.lock"), os.O_RDWR | os.O_CREAT)
        proc = mock.Mock()
        proc.stdin.fileno.return_value = 99
        found = worker.DockerWorker(
            "worker_1",
            GEN,
            proc,
            state=self.state,
            events=self.events,
            hooks=hooks,
            remove=self.remove,
            owner=self.owner,
            maintenance=factory,
            report=lambda word, text: self.reports.append((word, text)),
            load=lambda: self.record,
            store=self.record.update,
            start_reader=False,
        )
        found.accept(b"s", b'{"pid":1}')
        return found

    def rejected(self):
        try:
            with open(os.path.join(self.state, "rejected.jsonl")) as f:
                return [json.loads(line)["reason"] for line in f]
        except FileNotFoundError:
            return []

    def owner_open(self):
        try:
            os.fstat(self.owner)
        except OSError:
            return False
        return True


class HookFrameTest(Base):
    def test_bad_frames_are_logged_and_dropped_and_later_hooks_still_arrive(self):
        nested = b'{"label":"Stop","payload":' + b"[" * 100000 + b"]" * 100000 + b"}"
        cases = (
            (hook("Stop/../../x"), "label"),
            (hook("Stop\n"), "label"),
            (hook("x" * 65), "label"),
            (b'{"label":"Stop","payload":{},"delivery_id":"' + b"a" * 32 + b'","x":1}', "shape"),
            (hook(payload=[]), "shape"),
            (hook(delivery="A" * 32), "shape"),
            (
                b'{"label":"Stop","label":"Other","payload":{},"delivery_id":"' + b"a" * 32 + b'"}',
                "shape",
            ),
            (hook(payload={"n": float("nan")}), "shape"),
            (nested, "shape"),
            (b"\xff", "shape"),
            (hook(payload={"x": "y" * guard.HOOK_MAX}), "size"),
        )
        channel = self.make()
        for data, _ in cases:
            channel.accept(b"h", data)
        channel.accept(b"h", hook("Stop", {"sid": "s", "transcript_path": "/Users/x/.ssh/id"}))
        self.assertEqual(channel.read(0), [WorkerHook("Stop", {"sid": "s"}, "a" * 32)])
        self.assertEqual(self.rejected(), [reason for _, reason in cases])
        with open(os.path.join(self.state, "rejected.jsonl")) as f:
            logged = f.read()
        self.assertNotIn("Stop/", logged)
        self.assertEqual(channel.state(), Liveness.LIVE)

    def test_bad_framing_or_a_nested_non_hook_frame_fails_the_channel_closed(self):
        nested = b'{"returncode":' + b"[" * 100000 + b"]" * 100000 + b"}"
        for wire in (
            b"o" + (guard.FRAME_MAX + 1).to_bytes(4, "big"),
            guard.frame(b"x", nested),
            guard.frame(b"z", b"{}"),
        ):
            with self.subTest(wire=wire[:8]):
                channel = self.make()
                channel.proc.stdout.read.side_effect = [wire, b"more", b""]
                channel.pump()
                self.assertIsNotNone(channel.failure)
                self.assertEqual(channel.proc.stdout.read.call_count, 3)
                self.assertTrue(channel.wait_exit(5))
                self.assertEqual(channel.returncode(), None)

    def test_the_rejected_log_stops_at_its_cap(self):
        path = os.path.join(self.state, "rejected.jsonl")
        with open(path, "w") as f:
            f.write("x" * worker.LOG_MAX)
        self.make().accept(b"h", hook("bad/label"))
        self.assertEqual(os.path.getsize(path), worker.LOG_MAX)


class HookTest(Base):
    def deliver(self, channel, *frames):
        for data in frames:
            channel.accept(b"h", data)
        thread = threading.Thread(target=channel._deliver)
        thread.start()
        with channel.condition:
            channel.condition.wait_for(lambda: not channel._hooks_pending(), 5)
        channel.stopped.set()
        with channel.condition:
            channel.condition.notify_all()
        thread.join(5)

    def test_a_duplicate_delivery_is_journaled_once(self):
        hooks = Hooks()
        channel = self.make(hooks=hooks)
        self.deliver(channel, hook(delivery="a" * 32), hook(delivery="a" * 32))
        channel.accept(b"h", hook(delivery="a" * 32))
        self.assertEqual(hooks.calls, [("Stop", {"sid": "s"}, "a" * 32)])
        self.assertEqual(channel.read(0), [])

    def test_a_hook_carries_the_send_count_seen_as_it_was_accepted(self):
        hooks = Hooks()
        channel = self.make(hooks=hooks)
        prompted = os.path.join(self.state, "prompted")
        with open(prompted, "w") as f:
            f.write(f"{GEN} 0 0")
        channel.accept(b"h", hook())
        with open(prompted, "w") as f:
            f.write(f"{GEN} 1 1")  # a send after the guard acked the Stop, before its delivery
        self.deliver(channel)
        self.assertEqual(hooks.sents, [f"{GEN} 0 0"])

    def test_a_send_once_the_guard_let_the_client_go_on_is_never_in_its_stamp(self):
        hooks = Hooks()
        channel = self.make(hooks=hooks)
        prompted = os.path.join(self.state, "prompted")
        with open(prompted, "w") as f:
            f.write(f"{GEN} 0 0")
        runtime = guard.Guard(
            argparse.Namespace(
                hook_socket=self.home + "/hook",
                display_socket=None,
                display_grace=False,
                terminal=False,
                command=["fake"],
            )
        )
        wire, decoder = bytearray(), guard.Decoder()

        def client_goes_on(_reply):  # its Stop hook acked: a send goes in at once
            with open(prompted, "w") as f:
                f.write(f"{GEN} 1 1")

        acked = threading.Event()
        peer = mock.Mock()
        peer.recv.return_value = hook() + b"\n"
        peer.sendall.side_effect = lambda reply: (client_goes_on(reply), acked.set())
        runtime.peers[peer] = ("hook", bytearray())
        runtime.peer_read(peer)

        def to_harness(_fd, data):
            wire.extend(data)
            return len(data)

        def to_guard(_fd, data):
            for kind, payload in decoder.feed(bytes(data)):
                runtime.command(kind, payload)
            return len(data)

        with mock.patch.object(os, "write", side_effect=to_harness):
            runtime.flush()
        reads = iter([bytes(wire)])

        def read(_size):  # the channel's EOF only once its hook was delivered
            for data in reads:
                return data
            self.assertTrue(acked.wait(WAIT))
            self.deliver(channel)
            return b""

        channel.proc.stdout.read.side_effect = read
        with (
            mock.patch.object(select, "select", return_value=([], [channel.proc.stdin], [])),
            mock.patch.object(os, "write", side_effect=to_guard),
        ):
            channel.pump()
        self.assertEqual(hooks.sents, [f"{GEN} 0 0"])

    def test_a_failed_host_hook_retries_the_same_id_then_is_rejected(self):
        class Failing(Hooks):
            def call(self, label, body, event, sent=None):
                super().call(label, body, event, sent)
                return "hook exit 1" if event == "a" * 32 else None

        hooks = Failing()
        channel = self.make(hooks=hooks)
        channel.hook_pause, channel.hook_window = 0.01, 0.2
        self.deliver(channel, hook(delivery="a" * 32), hook("Later", delivery="b" * 32))
        ids = [event for _, _, event in hooks.calls]
        self.assertGreater(ids.count("a" * 32), 2)
        self.assertEqual(ids[-1], "b" * 32)
        self.assertEqual(self.rejected(), ["hook"])

    def test_no_hook_is_called_once_the_worker_stopped(self):
        hooks = Hooks()
        channel = self.make(hooks=hooks)
        channel.stopped.set()
        channel.accept(b"h", hook(delivery="a" * 32))
        thread = threading.Thread(target=channel._deliver)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(hooks.calls, [])

    def test_hooks_queued_at_stop_are_each_rejected(self):
        hooks = Hooks()
        channel = self.make(hooks=hooks)
        channel.accept(b"h", hook(delivery="a" * 32))
        channel.accept(b"h", hook(delivery="b" * 32))
        channel.stopped.set()
        thread = threading.Thread(target=channel._deliver)
        thread.start()
        thread.join(5)
        self.assertEqual(hooks.calls, [])
        self.assertEqual(self.rejected(), ["hook", "hook"])
        self.assertFalse(channel._hooks_pending())
        self.assertEqual(channel.hook_bytes, 0)

    def test_a_delivered_hook_is_not_rejected_with_those_left_at_stop(self):
        calling, release = threading.Event(), threading.Event()

        class Slow(Hooks):
            def call(self, label, body, event, sent=None):
                calling.set()
                release.wait(5)
                return super().call(label, body, event, sent)

        hooks = Slow()
        channel = self.make(hooks=hooks)
        for delivery in "abc":
            channel.accept(b"h", hook(delivery=delivery * 32))
        thread = threading.Thread(target=channel._deliver)
        thread.start()
        self.assertTrue(calling.wait(5))
        channel.stopped.set()
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([event for _, _, event in hooks.calls], ["a" * 32])
        self.assertEqual(self.rejected(), ["hook", "hook"])
        self.assertFalse(channel._hooks_pending())

    def test_hooks_queued_before_begin_are_each_rejected_at_end(self):
        hooks = Hooks()
        channel = self.make(hooks=hooks)
        channel.accept(b"h", hook(delivery="a" * 32))
        channel.accept(b"h", hook(delivery="b" * 32))
        channel.fail("guard channel ended without confirmed worker exit")
        self.assertTrue(channel.wait_exit(5))
        self.assertEqual(hooks.calls, [])
        self.assertEqual(self.rejected(), ["hook", "hook"])
        self.assertFalse(channel._hooks_pending())
        self.assertEqual(channel.hook_bytes, 0)
        channel.accept(b"h", hook(delivery="a" * 32))
        self.assertEqual(self.rejected(), ["hook", "hook"])

    def test_a_hook_before_an_exit_ahead_of_begin_is_still_journaled(self):
        hooks = Hooks()
        channel = self.make(hooks=hooks)
        channel.accept(b"h", hook(delivery="a" * 32))
        channel.accept(b"x", b'{"returncode":0}')
        channel.begin()
        self.assertTrue(channel.wait_exit(5))
        self.assertEqual(hooks.calls, [("Stop", {"sid": "s"}, "a" * 32)])
        self.assertEqual(self.rejected(), [])

    def test_a_hook_accepted_after_stop_is_rejected_not_queued(self):
        channel = self.make(hooks=Hooks())
        channel.stopped.set()
        channel.accept(b"h", hook(delivery="a" * 32))
        self.assertEqual(self.rejected(), ["hook"])
        self.assertFalse(channel._hooks_pending())

    def test_a_hook_resent_after_stop_is_rejected_at_most_once(self):
        hooks = Hooks()
        channel = self.make(hooks=hooks)
        self.deliver(channel, hook(delivery="a" * 32))
        channel.stopped.clear()
        channel.accept(b"h", hook(delivery="b" * 32))
        channel.stopped.set()
        thread = threading.Thread(target=channel._deliver)
        thread.start()
        thread.join(5)
        self.assertEqual(self.rejected(), ["hook"])
        for delivery in "abcc":
            channel.accept(b"h", hook(delivery=delivery * 32))
        self.assertEqual([event for _, _, event in hooks.calls], ["a" * 32])
        self.assertEqual(self.rejected(), ["hook", "hook"])

    def test_hooks_stay_in_order_and_hold_the_exit_code_until_journaled(self):
        gate = threading.Event()

        class Slow(Hooks):
            def call(self, label, body, event, sent=None):
                gate.wait(5)
                return super().call(label, body, event, sent)

        hooks = Slow()
        channel = self.make(hooks=hooks)
        for n, delivery in enumerate("abc"):
            channel.accept(b"h", hook(f"H{n}", delivery=delivery * 32))
        channel.accept(b"x", b'{"returncode":4}')
        self.assertIsNone(channel.returncode())
        gate.set()
        self.deliver(channel)
        self.assertTrue(channel.wait_exit(5))
        self.assertEqual([label for label, _, _ in hooks.calls], ["H0", "H1", "H2"])
        self.assertEqual(channel.returncode(), 4)
        self.assertTrue(hooks.stopped)


class MaintenanceTest(Base):
    def test_tick_never_calls_the_broker(self):
        channel = self.make()
        self.assertEqual(channel.tick(NOW), NOW)
        self.assertEqual(channel.tick(NOW + 100), NOW)
        self.assertEqual(self.broker.calls, [])

    def test_a_blocked_broker_does_not_block_tick_or_the_channel(self):
        entered, release = threading.Event(), threading.Event()

        def blocked():
            entered.set()
            release.wait(5)
            return SimpleNamespace(next_check=None, deadline=None, results=[], notices=[])

        self.broker = Broker(blocked)
        channel = self.make()
        channel.begin()
        self.assertTrue(entered.wait(5))
        self.assertEqual(channel.tick(NOW), NOW)
        channel.accept(b"h", hook())
        self.assertEqual(len(channel.read(0)), 1)
        release.set()

    def test_expiry_reports_and_ends_the_worker(self):
        with open(os.path.join(self.state, f"{GEN}.deadline.json"), "w") as f:
            json.dump({"at": "2026-10-17T12:00:30Z", "action": "close", "reason": "expired"}, f)

        def offline():
            raise SessionError("down")

        self.broker = Broker(offline)
        channel = self.make()

        def written(_fd, data):
            if guard.Decoder().feed(bytes(data))[0][0] == b"c":
                channel.accept(b"x", b'{"returncode":0}')
            return len(data)

        with (
            mock.patch.object(select, "select", return_value=([], [99], [])),
            mock.patch.object(os, "write", side_effect=written),
        ):
            self.assertEqual(channel.maintenance.tick(NOW), NOW + 30)
            self.assertIsNone(channel.maintenance.tick(NOW + 30))
            self.assertTrue(channel.wait_exit(5))
        self.assertEqual(channel.state(), Liveness.ABSENT)
        self.assertEqual(
            self.reports,
            [("credential_maintenance_failed", "transport"), ("client_auth_expired", "expired")],
        )
        self.assertIsNone(channel.tick(NOW + 60))


class EndTest(Base):
    def test_the_exit_code_waits_for_the_containers_removal(self):
        gate = threading.Event()
        original = self.remove

        def slow():
            gate.wait(5)
            original()

        self.remove = slow
        channel = self.make()
        channel.accept(b"x", b'{"returncode":5}')
        self.assertIsNone(channel.returncode())
        self.assertEqual(channel.state(), Liveness.LIVE)
        gate.set()
        self.assertTrue(channel.wait_exit(5))
        self.assertEqual((channel.state(), channel.returncode()), (Liveness.ABSENT, 5))

    def test_a_report_after_removal_is_dropped(self):
        channel = self.make()
        channel.fail("broken")
        self.assertTrue(channel.wait_exit(5))
        channel._report("credential_maintenance_failed", "transport")
        self.assertEqual(self.reports, [])

    def test_exit_frame_then_removal_then_absent(self):
        channel = self.make()
        channel.accept(b"x", b'{"returncode":3}')
        self.assertTrue(channel.wait_exit(5))
        self.assertEqual(
            (channel.state(), channel.returncode(), channel.read(0)), (Liveness.ABSENT, 3, [])
        )
        self.assertEqual(self.removed, [GEN])
        self.assertFalse(self.owner_open())
        channel.close()
        self.assertEqual(self.removed, [GEN])

    def test_channel_failure_is_unknown_until_removal(self):
        gate = threading.Event()
        self.remove_errors = []
        original = self.remove

        def slow():
            gate.wait(5)
            original()

        self.remove = slow
        channel = self.make()
        channel.fail("guard channel ended without confirmed worker exit")
        assert channel.exit_thread is not None
        channel.exit_thread.join(5)
        self.assertEqual(channel.state(), Liveness.UNKNOWN)
        self.assertTrue(self.owner_open())
        gate.set()
        self.assertTrue(channel.wait_exit(5))
        self.assertEqual((channel.state(), channel.returncode()), (Liveness.ABSENT, None))

    def test_a_release_still_writing_at_its_end_keeps_a_clean_exit(self):
        """A k already counted before the exit frame: the end never shuts stdin under its write."""
        channel = self.make()
        writing, closed = threading.Event(), threading.Event()
        channel.proc.stdin.close.side_effect = closed.set

        def write(_fd, _wire):  # the gone guard's pipe, unless its stdin was shut first
            writing.set()
            if closed.wait(WAIT / 10):
                raise ValueError("I/O operation on closed file")
            raise BrokenPipeError

        with (
            mock.patch.object(select, "select", return_value=([], [99], [])),
            mock.patch.object(os, "write", side_effect=write),
        ):
            channel.release_hook()
            self.assertTrue(writing.wait(WAIT))
            channel.accept(b"x", b'{"returncode":0}')
            self.assertTrue(channel.wait_exit(WAIT))
            assert channel.releaser is not None
            channel.releaser.join(WAIT)
        self.assertIsNone(channel.failure)
        self.assertEqual((channel.state(), channel.returncode()), (Liveness.ABSENT, 0))

    def test_close_asks_the_guard_then_removes(self):
        channel = self.make()

        def written(_fd, data):
            if guard.Decoder().feed(bytes(data))[0][0] == b"c":
                channel.accept(b"x", b'{"returncode":0}')
            return len(data)

        with (
            mock.patch.object(select, "select", return_value=([], [99], [])),
            mock.patch.object(os, "write", side_effect=written),
        ):
            channel.close()
        self.assertEqual((channel.state(), channel.returncode()), (Liveness.ABSENT, 0))
        self.assertEqual(self.removed, [GEN])

    def test_a_failed_removal_raises_and_keeps_the_owner_lock(self):
        self.remove_errors = [SessionError("broker: close failed")] * 2
        channel = self.make()
        channel.fail("broken")
        with channel.condition:  # its ender's own first try failed; close's retry fails too
            channel.condition.wait_for(lambda: channel.attempts >= 1, WAIT)
        with self.assertRaisesRegex(SessionError, "close failed"):
            channel.close()
        self.assertEqual(channel.state(), Liveness.UNKNOWN)
        self.assertTrue(self.owner_open())
        channel.close()
        self.assertEqual(channel.state(), Liveness.ABSENT)
        self.assertFalse(self.owner_open())


class TerminalTest(Base):
    def test_record_identity_and_activity(self):
        channel = self.make()
        self.assertEqual(channel.session_identity(), ("sid-1", GEN))
        self.assertIsNone(channel.activity())
        channel.mark(StatusState.DONE, self.events)
        self.assertEqual(channel.activity(), StatusState.DONE)
        with open(self.events) as f:
            self.assertTrue(f.read().rstrip().endswith("worker_1 done"))
        self.record["generation"] = "other"
        self.assertEqual(channel.session_identity(), (None, None))

    def test_guarded_view_reaches_only_record_and_liveness(self):
        view = worker.GuardedView(
            "worker_1",
            GEN,
            state=self.state,
            load=lambda: self.record,
            liveness=lambda: Liveness.LIVE,
            remove=self.remove,
        )
        self.assertEqual((view.state(), view.session_identity()), (Liveness.LIVE, ("sid-1", GEN)))
        for call in (lambda: view.send(mock.Mock()), lambda: view.press(()), view.screen):
            with self.assertRaisesRegex(SessionError, "owned by its harness"):
                call()
        view.close()
        self.assertEqual(self.removed, [GEN])


class GuardHookTest(Base):
    def call(self, **state):
        return HookCall("Stop", {}, "sid-1", self.home, self.events, state)

    def test_writes_its_generations_activity_and_the_events_line(self):
        with mock.patch("subprocess.run") as ran, mock.patch("subprocess.Popen") as opened:
            guard_hook.on_hook("working", self.call(target="worker_1", generation=GEN))
            guard_hook.on_hook("done", self.call(target="worker_1", generation=GEN))
        ran.assert_not_called()
        opened.assert_not_called()
        self.assertEqual(worker.activity(self.state, GEN), StatusState.DONE)
        with open(self.events) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith("worker_1 done"))

    def test_bad_target_or_generation_is_a_hook_error(self):
        for state in (
            {"target": "../x", "generation": GEN},
            {"target": "worker_1", "generation": "../x"},
            {"generation": GEN},
        ):
            with self.subTest(state=state), self.assertRaises(HookError):
                guard_hook.on_hook("done", self.call(**state))
