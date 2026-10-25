"""LinearRun reattaches the real HarnessDelegate's durable state through process-free Sessions."""

import os

from ensemble.delegate import Delegate
from ensemble.orchestrator.engine import runs
from ensemble.orchestrator.engine.linear_run import LinearRun
from ensemble.orchestrator.engine.types import Config, Docs, LinearSettings, TrackerKind
from ensemble.utils import load_class
from tests.delegate.fakes.harness_session import HarnessSession, RecordingHost, harness_launches
from tests.delegate.harness import SID, HarnessCase

HARNESS_DELEGATE = load_class("ensemble.delegate.engine.harness_delegate:HarnessDelegate", Delegate)


class HarnessProvider(HARNESS_DELEGATE):
    def __init__(self, role, spec, **options):
        super().__init__(
            role, spec, session_cls=HarnessSession, host="agent", harness_host="harness", **options
        )


class HarnessRun(HarnessCase):
    def setUp(self):
        super().setUp()
        linear = LinearSettings(
            team="",
            states={},
            human_members=(),
            harness_key="",
            manager_key="",
            task_label_group="",
            task_labels={},
            agent_label_group="",
            agent_labels={},
            project_repos={},
            roles={},
        )
        self.cfg = Config(
            task_tiers={},
            client_max_runs={},
            usage_limit={},
            roles={},
            local_clones={},
            tracker=TrackerKind.LINEAR,
            linear=linear,
            board=None,
            work_dir=os.path.join(self.home, "pipeline"),
            docs=Docs("", "", {}),
            stages={},
            delegate_kind="harness",
            delegates={"harness": HARNESS_DELEGATE},
            core={},
            local_prefix="",
        )

    def test_real_harness_state_round_trip_owns_both_sessions_and_resumes_once(self):
        issue = "TASK-1"
        self.work = self.cfg.issue_workdir(issue)
        owner = LinearRun(
            issue,
            "Echo input.",
            self.work,
            cfg=self.cfg,
            role="dummy-tester",
            sid=SID,
            task="echo",
            client="fake",
            mode="tui",
            delegate_cls=HarnessProvider,
        )
        runs.note(
            self.cfg.runs_log,
            "start",
            issue=issue,
            sid=SID,
            role="dummy-tester",
            client="fake",
            delegate_kind="harness",
        )
        record = owner.detach()
        self.assertEqual(HarnessSession.live[self.metadata()["agent_name"]].sid, SID)
        view = LinearRun.attach(issue, cfg=self.cfg)
        self.assertIs(type(view.delegate), HARNESS_DELEGATE)
        self.assertTrue(view.delegate.alive())
        self.assertEqual(view.status().name, issue)
        self.assertEqual(view.resume(), record)
        self.assertEqual(len(harness_launches()), 1)
        view.delegate.close()
        self.assertFalse(view.delegate.alive())
        self.assertTrue(all(not process.alive() for process in RecordingHost.processes.values()))
        resumed = view.resume()
        self.assertEqual((resumed.sid, resumed.sessions), (record.sid, record.sessions))
        self.assertEqual(view.resume(), resumed)
        self.assertEqual(len(harness_launches()), 2)
        self.addCleanup(view.delegate.close)
