"""Roster state, lease, durable files and event cursors without live layer commands."""

import datetime
import fcntl
import io
import itertools
import json
import os
import stat
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest import mock
from uuid import uuid4

from ensemble.manager.engine.roster import Roster
from ensemble.manager.engine.types import (
    ActiveHolder,
    Held,
    Lease,
    RosterError,
    Spec,
    Stale,
    StaleHolder,
    State,
    Unread,
)
from ensemble.utils import append_line
from tests.manager.case import with_ctl
from tests.manager.roster_case import RosterCase, mark


class RosterTest(RosterCase):
    def test_missing_roster_reads_do_not_create_directories(self):
        self.assertEqual(self.roster.entries(), {})
        self.assertEqual(self.roster.backlog(), (0, 0, []))
        self.assertEqual(self.roster.next_event(0, 0), (0, None))
        self.assertEqual(self.roster.advance(0, 0), 0)
        self.assertEqual(self.roster.advance(None, 0), 0)
        self.roster.check_lease()
        self.assertFalse(self.roster.rotate_events())
        self.assertEqual(os.listdir(self.ensemble), [])
        self.assertFalse(Path(self.directory).exists())

    def test_missing_entry_mutations_do_not_create_directories(self):
        for operation in (
            lambda: self.roster.note("missing", "note"),
            lambda: self.roster.remove("missing"),
            lambda: mark(self.roster, "missing", State.GONE),
            lambda: self.roster.claim_resume("missing"),
            lambda: self.roster.commit("missing", self.record),
            lambda: self.roster.receipt_record("missing"),
        ):
            with (
                self.subTest(operation=operation),
                self.assertRaisesRegex(RosterError, "not in roster"),
            ):
                operation()
            self.assertEqual(os.listdir(self.ensemble), [])
        with (
            self.assertRaisesRegex(RosterError, "not in roster"),
            self.roster.open_receipt("missing"),
        ):
            self.fail("missing launch receipt opened")
        self.assertFalse(Path(self.directory).exists())

    def test_missing_roster_with_stale_lease_does_not_create_directories(self):
        self.roster.bind(Lease(str(uuid4()), 1))
        with self.assertRaises(Stale):
            self.roster.check_lease()
        self.assertEqual(os.listdir(self.ensemble), [])

    def test_explicit_hold_and_events_can_create_missing_manager_directory(self):
        with self.roster.hold("owner"):
            self.assertTrue(Path(self.directory).is_dir())
        events_directory = str(Path(self.ensemble, "managers", "events"))
        self.assertTrue(Path(Roster(events_directory).events).is_file())

    def test_begin_commit_preserves_record_and_note(self):
        self.begin(note="keep")
        entry = self.roster.entries()["x"]
        self.assertIs(entry.state, State.STARTING)
        self.assertEqual(entry.argv, ("fake-ctl", "start", "x"))
        self.assertIsNone(entry.record)
        self.roster.commit("x", self.record)
        entry = Roster(self.directory).entries()["x"]
        self.assertEqual(
            (entry.record, entry.state, entry.note, entry.argv),
            (self.record, State.LIVE, "keep", ()),
        )
        self.assertEqual(entry.started, self.content()["entries"]["x"]["started"])

    def test_invalid_intents_write_nothing(self):
        for intent in (
            Spec("bad\nname", ("fake",)),
            Spec("x", ()),
            Spec("x", ("fake",), "bad\n"),
            Spec("x", ("fake",), "a" * 501),
        ):
            with self.subTest(intent=intent), self.assertRaises(RosterError):
                self.roster.begin(intent, sid=self.record.sid)
        self.assertFalse(os.path.exists(self.directory))

    def test_duplicate_name_and_missing_entry_fail(self):
        self.begin()
        with self.assertRaisesRegex(RosterError, "already"):
            self.begin()
        for operation in (
            lambda: self.roster.commit("missing", self.record),
            lambda: self.roster.remove("missing"),
            lambda: self.roster.note("missing", "note"),
            lambda: mark(self.roster, "missing", State.GONE),
        ):
            with self.assertRaisesRegex(RosterError, "not in roster"):
                operation()

    def test_complete_state_machine(self):
        for target in (State.DONE, State.WAITING, State.GONE):
            with self.subTest(target=target):
                self.live()
                mark(self.roster, "x", target)
                self.assertIs(self.roster.entries()["x"].state, target)
                self.roster.remove("x")
        self.live()
        mark(self.roster, "x", State.GONE)
        mark(self.roster, "x", State.LIVE)
        mark(self.roster, "x", State.GONE)
        mark(self.roster, "x", State.RESUMING)
        mark(self.roster, "x", State.GONE)
        mark(self.roster, "x", State.RESUMING)
        self.roster.commit("x", self.record)
        self.assertIs(self.roster.entries()["x"].state, State.LIVE)

    def test_invalid_transitions_are_atomic(self):
        self.live()
        mark(self.roster, "x", State.DONE)
        before = Path(self.roster.path).read_bytes()
        for state in State:
            if state == State.DONE:
                continue
            with self.subTest(state=state), self.assertRaises(RosterError):
                mark(self.roster, "x", state)
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        with self.assertRaises(RosterError):
            self.roster.commit("x", self.record)

    def test_adopt_rejects_noncanonical_sid_before_creating_files(self):
        for sid in (
            None,
            "not-a-uuid",
            "12345678-1234-1234-8234-123456789abc",
            "12345678-1234-4234-8234-123456789ABC",
            "{12345678-1234-4234-8234-123456789abc}",
            "12345678123442348234123456789abc",
        ):
            with self.subTest(sid=sid), self.assertRaisesRegex(RosterError, "canonical uuid4"):
                self.roster.adopt(replace(self.record, sid=sid))
            self.assertFalse(Path(self.directory).exists())

    def test_persisted_invalid_sid_is_skipped_for_every_state(self):
        self.live()
        valid = self.content()
        for state in State:
            for sid in (None, "not-a-uuid"):
                with self.subTest(state=state, sid=sid):
                    data = json.loads(json.dumps(valid))
                    entry = data["entries"]["x"]
                    entry["state"], entry["sid"] = state, sid
                    if state in {State.STARTING, State.LOST}:
                        entry["record"], entry["argv"] = None, ["fake-ctl", "start", "x"]
                    else:
                        entry["record"]["sid"] = sid
                    Path(self.roster.path).write_text(json.dumps(data))
                    before = Path(self.roster.path).read_bytes()
                    warning = io.StringIO()
                    with mock.patch("sys.stderr", warning):
                        self.assertEqual(self.roster.entries(), {})
                    self.assertEqual(len(warning.getvalue().splitlines()), 1)
                    self.assertIn("canonical uuid4", warning.getvalue())
                    self.assertEqual(Path(self.roster.path).read_bytes(), before)
                    self.assertEqual(list(Path(self.directory).glob("receipt-*.stdout")), [])

    def test_resume_preflight_rejects_invalid_sid_without_receipt_or_write(self):
        self.live()
        mark(self.roster, "x", State.GONE)
        data = self.content()
        data["entries"]["x"]["sid"] = None
        data["entries"]["x"]["record"]["sid"] = None
        Path(self.roster.path).write_text(json.dumps(data))
        before = Path(self.roster.path).read_bytes()
        for operation in (
            lambda: self.roster.claim_resume("x"),
            lambda: mark(self.roster, "x", State.RESUMING),
        ):
            warning = io.StringIO()
            with (
                mock.patch("sys.stderr", warning),
                self.assertRaisesRegex(RosterError, "not in roster"),
            ):
                operation()
            self.assertEqual(len(warning.getvalue().splitlines()), 1)
            self.assertIn("canonical uuid4", warning.getvalue())
            self.assertEqual(Path(self.roster.path).read_bytes(), before)
            self.assertEqual(list(Path(self.directory).glob("receipt-*.stdout")), [])

    def test_lost_adoption_retains_note_and_started(self):
        self.begin(note="keep")
        mark(self.roster, "x", State.LOST)
        old = self.roster.entries()["x"]
        self.assertEqual(self.roster.adopt(self.record), "x")
        adopted = self.roster.entries()["x"]
        self.assertEqual(
            (adopted.state, adopted.record, adopted.note, adopted.started),
            (State.LIVE, self.record, old.note, old.started),
        )

    def test_generic_record_names_are_safe_roster_keys(self):
        for name in ("external.run", "external run", "external/run", "../external", "外部"):
            with self.subTest(name=name):
                record = replace(self.record, name=name, sessions=(name,), sid=str(uuid4()))
                self.live(name, record=record)
                self.assertEqual(self.roster.adopt(record), name)
                self.assertEqual(Roster(self.directory).entries()[name].record, record)
        self.assertFalse((Path(self.directory).parent / "external").exists())

    def test_start_aliases_accept_printable_names_and_reject_controls(self):
        self.begin("alias / with spaces")
        self.assertIn("alias / with spaces", self.roster.entries())
        for name in ("", "bad\tname", "bad\nname"):
            with self.subTest(name=name), self.assertRaises(RosterError):
                self.begin(name)

    def test_adopt_takes_no_record_whose_sid_no_launch_holds(self):
        self.assertIsNone(self.roster.adopt(self.record))
        self.assertFalse(Path(self.directory).exists())
        self.live("alias")
        before = Path(self.roster.path).read_bytes()
        for record in (
            replace(self.record, sid=str(uuid4()), name="forged", sessions=("forged",)),
            replace(self.record, sid=str(uuid4()), name="alias", sessions=("alias",)),
            replace(self.record, sid=str(uuid4())),
        ):
            with self.subTest(record=record):
                self.assertIsNone(self.roster.adopt(record))
                self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertEqual(list(self.roster.entries()), ["alias"])

    def test_adopting_a_launched_record_is_idempotent(self):
        self.live("alias")
        before = Path(self.roster.path).read_bytes()
        self.assertEqual(self.roster.adopt(self.record), "alias")
        self.assertEqual(self.roster.adopt(self.record), "alias")
        self.assertEqual(Path(self.roster.path).read_bytes(), before)

    def test_note_can_be_replaced_or_cleared(self):
        self.live(note="first")
        self.roster.note("x", "second")
        self.assertEqual(self.roster.entries()["x"].note, "second")
        self.roster.note("x", None)
        self.assertIsNone(self.roster.entries()["x"].note)

    def test_close_failed_appends_one_json_line_per_call(self):
        self.live()
        before = int(time.time())
        path = self.roster.close_failed(
            "x", sid=self.record.sid, ctl=("ensemble-session",), reason="ctl: boom\nmore\x1b[0m"
        )
        self.roster.close_failed("y", sid=None, ctl=("other", "ctl"), reason="x" * 600)
        self.assertEqual(path, os.path.join(self.directory, "close-failures.jsonl"))
        first, second = (json.loads(line) for line in Path(path).read_text().splitlines())
        self.assertEqual(
            {k: v for k, v in first.items() if k != "at"},
            {
                "name": "x",
                "sid": self.record.sid,
                "ctl": ["ensemble-session"],
                "reason": "ctl: boom more[0m",
            },
        )
        self.assertIs(type(first["at"]), int)
        self.assertGreaterEqual(first["at"], before)
        self.assertEqual(
            (second["sid"], second["ctl"], second["reason"]), (None, ["other", "ctl"], "x" * 500)
        )
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(self.roster.entries()["x"].record, self.record)

    def test_close_failed_refuses_a_fifo_without_waiting(self):
        self.live()
        os.mkfifo(os.path.join(self.directory, "close-failures.jsonl"))
        with self.assertRaisesRegex(RosterError, "close-failures.jsonl"):
            self.roster.close_failed("x", sid=None, ctl=("c",), reason="r")

    def test_mark_if_applies_only_to_matching_snapshot(self):
        self.live(note="keep")
        expected = self.roster.entries()["x"]
        self.assertTrue(self.roster.mark_if("x", expected, State.GONE))
        current = self.roster.entries()["x"]
        self.assertEqual(current, replace(expected, state=State.GONE))

    def test_mark_if_ignores_missing_or_changed_state(self):
        self.live()
        expected = self.roster.entries()["x"]
        mark(self.roster, "x", State.DONE)
        before = Path(self.roster.path).read_bytes()
        self.assertFalse(self.roster.mark_if("x", expected, State.GONE))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.roster.remove("x")
        before = Path(self.roster.path).read_bytes()
        self.assertFalse(self.roster.mark_if("x", expected, State.GONE))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)

    def test_mark_if_ignores_replaced_sid_and_changed_note(self):
        self.live()
        expected = self.roster.entries()["x"]
        self.roster.note("x", "changed")
        before = Path(self.roster.path).read_bytes()
        self.assertFalse(self.roster.mark_if("x", expected, State.GONE))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.roster.remove("x")
        replacement = replace(self.record, sid=str(uuid4()))
        self.live(record=replacement)
        before = Path(self.roster.path).read_bytes()
        self.assertFalse(self.roster.mark_if("x", expected, State.GONE))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertEqual(self.roster.entries()["x"].record, replacement)

    def test_mark_if_checks_lease_before_missing_or_stale_snapshot(self):
        with self.roster.hold("first"):
            self.live()
            expected = self.roster.entries()["x"]
            with Roster(self.directory).hold("replacement", force=True):
                for name in ("x", "missing"):
                    with self.subTest(name=name), self.assertRaises(Stale):
                        self.roster.mark_if(name, expected, State.GONE)

    def test_resume_claim_is_atomic_across_instances(self):
        for state in (State.GONE, State.WAITING):
            with self.subTest(state=state):
                self.atomic_claim(state)
                self.roster.remove("x")

    def atomic_claim(self, state):
        self.live()
        mark(self.roster, "x", state)
        claims = []
        barrier = threading.Barrier(2)

        def claim():
            other = Roster(self.directory)
            barrier.wait()
            claims.append(other.claim_resume("x", state=state))

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(entry is not None for entry in claims), 1)
        self.assertIs(self.roster.entries()["x"].state, State.RESUMING)

    def test_hold_rejects_live_holder_then_takes_dead_holder(self):
        other = Roster(self.directory)
        with self.roster.hold("a") as first:
            with self.assertRaises(Held), self.roster.hold("nested"):
                self.fail("live holder replaced")
            self.roster.check_lease()
            with self.assertRaises(Held), other.hold("b"):
                self.fail("live holder replaced")
            with self.assertRaises(Held):
                other.begin(Spec("x", ("fake",)), sid=self.record.sid)
        with other.hold("b") as second:
            self.assertGreater(second.generation, first.generation)
            self.assertNotEqual(second.id, first.id)
            self.assertEqual(self.content()["holder"]["session"], "b")
            with self.assertRaises(Held):
                self.begin()
            other.begin(Spec("x", ("fake",)), sid=self.record.sid)

    def test_every_mutation_checks_current_lease(self):
        other = Roster(self.directory)
        with self.roster.hold("a"):
            self.live()
            with other.hold("b", force=True):
                for operation in (
                    lambda: self.roster.begin(Spec("y", ("fake",)), sid=str(uuid4())),
                    lambda: self.roster.commit("x", self.record),
                    lambda: mark(self.roster, "x", State.GONE),
                    lambda: self.roster.claim_resume("x"),
                    lambda: self.roster.remove("x"),
                    lambda: self.roster.note("x", "note"),
                    lambda: self.roster.close_failed("x", sid=None, ctl=("c",), reason="r"),
                    lambda: self.roster.adopt(self.record),
                    lambda: self.roster.advance(0, 0),
                    lambda: self.roster.backlog(require_lease=True),
                    lambda: self.roster.next_event(0, 0),
                    lambda: self.roster.watch_events(0, 0, acknowledge=False),
                    self.roster.rotate_events,
                    self.roster.release,
                    self.roster.check_lease,
                    lambda: self.roster.events,
                ):
                    with self.subTest(operation=operation), self.assertRaises(Stale):
                        operation()

    def test_only_current_holder_can_settle_receipts_during_reads(self):
        with self.roster.hold("owner") as lease:
            self.begin(note="keep")
            entry = self.roster.entries()["x"]
            receipt = Path(self.directory, entry.receipt)
            receipt.write_text(self.record.to_line() + "\n")
            data = self.content()
            data["entries"]["x"].update(
                record=json.loads(self.record.to_line()), state="live", argv=[]
            )
            Path(self.roster.path).write_text(json.dumps(data))
            before = Path(self.roster.path).read_bytes()
            for token in (None, Lease(lease.id, lease.generation + 1)):
                reader = Roster(self.directory)
                reader.bind(token)
                self.assertEqual(reader.entries()["x"].record, self.record)
                self.assertEqual(Path(self.roster.path).read_bytes(), before)
                self.assertTrue(receipt.exists())
            self.roster.entries()
            self.assertFalse(receipt.exists())
            self.assertIsNone(self.roster.entries()["x"].receipt)

    def test_bound_owner_requires_live_generation_and_release_clears_lease(self):
        self.roster.bind(Lease(str(uuid4()), 1))
        with self.assertRaises(Stale):
            self.begin()
        with self.roster.hold("a") as first:
            self.begin()
            self.roster.release()
            self.assertIsNone(self.content()["holder"])
            with self.assertRaises(Stale):
                self.roster.check_lease()
            with Roster(self.directory).hold("b") as second:
                self.assertGreater(second.generation, first.generation)
        self.assertIsNone(self.content()["holder"])

    def test_stale_pair_raises_stale_holder(self):
        with self.roster.hold("a") as ended:
            pass
        self.roster.bind(ended)
        with self.assertRaises(StaleHolder) as caught:
            self.roster.check_lease()
        self.assertEqual(
            str(caught.exception),
            f"{self.directory}: stale holder generation; start a new manager holder",
        )
        data = self.content()
        data["holder"] = {"id": ended.id, "session": "a"}
        Path(self.roster.path).write_text(json.dumps(data))
        with self.assertRaises(StaleHolder) as caught:
            self.roster.check_lease()
        self.assertEqual(str(caught.exception), f"{self.directory}: holder no longer alive")
        self.roster.bind(None)
        with Roster(self.directory).hold("b"):
            with self.assertRaises(Held) as caught:
                self.roster.check_lease()
            self.assertNotIsInstance(caught.exception, StaleHolder)

    def test_lease_state_needs_no_lease(self):
        self.roster.bind(Lease(str(uuid4()), 1))
        self.assertEqual(self.roster.lease_state(), (0, 0, None))
        self.assertFalse(Path(self.directory).exists())
        other = Roster(self.directory)
        with other.hold("owner"):
            Path(other.events).write_text("12:00:00 a done\n12:00:01 a done\n")
            other.advance(1, 0)
            self.assertEqual(self.roster.lease_state(), (1, 0, "owner"))
        self.assertEqual(self.roster.lease_state(), (1, 0, None))

    def test_a_lease_check_creates_no_holder_lock(self):
        self.live()
        self.roster.check_lease()
        self.assertEqual(self.roster.lease_state(), (0, 0, None))
        self.assertFalse(Path(self.directory, "holder.lock").exists())

    def test_a_holder_lock_directory_fails_the_lease_state(self):
        self.live()
        os.mkdir(os.path.join(self.directory, "holder.lock"))
        with self.assertRaisesRegex(RosterError, "not a regular holder lock"):
            self.roster.lease_state()

    def test_no_owner_retains_unleased_operations_only_when_holder_lock_free(self):
        self.live()
        self.roster.check_lease()
        self.roster.release()
        self.assertIsNone(self.content()["holder"])
        with Roster(self.directory).hold("owner"), self.assertRaises(Held):
            self.roster.note("x", "forbidden")

    def test_private_directory_and_regular_files(self):
        self.live()
        events = self.roster.events
        self.assertEqual(stat.S_IMODE(os.stat(self.directory).st_mode), 0o700)
        for path in (self.roster.path, events, os.path.join(self.directory, "roster.lock")):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_a_roster_or_lock_fifo_is_refused_without_waiting(self):
        for name in ("roster.json", "roster.lock"):
            with self.subTest(name=name):
                self.live()
                path = Path(self.directory, name)
                path.unlink()
                os.mkfifo(path)
                with self.assertRaises(RosterError):
                    self.roster.entries()
                path.unlink()

    def test_restrictive_umask_preserves_private_usable_new_directories(self):
        previous = os.umask(0o077)
        try:
            self.live()
            self.assertEqual(self.roster.entries()["x"].record, self.record)
            for path in (Path(self.directory).parent, Path(self.directory)):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        finally:
            os.umask(previous)

    def test_failed_save_leaves_previous_roster_and_no_temp(self):
        self.live()
        before = Path(self.roster.path).read_bytes()
        with (
            mock.patch.object(os, "replace", side_effect=OSError("disk full")),
            self.assertRaises(RosterError),
        ):
            self.roster.note("x", "new")
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertEqual(
            sorted(os.listdir(self.directory)),
            ["events.lock", "roster.json", "roster.lock"],
        )

    def test_invalid_json_schema_and_size_are_refused(self):
        self.live()
        valid = Path(self.roster.path).read_text()
        for raw in (
            "{",
            "[]",
            "{}",
            valid.replace('"live"', '"impossible"'),
            " " * ((1 << 20) + 1),
        ):
            Path(self.roster.path).write_text(raw)
            with self.subTest(raw=raw[:20]):
                if raw == valid.replace('"live"', '"impossible"'):
                    warning = io.StringIO()
                    with mock.patch("sys.stderr", warning):
                        self.assertEqual(self.roster.entries(), {})
                    self.assertEqual(len(warning.getvalue().splitlines()), 1)
                    self.assertIn("impossible", warning.getvalue())
                    self.assertEqual(Path(self.roster.path).read_text(), raw)
                    self.assertEqual(list(Path(self.directory).glob("receipt-*.stdout")), [])
                else:
                    with self.assertRaises(RosterError):
                        self.roster.entries()

    def test_invalid_entry_read_keeps_healthy_entry_and_original_ledger(self):
        self.live()
        self.live("bad", record=replace(self.record, name="bad", sid=str(uuid4())))
        data = self.content()
        data["entries"]["bad"]["note"] = "bad\nnote"
        Path(self.roster.path).write_text(json.dumps(data))
        before = Path(self.roster.path).read_bytes()
        warning = io.StringIO()
        with mock.patch("sys.stderr", warning):
            entries = self.roster.entries()
        self.assertEqual(list(entries), ["x"])
        self.assertEqual(entries["x"].record, self.record)
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertEqual(len(warning.getvalue().splitlines()), 1)
        self.assertIn("entry 'bad'", warning.getvalue())

    def test_next_write_drops_invalid_entry_and_updates_healthy_entry(self):
        self.live()
        data = self.content()
        data["entries"]["bad"] = {"record": "invalid"}
        Path(self.roster.path).write_text(json.dumps(data))
        warning = io.StringIO()
        with mock.patch("sys.stderr", warning):
            self.roster.note("x", "updated")
        self.assertEqual(len(warning.getvalue().splitlines()), 1)
        self.assertIn("entry 'bad'", warning.getvalue())
        self.assertEqual(list(self.content()["entries"]), ["x"])
        self.assertEqual(self.roster.entries()["x"].note, "updated")
        self.roster.remove("x")
        self.assertEqual(self.roster.entries(), {})

    def test_lock_waits_for_other_writer(self):
        self.begin()
        fd = os.open(os.path.join(self.directory, "roster.lock"), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        thread = threading.Thread(target=lambda: self.roster.note("x", "after"))
        thread.start()
        time.sleep(0.05)
        self.assertTrue(thread.is_alive())
        fcntl.flock(fd, fcntl.LOCK_UN)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.roster.entries()["x"].note, "after")

    def test_busy_roster_lock_times_out_without_changing_ledger(self):
        self.live()
        before = Path(self.roster.path).read_bytes()
        fd = os.open(Path(self.directory, "roster.lock"), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            ticks = 0

            def instant():
                nonlocal ticks
                ticks += 31
                return ticks

            with (
                mock.patch.object(time, "monotonic", side_effect=instant),
                self.assertRaisesRegex(RosterError, "busy"),
            ):
                self.roster.note("x", "blocked")
        finally:
            os.close(fd)
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertIsNone(self.roster.entries()["x"].note)

    def test_rotate_refuses_a_fifo_events_file(self):
        path = Path(self.roster.events)
        path.write_bytes(b"h" * (1 << 20) + b"\n12:00:00 x done\n")
        self.roster.advance(1, 0)
        ledger = Path(self.roster.path).read_bytes()
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(RosterError):
            self.roster.rotate_events()
        self.assertEqual(Path(self.roster.path).read_bytes(), ledger)

    def test_failed_archive_rename_preserves_events_cursor_and_generation(self):
        path = Path(self.roster.events)
        data = b"h" * (1 << 20) + b"\n12:00:00 x done\n"
        path.write_bytes(data)
        self.roster.advance(1, 0)
        ledger = Path(self.roster.path).read_bytes()
        original = os.replace

        def failed(source, destination):
            if os.fspath(source) == str(path) and os.fspath(destination) == str(path) + ".1":
                raise OSError("archive rename failed")
            return original(source, destination)

        with (
            mock.patch.object(os, "replace", side_effect=failed),
            self.assertRaisesRegex(RosterError, "archive rename failed"),
        ):
            self.roster.rotate_events()
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(Path(self.roster.path).read_bytes(), ledger)
        self.assertEqual((self.content()["cursor"], self.content()["gen"]), (1, 0))

    def test_created_and_rotated_events_file_mode_is_0600(self):
        path = Path(self.roster.events)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        path.write_bytes(b"h" * (1 << 20) + b"\n12:00:00 x done\n")
        self.roster.advance(1, 0)
        self.assertTrue(self.roster.rotate_events())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(str(path) + ".1").stat().st_mode), 0o600)

    def test_rotation_size_boundary_is_strict(self):
        path = Path(self.roster.events)
        path.write_bytes(b"h" * ((1 << 20) - 1) + b"\n")
        self.roster.advance(1, 0)
        ledger = Path(self.roster.path).read_bytes()
        self.assertFalse(self.roster.rotate_events())
        self.assertEqual(Path(self.roster.path).read_bytes(), ledger)
        append_line(path, b"12:00:00 x done\n")
        self.assertTrue(self.roster.rotate_events())
        self.assertEqual(self.roster.backlog(), (0, 1, ["12:00:00 x done"]))

    def test_rotation_consumed_byte_boundary_is_inclusive(self):
        path = Path(self.roster.events)
        half = (1 << 20) // 2
        path.write_bytes(b"h" * (half - 2) + b"\n" + b"u" * (half + 1) + b"\n")
        self.roster.advance(1, 0)
        self.assertFalse(self.roster.rotate_events())
        path.write_bytes(b"h" * (half - 1) + b"\n" + b"u" * half + b"\n")
        self.assertTrue(self.roster.rotate_events())
        self.assertEqual((self.content()["cursor"], self.content()["gen"]), (0, 1))

    def test_hostile_entry_diagnostic_is_one_escaped_line_with_healthy_entry_kept(self):
        self.live()
        data = self.content()
        hostile = "invalid\n\x1b[31mforged\u2028"
        data["entries"]["bad"] = {**data["entries"]["x"], "state": hostile}
        Path(self.roster.path).write_text(json.dumps(data))
        before = Path(self.roster.path).read_bytes()
        warning = io.StringIO()
        with mock.patch("sys.stderr", warning):
            entries = self.roster.entries()
        self.assertEqual(list(entries), ["x"])
        self.assertEqual(entries["x"].record, self.record)
        self.assertEqual(len(warning.getvalue().splitlines()), 1)
        self.assertIn(repr(hostile), warning.getvalue())
        self.assertNotIn("\x1b", warning.getvalue())
        self.assertNotIn("\u2028", warning.getvalue())
        self.assertEqual(Path(self.roster.path).read_bytes(), before)

    def test_top_level_faults_refuse_reads_and_writes_without_changing_bytes(self):
        self.live()
        valid = self.content()
        cases = []
        for field in valid:
            missing = {key: value for key, value in valid.items() if key != field}
            cases.append((f"missing {field}", missing))
        cases.append(("extra field", {**valid, "extra": 0}))
        for value in (True, 1, None, "2"):
            cases.append((f"version {value!r}", {**valid, "version": value}))
        for field in ("cursor", "gen", "lease_gen"):
            for value in (True, -1, None, "0", 1.5):
                cases.append((f"{field} {value!r}", {**valid, field: value}))
        for value in ([], None, "invalid"):
            cases.append((f"entries {value!r}", {**valid, "entries": value}))
        holder = {"id": str(uuid4()), "session": "owner"}
        for value in (
            False,
            [],
            "invalid",
            {},
            {"session": "owner"},
            {"id": holder["id"]},
            {**holder, "since": "now"},
            {**holder, "id": None},
            {**holder, "id": "invalid"},
            {**holder, "id": holder["id"].upper()},
            {**holder, "session": ""},
            {**holder, "session": "bad\nowner"},
            {**holder, "session": "bad\towner"},
        ):
            cases.append((f"holder {value!r}", {**valid, "holder": value, "lease_gen": 1}))
        cases.append(("holder with zero generation", {**valid, "holder": holder}))
        for label, data in cases:
            with self.subTest(label=label):
                raw = json.dumps(data).encode()
                Path(self.roster.path).write_bytes(raw)
                for operation in (self.roster.entries, lambda: self.roster.note("x", "blocked")):
                    with self.assertRaises(RosterError):
                        operation()
                    self.assertEqual(Path(self.roster.path).read_bytes(), raw)
                    self.assertEqual(list(Path(self.directory).glob("receipt-*.stdout")), [])

    def test_manager_directory_regular_file_is_refused_without_touching_it(self):
        path = Path(self.directory)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"keep")
        with self.assertRaises(RosterError):
            self.begin()
        self.assertEqual(path.read_bytes(), b"keep")
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_ensemble_regular_file_is_refused_without_touching_it(self):
        path = Path(self.ensemble)
        path.rmdir()
        path.write_bytes(b"keep")
        with self.assertRaises(RosterError):
            self.begin()
        self.assertEqual(path.read_bytes(), b"keep")
        self.assertFalse(Path(self.directory).exists())

    def test_roster_json_directory_is_refused_and_left_unchanged(self):
        self.live()
        path = Path(self.roster.path)
        path.unlink()
        path.mkdir()
        with self.assertRaises(RosterError):
            self.roster.entries()
        self.assertTrue(path.is_dir())
        self.assertEqual(list(path.iterdir()), [])

    def test_valid_roster_at_exact_size_cap_is_read_without_rewrite(self):
        self.live()
        raw = Path(self.roster.path).read_bytes()
        padded = raw + b" " * ((1 << 20) - len(raw))
        Path(self.roster.path).write_bytes(padded)
        self.assertEqual(self.roster.entries()["x"].record, self.record)
        self.assertEqual(Path(self.roster.path).read_bytes(), padded)

    def test_large_event_history_cursor_slices_across_read_blocks(self):
        path = Path(self.roster.events)
        lines = [f"12:00:00 worker done {index}" for index in range(20000)]
        path.write_text("\n".join(lines) + "\npartial")
        self.roster.advance(12345, 0)
        self.assertEqual(self.roster.backlog(), (12345, 0, lines[12345:]))
        self.assertEqual(self.roster.next_event(12345, 0), (12346, lines[12345]))
        self.roster.advance(len(lines) - 2, 0)
        self.assertEqual(self.roster.backlog(), (len(lines) - 2, 0, lines[-2:]))

    def test_existing_managers_parent_mode_is_preserved(self):
        parent = Path(self.directory).parent
        parent.mkdir(parents=True)
        parent.chmod(0o755)
        self.assertTrue(Path(self.roster.events).exists())
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(Path(self.directory).stat().st_mode), 0o700)

    def test_events_ignore_partial_line_and_advance_is_monotonic(self):
        path = self.roster.events
        append_line(path, "one\ntwo\npartial")
        self.assertEqual(self.roster.backlog(), (0, 0, ["one", "two"]))
        self.assertEqual(self.roster.next_event(0, 0), (1, "one"))
        self.assertEqual(self.roster.next_event(2, 0), (2, None))
        self.assertEqual(self.roster.advance(None, 0), 2)
        self.roster.advance(1, 0)
        self.assertEqual(self.roster.backlog(), (2, 0, []))
        append_line(path, " complete\n")
        self.assertEqual(self.roster.backlog(), (2, 0, ["partial complete"]))

    def test_bad_cursors_are_stale_and_rotation_retains_only_unacknowledged_events(self):
        self.assertTrue(os.path.isfile(self.roster.events))
        for after, gen in ((1, 0), (-1, 0), (0, 1)):
            with self.subTest(after=after, gen=gen), self.assertRaises(Stale):
                self.roster.advance(after, gen)
        handled = "h" * (1 << 20)
        Path(self.roster.events).write_text(handled + "\npending\n")
        self.roster.watch_events(2, 0, acknowledge=False)
        self.assertEqual(self.roster.backlog(), (0, 0, [handled, "pending"]))
        self.assertEqual(self.roster.next_event(0, 0), (1, handled))
        self.assertFalse(self.roster.rotate_events())
        self.roster.advance(1, 0)
        self.roster.advance(1, 0)
        self.assertTrue(self.roster.rotate_events())
        self.assertEqual(self.roster.backlog(), (0, 1, ["pending"]))
        self.assertTrue(os.path.exists(self.roster.events + ".1"))
        with self.assertRaises(Stale):
            self.roster.advance(1, 0)
        with self.assertRaises(Stale):
            self.roster.next_event(1, 0)
        self.assertEqual(self.roster.backlog(), (0, 1, ["pending"]))

    def test_rotation_preserves_bytes_and_completes_partial_tail(self):
        path = self.roster.events
        append_line(path, b"h" * (1 << 20) + b"\ninvalid\xff partial")
        self.roster.advance(1, 0)
        self.assertTrue(self.roster.rotate_events())
        self.assertEqual(Path(path).read_bytes(), b"invalid\xff partial\n")

    def test_failed_rotation_refill_keeps_archive_and_rolls_back_generation(self):
        path = self.roster.events
        data = b"h" * (1 << 20) + b"\nunread\n"
        append_line(path, data)
        self.roster.advance(1, 0)
        ledger = Path(self.roster.path).read_bytes()
        original = os.replace

        def failed(source, destination):
            if os.fspath(destination) == path + ".next":
                raise OSError("disk full")
            return original(source, destination)

        with (
            mock.patch.object(os, "replace", side_effect=failed),
            self.assertRaises(RosterError) as failure,
        ):
            self.roster.rotate_events()
        self.assertEqual(Path(path + ".1").read_bytes(), data)
        self.assertEqual(Path(path).read_bytes(), data)
        self.assertEqual(Path(self.roster.path).read_bytes(), ledger)
        self.assertEqual(self.roster.backlog(), (1, 0, ["unread"]))
        self.assertIn(path + ".1", str(failure.exception))
        self.assertIn("after 1", str(failure.exception))

    def test_ledger_publish_failure_after_rotation_restores_event_bytes_and_numbering(self):
        path = self.roster.events
        data = b"h" * (1 << 20) + b"\nunread\n"
        append_line(path, data)
        self.roster.advance(1, 0)
        ledger = Path(self.roster.path).read_bytes()
        original = os.replace
        failed_once = False

        def failed(source, destination):
            nonlocal failed_once
            if os.fspath(destination) == self.roster.path and not failed_once:
                self.assertEqual(Path(path).read_bytes(), b"unread\n")
                failed_once = True
                raise OSError("ledger publish failed")
            return original(source, destination)

        with (
            mock.patch.object(os, "replace", side_effect=failed),
            self.assertRaisesRegex(RosterError, "ledger publish failed"),
        ):
            self.roster.rotate_events()
        self.assertTrue(failed_once)
        self.assertEqual(Path(path).read_bytes(), data)
        self.assertEqual(Path(path + ".1").read_bytes(), data)
        self.assertEqual(Path(self.roster.path).read_bytes(), ledger)
        self.assertEqual((self.content()["cursor"], self.content()["gen"]), (1, 0))
        self.assertEqual(self.roster.backlog(), (1, 0, ["unread"]))

    def test_next_event_requires_attach_when_rotation_due(self):
        path = self.roster.events
        append_line(path, "h" * (1 << 20) + "\nunread\n")
        self.roster.advance(1, 0)
        with self.assertRaisesRegex(Stale, "rotation due"):
            self.roster.next_event(1, 0)

    def test_rotation_copies_append_to_inflight_descriptor_and_new_path(self):
        path = self.roster.events
        append_line(path, "h" * (1 << 20) + "\nold\n")
        self.roster.advance(1, 0)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        self.addCleanup(os.close, fd)
        original, appended = os.replace, []

        def append(source, destination):
            original(source, destination)
            if os.fspath(destination) == path + ".1":
                os.write(fd, b"inflight\n")
                append_line(path, "new\n")
                appended.append(destination)

        with mock.patch.object(os, "replace", side_effect=append):
            self.assertTrue(self.roster.rotate_events())
        self.assertTrue(appended)
        self.assertCountEqual(self.roster.backlog()[2], ["old", "inflight", "new"])

    def test_an_events_fifo_is_refused(self):
        path = self.roster.events
        Path(path).unlink()
        os.mkfifo(path)
        with self.assertRaises(RosterError):
            self.roster.backlog()

    def test_mark_resuming_sets_new_start_time(self):
        self.live()
        data = self.content()
        old = "2020-01-01T00:00:00+00:00"
        data["entries"]["x"]["started"] = old
        Path(self.roster.path).write_text(json.dumps(data))
        mark(self.roster, "x", State.GONE)
        self.roster.claim_resume("x")
        started = self.roster.entries()["x"].started
        self.assertGreater(
            datetime.datetime.fromisoformat(started), datetime.datetime.fromisoformat(old)
        )

    def test_sid_and_private_receipt_exist_before_start_and_cleanup_after_commit(self):
        self.begin("alias", "keep")
        entry = self.roster.entries()["alias"]
        self.assertEqual(entry.sid, self.record.sid)
        receipt = Path(self.directory, entry.receipt)
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
        with self.roster.open_receipt("alias") as output:
            output.write(b"diagnostic\n" + self.record.to_line().encode() + b"\n")
        self.assertEqual(self.roster.receipt_record("alias"), self.record)
        self.roster.commit("alias", self.record)
        self.assertFalse(receipt.exists())
        self.assertIsNone(self.roster.entries()["alias"].receipt)

    def test_receipt_record_ignores_blank_trailers_for_starting_and_resuming(self):
        for state in (State.STARTING, State.RESUMING):
            for trailer in (b"\n\n", b"\n \t\r\n\v\f\n", "\n\u2003\n".encode()):
                with self.subTest(state=state, trailer=trailer):
                    self._prepare_receipt(state)
                    with self.roster.open_receipt("x") as output:
                        output.write(b"diagnostic\xff\n" + self.record.to_line().encode() + trailer)
                    before = Path(self.roster.path).read_bytes()
                    self.assertEqual(self.roster.receipt_record("x"), self.record)
                    self.assertEqual(Path(self.roster.path).read_bytes(), before)
                    self.roster.commit("x", self.roster.receipt_record("x"))
                    entry = self.roster.entries()["x"]
                    self.assertEqual(
                        (entry.record, entry.state, entry.note), (self.record, State.LIVE, "keep")
                    )
                    self.assertIsNone(entry.receipt)
                    self.roster.remove("x")

    def test_receipt_record_refuses_no_record_or_a_malformed_final_line(self):
        malformed = b"not a Record\n \t\n"
        for contents, message in (
            (b"", "no Record received"),
            (b"\n \t\r\n\v\f\n", "no Record received"),
            ("\n\u2003\n".encode(), "no Record received"),
            (malformed, "invalid final Record"),
            (self.record.to_line().encode() + b"\n" + malformed, "invalid final Record"),
        ):
            for state in (State.STARTING, State.RESUMING):
                with self.subTest(state=state, contents=contents):
                    self._prepare_receipt(state)
                    with self.roster.open_receipt("x") as output:
                        output.write(contents)
                    before = Path(self.roster.path).read_bytes()
                    entry = self.roster.entries()["x"]
                    with self.assertRaisesRegex(RosterError, message):
                        self.roster.receipt_record("x")
                    self.assertEqual(Path(self.roster.path).read_bytes(), before)
                    self.assertEqual(self.roster.entries()["x"], entry)
                    self.roster.remove("x")

    def test_commit_rejects_null_different_and_previously_owned_sid(self):
        self.begin()
        before = Path(self.roster.path).read_bytes()
        for sid in (None, str(uuid4())):
            with self.subTest(sid=sid), self.assertRaisesRegex(RosterError, "Record.sid"):
                self.roster.commit("x", replace(self.record, sid=sid))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        with self.assertRaisesRegex(RosterError, "already owned"):
            self.roster.begin(Spec("second", ("fake",)), sid=self.record.sid)

    def test_lost_adoption_matches_sid_not_name_or_sessions(self):
        self.begin("alias", "keep")
        mark(self.roster, "alias", State.LOST)
        before = self.roster.entries()["alias"]
        record = replace(self.record, name="other", sessions=("unrelated",))
        self.assertEqual(self.roster.adopt(record), "alias")
        after = self.roster.entries()["alias"]
        self.assertEqual(
            (after.record, after.note, after.started), (record, before.note, before.started)
        )
        self.assertEqual(self.roster.adopt(record), "alias")
        self.assertEqual(list(self.roster.entries()), ["alias"])

    def test_unmatched_sid_leaves_a_lost_entry_unchanged(self):
        self.begin()
        mark(self.roster, "x", State.LOST)
        before = Path(self.roster.path).read_bytes()
        self.assertIsNone(self.roster.adopt(replace(self.record, sid=str(uuid4()))))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)

    def test_adoption_ignores_another_sid_naming_a_live_control_name(self):
        self.live("alias", "keep")
        before = Path(self.roster.path).read_bytes()
        entries = self.roster.entries()
        self.assertIsNone(self.roster.adopt(replace(self.record, sid=str(uuid4()))))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertEqual(self.roster.entries(), entries)
        self.assertEqual(self.roster.adopt(self.record), "alias")
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertEqual(list(self.roster.entries()), ["alias"])

    def test_lost_adoption_refuses_another_alias_owning_the_control_name(self):
        self.live("owner", "owner note")
        sid = str(uuid4())
        self.roster.begin(Spec("lost alias", ("fake-ctl", "start"), "lost note"), sid=sid)
        mark(self.roster, "lost alias", State.LOST)
        before = Path(self.roster.path).read_bytes()
        entries = self.roster.entries()
        with self.assertRaisesRegex(RosterError, "already owned by owner"):
            self.roster.adopt(replace(self.record, sid=sid))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertEqual(self.roster.entries(), entries)
        self.assertIs(self.roster.entries()["lost alias"].state, State.LOST)
        self.assertEqual(
            [entry.record for entry in self.roster.entries().values() if entry.record is not None],
            [self.record],
        )

    def test_commit_control_conflict_preserves_starting_intent_and_receipt(self):
        self.live("owner", "owner note")
        sid = str(uuid4())
        intent = self.roster.begin(
            Spec("starting alias", ("fake-ctl", "start"), "starting note"), sid=sid
        )
        if intent.receipt is None:
            self.fail("starting intent has no receipt")
        receipt = Path(self.directory, intent.receipt)
        before = Path(self.roster.path).read_bytes()
        entries = self.roster.entries()
        with self.assertRaisesRegex(RosterError, "already owned by owner"):
            self.roster.commit("starting alias", replace(self.record, sid=sid))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertEqual(self.roster.entries(), entries)
        self.assertEqual(self.roster.entries()["starting alias"], intent)
        self.assertTrue(receipt.exists())
        self.assertEqual(receipt.read_bytes(), b"")
        self.assertEqual(
            [entry.record for entry in self.roster.entries().values() if entry.record is not None],
            [self.record],
        )

    def test_control_names_are_scoped_to_the_exact_ctl_command(self):
        self.live("owner")
        sid = str(uuid4())
        self.roster.begin(Spec("other ctl", ("ensemble-delegate", "start")), sid=sid)
        other = with_ctl(replace(self.record, sid=sid), "ensemble-delegate")
        self.roster.commit("other ctl", other)
        self.assertEqual(self.roster.adopt(other), "other ctl")
        distinct_name = replace(self.record, sid=str(uuid4()), name="different")
        self.live("different", record=distinct_name)
        self.assertEqual(self.roster.adopt(distinct_name), "different")
        self.assertEqual(
            {name: entry.record for name, entry in self.roster.entries().items()},
            {"owner": self.record, "other ctl": other, "different": distinct_name},
        )

    def test_ambiguous_or_already_owned_sid_is_refused_without_writes(self):
        self.begin("alias")
        mark(self.roster, "alias", State.LOST)
        data = self.content()
        data["entries"]["duplicate"] = data["entries"]["alias"]
        Path(self.roster.path).write_text(json.dumps(data))
        before = Path(self.roster.path).read_bytes()
        with self.assertRaisesRegex(RosterError, "more than one"):
            self.roster.adopt(self.record)
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.roster.remove("duplicate")
        self.roster.adopt(self.record)
        with self.assertRaisesRegex(RosterError, "already owned"):
            self.roster.adopt(with_ctl(self.record, "ensemble-delegate"))

    def test_adopted_record_retires_empty_or_mismatching_receipt(self):
        for contents in (
            b"",
            b"not a Record\n",
            replace(self.record, detail="older").to_line().encode(),
        ):
            with self.subTest(contents=contents):
                self.begin("alias")
                entry = self.roster.entries()["alias"]
                receipt = Path(self.directory, entry.receipt)
                receipt.write_bytes(contents)
                mark(self.roster, "alias", State.LOST)
                self.roster.adopt(self.record)
                self.assertIsNone(self.roster.entries()["alias"].receipt)
                self.assertFalse(receipt.exists())
                self.roster.remove("alias")

    def test_adoption_keeps_active_stdout_and_original_record_read_available(self):
        self.begin("alias")
        entry = self.roster.entries()["alias"]
        receipt = Path(self.directory, entry.receipt)
        mark(self.roster, "alias", State.LOST)
        with self.roster.open_receipt("alias") as output:
            self.roster.adopt(self.record)
            self.assertEqual(self.roster.entries()["alias"].receipt, entry.receipt)
            self.assertTrue(receipt.exists())
            contents = b"late child output\n" + self.record.to_line().encode() + b"\n"
            output.write(contents)
            output.flush()
            self.assertEqual(receipt.read_bytes(), contents)
            self.assertEqual(self.roster.receipt_record("alias"), self.record)
            self.roster.commit("alias", self.record)
            self.assertTrue(receipt.exists())
        self.roster.entries()
        self.assertFalse(receipt.exists())

    def test_concurrent_adoption_does_not_bypass_stdout_sid_validation(self):
        for sid in (None, str(uuid4())):
            with self.subTest(sid=sid):
                self.begin("alias")
                entry = self.roster.entries()["alias"]
                mark(self.roster, "alias", State.LOST)
                wrong = replace(self.record, sid=sid)
                with self.roster.open_receipt("alias") as output:
                    self.roster.adopt(self.record)
                    self.assertEqual(self.roster.entries()["alias"].receipt, entry.receipt)
                    output.write(wrong.to_line().encode() + b"\n")
                    output.flush()
                    actual = self.roster.receipt_record("alias")
                    self.assertEqual(actual, wrong)
                    with self.assertRaisesRegex(RosterError, "Record.sid"):
                        self.roster.commit("alias", actual)
                    self.assertEqual(self.roster.entries()["alias"].record, self.record)
                self.roster.entries()
                self.assertIsNone(self.roster.entries()["alias"].receipt)
                self.roster.remove("alias")

    def test_missing_receipt_reference_is_repaired_and_entry_remains_removable(self):
        for state in (State.LOST, State.LIVE, State.GONE):
            with self.subTest(state=state):
                self.begin()
                entry = self.roster.entries()["x"]
                receipt = Path(self.directory, entry.receipt)
                if state == State.LOST:
                    mark(self.roster, "x", state)
                elif state in {State.LIVE, State.GONE}:
                    data = self.content()
                    data["entries"]["x"].update(
                        record=json.loads(self.record.to_line()), state=state, argv=[]
                    )
                    Path(self.roster.path).write_text(json.dumps(data))
                receipt.unlink()
                warning = io.StringIO()
                with mock.patch("sys.stderr", warning):
                    self.roster.entries()
                self.assertIn("missing receipt", warning.getvalue())
                self.assertIsNone(self.roster.entries()["x"].receipt)
                self.roster.remove("x")
                self.assertEqual(self.roster.entries(), {})

    def test_missing_receipt_maintenance_preserves_active_launch_snapshots(self):
        for state in (State.STARTING, State.RESUMING):
            with self.subTest(state=state):
                self._prepare_receipt(state)
                entry = self.roster.entries()["x"]
                Path(self.directory, entry.receipt).unlink()
                self.roster.check_lease()
                try:
                    self.assertEqual(self.roster.entries()["x"], entry)
                    target = State.LOST if state == State.STARTING else State.GONE
                    self.assertTrue(self.roster.mark_if("x", entry, target))
                finally:
                    self.roster.remove("x")

    def test_a_fifo_orphan_receipt_warns_without_blocking_ledger_operations(self):
        self.live()
        orphan = Path(self.directory, "receipt-unsafe.stdout")
        os.mkfifo(orphan)
        warning = io.StringIO()
        with mock.patch("sys.stderr", warning):
            self.roster.note("x", "fifo")
            self.assertEqual(self.roster.entries()["x"].note, "fifo")
        self.assertTrue(orphan.exists())
        self.assertIn("left in place", warning.getvalue())
        orphan.unlink()
        self.roster.remove("x")

    def test_receipt_survives_failure_until_remove_and_orphans_are_cleaned(self):
        self.begin()
        entry = self.roster.entries()["x"]
        receipt = Path(self.directory, entry.receipt)
        mark(self.roster, "x", State.LOST)
        self.assertTrue(receipt.exists())
        orphan = Path(self.directory, "receipt-orphan.stdout")
        orphan.write_bytes(b"abandoned")
        self.roster.entries()
        self.assertFalse(orphan.exists())
        self.assertTrue(receipt.exists())
        self.roster.remove("x")
        self.assertFalse(receipt.exists())

    def test_a_receipt_fifo_is_refused(self):
        self.begin()
        entry = self.roster.entries()["x"]
        path = Path(self.directory, entry.receipt)
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises((OSError, RosterError)), self.roster.open_receipt("x"):
            self.fail("unsafe receipt opened")
        path.unlink()
        self.roster.remove("x")

    def test_holder_lock_is_private_noninheritable_and_force_fences_old_generation(self):
        with self.roster.hold("label") as old:
            path = Path(self.directory, "holder.lock")
            previous_inode = path.stat().st_ino
            fd = os.open(path, os.O_RDONLY)
            try:
                self.assertFalse(os.get_inheritable(fd))
                self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o600)
            finally:
                os.close(fd)
            other = Roster(self.directory)
            with other.hold("replacement", force=True) as new:
                self.assertGreater(new.generation, old.generation)
                self.assertNotEqual(path.stat().st_ino, previous_inode)
                self.assertEqual(self.content()["holder"]["id"], new.id)
                with self.assertRaises(Stale):
                    self.roster.check_lease()
            self.assertIsNone(self.content()["holder"])

    def test_holder_lock_fifo_and_other_lock_errors_refuse_even_force(self):
        Path(self.directory).mkdir(parents=True, mode=0o700)
        path = Path(self.directory, "holder.lock")
        os.mkfifo(path)
        with self.assertRaises(RosterError), self.roster.hold("label", force=True):
            self.fail("unsafe lock acquired")
        path.unlink()
        real_flock = fcntl.flock
        count = 0

        def flock(fd, flags):
            nonlocal count
            count += 1
            if count == 2:
                raise PermissionError("probe denied")
            return real_flock(fd, flags)

        with (
            mock.patch("fcntl.flock", side_effect=flock),
            self.assertRaisesRegex(RosterError, "probe denied"),
            self.roster.hold("label", force=True),
        ):
            self.fail("probe error became takeover")
        self.assertEqual(count, 2)
        self.assertIsNone(self.content()["holder"] if Path(self.roster.path).exists() else None)


