"""tests/utils/fakes/ctl.py: the installed fake ctl, found on PATH, answers status, show and close
for the Records on its stdin, prints a Record for start and resume, and logs each call.
"""

import os
import subprocess
import unittest
import uuid

from ensemble.utils.record import Record, Status
from tests.utils.case import Case
from tests.utils.contracts.ctl import NamedCtlContract
from tests.utils.fakes import ctl
from tests.utils.fakes.ctl import lines, record
from tests.utils.sandbox import clean_env


def _status(name, *, alive=True, state="working", outcome=None, attach=None):
    return Status(name=name, alive=alive, state=state, outcome=outcome, attach=attach, hidden=False)


class Installed(Case):
    def setUp(self):
        self.bin = os.path.join(self.home, "bin")
        self.state = os.path.join(self.home, "ctl state")
        self.ctl = ctl.install(self.bin, self.state)
        self.env = clean_env()
        self.env.pop("ENSEMBLE_SID", None)
        self.env["PATH"] = os.pathsep.join((self.bin, os.environ.get("PATH", os.defpath)))


class FakeCtlContract(NamedCtlContract, Installed):
    command = ctl.COMMAND

    def call(self, *argv, stdin="", sid=None):
        env = {**self.env, **({} if sid is None else {"ENSEMBLE_SID": sid})}
        return subprocess.run(
            [self.ctl, *argv], input=stdin, env=env, capture_output=True, text=True, timeout=30
        )

    def start(self, name, sid=None):
        done = self.call("start", name, sid=sid)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        return Record.parse(done.stdout)

    def inventory(self):
        return ctl._sessions(self.state)

    def refuse(self):
        return self.call("start", "")

    def end(self, name):
        ctl.set_session(self.state, name, alive=False, state="gone")


