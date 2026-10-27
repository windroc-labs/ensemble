"""RunRecord reads and writes delegate.json without the Session or ensemble config."""

import fcntl
import json
import os
from dataclasses import replace

from ensemble.delegate import ConfigError, Outcome
from ensemble.delegate.engine.harness_delegate import HarnessDelegate
from ensemble.delegate.engine.record import HANDLER, RunRecord, worker
from ensemble.delegate.engine.types import Layout, Limit, Result
from ensemble.session import HookCallback
from ensemble.utils import OutcomeStatus
from tests.delegate.harness import SID, HarnessCase


class RunRecordTest(HarnessCase):
    def record(self) -> RunRecord:
        return RunRecord(Layout(self.work))

    def test_a_full_state_round_trips(self):
        self.make()
        record = self.record()
        state = record.read()
        session = replace(
            state.plan.session, handlers=(HookCallback("pkg.handler", {"worker": 1}, ("/r",)),)
        )
        outcome = Outcome(
            OutcomeStatus.DONE, "title", "summary", url="url", deliverable="deliverable"
        )
        full = replace(
            state,
            plan=replace(state.plan, session=session),
            completed=True,
            result=Result(2, outcome, "the client exited 2", Limit(12.5)),
            previous=Result(1, None, "stopped: Stopped"),
            bound=True,
            closing=True,
        )
        record.save(full)
        self.assertEqual(record.read(), full)

    def test_a_bad_record_is_a_config_error(self):
        self.make()
        data = self.metadata()
        for key, value in (("version", 2), ("spec", {**data["spec"], "workdir": "/elsewhere"})):
            with self.subTest(key=key):
                with open(self.state_record(), "w", encoding="utf-8") as file:
                    json.dump({**data, key: value}, file)
                with self.assertRaisesRegex(ConfigError, "^invalid .*delegate.json: "):
                    self.record().read()
        with self.assertRaisesRegex(ConfigError, "missing metadata"):
            self.record().parse(None)

    def test_it_reads_and_writes_while_the_config_is_unreadable(self):
        self.make()
        with open(self.local, "w", encoding="utf-8") as file:
            file.write("not toml [")
        with self.assertRaises(ConfigError):
            HarnessDelegate.attach(self.work)
        record = self.record()
        record.save(record.read())