class SnapshotTest(RosterCase):
    def files(self):
        return {
            name: Path(self.directory, name).read_bytes() for name in os.listdir(self.directory)
        }

    def test_snapshot_creates_no_file(self):
        Path(self.directory).mkdir(parents=True, mode=0o700)
        self.assertEqual(self.roster.snapshot(), (None, {}))
        self.assertEqual(os.listdir(self.directory), [])

    def test_snapshot_of_a_held_roster_changes_nothing(self):
        self.live()
        with Roster(self.directory).hold("owner") as lease:
            before = self.files()
            holder, entries = self.roster.snapshot()
            self.assertEqual(self.files(), before)
            self.assertEqual(
                (holder, entries), (ActiveHolder(lease.id, "owner"), self.roster.entries())
            )
            with self.assertRaises(Held), Roster(self.directory).hold(None):
                pass

    def test_two_snapshots_while_held_both_see_the_holder(self):
        self.live()
        with Roster(self.directory).hold("owner") as lease:
            lock = os.open(os.path.join(self.directory, "roster.lock"), os.O_RDONLY)
            try:
                fcntl.flock(lock, fcntl.LOCK_SH)
                self.assertEqual(self.roster.snapshot()[0], ActiveHolder(lease.id, "owner"))
                self.assertEqual(
                    Roster(self.directory).snapshot()[0], ActiveHolder(lease.id, "owner")
                )
            finally:
                os.close(lock)

    def test_hold_succeeds_after_a_snapshot(self):
        self.live()
        self.assertEqual(self.roster.snapshot(), (None, self.roster.entries()))
        with Roster(self.directory).hold("owner") as lease:
            self.assertEqual(self.roster.snapshot()[0], ActiveHolder(lease.id, "owner"))
        self.assertIsNone(self.roster.snapshot()[0])

    def test_a_holder_without_a_session_is_known_by_its_id(self):
        self.live()
        with Roster(self.directory).hold(None) as lease:
            holder = self.roster.snapshot()[0]
        self.assertEqual(holder, ActiveHolder(lease.id, None))
        self.assertEqual(holder.label, lease.id)
        self.assertEqual(ActiveHolder(lease.id, "owner").label, "owner")

    def test_a_held_lock_with_no_recorded_holder_is_unread(self):
        self.live()
        with Roster(self.directory).hold("owner"):
            valid = Path(self.roster.path).read_text()
            data = json.loads(valid)
            Path(self.roster.path).write_text(json.dumps({**data, "holder": None}))
            holder, entries = self.roster.snapshot()
            Path(self.roster.path).write_text(valid)
        self.assertEqual(holder, Unread("holder not recorded"))
        self.assertEqual(entries, self.roster.entries())

    def test_without_roster_lock_holder_lock_is_never_opened(self):
        Path(self.directory).mkdir(parents=True, mode=0o700)
        Path(self.directory, "holder.lock").touch(mode=0o600)
        opened = []
        real_open = os.open

        def record(path, *args, **kwargs):
            opened.append(os.path.basename(path))
            return real_open(path, *args, **kwargs)

        with mock.patch("os.open", side_effect=record):
            self.assertEqual(self.roster.snapshot(), (None, {}))
        self.assertNotIn("holder.lock", opened)

    def test_a_corrupt_roster_is_unread_and_its_holder_still_known(self):
        self.live()
        with Roster(self.directory).hold("owner"):
            valid = Path(self.roster.path).read_text()
            Path(self.roster.path).write_text("{")
            holder, entries = self.roster.snapshot()
            Path(self.roster.path).write_text(valid)
        self.assertEqual(holder, entries)
        self.assertIsInstance(entries, Unread)
        self.assertIn(self.roster.path, entries.error)

    def test_a_lock_that_is_no_regular_file_fails_the_snapshot(self):
        self.live()
        for name in ("roster.lock", "holder.lock"):
            with self.subTest(name=name):
                path = Path(self.directory, name)
                path.unlink(missing_ok=True)
                path.mkdir()
                with self.assertRaisesRegex(RosterError, "not a regular"):
                    self.roster.snapshot()
                path.rmdir()
                path.touch(mode=0o600)

    def test_a_busy_roster_lock_fails_the_snapshot(self):
        self.live()
        lock = os.open(os.path.join(self.directory, "roster.lock"), os.O_RDONLY)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with (
                mock.patch("time.monotonic", side_effect=itertools.count(0, 100)),
                self.assertRaisesRegex(RosterError, "roster.lock: busy"),
            ):
                self.roster.snapshot()
        finally:
            os.close(lock)
