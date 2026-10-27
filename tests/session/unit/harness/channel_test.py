"""harness/channel.py: a session harness's dir and the files its harness and views share."""

import fcntl
import json
import os
import stat
import threading
import time
from typing import cast
from unittest import mock

from ensemble.session.engine.types import (
    Activity,
    Blocked,
    ClientBound,
    Exited,
    LimitHit,
    Reply,
    SessionError,
    Stderr,
    TurnEnd,
    TurnFailed,
    TurnStarted,
    Unanswered,
)
from ensemble.session.harness import channel
from ensemble.session.harness.channel import Channel, decode, encode
from ensemble.session.harness.types import Logged, Op
from tests.session.case import Case

EVENTS = (
    TurnStarted(),
    Activity("Bash"),
    Reply("hi"),
    TurnEnd("full\nreply", 2),
    TurnFailed("server_overloaded", "detail"),
    Blocked("Bash"),
    LimitHit(1767225600.0),
    LimitHit(None),
    Exited(-1),
    Stderr("oops"),
    ClientBound("n1"),
)


class Base(Case):
    def setUp(self):
        self.channel = Channel.create()
        self.root = os.path.realpath(os.path.join(self.ensemble, "harness"))

    def hold_lock(self) -> int:
        fd = os.open(self.channel.lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        return fd


class CodecTest(Case):
    def test_every_event_round_trips(self):
        for event in EVENTS:
            with self.subTest(event=event):
                record = json.loads(json.dumps(encode(event)))
                self.assertEqual(decode(record), event)

    def test_a_malformed_record_is_none(self):
        for record in (
            None,
            [],
            {},
            {"type": "Nope", "fields": {}},
            {"type": "TurnEnd", "fields": {"reply": 5, "pending": 0}},
            {"type": "TurnEnd", "fields": {"reply": "r", "pending": True}},
            {"type": "TurnEnd", "fields": {"reply": "r"}},
            {"type": "TurnEnd", "fields": {"reply": "r", "pending": 0, "extra": 1}},
            {"type": "Exited", "fields": {"returncode": "1"}},
            {"type": "LimitHit", "fields": {"resets_at": "soon"}},
            {"type": "TurnStarted", "fields": []},
        ):
            with self.subTest(record=record):
                self.assertIsNone(decode(record))


class DirTest(Base):
    def test_create_makes_a_private_hex_dir_under_the_root(self):
        name = os.path.basename(self.channel.directory)
        self.assertRegex(name, r"\A[0-9a-f]{32}\Z")
        self.assertEqual(os.path.dirname(os.path.realpath(self.channel.directory)), self.root)
        self.assertEqual(stat.S_IMODE(os.stat(self.channel.directory).st_mode), 0o700)
        self.assertNotEqual(Channel.create().directory, self.channel.directory)

    def test_a_request_is_private_taken_once_and_never_overwritten(self):
        self.channel.write_request({"version": 1, "spec": {"env": {"K": "v"}}})
        path = os.path.join(self.channel.directory, "request.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            self.channel.write_request({"version": 1})
        self.assertEqual(self.channel.take_request()["spec"], {"env": {"K": "v"}})
        self.assertFalse(os.path.exists(path))
        with self.assertRaises(SessionError):
            self.channel.take_request()
        self.channel.write_request({"version": 1})
        self.channel.discard_request()
        self.channel.discard_request()
        self.assertFalse(os.path.exists(path))

    def test_a_request_json_cannot_hold_writes_nothing(self):
        with self.assertRaises(TypeError):
            self.channel.write_request({"spec": {"env": {"K": "secret"}}, "state": {1, 2}})
        self.assertFalse(os.path.exists(os.path.join(self.channel.directory, "request.json")))

    def test_a_request_that_is_no_object_is_refused(self):
        with open(os.path.join(self.channel.directory, "request.json"), "w") as f:
            f.write("[]")
        with self.assertRaisesRegex(SessionError, "request.json"):
            self.channel.take_request()


class LogTest(Base):
    def record(self, seq, event, turn=None) -> dict:
        return {"seq": seq, "id": f"g:{seq}", "turn": turn, **encode(event), "ts": "t"}

    def test_read_gives_complete_valid_records_past_the_cursor(self):
        self.channel.append(self.record(1, TurnStarted(), "g:t1"))
        self.channel.append(self.record(2, TurnEnd("r", 0), "g:t1"))
        found, cursor = self.channel.read(0)
        self.assertEqual(
            found,
            [Logged(1, "g:1", "g:t1", TurnStarted()), Logged(2, "g:2", "g:t1", TurnEnd("r", 0))],
        )
        self.assertEqual(self.channel.read(cursor), ([], cursor))
        with open(self.channel.events, "ab") as f:
            f.write(b'{"seq": 3, "id": "g:3"')
        self.assertEqual(self.channel.read(cursor), ([], cursor))
        with open(self.channel.events, "ab") as f:
            f.write(b', "turn": null, "type": "Exited", "fields": {"returncode": 0}, "ts": "t"}\n')
        self.assertEqual(self.channel.read(cursor)[0], [Logged(3, "g:3", None, Exited(0))])

    def test_malformed_lines_are_skipped(self):
        with open(self.channel.events, "wb") as f:
            for line in (
                b"not json",
                b"[]",
                json.dumps({**self.record(1, Reply("x")), "seq": "1"}).encode(),
                json.dumps({**self.record(1, Reply("x")), "id": 5}).encode(),
                json.dumps({**self.record(1, Reply("x")), "turn": 5}).encode(),
                json.dumps({**self.record(1, Reply("x")), "type": "Nope"}).encode(),
                json.dumps(self.record(2, Reply("ok"))).encode(),
            ):
                f.write(line + b"\n")
        self.assertEqual(self.channel.read(0)[0], [Logged(2, "g:2", None, Reply("ok"))])

    def test_no_log_reads_nothing(self):
        self.assertEqual(self.channel.read(0), ([], 0))


class LockTest(Base):
    def test_alive_while_its_lock_is_held(self):
        self.assertFalse(self.channel.alive())
        self.channel.hold()
        self.assertTrue(self.channel.alive())
        with self.assertRaisesRegex(SessionError, "already held"):
            Channel(self.channel.directory).hold()
        self.channel.release()
        self.assertFalse(self.channel.alive())

    def test_a_second_hold_is_refused(self):
        self.channel.hold()
        self.addCleanup(self.channel.release)
        other = Channel(self.channel.directory)
        with self.assertRaises(SessionError) as caught:
            other.hold()
        self.assertEqual(
            str(caught.exception), f"session harness {self.channel.directory} is already held"
        )

    def test_a_missing_dir_is_dead(self):
        self.assertFalse(Channel(os.path.join(self.root, "f" * 32)).alive())


class RequestTest(Base):
    def serve(self, *, ok=True, error=None, unanswered=False):
        def answer():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                for request_id, op in self.channel.requests():
                    self.served.append(op)
                    self.channel.reply(request_id, ok=ok, error=error, unanswered=unanswered)
                    return
                time.sleep(0.01)

        self.served = []
        thread = threading.Thread(target=answer)
        thread.start()
        self.addCleanup(thread.join)

    def test_an_answered_request_is_true(self):
        self.hold_lock()
        self.serve()
        self.assertTrue(self.channel.request(Op.CLOSE, timeout=5))
        self.assertEqual(self.served, [Op.CLOSE])
        self.assertEqual(os.listdir(os.path.join(self.channel.directory, "replies")), [])

    def test_a_refused_request_raises_its_error(self):
        self.hold_lock()
        self.serve(ok=False, error="cannot close")
        with self.assertRaisesRegex(SessionError, "cannot close"):
            self.channel.request(Op.STOP, timeout=5)

    def test_an_unanswered_reply_raises_unanswered(self):
        self.hold_lock()
        self.serve(ok=False, error="worker exit not confirmed", unanswered=True)
        with self.assertRaisesRegex(Unanswered, "not confirmed"):
            self.channel.request(Op.CLOSE, timeout=5)

    def test_no_harness_is_false(self):
        self.assertFalse(self.channel.request(Op.CLOSE, timeout=1))
        self.assertFalse(os.path.exists(os.path.join(self.channel.directory, "requests")))

    def test_no_answer_from_a_live_harness_is_unanswered(self):
        self.hold_lock()
        with self.assertRaises(Unanswered):
            self.channel.request(Op.CLOSE, timeout=0.05)
        self.assertEqual(self.channel.requests(), [])

    def test_an_unknown_op_is_refused_before_writing(self):
        with self.assertRaises(ValueError):
            self.channel.request(cast(Op, "kill"))

    def test_bad_request_files_are_deleted_and_never_served(self):
        folder = os.path.join(self.channel.directory, "requests")
        os.makedirs(folder, exist_ok=True)
        good = "a" * 32
        for name, body in (
            ("x.json", '{"op": "close"}'),
            ("b" * 32 + ".json", '{"op": "kill"}'),
            ("c" * 32 + ".json", "not json"),
            ("d" * 32 + ".json", "[]"),
            (good + ".json", '{"op": "stop"}'),
        ):
            with open(os.path.join(folder, name), "w") as f:
                f.write(body)
        os.mkdir(os.path.join(folder, "e" * 32 + ".json"))
        outside = os.path.join(self.channel.directory, "outside.json")
        with open(outside, "w") as f:
            f.write('{"op": "close"}')
        os.symlink(outside, os.path.join(folder, "f" * 32 + ".json"))
        with open(os.path.join(folder, ".tmp-being-saved"), "w") as f:
            f.write('{"op": "close"}')
        with open(os.path.join(folder, "9" * 32 + ".json"), "w") as f:
            f.write('{"op": "close", "pad": "' + "x" * channel.FILE_MAX + '"}')
        with self.assertLogs("session", "WARNING"):
            self.assertEqual(self.channel.requests(), [(good, "stop")])
        self.assertEqual(os.listdir(folder), [".tmp-being-saved"])


class StatusTest(Base):
    def test_status_round_trips_and_a_bad_one_is_empty(self):
        self.assertEqual(self.channel.status(), {})
        self.channel.write_status({"state": "running", "opening": "/color"})
        self.assertEqual(self.channel.status(), {"state": "running", "opening": "/color"})
        with open(self.channel.status_file, "w") as f:
            f.write("[]")
        self.assertEqual(self.channel.status(), {})

    def test_an_oversized_status_is_empty(self):
        with open(self.channel.status_file, "w") as f:
            f.write('{"state": "running", "pad": "' + "x" * channel.FILE_MAX + '"}')
        self.assertEqual(self.channel.status(), {})


class SweepTest(Base):
    def make(self, name, *, status=True, age=0.0) -> str:
        path = os.path.join(self.root, name)
        os.makedirs(path, exist_ok=True)
        when = time.time() - age
        if status:
            status_file = os.path.join(path, "harness.json")
            with open(status_file, "w") as f:
                f.write("{}")
            os.utime(status_file, (when, when))
        os.utime(path, (when, when))
        return path

    def test_only_stale_free_hex_dirs_go(self):
        old, day = channel.STALE_AGE + 60, channel.DAY + 60
        stale = self.make("1" * 32, age=old)
        fresh = self.make("2" * 32)
        unstarted = self.make("3" * 32, status=False, age=day)
        starting = self.make("4" * 32, status=False)
        held = self.make("5" * 32, age=old)
        other = self.make("not-hex", age=old)
        target = os.path.join(self.home, "6" * 32)
        os.makedirs(target)
        link = os.path.join(self.root, "7" * 32)
        os.symlink(target, link)
        fd = os.open(os.path.join(held, "harness.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        channel.sweep()
        kept = set(os.listdir(self.root))
        self.assertNotIn(os.path.basename(stale), kept)
        self.assertNotIn(os.path.basename(unstarted), kept)
        for path in (fresh, starting, held, other, link, self.channel.directory):
            with self.subTest(path=path):
                self.assertIn(os.path.basename(path), kept)
        self.assertTrue(os.path.isdir(target))

    def test_a_sweep_error_is_logged_never_raised(self):
        self.make("1" * 32, age=channel.STALE_AGE + 60)
        with (
            mock.patch("shutil.rmtree", side_effect=PermissionError(13, "denied")),
            self.assertLogs("session", "WARNING"),
        ):
            channel.sweep()
