"""Shared ctl receipt, refusal, content and resume assertions. Adapters run their real entry,
start(name, sid=None) returns its printed Record, end(name) stops that run, refuse() returns a
rejected startup, and inventory() lists its sessions; listed: whether a start's Record lists any
(a headless delegate run lists none). NamedCtlContract additionally
checks name-addressed missing sessions. AnswerCtlContract supplies waiting(record) and input_of.
"""

import uuid
from dataclasses import replace

from ensemble.utils.record import Record, Status
from tests.utils.fakes.ctl import lines


class CtlContract:
    command: str
    listed = True

    def call(self, *argv: str, stdin: str = "", sid: str | None = None): ...

    def start(self, name: str, sid: str | None = None) -> Record: ...

    def end(self, name: str) -> None: ...

    def refuse(self): ...

    def inventory(self): ...

    def statuses(self, *records: Record) -> list[Status]:
        done = self.call("status", stdin=lines(*records))
        self.assertEqual(done.returncode, 0, done.stderr)
        return [Status.parse(line) for line in done.stdout.splitlines()]

    def test_start_prints_a_record_its_command_answers_and_resumes(self):
        sid = str(uuid.uuid4())
        started = self.start("w1", sid=sid)
        self.assertEqual(started.sid, sid)
        self.assertEqual(bool(started.sessions), self.listed)
        self.assertEqual(started.ctl, (self.command,))
        self.assertEqual(started.resume[:2], (self.command, "resume"))
        self.assertEqual(Record.parse(started.to_line()), started)
        self.assertTrue(self.statuses(started)[0].alive)

    def test_status_answers_each_stdin_record_in_order(self):
        first, second = (self.start(name) for name in ("w1", "w2"))
        found = self.statuses(second, first)
        self.assertEqual(
            [(s.name, s.alive) for s in found], [(second.name, True), (first.name, True)]
        )

    def test_refused_start_exits_2_without_a_receipt_or_new_session(self):
        before = self.inventory()
        done = self.refuse()
        self.assertEqual(self.inventory(), before)
        self.assertEqual((done.returncode, done.stdout), (2, ""))

    def test_content_accepts_a_key_or_one_stdin_record(self):
        started = self.start("w1")
        keyed = self.call("content", started.resume[2])
        piped = self.call("content", stdin=lines(started))
        self.assertEqual((keyed.returncode, piped.returncode), (0, 0), (keyed.stderr, piped.stderr))
        self.assertTrue(keyed.stdout)
        self.assertEqual(piped.stdout, keyed.stdout)

    def test_input_cannot_restart_a_live_run(self):
        started = self.start("w1")
        before = self.inventory()
        done = self.call(*started.resume[1:], "--input", "answer")
        self.assertEqual(self.inventory(), before)
        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertTrue(done.stderr)
        self.assertTrue(self.statuses(started)[0].alive)

    def test_resume_of_a_live_one_prints_its_record_and_another_sid_exits_2(self):
        started = self.start("w1")
        again = self.call(*started.resume[1:], sid=started.sid)
        self.assertEqual((again.returncode, Record.parse(again.stdout)), (0, started))
        before = self.inventory()
        refused = self.call(*started.resume[1:], sid=str(uuid.uuid4()))
        self.assertEqual(self.inventory(), before)
        self.assertEqual((refused.returncode, refused.stdout), (2, ""))
        self.assertTrue(self.statuses(started)[0].alive)

    def test_resume_of_an_ended_one_starts_it_again_with_its_sid(self):
        started = self.start("w1")
        self.end("w1")
        self.assertFalse(self.statuses(started)[0].alive)
        again = self.call(*started.resume[1:], sid=started.sid)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(Record.parse(again.stdout).sid, started.sid)
        self.assertTrue(self.statuses(started)[0].alive)

    def test_close_of_stdin_records_ends_each(self):
        records = [self.start(name) for name in ("w1", "w2")]
        done = self.call("close", stdin=lines(*records))
        self.assertEqual((done.returncode, done.stdout), (0, ""), done.stderr)
        self.assertEqual([s.alive for s in self.statuses(*records)], [False, False])

    def test_a_bad_stdin_line_exits_2_and_closes_nothing(self):
        started = self.start("w1")
        for verb in ("status", "close", "show"):
            with self.subTest(verb=verb):
                done = self.call(verb, stdin=lines(started) + "not json\n")
                self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertTrue(self.statuses(started)[0].alive)


class NamedCtlContract(CtlContract):
    def test_named_start_receipt_identifies_its_session_and_resume_key(self):
        sid = str(uuid.uuid4())
        started = self.start("w1", sid=sid)
        self.assertEqual((started.name, started.sessions, started.sid), ("w1", ("w1",), sid))
        self.assertEqual(
            (started.ctl, started.resume), ((self.command,), (self.command, "resume", "w1"))
        )

    def test_status_answers_an_unknown_name_as_gone(self):
        started = self.start("w1")
        other = replace(started, name="w9", sessions=("w9",))
        found = self.statuses(started, other)
        self.assertEqual([(s.name, s.alive) for s in found], [("w1", True), ("w9", False)])
        self.assertEqual((found[1].state, found[1].outcome, found[1].attach), ("gone", None, None))


class AnswerCtlContract(CtlContract):
    def waiting(self, record: Record) -> None: ...

    def input_of(self, record: Record) -> str: ...

    def test_input_starts_a_waiting_run_with_its_saved_identity(self):
        started = self.start("w1")
        self.waiting(started)
        self.assertFalse(self.statuses(started)[0].alive)
        done = self.call(*started.resume[1:], "--input", "the answer", sid=started.sid)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Record.parse(done.stdout), started)
        self.assertEqual(self.input_of(started), "the answer")
        self.assertTrue(self.statuses(started)[0].alive)