class PhaseTest(HarnessCase):
    """Owner phases: opening -> bound -> completed -> bound n + 1, each a compare-and-set."""

    def setUp(self):
        super().setUp()
        self.make()
        self.record = RunRecord(Layout(self.work))
        state = self.record.read()
        handler = HookCallback(HANDLER, {"worker": 1}, ("/r",))
        session = replace(state.plan.session, handlers=(handler,))
        self.record.save(replace(state, plan=replace(state.plan, session=session)))
        self.done = Result(0, Outcome(OutcomeStatus.NEEDS_INPUT, "title", "summary"))

    def test_a_record_without_previous_or_bound_or_a_bad_bound_is_invalid(self):
        for key, value in (("previous", None), ("bound", None), ("bound", 1)):
            with self.subTest(key=key, value=value):
                data = self.metadata()
                if value is None:
                    del data[key]
                else:
                    data[key] = value
                with open(self.state_record(), "w", encoding="utf-8") as file:
                    json.dump(data, file)
                with self.assertRaisesRegex(ConfigError, "^invalid .*delegate.json: "):
                    self.record.read()

    def test_a_record_without_closing_or_a_bad_closing_is_invalid(self):
        data = self.metadata()
        del data["closing"]
        with open(self.state_record(), "w", encoding="utf-8") as file:
            json.dump(data, file)
        with self.assertRaisesRegex(ConfigError, "^invalid .*delegate.json: "):
            self.record.read()
        for value in (1, "false", None):
            with self.subTest(value=value):
                with open(self.state_record(), "w", encoding="utf-8") as file:
                    json.dump({**data, "closing": value}, file)
                with self.assertRaisesRegex(
                    ConfigError, "^invalid .*delegate.json: invalid closing$"
                ):
                    self.record.read()

    def test_the_worker_token_comes_from_the_registration(self):
        self.assertEqual(worker(self.record.read()), 1)
        state = self.record.read()
        for given in ({}, {"worker": "1"}, {"worker": True}):
            with self.subTest(given=given):
                handler = HookCallback(HANDLER, given, ())
                session = replace(state.plan.session, handlers=(handler,))
                self.assertIsNone(worker(replace(state, plan=replace(state.plan, session=session))))
        other = HookCallback("pkg.writeback", {"worker": 7}, ())
        mine = HookCallback(HANDLER, {"worker": 3}, ())
        for handlers, want in (((other,), None), ((other, mine), 3)):
            with self.subTest(handlers=len(handlers)):
                session = replace(state.plan.session, handlers=handlers)
                found = replace(state, plan=replace(state.plan, session=session))
                self.assertEqual(worker(found), want)
        self.assertIsNone(
            worker(
                replace(
                    state,
                    plan=replace(state.plan, session=replace(state.plan.session, handlers=())),
                )
            )
        )

    def test_bind_takes_an_opening_attempt_once(self):
        self.assertFalse(self.record.bind(2, 1))
        self.assertFalse(self.record.bind(1, 2))
        self.assertTrue(self.record.bind(1, 1))
        self.assertTrue(self.record.read().bound)
        self.assertTrue(self.record.bind(1, 1))
        self.assertTrue(self.record.complete(1, self.done, sid=SID))
        self.assertFalse(self.record.bind(1, 1))

    def test_complete_is_a_compare_and_set(self):
        self.assertFalse(self.record.complete(1, self.done, sid="other"))
        self.assertFalse(self.record.complete(2, self.done, sid=SID))
        self.record.bind(1, 1)
        self.assertFalse(self.record.complete(1, self.done, sid=SID, unbound=True))
        self.assertTrue(self.record.complete(1, self.done, sid=SID))
        self.assertFalse(self.record.complete(1, Result(1, None, "late"), sid=SID))
        state = self.record.read()
        self.assertEqual((state.completed, state.result), (True, self.done))

    def test_complete_closing_sets_closing_so_no_follow_up_opens(self):
        self.assertTrue(self.record.complete(1, self.done, sid=SID, closing=True))
        self.assertTrue(self.record.read().closing)
        self.assertFalse(self.record.follow_up(1, 1))

    def test_follow_up_opens_the_next_attempt_bound_keeping_the_previous_result(self):
        self.assertFalse(self.record.follow_up(1, 1))
        self.record.complete(1, self.done, sid=SID)
        self.assertFalse(self.record.follow_up(1, 2))
        self.assertFalse(self.record.follow_up(2, 1))
        self.assertTrue(self.record.follow_up(1, 1))
        state = self.record.read()
        self.assertEqual(
            (state.attempt, state.completed, state.result, state.previous, state.bound),
            (2, False, None, self.done, True),
        )
        self.assertEqual(self.record.read(), state)
        self.assertFalse(self.record.follow_up(1, 1))

    def test_follow_up_is_refused_while_a_close_holds_its_claim(self):
        self.record.complete(1, self.done, sid=SID)
        self.assertFalse(self.record.store.closing())
        with self.record.store.closing_claim(), self.record.store.closing_claim():
            self.assertTrue(self.record.store.closing())
            self.assertFalse(self.record.follow_up(1, 1))
        self.assertFalse(self.record.store.closing())
        self.assertTrue(self.record.follow_up(1, 1))

    def test_a_killed_closers_claim_goes_with_it(self):
        """No state to clear: the kernel drops the lock with its holder's descriptors."""
        self.record.complete(1, self.done, sid=SID)
        fd = os.open(self.record.store.close_lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_SH)
        self.assertFalse(self.record.follow_up(1, 1))
        os.close(fd)  # as at its holder's death
        self.assertTrue(self.record.follow_up(1, 1))
