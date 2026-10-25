"""The real HarnessDelegate runs the Delegate contract through process-free owned Sessions."""

import json
import subprocess
import sys

from ensemble.delegate import Spec
from ensemble.delegate.composer.prompt import RESUME
from ensemble.delegate.engine.types import Begun
from ensemble.session import Exited, NotShowable, Reply
from tests.delegate.case import REPO, child_env
from tests.delegate.contracts.delegate import DelegateContract
from tests.delegate.fakes.harness_session import (
    FakeViewer,
    HarnessSession,
    RecordingHost,
    ShowingHost,
    harness_launches,
)
from tests.delegate.harness import SID, HarnessCase, ViewedHarnessDelegate
from tests.delegate.replies import block
from tests.delegate.unit.runs import DONE

CLASS_QUERIES = """import json, sys, unittest
from ensemble.delegate import HarnessDelegate
from tests.delegate.contracts.delegate import DelegateContract
from tests.delegate.fakes.harness_session import HarnessSession
from tests.delegate.harness import HarnessCase

class ClassQueries(DelegateContract, HarnessCase):
    delegate_cls = HarnessDelegate
    ROLE = "dummy-tester"
    SHOWABLE = sys.argv[1] == "True"

    def setUp(self):
        super().setUp()
        if self.SHOWABLE:
            script = HarnessSession.script
            HarnessSession.reset(showable=True)
            HarnessSession.script = script

    def runTest(self):
        DelegateContract.test_class_methods_answer_for_its_roles_and_refuse_an_unknown_one(self)

result = unittest.TextTestRunner(verbosity=2).run(ClassQueries())
print(json.dumps({"tests": result.testsRun, "showable": ClassQueries.SHOWABLE}))
sys.exit(not result.wasSuccessful())
"""


class HarnessContract(DelegateContract, HarnessCase):
    delegate_cls = ViewedHarnessDelegate
    ROLE = "dummy-tester"
    SHOWABLE = False

    def test_class_methods_answer_for_its_roles_and_refuse_an_unknown_one(self):
        result = subprocess.run(
            [sys.executable, "-P", "-S", "-c", CLASS_QUERIES, str(self.SHOWABLE)],
            cwd=self.home,
            env=child_env(None, REPO),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), {"tests": 1, "showable": self.SHOWABLE})

    def make(self, workdir, **kw):
        role = kw.pop("role", self.ROLE)
        spec = Spec("Hello.", workdir, client="fake", mode="tui", **kw)
        return ViewedHarnessDelegate(
            role, spec, session_cls=HarnessSession, host="agent", harness_host="harness"
        )

    def end(self, record):
        self.stop(record)

    def finish(self, record):
        view = ViewedHarnessDelegate.attach(record)
        view.close()
        view.run()
        view.close()

    def test_needs_input_continues_with_an_explicit_new_resume_construction(self):
        workdir = self.workdir()
        needed = {**DONE, "status": "needs_input", "questions": ["Which answer?"]}
        HarnessSession.script = (Reply(block(needed)), Exited(0))
        first = ViewedHarnessDelegate(
            self.ROLE,
            Spec("Hello.", workdir, sid=SID, client="fake"),
            session_cls=HarnessSession,
            host="agent",
            harness_host="harness",
        )
        seen = []
        result = first.run(seen.append)
        self.assertEqual(result.outcome.status, "needs_input")
        self.assertEqual(seen[0], Begun(SID))
        HarnessSession.script = (Reply(block(DONE)), Exited(0))
        answer = "Continue with option B."
        continued = ViewedHarnessDelegate(
            self.ROLE,
            Spec(answer, workdir, sid=SID, resume=True, client="fake"),
            session_cls=HarnessSession,
            host="agent",
            harness_host="harness",
        )
        self.addCleanup(continued.close)
        seen = []
        result = continued.run(seen.append)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.outcome.status, "done")
        self.assertEqual(seen[0], Begun(SID))
        self.work = workdir
        self.assertEqual(self.metadata()["attempt"], 2)
        launched = HarnessSession.started[-1]["spec"]
        self.assertEqual((launched.sid, launched.resume), (SID, True))
        self.assertTrue(launched.prompt.startswith(RESUME))
        self.assertTrue(launched.prompt.endswith("\n" + answer + "\n"))


