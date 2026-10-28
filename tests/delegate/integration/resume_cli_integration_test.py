"""ensemble-delegate resume, each a fresh process (tests/delegate/fakes/control_launcher.py), on a
detached headless run whose drive and Hosts keep their state on disk
(tests/delegate/fakes/harness_session.py FakeControl, PersistentSession): one Record line, a
live run's unchanged, a stopped run's resumed once, a run of another $ENSEMBLE_SID refused, and
an old-layout workdir refused naming its missing run record.
"""

import os
import shutil
import subprocess
import uuid

from ensemble.delegate import Spec
from ensemble.delegate.engine.types import Layout
from ensemble.utils import Record
from tests.delegate.case import PY, REPO, child_env
from tests.delegate.fakes.harness_session import (
    DrivenDelegate,
    FakeControl,
    PersistentSession,
    harness_launches,
)
from tests.delegate.harness import SID, HarnessCase

RESUME = (*PY, os.path.join(REPO, "tests", "delegate", "fakes", "control_launcher.py"), "resume")
DRIVE = f"dummy-tester-{SID[:8]}-drive"


class ResumeCommand(HarnessCase):
    def setUp(self):
        super().setUp()
        self.record = DrivenDelegate(
            "dummy-tester",
            Spec("input", self.work, client="fake", sid=SID),
            session_cls=PersistentSession,
            host="agent",
            display="opaque",
        ).detach()

    def env(self, sid):
        env = child_env(None, REPO)
        env.pop("ENSEMBLE_SID", None)
        return env if sid is None else {**env, "ENSEMBLE_SID": sid}

    def resume(self, *keys, stdin="", sid=None):
        return subprocess.run(
            (*RESUME, *keys),
            cwd=self.home,
            env=self.env(sid),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def resumed(self, *keys, stdin="", sid=None) -> Record:
        res = self.resume(*keys, stdin=stdin, sid=sid)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(res.stdout.splitlines()), 1, res.stdout)
        return Record.parse(res.stdout)

    def launches(self):
        return [launch["name"] for launch in harness_launches()]

    def end(self):
        FakeControl(self.metadata()["drive"]).close()

    def test_a_live_run_prints_its_record_from_the_workdir_or_stdin_starting_nothing(self):
        self.assertEqual(self.resumed(self.work), self.record)
        self.assertEqual(self.resumed(stdin=self.record.to_line() + "\n"), self.record)
        self.assertEqual(self.launches(), [DRIVE])
        self.assertEqual(self.metadata()["attempt"], 1)

    def test_a_stopped_run_resumes_once_keeping_its_identity_and_placement(self):
        self.end()
        self.assertEqual(self.resumed(self.work, sid=SID), self.record)
        self.assertEqual(self.record.display, "opaque")
        self.assertEqual(self.launches(), [DRIVE] * 2)
        self.assertEqual(self.metadata()["attempt"], 2)
        self.assertEqual(self.resumed(self.work, sid=SID), self.record)
        self.assertEqual(len(self.launches()), 2)
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_another_ensemble_sid_exits_2_starting_nothing(self):
        other = str(uuid.uuid4())
        refused = (2, "", f"delegate: {self.work}: sid {other} is not the run's (its sid: {SID})\n")
        for ended in (False, True):
            if ended:
                self.end()
            with self.subTest(ended=ended):
                res = self.resume(self.work, sid=other)
                self.assertEqual((res.returncode, res.stdout, res.stderr), refused)
                self.assertEqual(len(self.launches()), 1)
                self.assertEqual(self.metadata()["attempt"], 1)
        self.assertEqual(self.resumed(self.work, sid=SID), self.record)
        self.assertEqual(len(self.launches()), 2)

    def test_old_layout_resume_names_run_record(self):
        run = Layout(self.work).run
        record = os.path.join(run, "delegate.json")
        os.replace(record, os.path.join(self.work, "delegate.json"))
        shutil.rmtree(run)
        res = self.resume(self.work, sid=SID)
        self.assertEqual(
            (res.returncode, res.stdout, res.stderr),
            (2, "", f"delegate: {self.work}: invalid {record}: missing metadata\n"),
        )
        self.assertFalse(os.path.lexists(run))
        self.assertEqual(len(self.launches()), 1)