class FakeCtl(Installed):
    def call(self, *argv, stdin=""):
        return subprocess.run(
            argv, input=stdin, env=self.env, capture_output=True, text=True, timeout=30
        )

    def start(self, name, *argv):
        done = self.call(self.ctl, "start", name, *argv)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        return Record.parse(done.stdout)

    def status(self, *records):
        done = self.call(self.ctl, "status", stdin=lines(*records))
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        return [Status.parse(line) for line in done.stdout.splitlines()]

    def test_start_prints_one_record_whose_ctl_and_resume_name_the_fake(self):
        self.assertEqual(self.ctl, "ensemble-orchestrator")
        started = self.start("w1", "--display", "d")
        self.assertEqual(started, record("w1", "d", sid=started.sid))
        self.assertEqual(str(uuid.UUID(started.sid)), started.sid)
        self.assertEqual(self.status(record("w1")), [_status("w1")])

    def test_start_honors_environment_sid_and_resume_keeps_it(self):
        sid = str(uuid.uuid4())
        self.env["ENSEMBLE_SID"] = sid
        started = self.start("w1", "--display", "d")
        self.assertEqual(started.sid, sid)
        ctl.set_session(self.state, "w1", alive=False)
        resumed = self.call(*started.resume)
        self.assertEqual((resumed.returncode, Record.parse(resumed.stdout)), (0, started))
        self.env.pop("ENSEMBLE_SID")
        ctl.set_session(self.state, "w1", alive=False, state="gone", outcome="failed")
        resumed = self.call(*started.resume)
        self.assertEqual((resumed.returncode, Record.parse(resumed.stdout)), (0, started))
        self.assertEqual(self.status(started), [_status("w1")])

    def test_resume_rejects_a_sid_that_differs_from_its_saved_identity(self):
        started = self.start("w1")
        ctl.set_session(self.state, "w1", alive=False)
        self.env["ENSEMBLE_SID"] = str(uuid.uuid4())
        refused = self.call(*started.resume)
        self.assertEqual((refused.returncode, refused.stdout), (2, ""))
        self.assertIn("does not match saved sid", refused.stderr)
        self.assertFalse(self.status(started)[0].alive)

    def test_status_prints_one_line_per_record_and_an_unknown_name_is_gone(self):
        self.start("w1")
        self.assertEqual(
            self.status(record("w1"), record("w2")),
            [_status("w1"), _status("w2", alive=False, state="gone")],
        )
        ctl.set_session(self.state, "w1", alive=False, state="dead", outcome="needs_input")
        self.assertEqual(
            self.status(record("w1")),
            [_status("w1", alive=False, state="dead", outcome="needs_input")],
        )

    def test_attach_is_a_command_only_while_alive_and_showable(self):
        self.start("w1")
        self.assertIsNone(self.status(record("w1"))[0].attach)
        ctl.set_session(self.state, "w1", showable=True)
        self.assertEqual(self.status(record("w1"))[0].attach, "ensemble-orchestrator attach w1")
        ctl.set_session(self.state, "w1", alive=False)
        self.assertIsNone(self.status(record("w1"))[0].attach)
        with self.assertRaises(TypeError):
            ctl.set_session(self.state, "w1", alvie=True)

    def test_show_exits_1_for_a_session_that_cannot_show(self):
        for name in ("w1", "w2"):
            self.start(name)
        ctl.set_session(self.state, "w2", showable=True)
        shown = self.call(self.ctl, "show", stdin=lines(record("w2")))
        self.assertEqual((shown.returncode, shown.stdout, shown.stderr), (0, "", ""))
        refused = self.call(self.ctl, "show", stdin=lines(record("w1"), record("w2")))
        self.assertEqual(
            (refused.returncode, refused.stdout, refused.stderr),
            (1, "", "session w1: host fake cannot show\n"),
        )

    def test_close_ends_each_session(self):
        for name in ("w1", "w2"):
            self.start(name)
        done = self.call(self.ctl, "close", stdin=lines(record("w1"), record("w2")))
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        self.assertEqual(
            self.status(record("w1"), record("w2")),
            [_status(name, alive=False, state="gone") for name in ("w1", "w2")],
        )

    def test_resume_of_a_live_session_prints_its_record_and_starts_nothing(self):
        started = self.start("w1", "--display", "d")
        ctl.set_session(self.state, "w1", state="blocked")
        again = self.call(*started.resume)
        self.assertEqual((again.returncode, again.stdout), (0, started.to_line() + "\n"))
        self.assertEqual(self.status(started), [_status("w1", state="blocked")])

    def test_bad_stdin_a_bad_name_or_an_unknown_subcommand_exits_2_and_changes_nothing(self):
        self.start("w1")
        for argv, stdin in (
            (("status",), lines(record("w1")) + "not json\n"),
            (("close",), lines(record("w1")) + "[]\n"),
            (("show",), '{"v": 2}\n'),
            (("start", ""), ""),
            (("start", "a\nb"), ""),
            (("start",), ""),
            (("attach", "w1"), ""),
        ):
            with self.subTest(argv=argv, stdin=stdin):
                done = self.call(self.ctl, *argv, stdin=stdin)
                self.assertEqual((done.returncode, done.stdout), (2, ""))
                self.assertTrue(done.stderr)
        self.assertEqual(self.status(record("w1")), [_status("w1")])

    def test_stdin_too_deep_to_decode_exits_2(self):
        done = self.call(self.ctl, "status", stdin="[" * 200000 + "\n")
        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertIn("not JSON", done.stderr)

    def test_calls_logs_each_call_with_its_argv_and_names(self):
        self.assertEqual(ctl.calls(self.state), [])
        self.start("w1", "--display", "d")
        self.status(record("w1"), record("w2"))
        self.call(self.ctl, "close", stdin="not json\n")
        self.assertEqual(
            ctl.calls(self.state),
            [
                {"argv": ["start", "w1", "--display", "d"], "names": ["w1"]},
                {"argv": ["status"], "names": ["w1", "w2"]},
                {"argv": ["close"], "names": []},
            ],
        )


if __name__ == "__main__":
    unittest.main()