class ShowingHarnessContract(HarnessContract):
    SHOWABLE = True

    def setUp(self):
        super().setUp()
        script = HarnessSession.script
        HarnessSession.reset(showable=True)
        HarnessSession.script = script


class Ownership(HarnessCase):
    def pair(self, *, agent_showable=True, harness_showable=True):
        HarnessSession.reset()
        for name, capable in (("agent", agent_showable), ("harness", harness_showable)):
            kind = ShowingHost if capable else RecordingHost
            HarnessSession.hosts[name] = kind(name)
        delegate = self.make(
            spec=Spec(
                "input",
                self.work,
                mode="tui",
                client="fake",
                sid=SID,
                name="agent-window",
            ),
            host="agent",
            harness_host="harness",
            display="target",
        )
        record = delegate.detach()
        return record, ViewedHarnessDelegate.attach(record)

    def test_harness_owns_liveness_and_state_while_agent_owns_display_target(self):
        record, view = self.pair()
        agent = HarnessSession.live[self.metadata()["agent_name"]]
        self.assertTrue(view.alive())
        self.assertEqual((view.status().state, agent.alive()), ("working", True))
        self.assertEqual(view.attach_command(), agent.attach_command())
        self.assertEqual(view.status().attach, agent.attach_command())
        harness = RecordingHost.processes["harness", self.metadata()["harness_name"]]
        harness.close()
        self.assertFalse(view.alive())
        self.assertEqual(view.status().state, "working")
        self.assertEqual(view.attach_command(), agent.attach_command())
        self.assertEqual(record.sid, SID)

    def test_dead_agent_falls_back_to_harness_and_close_closes_all_twice(self):
        _, view = self.pair()
        agent_name = self.metadata()["agent_name"]
        harness_name = self.metadata()["harness_name"]
        HarnessSession.live[agent_name].close()
        self.assertEqual(view.attach_command(), f"harness attach {harness_name}")
        view.close()
        view.close()
        self.assertFalse(view.alive())
        self.assertFalse(RecordingHost.processes["agent", agent_name].alive())
        self.assertFalse(RecordingHost.processes["harness", harness_name].alive())

    def test_show_reaches_only_the_live_agent(self):
        _, view = self.pair()
        view.show()
        self.assertEqual(FakeViewer.shown, [(self.metadata()["agent_name"], "target", None)])

    def test_hidden_agent_uses_harness_target_and_hidden_pair_has_no_attach(self):
        _, view = self.pair(agent_showable=False)
        self.assertEqual(view.attach_command(), f"harness attach {self.metadata()['harness_name']}")
        view.close()
        self.work += "-hidden"
        _, view = self.pair(agent_showable=False, harness_showable=False)
        self.assertIsNone(view.status().attach)
        self.assertIsNone(view.attach_command())
        with self.assertRaises(NotShowable):
            view.show()

    def test_resume_closes_leftover_agent_before_restarting_lifecycle_owner(self):
        record, view = self.pair()
        agent_name = self.metadata()["agent_name"]
        RecordingHost.processes["harness", self.metadata()["harness_name"]].close()
        before = len(harness_launches())
        old_agent = HarnessSession.live[agent_name]
        resumed = view.resume()
        self.assertEqual((resumed.sid, resumed.sessions), (record.sid, record.sessions))
        self.assertEqual(old_agent.alive(), False)
        self.assertEqual(len(harness_launches()), before + 1)
        self.assertEqual(view.resume(), resumed)
        self.assertEqual(len(harness_launches()), before + 1)
